import asyncio
import json
import time
from urllib.parse import quote as urlquote

import httpx
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from .bank import BankError, table, yuan
from .db import dumps
from .market_clock import next_checkpoint
from .securities_common import owner, require_bridge, secret


def portfolio(db):
    from .account_lock import check
    check(db)
    return {"positions": [dict(r) for r in db.execute("SELECT * FROM securities_positions ORDER BY symbol")], "orders": [{**dict(r), "items": json.loads(r["items_json"]), "side": json.loads(r["items_json"])[0].get("side", "buy")} for r in db.execute("SELECT * FROM securities_orders ORDER BY created_at DESC LIMIT 50")], "realized_profit_minor": sum(i.get("realized_profit_minor", 0) for r in db.execute("SELECT items_json FROM securities_orders") for i in json.loads(r[0])), "simulated": True, "valuation": "买入成本为记账数据，市值与浮动盈亏由关联证券平台API估值。"}


def quote_scope(db, quote_id, conversation_id):
    row = db.execute("SELECT * FROM securities_quotes WHERE id=?", (quote_id,)).fetchone()
    if not row or row["conversation_id"] != conversation_id:
        raise BankError("证券报价不属于当前会话，请重新查询自选并生成报价。")


class SecuritiesBridge:
    def __init__(self, database, settings):
        self.db = database
        self.settings = settings
        self.transport = None
        self.valuation_cache = {}
        self.valuation_lock = asyncio.Lock()

    async def request(self, path, body=None):
        from .safety import platform_request
        async def operation():
            async with httpx.AsyncClient(base_url=self.settings.securities_url, timeout=12 if path.endswith('/valuation') else 120, transport=self.transport, trust_env=False, verify=not self.settings.securities_url.startswith('http://')) as client:
                return await client.request("GET" if body is None else "POST", path, json=body, headers={"x-securities-key": secret(self.settings.securities_bridge_key_path)})
        return await platform_request(self.db, 'securities', operation)

    async def valued_positions(self, positions, *, refresh=False):
        if not positions:
            return []
        key = dumps(positions)
        async with self.valuation_lock:
            previous = self.valuation_cache.get(key)
            if not refresh and previous and previous[0] > time.time():
                return previous[1]
            with self.db.transaction() as db:
                investor = owner(db)
            try:
                result = await asyncio.wait_for(self.request(f'/internal/securities/{investor}/valuation', {'positions': positions}), timeout=15)
                values = result['positions']
                if [(p['symbol'],p['shares'],p['cost_minor']) for p in values] != [(p['symbol'],p['shares'],p['cost_minor']) for p in positions]:
                    raise BankError('证券估值持仓不匹配。')
                values = [{**p, 'valuation_stale': bool(p.get('valuation_stale')), 'value_minor': p['market_value_minor'] if p['market_value_minor'] is not None else p['cost_minor']} for p in values]
                if previous:
                    known = {p['symbol']: p for p in previous[1]}
                    values = [{**known[p['symbol']], 'valuation_stale': True} if p['market_value_minor'] is None and known.get(p['symbol'],{}).get('market_value_minor') is not None else p for p in values]
            except (BankError, TimeoutError, KeyError, TypeError):
                values = [{**p, 'valuation_stale': True} for p in previous[1]] if previous else [{**p, 'value_minor': p['cost_minor'], 'market_value_minor': None, 'profit_minor': None, 'profit_percent': None, 'price_minor': None, 'quote_date': None, 'source': None, 'price_kind': None, 'valuation_stale': True} for p in positions]
            retry_soon = any(p.get('valuation_stale') or p['market_value_minor'] is None for p in values)
            self.valuation_cache = {key: (time.time()+2 if retry_soon else next_checkpoint().timestamp(), values)}
            return values

    async def snapshot(self, *, refresh=False):
        from .bank import Bank
        value = Bank(self.db).snapshot()
        positions = await self.valued_positions(value['securities_positions'], refresh=refresh)
        stocks = sum(p['value_minor'] for p in positions)
        investments = sum(h['value_minor'] for h in value['holdings'])+stocks
        complete = all(p['market_value_minor'] is not None for p in positions)
        return {**value, 'securities_positions': positions, 'securities_value_minor': stocks, 'securities_market_value_minor': stocks if complete else None, 'securities_profit_minor': stocks-value['securities_cost_minor'] if complete else None, 'securities_valuation_complete': complete, 'securities_valuation_stale': any(p.get('valuation_stale') for p in positions), 'investment_assets_minor': investments, 'total_assets_minor': value['account_balance_minor']+investments}

    async def query(self, name, args, conversation_id):
        from .stock_trading import status
        if name == 'get_securities_positions':
            with self.db.connection() as db:
                value = portfolio(db)
            positions = await self.valued_positions(value['positions'])
            return {**value, 'positions': positions, 'display': table('有数证券模拟持仓', ['股票', '代码', '股数', '成本', '估值', '浮动盈亏'], [[p['name'], p['symbol'], str(p['shares']), yuan(p['cost_minor']), yuan(p['value_minor']), yuan(p['profit_minor']) if p['profit_minor'] is not None else '暂无行情'] for p in positions], note='估值来自关联证券平台；行情不可用时保留上次估值或按成本暂估，详见来源和时间。')}
        if name == 'search_securities_stocks':
            value = await self.request('/internal/securities/stocks/search?q=' + urlquote(args['query'], safe=''))
            answer = '请按名称和代码核对候选；名称不明确时先让用户选择。可购买非自选股票，查价格不需要购买数量。' if value['items'] else '证券平台未找到匹配股票，请提供更准确名称或六位代码。'
            return {**value, 'answer': answer, 'outcome': {'status': 'verified'}, 'display': table('证券股票搜索', ['股票', '代码'], [[i['name'], i['symbol']] for i in value['items']], note=value['source'] + '。' + answer)}
        if name == 'get_stock_price':
            value = await self.request(f"/internal/securities/stocks/{args['symbol']}/price")
            value['trading_status']=status()
            answer = f"{value['name']}（{value['symbol']}）每股 {yuan(value['price_minor'])}；{value['price_kind']}，时间 {value['as_of'] or '未提供'}。{value['note']}"
            return {**value, 'answer': answer, 'outcome': {'status': 'verified'}, 'display': table('证券股票价格', ['股票', '代码', '每股价格', '价格类型', '时间', '行情来源'], [[value['name'], value['symbol'], yuan(value['price_minor']), value['price_kind'], value['as_of'] or '未提供', value['source']]], note=value['note'])}
        with self.db.transaction() as db:
            investor = owner(db)
            risk = db.execute("SELECT risk_level FROM risk_profile WHERE id=1").fetchone()
            level = risk[0] if risk else None
        linked = await self.request(f"/internal/securities/{investor}/watchlist")
        if not linked["connected"]:
            answer = "请先在独立证券网站 http://127.0.0.1:8001/ 点击“连接有数账户”并同步自选。当前尚未连接，不能读取或购买；尚未扣款。"
            return {"outcome": {"status": "needs_clarification"}, "answer": answer, "connected": False, "display": table("连接证券账户", [], [], note=answer)}
        if name == 'add_securities_watchlist':
            value = await self.request(f'/internal/securities/{investor}/watchlist/add', args)
            item = value['stock']
            answer = f"{item['name']}（{item['symbol']}）" + ('已加入关联证券账户的自选。' if value['added'] else '已在关联证券账户的自选中，无需重复添加。')
            return {**value, 'answer': answer, 'outcome': {'status': 'verified'}, 'display': table('证券自选已更新', ['股票', '代码', '结果'], [[item['name'], item['symbol'], '已加入' if value['added'] else '已在自选']], note=answer)}
        if name == "get_securities_watchlist":
            items = linked["watchlist"]
            answer = "非自选股票也可先搜索核对再购买；查单股价格用get_stock_price，无需购买数量。请用户说明要购买哪些股票、每只预算金额（元）、股数或手数；按金额自动换算整手，不能默认购买全部或默认数量。" if items else "该账户的证券自选为空，请先在证券网站添加自选。"
            return {**linked, "risk_level": level, "required_risk_level": 4, "outcome": {"status": "verified"}, "answer": answer, "display": table("有数证券账户自选", ["股票", "代码", "模拟风险", "演示最小股数"], [[p["name"], p["symbol"], "R4", "100股"] for p in items], note=f"当前测评：{'R'+str(level) if level else '未完成'}。股票模拟买入需R4及以上、用户确认、支付密码和短信验证码。{answer}")}
        from .stock_trading import require_open
        require_open()
        if name == 'quote_stock_sale':
            with self.db.connection() as db:
                check_sale_positions(db, args['orders'])
        quote = await self.request(f"/internal/securities/{investor}/{'sale-quote' if name == 'quote_stock_sale' else 'quote'}", args)
        quote['trading_status']=status()
        if quote.get("owner") != investor or not quote.get("items") or quote["amount_minor"] != sum(i["price_minor"] * i["shares"] for i in quote["items"]):
            raise BankError("证券服务返回的报价不完整，尚未扣款。")
        with self.db.transaction() as db:
            db.execute("INSERT INTO securities_quotes VALUES (?,?,?,?)", (quote["quote_id"], conversation_id, dumps(quote), quote["expires"]))
        if name == 'quote_stock_sale':
            return {**quote, 'outcome': {'status': 'verified'}, 'answer': '凭当前报价调用sell_stock_basket生成确认卡；卖出不要求风险等级，必须核对到账账户并验证支付密码和短信。', 'display': table('证券模拟卖出报价', ['股票', '代码', '股数', '模拟单价', '预计到账', '行情日期'], [[i['name'], i['symbol'], str(i['shares']), yuan(i['price_minor']), yuan(i['amount_minor']), i['quote_date']] for i in quote['items']], note=quote['pricing_mode'])}
        budget_note = " ".join(f"{i['name']}预算 {yuan(i['budget_minor'])}，实际 {yuan(i['amount_minor'])}，剩余 {yuan(i['remaining_minor'])} 不扣款。" for i in quote['items'] if 'budget_minor' in i)
        answer = budget_note + f"合计 {yuan(quote['amount_minor'])}，报价5分钟有效。" + ("当前测评未达到R4，不能购买；请先由用户完成风险测评。" if level is None or level < 4 else "确认购买可用 purchase_stock_basket 生成确认卡，付款账户在支付窗口选择。")
        return {**quote, "eligible": level is not None and level >= 4, "outcome": {"status": "verified"}, "answer": answer, "display": table("证券模拟买入报价", ["股票", "代码", "股数", "模拟单价", "金额", "行情日期"], [[i["name"], i["symbol"], str(i["shares"]), yuan(i["price_minor"]), yuan(i["amount_minor"]), i["quote_date"]] for i in quote["items"]], note=quote["pricing_mode"] + " " + answer)}


def purchase_summary(bank, db, args):
    from .stock_trading import require_open
    require_open()
    row = db.execute("SELECT * FROM securities_quotes WHERE id=?", (args["quote_id"],)).fetchone()
    if not row or row["expires"] <= time.time():
        raise BankError("证券报价不存在或已超过5分钟，请重新生成；尚未扣款。")
    quote = json.loads(row["payload"])
    if quote.get("side", "buy") != "buy":
        raise BankError("卖出报价不能用于买入。")
    if quote["owner"] != owner(db):
        raise BankError("证券报价不属于当前用户。")
    if db.execute("SELECT 1 FROM securities_orders WHERE quote_id=?", (args["quote_id"],)).fetchone():
        raise BankError("该证券报价已经成交，不能重复扣款。")
    risk = db.execute("SELECT risk_level FROM risk_profile WHERE id=1").fetchone()
    if not risk or risk[0] < 4:
        raise BankError("股票模拟买入为R4风险，需先由用户完成相匹配的风险测评，不能购买本行理财代替。")
    account = bank._check_debit(db, args["account_id"], quote["amount_minor"])
    fields = [["付款账户", account["name"]], ["总金额", yuan(quote["amount_minor"])], ["模拟风险", "R4 · 必须支付密码和短信验证"]]
    fields += [[i["name"] + " " + i["symbol"], f"{i['shares']}股 × {yuan(i['price_minor'])} = {yuan(i['amount_minor'])}（行情日期 {i['quote_date']}）"] for i in quote["items"]]
    fields += [[i['name'] + '预算与余款', f"预算 {yuan(i['budget_minor'])}；剩余 {yuan(i['remaining_minor'])} 不扣款"] for i in quote['items'] if 'budget_minor' in i]
    fields.append(["成交说明", quote["pricing_mode"]])
    return {"title": "确认证券模拟买入", "fields": [{"label": a, "value": b} for a, b in fields], "amount_minor": quote["amount_minor"], "quote_id": quote["quote_id"], "simulated": True}


def execute_purchase(bank, db, args, action_id):
    from .stock_trading import require_open
    require_open()
    from .db import now, uid

    quote = json.loads(db.execute("SELECT payload FROM securities_quotes WHERE id=?", (args["quote_id"],)).fetchone()[0])
    amount = quote["amount_minor"]
    order_id = uid("stockorder_")
    db.execute("INSERT INTO securities_orders VALUES (?,?,?,?,?,?,?)", (order_id, action_id, args["quote_id"], args["account_id"], amount, dumps(quote["items"]), now()))
    db.execute("UPDATE accounts SET balance_minor=balance_minor-? WHERE id=?", (amount, args["account_id"]))
    bank._ledger(db, action_id + ":stocks", args["account_id"], "有数证券模拟交易", "out", amount, "证券投资", "模拟股票买入，订单 " + order_id)
    for item in quote["items"]:
        db.execute("INSERT INTO securities_positions VALUES (?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET shares=shares+excluded.shares,cost_minor=cost_minor+excluded.cost_minor", (item["symbol"], item["name"], item["shares"], item["amount_minor"]))
    return {"order_id": order_id, "items": quote["items"], "amount_minor": amount, "simulated": True, "external_real_order": False}


class PortfolioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str


def register_bank_securities(app, database, agent, settings, session):
    bridge = SecuritiesBridge(database, settings)
    agent.securities = bridge

    @app.post("/api/securities/pairing-code", dependencies=[Depends(session)])
    async def pairing_code():
        from .connections import generate
        return generate(database)

    @app.post("/api/securities/portfolio", dependencies=[Depends(session)])
    def read_portfolio(body: PortfolioRequest, request: Request):
        require_bridge(request, settings.securities_bridge_key_path)
        with database.transaction() as db:
            if body.owner != owner(db):
                raise HTTPException(403, "证券连接账户与当前有数登录账户不同。")
            return portfolio(db)

    @app.post('/internal/securities/portfolio')
    def internal_portfolio(body: PortfolioRequest, request: Request):
        require_bridge(request, settings.securities_bridge_key_path)
        with database.transaction() as db:
            if body.owner != owner(db):
                raise HTTPException(403, '证券关联不属于此账户。')
            return portfolio(db)


def check_sale_positions(db, orders):
    if len({i['symbol'] for i in orders}) != len(orders):
        raise BankError('同一股票不能重复卖出。')
    for item in orders:
        position = db.execute('SELECT * FROM securities_positions WHERE symbol=?', (item['symbol'],)).fetchone()
        if not position or item['shares'] > position['shares']:
            raise BankError('卖出股数不能超过当前实际持仓。')


def sale_summary(bank, db, args):
    from .stock_trading import require_open
    require_open()
    row = db.execute('SELECT * FROM securities_quotes WHERE id=?', (args['quote_id'],)).fetchone()
    if not row or row['expires'] <= time.time():
        raise BankError('证券报价不存在或已超过5分钟，请重新生成。')
    quote = json.loads(row['payload'])
    if quote.get('side') != 'sell' or quote['owner'] != owner(db):
        raise BankError('报价方向或所属账户不匹配。')
    if db.execute('SELECT 1 FROM securities_orders WHERE quote_id=?', (args['quote_id'],)).fetchone():
        raise BankError('该证券报价已经成交，不能重复卖出。')
    check_sale_positions(db, quote['items'])
    account = db.execute('SELECT * FROM accounts WHERE id=?', (args['account_id'],)).fetchone()
    if not account or account["kind"] == "bank_card_unlinked":
        raise BankError('到账账户不存在或已解绑，请重新选择。')
    fields = [{'label':'到账账户','value':account['name']}, {'label':'预计到账','value':yuan(quote['amount_minor'])}]
    fields += [{'label': i['name']+' '+i['symbol'], 'value':f"{i['shares']}股 × {yuan(i['price_minor'])}（行情日期 {i['quote_date']}）"} for i in quote['items']]
    fields += [{'label':'成交说明','value':quote['pricing_mode']}, {'label':'验证要求','value':'支付密码和短信验证码'}]
    return {'title':'确认证券模拟卖出','fields':fields,'amount_minor':quote['amount_minor'],'simulated':True}


def execute_sale(bank, db, args, action_id):
    from .stock_trading import require_open
    require_open()
    from .db import now, uid

    quote = json.loads(db.execute('SELECT payload FROM securities_quotes WHERE id=?', (args['quote_id'],)).fetchone()[0])
    items = []
    for item in quote['items']:
        position = db.execute('SELECT * FROM securities_positions WHERE symbol=?', (item['symbol'],)).fetchone()
        released = position['cost_minor'] * item['shares'] // position['shares']
        remaining = position['shares'] - item['shares']
        if remaining:
            db.execute('UPDATE securities_positions SET shares=?,cost_minor=cost_minor-? WHERE symbol=?', (remaining, released, item['symbol']))
        else:
            db.execute('DELETE FROM securities_positions WHERE symbol=?', (item['symbol'],))
        items.append({**item, 'side':'sell', 'released_cost_minor':released, 'realized_profit_minor':item['amount_minor']-released})
    order_id = uid('stockorder_')
    amount = quote['amount_minor']
    db.execute('INSERT INTO securities_orders VALUES (?,?,?,?,?,?,?)', (order_id, action_id, args['quote_id'], args['account_id'], amount, dumps(items), now()))
    db.execute('UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?', (amount, args['account_id']))
    bank._ledger(db, action_id+':stocks', args['account_id'], '有数证券模拟交易', 'in', amount, '证券卖出', '模拟股票卖出，订单 '+order_id)
    return {'order_id':order_id,'items':items,'amount_minor':amount,'side':'sell','realized_profit_minor':sum(i['realized_profit_minor'] for i in items),'simulated':True,'external_real_order':False}
