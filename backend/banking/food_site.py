"""Independent fictional delivery platform on port 8002; no real orders."""
import secrets
import sqlite3
import time
from contextlib import contextmanager

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .config import ROOT
from .db import uid
from .food_auth import register_food_auth
from .food_catalog import PLACES, SHOPS, distance
from .food_common import Body, Location, Owner, Quote, require_bridge
from .securities_common import secret, token_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS food_pairing(code_hash TEXT PRIMARY KEY,owner TEXT NOT NULL,label TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS food_places(user_id TEXT PRIMARY KEY,place_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS food_pair_limits(client TEXT PRIMARY KEY,window INTEGER NOT NULL,attempts INTEGER NOT NULL);
"""


class Pair(Owner):
    label: str = Field(min_length=1, max_length=40)


class Connect(Body):
    code: str = Field(pattern=r'^[0-9]{8}$')


def create_food_app(database_path=None, key_path=None, bank_url='http://127.0.0.1:8000'):
    path = database_path or ROOT/'data/food-users.sqlite3'
    key = key_path or ROOT/'data/.food-bridge-key'
    path.parent.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title='有数外卖 · 仿美团演示站')

    @contextmanager
    def connection():
        with sqlite3.connect(path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            db.execute('BEGIN IMMEDIATE')
            yield db

    with connection() as db:
        db.executescript(SCHEMA)
    identity = register_food_auth(app, connection)

    @app.middleware('http')
    async def origin_guard(request, call_next):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            origin = request.headers.get('origin')
            if origin and origin not in {'http://127.0.0.1:8002', 'http://localhost:8002'}:
                return JSONResponse({'detail': '不接受来自其他站点的请求。'}, status_code=403)
        return await call_next(request)

    def context(person):
        with connection() as db:
            row = db.execute('SELECT place_id FROM food_places WHERE user_id=?', (person['id'],)).fetchone()
        place = next((p for p in PLACES if row and p['id'] == row[0]), None)
        return {'authenticated': True, 'user_id': person['id'], 'label': person['label'], 'connected': bool(person['bank_owner']), 'bank_label': person['bank_label'] or '', 'place': place, 'places': PLACES, 'simulated': True}

    def linked(investor):
        with connection() as db:
            row = db.execute('SELECT u.*,b.bank_owner,b.bank_label FROM food_users u JOIN food_bindings b ON b.food_user=u.id WHERE b.bank_owner=?', (investor,)).fetchone()
        if not row:
            raise HTTPException(400, '请先在独立外卖站使用同步码连接当前有数账户。')
        return dict(row)

    def set_location(person, place_id):
        if place_id not in {p['id'] for p in PLACES}:
            raise HTTPException(400, '请选择有效的演示位置。')
        with connection() as db:
            db.execute('INSERT OR REPLACE INTO food_places VALUES (?,?)', (person['id'], place_id))
        return context(person)

    def nearby(person, query=''):
        current = context(person)
        if not current['place']:
            return {**current, 'shops': [], 'needs_location': True}
        result = []
        for shop in SHOPS:
            km = distance(current['place'], shop)
            terms = shop['name'] + shop['category'] + ' '.join(i['name'] for i in shop['menu'])
            if km <= shop['radius_km'] and (not query or query in terms):
                result.append({k: v for k, v in shop.items() if k != 'menu'} | {'distance_km': km, 'matched_items': [i for i in shop['menu'] if query and query in i['name']]})
        return {**current, 'shops': sorted(result, key=lambda s: s['distance_km']), 'needs_location': False}

    def menu(person, shop_id):
        current = nearby(person)
        if current['needs_location']:
            raise HTTPException(400, '请先设置演示收货位置。')
        if shop_id not in {s['id'] for s in current['shops']}:
            raise HTTPException(400, '该店铺不存在或超出当前位置配送范围。')
        return next(s for s in SHOPS if s['id'] == shop_id)

    async def orders(person):
        if not person['bank_owner']:
            return {'orders': [], 'simulated': True}
        try:
            async with httpx.AsyncClient(base_url=bank_url, transport=getattr(app.state, 'bank_transport', None), timeout=10) as client:
                response = await client.post('/internal/food/orders', json={'owner': person['bank_owner']}, headers={'x-food-key': secret(key)})
        except httpx.HTTPError:
            raise HTTPException(503, '有数后台暂时无法连接，订单未能刷新。') from None
        if not response.is_success:
            raise HTTPException(response.status_code, '无法读取当前关联账户订单。')
        return response.json()

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'service': 'food-demo'}

    @app.get('/api/food/session')
    def session(request: Request):
        try:
            person = identity(request)
        except HTTPException:
            return {'authenticated': False, 'connected': False, 'places': PLACES, 'place': None}
        return context(person)

    @app.post('/api/food/location')
    def location(body: Location, request: Request):
        return set_location(identity(request), body.place_id)

    @app.get('/api/food/shops')
    def shops(request: Request, q: str = Query(default='', max_length=60)):
        return nearby(identity(request), q.strip())

    @app.get('/api/food/shops/{shop_id}')
    def shop_menu(shop_id: str, request: Request):
        return menu(identity(request), shop_id)

    @app.get('/api/food/orders')
    async def read_orders(request: Request):
        return await orders(identity(request))

    @app.post('/internal/food/pairing-code')
    def pairing(body: Pair, request: Request):
        require_bridge(request, key)
        code = ''.join(secrets.choice('0123456789') for _ in range(8))
        with connection() as db:
            db.execute('DELETE FROM food_pairing WHERE owner=? OR expires<=?', (body.owner, time.time()))
            db.execute('INSERT INTO food_pairing VALUES (?,?,?,?)', (token_hash(code), body.owner, body.label, time.time()+300))
        return {'code': code, 'expires': time.time()+300}

    @app.post('/api/food/connect')
    async def connect(body: Connect, request: Request):
        person = identity(request)
        client = request.client.host if request.client else 'unknown'
        window = int(time.time()//60)
        with connection() as db:
            db.execute('INSERT INTO food_pair_limits VALUES (?,?,1) ON CONFLICT(client) DO UPDATE SET attempts=CASE WHEN window=excluded.window THEN attempts+1 ELSE 1 END,window=excluded.window', (client, window))
            attempts = db.execute('SELECT attempts FROM food_pair_limits WHERE client=?', (client,)).fetchone()[0]
        if attempts > 10:
            raise HTTPException(429, '同步码尝试过于频繁，请稍后重试。')
        with connection() as db:
            local = db.execute('SELECT * FROM food_pairing WHERE code_hash=?', (token_hash(body.code),)).fetchone()
            if local and local['expires'] <= time.time():
                raise HTTPException(400, '同步码已过期或已使用。')
            row = dict(local) if local else None
        if row is None:
            try:
                async with httpx.AsyncClient(base_url=bank_url, transport=getattr(app.state,'bank_transport',None),timeout=8) as client:
                    response = await client.post('/internal/connections/redeem', json={'code':body.code,'platform':'food','platform_user':person['id']}, headers={'x-food-key':secret(key)})
            except httpx.HTTPError:
                raise HTTPException(503, '无法验证同步码，请确认有数后台已启动。') from None
            if not response.is_success:
                raise HTTPException(response.status_code, response.json().get('detail','同步码验证失败。'))
            row = response.json()
        with connection() as db:
            old = db.execute('SELECT * FROM food_bindings WHERE bank_owner=? OR food_user=?', (row['owner'], person['id'])).fetchall()
            if any(b['bank_owner'] != row['owner'] or b['food_user'] != person['id'] for b in old):
                raise HTTPException(409, '一个有数账户只能关联一个外卖账号，不能替换其他账号关联。')
            db.execute('INSERT OR IGNORE INTO food_bindings VALUES (?,?,?)', (row['owner'], person['id'], row['label']))
            db.execute('UPDATE food_pairing SET expires=0 WHERE code_hash=?', (token_hash(body.code),))
        return context(identity(request))

    @app.get('/internal/food/{investor}/context')
    def internal_context(investor: str, request: Request):
        require_bridge(request, key)
        try:
            return context(linked(investor))
        except HTTPException as exc:
            if exc.status_code != 400:
                raise
            return {'connected': False, 'place': None, 'places': PLACES, 'simulated': True}

    @app.post('/internal/food/{investor}/location')
    def internal_location(investor: str, body: Location, request: Request):
        require_bridge(request, key)
        return set_location(linked(investor), body.place_id)

    @app.get('/internal/food/{investor}/shops')
    def internal_shops(investor: str, request: Request, q: str = Query(default='', max_length=60)):
        require_bridge(request, key)
        return nearby(linked(investor), q.strip())

    @app.get('/internal/food/{investor}/menu/{shop_id}')
    def internal_menu(investor: str, shop_id: str, request: Request):
        require_bridge(request, key)
        return menu(linked(investor), shop_id)

    @app.post('/internal/food/{investor}/quote')
    def quote(investor: str, body: Quote, request: Request):
        require_bridge(request, key)
        person = linked(investor)
        shop = menu(person, body.shop_id)
        choices = {i['id']: i for i in shop['menu']}
        quantities = {}
        for item in body.items:
            quantities[item.item_id] = quantities.get(item.item_id, 0) + item.quantity
        if any(q > 20 for q in quantities.values()) or sum(quantities.values()) > 100:
            raise HTTPException(400, '每道菜最多20份，每单最多100份。')
        if any(code not in choices or not choices[code]['available'] for code in quantities):
            raise HTTPException(400, '菜品不存在、不属于该店铺或已售罄。')
        items = [{**choices[code], 'quantity': q, 'amount_minor': choices[code]['price_minor']*q} for code, q in quantities.items()]
        subtotal = sum(i['amount_minor'] for i in items)
        if subtotal < shop['minimum_minor']:
            raise HTTPException(400, f"未达到店铺起送金额 ¥{shop['minimum_minor']/100:.2f}，尚未扣款。")
        packing = sum(i['packing_minor']*i['quantity'] for i in items)
        return {'quote_id': uid('foodquote_'), 'owner': investor, 'shop_id': shop['id'], 'shop_name': shop['name'], 'items': items, 'quantity': sum(quantities.values()), 'subtotal_minor': subtotal, 'packing_minor': packing, 'delivery_minor': shop['delivery_minor'], 'amount_minor': subtotal+packing+shop['delivery_minor'], 'place': context(person)['place'], 'note': body.note, 'minutes': shop['minutes'], 'expires': time.time()+300, 'simulated': True, 'source': '独立外卖演示站API；店铺、菜单、价格、地址均为虚拟数据'}

    @app.get('/')
    def home():
        return FileResponse(ROOT/'frontend/dist/food.html')

    assets = ROOT/'frontend/dist/assets'
    if assets.is_dir():
        app.mount('/assets', StaticFiles(directory=assets), name='assets')
    return app


app = create_food_app()
