import time

import httpx
from banking.bank import Bank
from banking.food_site import create_food_app
from banking.main import create_app
from banking.stock_site import create_stock_app
from conftest import ScriptedModel
from test_food_delivery import food_login
from test_security import register
from test_stock_accounts import stock_login


async def test_one_code_two_platforms_and_matching_valuations(settings, tmp_path):
    settings.app_auth_enabled = True
    settings.food_bridge_key_path = tmp_path/'food-key'
    settings.securities_bridge_key_path = tmp_path/'stock-key'
    bank_app = create_app(settings, ScriptedModel())
    stock_app = create_stock_app(tmp_path/'market.sqlite3', broker_path=tmp_path/'stock.sqlite3', key_path=settings.securities_bridge_key_path)
    food_app = create_food_app(tmp_path/'food.sqlite3', settings.food_bridge_key_path)
    stock_app.state.bank_transport = httpx.ASGITransport(app=bank_app)
    food_app.state.bank_transport = httpx.ASGITransport(app=bank_app)
    bridge = bank_app.state.agent.securities
    bridge.transport = httpx.ASGITransport(app=stock_app)
    bank_app.state.agent.food.transport = httpx.ASGITransport(app=food_app)
    price, available = 12, True
    async def market(operation, symbol='', adjust=''):
        return {'status':'ok' if available else 'unavailable', 'rows':[{'symbol':'000001','name':'平安银行','date':'2026-10-16','open':price,'close':price}] if available else [], 'source':'isolated-securities-api', 'fetched_at':'2026-10-16T12:00:00+08:00'}
    stock_app.state.stock_service.get = market
    async with bank_app.router.lifespan_context(bank_app), httpx.AsyncClient(transport=httpx.ASGITransport(app=bank_app),base_url='http://127.0.0.1:8000') as bank, httpx.AsyncClient(transport=httpx.ASGITransport(app=stock_app),base_url='http://127.0.0.1:8001') as stocks, httpx.AsyncClient(transport=httpx.ASGITransport(app=food_app),base_url='http://127.0.0.1:8002') as food:
        assert (await bank.post('/api/connections/pairing-code',json={})).status_code == 401
        await register(bank)
        await stock_login(stocks,'11111111111')
        await food_login(food)
        code = (await bank.post('/api/connections/pairing-code',json={})).json()['code']
        assert (await stocks.post('/api/securities/connect',json={'symbols':['000001'],'code':code})).status_code == 200
        # Stock redemption does not consume the food platform's use of this same code.
        assert (await food.post('/api/food/connect',json={'code':code})).status_code == 200
        assert (await stocks.post('/api/securities/connect',json={'symbols':[],'code':code})).status_code == 400
        assert (await food.post('/api/food/connect',json={'code':code})).status_code == 400
        with bank_app.state.database.transaction() as db:
            db.execute("INSERT INTO securities_positions VALUES ('000001','平安银行',100,100000)")
            count = db.execute('SELECT count(*) FROM transactions').fetchone()[0]
        initial = (await bank.get('/api/bank')).json()
        cash = initial['account_balance_minor']
        assert initial['securities_value_minor'] == 120000
        assert initial['securities_profit_minor'] == 20000
        assert initial['securities_positions'][0]['profit_percent'] == 20
        assert (await stocks.get('/api/securities/account')).json()['market_value_minor'] == 120000
        for new_price in (15, 8):
            price = new_price
            bridge.valuation_cache.clear()
            value = (await bank.get('/api/bank?refresh=true')).json()
            assert value['securities_value_minor'] == new_price*100*100
            assert value['account_balance_minor'] == cash
            assert value['securities_cost_minor'] == 100000
            assert value['securities_market_value_minor'] == (await stocks.get('/api/securities/account')).json()['market_value_minor']
            assert value['total_assets_minor'] == cash+value['investment_assets_minor']
        available = False
        bridge.valuation_cache = {k:(0,v[1]) for k,v in bridge.valuation_cache.items()}
        stale = (await bank.get('/api/bank')).json()
        assert stale['securities_value_minor'] == 80000 and stale['securities_valuation_stale']
        bridge.valuation_cache.clear()
        unknown = (await bank.get('/api/bank')).json()
        assert unknown['securities_value_minor'] == 100000 and unknown['securities_market_value_minor'] is None
        assert unknown['securities_profit_minor'] is None and not unknown['securities_valuation_complete']
        with bank_app.state.database.connection() as db:
            assert db.execute('SELECT count(*) FROM transactions').fetchone()[0] == count
        old = (await bank.post('/api/connections/pairing-code',json={})).json()['code']
        new = (await bank.post('/api/connections/pairing-code',json={})).json()['code']
        assert old != new
        assert (await food.post('/api/food/connect',json={'code':old})).status_code == 400
        assert (await food.post('/api/food/connect',json={'code':new})).status_code == 200
        with bank_app.state.database.transaction() as db:
            db.execute('UPDATE connection_code SET expires=?',(time.time()-1,))
        assert (await stocks.post('/api/securities/connect',json={'symbols':[],'code':new})).status_code == 400


def test_monthly_bill_separates_all_investment_cash_flows(database):
    bank = Bank(database)
    values = [('meal','out',1000,'餐饮'),('salary','in',2000,'工资'),('stock-buy','out',500000,'证券投资'),('stock-sale','in',700000,'证券卖出'),('fund-buy','out',30000,'理财'),('fund-sale','in',32000,'理财'),('own-out','out',100000,'内部转账'),('own-in','in',100000,'内部转账')]
    with database.transaction() as db:
        for identifier, direction, amount, category in values:
            bank._ledger(db,identifier,'daily',category,direction,amount,category,'隔离测试')
            db.execute("UPDATE transactions SET occurred_at='2026-03-12T12:00:00+08:00' WHERE action_id=?",(identifier,))
    value = bank.query('analyze_bills',{'month':'2026-03'})
    assert value['income_minor'] == 2000 and value['expense_minor'] == 1000
    assert value['categories'] == {'餐饮':1000}
    assert value['investment_flows']['out_minor'] == 530000
    assert value['investment_flows']['in_minor'] == 732000
    assert len(value['investment_flows']['references']) == 4
    assert value['anomalies'] == []
    with database.connection() as db:
        assert db.execute("SELECT count(*) FROM transactions WHERE occurred_at LIKE '2026-03%'").fetchone()[0] == 8


async def test_slow_market_request_keeps_filling_cache():
    import asyncio
    from types import SimpleNamespace

    import pytest
    from banking.stock_valuation import cached_read
    finished = asyncio.Event()
    async def get(operation):
        await asyncio.sleep(.03)
        finished.set()
        return {'status':'ok'}
    service = SimpleNamespace(get=get)
    with pytest.raises(TimeoutError):
        await cached_read(service,'quotes',timeout=.001)
    await asyncio.wait_for(finished.wait(),1)
    await asyncio.sleep(0)
    assert not service.valuation_fetches
