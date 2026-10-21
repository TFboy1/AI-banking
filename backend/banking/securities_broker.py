import asyncio
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

import httpx
from fastapi import HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .db import TZ, uid
from .market_clock import day_price
from .securities_auth import register_stock_auth
from .securities_common import (
    QuoteStockPurchase,
    QuoteStocks,
    StockSymbol,
    ValuationRequest,
    HistoricalValuationRequest,
    require_bridge,
    secret,
    token_hash,
)
from .stocks import CATALOG

SCHEMA = """
CREATE TABLE IF NOT EXISTS investors (id TEXT PRIMARY KEY,label TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY,owner TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS watchlist (owner TEXT NOT NULL,symbol TEXT NOT NULL,ordinal INTEGER NOT NULL,PRIMARY KEY(owner,symbol));
CREATE TABLE IF NOT EXISTS pairing_codes (code_hash TEXT PRIMARY KEY,owner TEXT NOT NULL,label TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pairing_attempts (client TEXT PRIMARY KEY,window INTEGER NOT NULL,attempts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS paired_accounts (owner TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS stock_identities (symbol TEXT PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS watchlist_audit (owner TEXT NOT NULL,symbol TEXT NOT NULL,operation TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS broker_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
"""


class WatchlistBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbols: list[str] = Field(max_length=20)


class ConnectBody(WatchlistBody):
    code: str = Field(pattern=r"^[0-9]{8}$")


class PairingIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=40)


def valid_symbols(symbols):
    import re

    if any(not re.fullmatch(r"[0-9]{6}", s) for s in symbols):
        raise HTTPException(422, "自选股票须为六位代码。")
    if any(s not in {i["symbol"] for i in CATALOG} for s in symbols):
        raise HTTPException(422, "测试版本仅支持股票池中的20只代表性股票。")
    return list(dict.fromkeys(symbols))


def register_broker(app, database_path, key_path, bank_url):
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as db:
        db.executescript(SCHEMA)
        if not db.execute("SELECT 1 FROM broker_meta WHERE key='code-pairing-v1'").fetchone():
            # Previously authorized watchlists are preserved, but old cookie-only
            # sessions must reconnect using the newly required pairing code.
            db.execute('DELETE FROM sessions')
            db.execute("INSERT INTO broker_meta VALUES ('code-pairing-v1','1')")

    @contextmanager
    def connection():
        with sqlite3.connect(database_path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            yield db

    identity = register_stock_auth(app, connection)

    def watchlist(investor):
        names = {s["symbol"]: s["name"] for s in CATALOG}
        with connection() as db:
            names.update({r[0]: r[1] for r in db.execute("SELECT symbol,name FROM stock_identities")})
            items = db.execute("SELECT symbol FROM watchlist WHERE owner=? ORDER BY ordinal", (investor,)).fetchall()
        return [{"symbol": r[0], "name": names.get(r[0], r[0]), "risk_level": 4, "minimum_shares": 100} for r in items]

    async def bank_request(request, path, body):
        try:
            async with httpx.AsyncClient(base_url=bank_url, timeout=8, transport=getattr(app.state, "bank_transport", None)) as client:
                result = await client.post(path, json=body, headers={"x-securities-key": secret(key_path)})
        except httpx.HTTPError:
            raise HTTPException(503, "无法连接有数银行，请确认8000后台已启动。") from None
        if not result.is_success:
            raise HTTPException(result.status_code, result.json().get("detail", "有数后台请求未完成，请检查服务与账号关联。"))
        return result.json()

    @app.middleware("http")
    async def origin_guard(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin not in {"http://127.0.0.1:8001", "http://localhost:8001"}:
                return Response(status_code=403)
        return await call_next(request)

    @app.get("/api/securities/session")
    def session(request: Request):
        try:
            person = identity(request)
        except HTTPException:
            return {"authenticated": False, "connected": False, "label": "", "watchlist": []}
        return {"authenticated": True, "user_id": person["id"], "connected": bool(person["bank_owner"]), "label": person["label"], "phone_masked": person["phone"][:3]+"****"+person["phone"][-4:], "bank_label": person["bank_label"], "watchlist": watchlist(person["id"])}

    @app.post("/internal/securities/pairing-code")
    def pairing_code(body: PairingIdentity, request: Request):
        require_bridge(request, key_path)
        code = ''.join(secrets.choice('0123456789') for _ in range(8))
        expires = time.time() + 300
        with connection() as db:
            db.execute('DELETE FROM pairing_codes WHERE owner=? OR expires<=?', (body.owner, time.time()))
            db.execute('INSERT INTO pairing_codes VALUES (?,?,?,?)', (token_hash(code), body.owner, body.label, expires))
        return {'code': code, 'expires': expires, 'expires_in': 300}

    @app.post("/api/securities/connect")
    async def connect(body: ConnectBody, request: Request, response: Response):
        person = identity(request)
        symbols = valid_symbols(body.symbols)
        client = request.client.host if request.client else 'unknown'
        window = int(time.time() // 60)
        with connection() as db:
            db.execute('INSERT INTO pairing_attempts VALUES (?,?,1) ON CONFLICT(client) DO UPDATE SET attempts=CASE WHEN window=excluded.window THEN attempts+1 ELSE 1 END,window=excluded.window', (client, window))
            attempts = db.execute('SELECT attempts FROM pairing_attempts WHERE client=?', (client,)).fetchone()[0]
        if attempts > 10:
            raise HTTPException(429, '同步码尝试过于频繁，请稍后重试。')
        with connection() as db:
            row = db.execute("SELECT * FROM pairing_codes WHERE code_hash=?", (token_hash(body.code),)).fetchone()
            if row and row['expires'] <= time.time():
                raise HTTPException(400, '同步码已过期或已使用。')
            authorization = dict(row) if row else None
        if authorization is None:
            authorization = await bank_request(request, '/internal/connections/redeem', {'code': body.code, 'platform': 'securities', 'platform_user': person['id']})
        investor, label = authorization['owner'], authorization['label']
        with connection() as db:
            existing_bank = db.execute('SELECT * FROM stock_bindings WHERE bank_owner=?', (investor,)).fetchone()
            existing_stock = db.execute('SELECT * FROM stock_bindings WHERE stock_user=?', (person['id'],)).fetchone()
            if existing_bank and existing_bank['stock_user'] != person['id']:
                raise HTTPException(409, '该有数账户已关联其他证券账号，一个有数账户只能关联一个证券账号。')
            if existing_stock and existing_stock['bank_owner'] != investor:
                raise HTTPException(409, '当前证券账号已关联其他有数账户，不能替换关联。')
            db.execute('UPDATE pairing_codes SET expires=0 WHERE code_hash=?', (token_hash(body.code),))
            db.execute('INSERT OR IGNORE INTO stock_bindings VALUES (?,?,?)', (investor, person['id'], label))
            previous = [r[0] for r in db.execute('SELECT symbol FROM watchlist WHERE owner=? ORDER BY ordinal', (person['id'],))]
            legacy = [r[0] for r in db.execute('SELECT symbol FROM watchlist WHERE owner=? ORDER BY ordinal', (investor,))]
            combined = list(dict.fromkeys(previous + legacy + symbols))[:20]
            db.execute('DELETE FROM watchlist WHERE owner=? OR owner=?', (person['id'], investor))
            db.executemany('INSERT INTO watchlist VALUES (?,?,?)', [(person['id'], code, i) for i, code in enumerate(combined)])
        return session(request)

    @app.put("/api/securities/watchlist")
    def save_watchlist(body: WatchlistBody, request: Request):
        person = identity(request)
        symbols = valid_symbols(body.symbols)
        with connection() as db:
            db.execute("DELETE FROM watchlist WHERE owner=?", (person["id"],))
            db.executemany("INSERT INTO watchlist VALUES (?,?,?)", [(person["id"], s, i) for i, s in enumerate(symbols)])
        return {"watchlist": watchlist(person["id"])}

    @app.get("/api/securities/portfolio")
    async def portfolio(request: Request):
        person = identity(request)
        if not person["bank_owner"]:
            return {"positions": [], "orders": [], "simulated": True}
        return await bank_request(request, "/internal/securities/portfolio", {"owner": person["bank_owner"]})

    @app.get("/internal/securities/{investor}/watchlist")
    def internal_watchlist(investor: str, request: Request):
        require_bridge(request, key_path)
        with connection() as db:
            person = db.execute("SELECT u.* FROM stock_users u JOIN stock_bindings b ON b.stock_user=u.id WHERE b.bank_owner=?", (investor,)).fetchone()
        return {"connected": bool(person), "watchlist": watchlist(person["id"]) if person else [], "label": person["label"] if person else ""}

    @app.get('/api/securities/account')
    async def account(request: Request):
        value = await portfolio(request)

        from .stock_valuation import totals, value_positions
        positions = await value_positions(app.state.stock_service, value['positions'])
        return {**value, 'positions': positions, **totals(positions)}

    @app.post('/internal/securities/{investor}/valuation')
    async def valuation(investor: str, body: ValuationRequest, request: Request):
        require_bridge(request, key_path)
        if not internal_watchlist(investor, request)['connected']:
            raise HTTPException(400, '当前有数账户未关联证券账号。')
        from .stock_valuation import totals, value_positions
        positions = await value_positions(app.state.stock_service, [p.model_dump() for p in body.positions])
        return {'positions': positions, **totals(positions)}

    @app.post('/internal/securities/{investor}/valuation-at')
    async def historical_valuation(investor:str,body:HistoricalValuationRequest,request:Request):
        require_bridge(request,key_path)
        if not internal_watchlist(investor,request)['connected']:
            raise HTTPException(400,'当前有数账户未关联证券账号。')
        if body.cutoff_date>datetime.now(TZ).date():raise HTTPException(400,'不能查询未来时点的历史估值。')
        from .stock_valuation import totals,value_positions
        positions=await value_positions(app.state.stock_service,[p.model_dump() for p in body.positions],cutoff_date=body.cutoff_date.isoformat())
        return {'positions':positions,**totals(positions),'cutoff_date':body.cutoff_date.isoformat(),'valuation':'按截止日之前最近的已确认收盘价估值，不使用截止日内的开盘价或收盘价。'}

    async def search_market(keyword):
        keyword = keyword.strip().lower()
        selected = [dict(item) for item in CATALOG if keyword in item['symbol'] or keyword in item['name'].lower()]
        return selected[:30]

    async def market_identity(symbol):
        items = await search_market(symbol)
        match = next((i for i in items if i['symbol'] == symbol), None)
        if not match:
            raise HTTPException(400, '该股票不在当前20只测试股票池中，请从证券平台股票池选择。')
        with connection() as db:
            db.execute('INSERT OR REPLACE INTO stock_identities VALUES (?,?)', (match['symbol'], match['name']))
        return match

    @app.get('/internal/securities/stocks/search')
    async def search(request: Request, q: str = Query(min_length=1, max_length=50)):
        require_bridge(request, key_path)
        return {'items': await search_market(q), 'source': '有数证券股票目录/行情服务', 'scope': '测试股票池：20只代表性A股'}

    @app.get('/internal/securities/stocks/{symbol}/price')
    async def stock_price(symbol: str, request: Request):
        require_bridge(request, key_path)
        if not re.fullmatch(r'[0-9]{6}', symbol):
            raise HTTPException(422, '请输入六位股票代码。')
        identity = await market_identity(symbol)
        history = await app.state.stock_service.get('history', symbol, '')
        if history.get('status') != 'ok' or not history.get('rows'):
            raise HTTPException(503, '证券行情暂不可用，无法确认股价；尚未生成订单或扣款。')
        last = history['rows'][-1]
        age = (datetime.now(TZ).date() - date.fromisoformat(last['date'])).days
        amount, kind = day_price(last)
        price = Decimal(str(amount or 0))
        if not price.is_finite() or price <= 0 or not 0 <= age <= 10:
            raise HTTPException(503, '证券行情过旧或无效，无法确认股价。')
        return {**identity, 'price_minor': int((price*100).quantize(Decimal(1), rounding=ROUND_HALF_UP)), 'price_kind': kind, 'as_of': last['date'], 'source': history.get('source'), 'note': '演示只使用开盘价、收盘价，不代表实时价格。', 'simulated': False}

    @app.post('/internal/securities/{investor}/watchlist/add')
    async def add_watchlist(investor: str, body: StockSymbol, request: Request):
        require_bridge(request, key_path)
        item = await market_identity(body.symbol)
        with connection() as db:
            person = db.execute('SELECT stock_user FROM stock_bindings WHERE bank_owner=?', (investor,)).fetchone()
            if not person:
                raise HTTPException(400, '当前有数账户尚未关联证券账号。')
            stock_user = person['stock_user']
            exists = db.execute('SELECT 1 FROM watchlist WHERE owner=? AND symbol=?', (stock_user, body.symbol)).fetchone()
            if not exists:
                count = db.execute('SELECT count(*) FROM watchlist WHERE owner=?', (stock_user,)).fetchone()[0]
                if count >= 20:
                    raise HTTPException(400, '证券自选最多20只，请先在证券平台移除部分股票。')
                ordinal = db.execute('SELECT coalesce(max(ordinal),-1)+1 FROM watchlist WHERE owner=?', (stock_user,)).fetchone()[0]
                db.execute('INSERT INTO watchlist VALUES (?,?,?)', (stock_user, body.symbol, ordinal))
                db.execute('INSERT INTO watchlist_audit VALUES (?,?,?,?)', (stock_user, body.symbol, 'add', time.time()))
        return {'status': 'executed', 'added': not bool(exists), 'stock': item, 'watchlist': watchlist(stock_user), 'source': '有数证券账户API'}

    @app.post("/internal/securities/{investor}/quote")
    async def quote(investor: str, body: QuoteStockPurchase, request: Request):
        require_bridge(request, key_path)
        from .stock_trading import require_open
        require_open()
        linked = internal_watchlist(investor, request)
        if not linked["connected"]:
            raise HTTPException(400, "当前有数账户尚未关联证券账号。")
        symbols = [item.symbol for item in body.orders]
        if len(set(symbols)) != len(symbols):
            raise HTTPException(400, "同一股票不能重复报价。")
        identities = await asyncio.gather(*(market_identity(symbol) for symbol in symbols))
        allowed = {i['symbol']: i for i in identities}
        return await build_quote(investor, body, allowed, 'buy')

    @app.post('/internal/securities/{investor}/sale-quote')
    async def sale_quote(investor: str, body: QuoteStocks, request: Request):
        require_bridge(request, key_path)
        from .stock_trading import require_open
        require_open()
        if not internal_watchlist(investor, request)['connected']:
            raise HTTPException(400, '当前有数账户尚未关联证券账号。')
        allowed = {item.symbol: {'name': next((s['name'] for s in CATALOG if s['symbol']==item.symbol), item.symbol)} for item in body.orders}
        if len(allowed) != len(body.orders):
            raise HTTPException(400, '同一股票不能重复报价。')
        return await build_quote(investor, body, allowed, 'sell')

    async def build_quote(investor, body, allowed, side):
        try:
            histories = await asyncio.wait_for(asyncio.gather(*(app.state.stock_service.get("history", order.symbol, "") for order in body.orders)), timeout=100)
        except TimeoutError:
            raise HTTPException(503, "行情查询超时，尚未生成报价或扣款，请减少股票数量后重试。") from None
        items = []
        for order, result in zip(body.orders, histories):
            if result.get("status") != "ok" or not result["rows"]:
                raise HTTPException(503, f"{order.symbol} 没有可核对的行情，不能生成交易报价。")
            last = result["rows"][-1]
            age = (datetime.now(TZ).date() - date.fromisoformat(last["date"])).days
            if age < 0 or age > 10:
                raise HTTPException(400, "历史行情日期过旧或无效，请更新行情后再试。")
            amount, kind = day_price(last)
            price_minor = int((Decimal(str(amount or 0)) * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            if price_minor <= 0:
                raise HTTPException(400, "股票价格无效。")
            shares = order.shares
            budget = getattr(order, 'amount_yuan', None)
            extra = {}
            if budget is not None:
                budget_minor = int(Decimal(budget) * 100)
                shares = budget_minor // (price_minor * 100) * 100
                minimum_minor = price_minor * 100
                if shares < 100:
                    raise HTTPException(400, f"{allowed[order.symbol]['name']}预算 ¥{Decimal(budget):.2f} 不足一手（100股）；按本次报价每股 ¥{Decimal(price_minor)/100:.2f}，至少需要 ¥{Decimal(minimum_minor)/100:.2f}。尚未生成订单或扣款。")
                if shares > 100000:
                    raise HTTPException(400, "预算换算股数超过单只股票100000股上限。")
                extra = {'budget_minor': budget_minor, 'remaining_minor': budget_minor - price_minor * shares}
            items.append({"symbol": order.symbol, "name": allowed[order.symbol]["name"], "shares": shares, "price_minor": price_minor, "amount_minor": price_minor * shares, **extra, "quote_date": last["date"], "price_kind": kind, "source": result["source"], "risk_level": 4})
        total = sum(item["amount_minor"] for item in items)
        if total > 100_000_000:
            raise HTTPException(400, "模拟订单总额超过上限。")
        return {"quote_id": uid("stockquote_"), "side": side, "owner": investor, "items": items, "amount_minor": total, "expires": time.time() + 300, "risk_level": 4, "simulated": True, "pricing_mode": "仅在交易日09:30–11:30、13:00–15:00模拟买卖，按已取得的开盘价模拟成交；收盘价只用于展示和估值，不代表实时成交价；演示不计佣金税费，统一按100股整数手交易；按金额买入向下取整，剩余预算不扣款。"}
