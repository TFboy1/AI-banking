"""Independent fictional Ctrip-style site; no real ticketing or fulfillment."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from .config import ROOT
from .db import dumps, uid
from .travel_auth import register_travel_auth
from .travel_common import Body, Trip, Quote, require_bridge
from .travel_catalog import CITIES, validate_trip, inventory
from .securities_common import secret

SCHEMA = """
CREATE TABLE IF NOT EXISTS travel_searches(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,payload TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS travel_pair_limits(client TEXT PRIMARY KEY,window INTEGER NOT NULL,attempts INTEGER NOT NULL);
"""

class Connect(Body):
    code: str = Field(pattern=r'^[0-9]{8}$')

def create_travel_app(database_path=None,key_path=None,bank_url='http://127.0.0.1:8000'):
    path=Path(database_path or ROOT/'data/travel-users.sqlite3')
    key=key_path or ROOT/'data/.travel-bridge-key'
    path.parent.mkdir(parents=True,exist_ok=True)
    app=FastAPI(title='有数旅行 · 仿携程演示站')

    @contextmanager
    def connection():
        db=sqlite3.connect(path,timeout=10)
        try:
            db.row_factory=sqlite3.Row
            with db:
                db.execute('BEGIN IMMEDIATE')
                yield db
        finally:
            db.close()
    with connection() as db:db.executescript(SCHEMA)
    identity=register_travel_auth(app,connection)

    @app.middleware('http')
    async def origin_guard(request,call_next):
        if request.method not in {'GET','HEAD','OPTIONS'} and request.headers.get('origin') not in {None,'http://127.0.0.1:8004','http://localhost:8004'}:
            return JSONResponse({'detail':'不接受来自其他站点的请求。'},status_code=403)
        return await call_next(request)

    def context(person):
        from .travel_changes import POLICY
        return {'authenticated':True,'user_id':person['id'],'label':person['label'],
            'connected':bool(person['bank_owner']),'bank_label':person['bank_label'] or '',
            'cities':CITIES,'policy':POLICY,'simulated':True}

    def linked(owner):
        with connection() as db:
            row=db.execute('SELECT u.*,b.bank_owner,b.bank_label FROM travel_users u JOIN travel_bindings b ON b.travel_user=u.id WHERE b.bank_owner=?',(owner,)).fetchone()
        if not row:raise HTTPException(400,'请先在旅行站登录，输入有数设置里的通用同步码关联账户。')
        return dict(row)

    def search(person,body):
        trip=validate_trip(body)
        value=inventory(trip)
        value.update(search_id=uid('tripsearch_'),expires=time.time()+1800)
        with connection() as db:
            db.execute('DELETE FROM travel_searches WHERE expires<=?',(time.time(),))
            db.execute('INSERT INTO travel_searches VALUES (?,?,?,?)',(value['search_id'],person['id'],dumps(value),value['expires']))
        return value

    def quote(person,body):
        with connection() as db:
            row=db.execute('SELECT * FROM travel_searches WHERE id=? AND user_id=? AND expires>?',(body.search_id,person['id'],time.time())).fetchone()
        if not row:raise HTTPException(400,'行程搜索已过期或不属于当前账户，请重新搜索。')
        saved=json.loads(row['payload'])
        # Revalidate dates and regenerate current inventory rather than trusting
        # a browser-supplied price, destination, or hotel/night count.
        value=inventory(validate_trip(Trip.model_validate(saved['trip'])))
        items=[]
        for group,identifier in [('outbound',body.outbound_id),('inbound',body.inbound_id),('hotels',body.hotel_id)]:
            item=next((dict(i) for i in value[group] if i['id']==identifier and i['available']),None)
            if not item:raise HTTPException(400,'所选班次或酒店不属于当前行程，或已不可预订。')
            item['leg']=group
            item['amount_minor']=item['price_minor']*item['quantity']*item.get('nights',1)
            items.append(item)
        return {'quote_id':uid('travelquote_'),'owner':person['bank_owner'],'selection':body.model_dump(),
            'trip':value['trip'],'items':items,'amount_minor':sum(i['amount_minor'] for i in items),
            'expires':time.time()+300,'simulated':True,'source':value['source'],'policy':{'cancel_fee_minor':0,'change_fee_minor':0,'label':'免费退改，仅补退差价'}}

    @app.get('/api/health')
    def health():return {'status':'ok','service':'travel-demo'}

    @app.get('/api/travel/session')
    def session(request:Request):
        try:return context(identity(request))
        except HTTPException as exc:
            if exc.status_code!=401:raise
            return {'authenticated':False,'connected':False,'cities':CITIES,'simulated':True}

    @app.post('/api/travel/connect')
    async def connect(body:Connect,request:Request):
        person=identity(request)
        if person['bank_owner']:raise HTTPException(409,'当前旅行账户已关联有数，无需重复关联。')
        client=request.client.host if request.client else 'unknown'
        with connection() as db:
            db.execute('INSERT INTO travel_pair_limits VALUES (?,?,1) ON CONFLICT(client) DO UPDATE SET attempts=CASE WHEN window=excluded.window THEN attempts+1 ELSE 1 END,window=excluded.window',(client,int(time.time()//60)))
            count=db.execute('SELECT attempts FROM travel_pair_limits WHERE client=?',(client,)).fetchone()[0]
        if count>10:raise HTTPException(429,'同步码尝试过于频繁。')
        try:
            async with httpx.AsyncClient(base_url=bank_url,transport=getattr(app.state,'bank_transport',None),timeout=10,trust_env=False,verify=False) as client:
                response=await client.post('/internal/connections/redeem',json={'code':body.code,'platform':'travel','platform_user':person['id']},headers={'x-travel-key':secret(key)})
        except httpx.HTTPError:raise HTTPException(503,'无法连接有数后台，请稍后重试。') from None
        if not response.is_success:raise HTTPException(response.status_code,response.json().get('detail','关联失败。'))
        value=response.json()
        with connection() as db:
            old=db.execute('SELECT * FROM travel_bindings WHERE bank_owner=? OR travel_user=?',(value['owner'],person['id'])).fetchall()
            if any(r['bank_owner']!=value['owner'] or r['travel_user']!=person['id'] for r in old):
                raise HTTPException(409,'一个有数账户只能关联一个旅行账户。')
            db.execute('INSERT OR IGNORE INTO travel_bindings VALUES (?,?,?)',(value['owner'],person['id'],value['label']))
        return context(identity(request))

    @app.post('/api/travel/search')
    def public_search(body:Trip,request:Request):return search(identity(request),body)

    @app.get('/api/travel/orders')
    async def public_orders(request:Request):
        person=identity(request)
        if not person['bank_owner']:return {'orders':[],'simulated':True}
        try:
            async with httpx.AsyncClient(base_url=bank_url,transport=getattr(app.state,'bank_transport',None),timeout=10,trust_env=False,verify=False) as client:
                response=await client.post('/internal/travel/orders',json={'owner':person['bank_owner']},headers={'x-travel-key':secret(key)})
        except httpx.HTTPError:raise HTTPException(503,'有数后台不可用，订单尚未刷新。') from None
        if not response.is_success:raise HTTPException(response.status_code,'无法读取当前关联账户的旅行订单。')
        return response.json()

    @app.get('/internal/travel/{owner}/context')
    def internal_context(owner:str,request:Request):
        require_bridge(request,key)
        try:return context(linked(owner))
        except HTTPException as exc:
            if exc.status_code!=400:raise
            return {'connected':False,'cities':CITIES,'simulated':True}

    @app.post('/internal/travel/{owner}/search')
    def internal_search(owner:str,body:Trip,request:Request):
        require_bridge(request,key)
        return search(linked(owner),body)

    @app.post('/internal/travel/{owner}/quote')
    def internal_quote(owner:str,body:Quote,request:Request):
        require_bridge(request,key)
        return quote(linked(owner),body)

    @app.get('/')
    def home():return FileResponse(ROOT/'frontend/dist/travel.html')
    assets=ROOT/'frontend/dist/assets'
    if assets.is_dir():app.mount('/assets',StaticFiles(directory=assets),name='assets')
    return app

app=create_travel_app()
