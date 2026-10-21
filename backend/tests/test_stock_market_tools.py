from datetime import datetime

import httpx
from banking.db import TZ
from banking.main import create_app
from banking.securities_common import owner, secret
from banking.stock_site import create_stock_app
from conftest import ScriptedModel, ask, tool
from test_security import register
from test_stock_accounts import stock_login


async def test_market_search_price_nonfavorite_buy_and_add(settings, tmp_path):
    settings.app_auth_enabled = True
    settings.securities_bridge_key_path = tmp_path / 'key'
    app = create_app(settings, ScriptedModel())
    stocks = create_stock_app(tmp_path / 'market.sqlite3', broker_path=tmp_path / 'broker.sqlite3', key_path=settings.securities_bridge_key_path)
    stocks.state.bank_transport = httpx.ASGITransport(app=app)
    bridge = app.state.agent.securities
    bridge.transport = httpx.ASGITransport(app=stocks)
    snapshot_ok = True
    async def market_data(operation, symbol='', adjust=''):
        if operation == 'quotes':
            return {'status': 'ok' if snapshot_ok else 'unavailable', 'rows': [{'symbol': '000001', 'name': '平安银行', 'price': 12.34}, {'symbol': '600036', 'name': '招商银行', 'price': 30}, {'symbol': '601166', 'name': '兴业银行', 'price': 20}], 'source': 'securities-api-test-history', 'fetched_at': datetime.now(TZ).isoformat()}
        return {'status': 'ok', 'rows': [{'date': datetime.now(TZ).date().isoformat(), 'open': 12.34 if symbol=='000001' and snapshot_ok else 30, 'close': 12.34 if symbol=='000001' and snapshot_ok else 30}], 'source': 'securities-api-test-history'}
    stocks.state.stock_service.get = market_data
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as bank, httpx.AsyncClient(transport=httpx.ASGITransport(app=stocks), base_url='http://127.0.0.1:8001') as market:
        await register(bank)
        await stock_login(market, '11111111111')
        code = (await bank.post('/api/securities/pairing-code', json={})).json()['code']
        assert (await market.post('/api/securities/connect', json={'symbols': ['000001'], 'code': code})).status_code == 200
        conv = (await bank.get('/api/bootstrap')).json()['conversation_id']
        original = (await bank.get('/api/bank')).json()
        assert len((await market.get('/api/stocks/catalog')).json()['items']) == 20
        assert (await market.get('/api/stocks/601166/history')).status_code == 400
        assert (await market.get('/api/stocks/quotes')).json()['total'] == 20
        assert (await market.put('/api/securities/watchlist', json={'symbols':['601166']})).status_code == 422
        result = await bridge.query('search_securities_stocks', {'query': '招商银行'}, conv)
        assert result['items'] == [{'symbol': '600036', 'name': '招商银行'}]
        # Stocks outside the fixed test pool are not returned.
        other = await bridge.query('search_securities_stocks', {'query': '兴业银行'}, conv)
        assert other['items'] == []
        price = await bridge.query('get_stock_price', {'symbol': '000001'}, conv)
        assert price['price_minor'] == 1234 and price['price_kind'] in {'开盘价','收盘价'}
        assert price['source'] == 'securities-api-test-history'
        quote = await bridge.query('quote_stock_purchase', {'orders': [{'symbol': '600036', 'amount_yuan': '6000'}]}, conv)
        assert quote['items'][0]['name'] == '招商银行' and quote['items'][0]['shares'] == 200
        assert [i['symbol'] for i in (await market.get('/api/securities/session')).json()['watchlist']] == ['000001']
        with app.state.database.connection() as db:
            quotes = db.execute('SELECT count(*) FROM securities_quotes').fetchone()[0]
            investor = owner(db)
        await ask(app.state.agent, [[tool('get_stock_price', symbol='000001')], '平安银行的行情快照为每股12.34元。'], '平安银行目前单股价格多少')
        assert 'securities-api-test-history' in str(app.state.agent.model.requests[-1])
        # Even a mistaken trade-quote call for a pure price inquiry is read-only.
        await ask(app.state.agent, [[tool('quote_stock_purchase', orders=[{'symbol': '000001', 'shares': 100}])], '已查询价格。'], '平安银行单股价格多少')
        with app.state.database.connection() as db:
            assert db.execute('SELECT count(*) FROM securities_quotes').fetchone()[0] == quotes
        await ask(app.state.agent, [[tool('add_securities_watchlist', symbol='600036')]], '我想买6000元招商银行股票')
        assert len((await market.get('/api/securities/session')).json()['watchlist']) == 1
        await ask(app.state.agent, [[tool('search_securities_stocks', query='招商银行')], [tool('add_securities_watchlist', symbol='600036')], '招商银行已加入自选。'], '帮我把招商银行加入自选')
        assert [i['symbol'] for i in (await market.get('/api/securities/session')).json()['watchlist']] == ['000001', '600036']
        repeat = await bridge.query('add_securities_watchlist', {'symbol': '600036'}, conv)
        assert repeat['status'] == 'executed' and not repeat['added']
        favorites = (await market.get('/api/securities/session')).json()['watchlist']
        assert len(favorites) == 2
        assert (await bank.get('/api/bank')).json() == original
        headers = {'x-securities-key': secret(settings.securities_bridge_key_path)}
        assert (await market.get('/internal/securities/stocks/search?q=招商银行')).status_code == 403
        assert (await market.post('/internal/securities/not-linked/watchlist/add', json={'symbol': '600036'}, headers=headers)).status_code == 400
        assert (await market.post(f'/internal/securities/{investor}/quote', json={'orders': [{'symbol': '999999', 'shares': 100}]}, headers=headers)).status_code == 400
        snapshot_ok = False
        fallback = await bridge.query('get_stock_price', {'symbol': '000001'}, conv)
        assert fallback['price_minor'] == 3000 and fallback['price_kind'] in {'开盘价','收盘价'}
        assert '不代表实时价格' in fallback['note']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stocks), base_url='http://127.0.0.1:8001') as stranger:
            await stock_login(stranger, '12222222222')
            assert (await stranger.get('/api/securities/session')).json()['watchlist'] == []


async def test_watchlist_success_requires_receipt(agent):
    await ask(agent, ['招商银行已加入自选。'], '帮我把招商银行加入自选')
    with agent.db.connection() as db:
        text = db.execute("SELECT text FROM messages WHERE role='assistant' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        assert '尚未添加' in text
