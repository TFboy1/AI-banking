import time
from datetime import datetime

import httpx
import pytest
from banking.bank import BankError
from banking.db import TZ
from banking.main import create_app
from banking.securities_common import owner, quantity_authorized, secret
from banking.stock_site import create_stock_app
from conftest import ScriptedModel, ask, tool
from test_security import register
from test_stock_accounts import stock_login


async def test_connected_watchlist_red_payment_atomic_settlement(settings, tmp_path):
    settings.app_auth_enabled = True
    settings.securities_bridge_key_path = tmp_path / 'bridge-key'
    app = create_app(settings, ScriptedModel())
    stocks = create_stock_app(tmp_path / 'market.sqlite3', broker_path=tmp_path / 'broker.sqlite3', key_path=settings.securities_bridge_key_path)
    stocks.state.bank_transport = httpx.ASGITransport(app=app)
    app.state.agent.securities.transport = httpx.ASGITransport(app=stocks)

    async def history(*args):
        from banking.stock_trading import clock
        return {'status': 'ok', 'rows': [{'date': clock().date().isoformat(), 'open':10, 'close': 10,'close_confirmed':True}], 'source': 'isolated-test-fixture'}

    stocks.state.stock_service.get = history
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as bank, httpx.AsyncClient(transport=httpx.ASGITransport(app=stocks), base_url='http://127.0.0.1:8001') as market:
        assert (await market.get('/api/securities/session')).json()['connected'] is False
        assert (await market.post('/api/securities/connect', json={'symbols': ['000001']})).status_code == 422
        assert (await bank.post('/api/securities/pairing-code', json={})).status_code == 401
        await register(bank)
        await stock_login(market, "11111111111")
        old_code = (await bank.post('/api/securities/pairing-code', json={})).json()['code']
        code = (await bank.post('/api/securities/pairing-code', json={})).json()['code']
        assert (await market.post('/api/securities/connect', json={'symbols': ['000001'], 'code': old_code})).status_code == 400
        linked = await market.post('/api/securities/connect', json={'symbols': ['000001'], 'code': code})
        assert (await market.post('/api/securities/connect', json={'symbols': ['000001'], 'code': code})).status_code == 400
        assert linked.status_code == 200, linked.text
        assert linked.json()['watchlist'][0]['symbol'] == '000001'
        conv = (await bank.get('/api/bootstrap')).json()['conversation_id']
        bridge = app.state.agent.securities
        data = await bridge.query('get_securities_watchlist', {}, conv)
        assert data['connected'] and len(data['watchlist']) == 1
        quote = await bridge.query('quote_stock_purchase', {'orders': [{'symbol': '000001', 'shares': 100}]}, conv)
        assert not quote['eligible']
        with app.state.database.transaction() as db:
            with pytest.raises(BankError, match='R4'):
                app.state.agent.create_direct_approval(db, conv, 'purchase_stock_basket', {'quote_id': quote['quote_id']})
            db.execute("INSERT OR REPLACE INTO risk_profile VALUES (1, '{}', 4)")
            with pytest.raises(BankError, match='会话'):
                app.state.agent.create_direct_approval(db, 'wrong-conversation', 'purchase_stock_basket', {'quote_id': quote['quote_id']})
            message = app.state.agent.create_direct_approval(db, conv, 'purchase_stock_basket', {'quote_id': quote['quote_id']})
        approval = message['data']['approval_id']
        path = f'/api/approvals/{approval}'
        context = (await bank.get(path + f'/payment?conversation_id={conv}')).json()
        assert context['level'] == 'red' and context['sms_required']
        before = (await bank.get('/api/bank')).json()
        body = {'conversation_id': conv, 'decision': 'confirm', 'account_id': 'daily'}
        assert (await bank.post(path + '/decision', json=body)).status_code == 428
        body['pin'] = '2468'
        assert (await bank.post(path + '/decision', json=body)).status_code == 400
        sms = (await bank.post(path + '/sms', json={'conversation_id': conv, 'account_id': 'daily'})).json()
        body['sms_code'] = sms['demo_code']
        result = await bank.post(path + '/decision', json=body)
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'executed'
        after = (await bank.get('/api/bank')).json()
        assert after['total_assets_minor'] == before['total_assets_minor']
        assert after['securities_cost_minor'] == 100000
        balances = lambda data: next(a['balance_minor'] for a in data['accounts'] if a['id'] == 'daily')
        assert balances(before) - balances(after) == 100000
        assert (await bank.post(path + '/decision', json=body)).json()['status'] == 'executed'
        assert (await bank.get('/api/bank')).json() == after
        portfolio = (await market.get('/api/securities/portfolio')).json()
        assert portfolio['positions'][0]['shares'] == 100 and len(portfolio['orders']) == 1
        with app.state.database.transaction() as db:
            with pytest.raises(BankError, match='已经成交'):
                app.state.agent.create_direct_approval(db, conv, 'purchase_stock_basket', {'quote_id': quote['quote_id']})
            investor = owner(db)
        assert (await market.get(f'/internal/securities/{investor}/watchlist')).status_code == 403
        headers = {'x-securities-key': secret(settings.securities_bridge_key_path)}
        assert (await bank.post('/api/securities/portfolio', json={'owner': 'someone-else'}, headers=headers)).status_code == 403
        non_favorite = await market.post(f'/internal/securities/{investor}/quote', headers=headers, json={'orders': [{'symbol': '600036', 'shares': 100}]})
        assert non_favorite.status_code == 200
        assert non_favorite.json()['items'][0]['name'] == '招商银行'
        assert (await market.post(f'/internal/securities/{investor}/quote', headers=headers, json={'orders': [{'symbol': '000001', 'shares': 101}]})).status_code == 422
        async def unavailable(*args):
            return {'status': 'stale', 'rows': [{'date': '2020-01-01', 'close': 10}], 'source': 'isolated-test-fixture'}
        stocks.state.stock_service.get = unavailable
        # The existing platform safety layer may replace provider details with
        # its user-facing unavailable message. Both must leave funds unchanged.
        before_unavailable=(await bank.get('/api/bank')).json()['account_balance_minor']
        with pytest.raises(BankError, match='行情|证券平台'):
            await bridge.query('quote_stock_purchase', {'orders': [{'symbol': '000001', 'shares': 100}]}, conv)
        assert (await bank.get('/api/bank')).json()['account_balance_minor']==before_unavailable
        stocks.state.stock_service.get = history
        new_quote = await bridge.query('quote_stock_purchase', {'orders': [{'symbol': '000001', 'shares': 100}]}, conv)
        with app.state.database.transaction() as db:
            db.execute('UPDATE securities_quotes SET expires=? WHERE id=?', (time.time()-1, new_quote['quote_id']))
            with pytest.raises(BankError, match='5分钟'):
                app.state.agent.create_direct_approval(db, conv, 'purchase_stock_basket', {'quote_id': new_quote['quote_id']})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stocks), base_url='http://127.0.0.1:8001') as stranger:
            assert (await stranger.get('/api/securities/session')).json()['connected'] is False
            assert (await stranger.put('/api/securities/watchlist', json={'symbols': []})).status_code == 401
        await ask(app.state.agent, [[tool('list_products')], '请说明要买哪些自选以及数量。'], '帮我购买我在有数证券里面的自选')
        assert all('模拟产品与风险匹配' not in str(m) for m in app.state.agent.model.requests[-1])
        with app.state.database.connection() as db:
            quote_count = db.execute('SELECT count(*) FROM securities_quotes').fetchone()[0]
        await ask(app.state.agent, [[tool('quote_stock_purchase', orders=[{'symbol': '000001', 'shares': 100}])]], '买我的自选')
        with app.state.database.connection() as db:
            assert db.execute('SELECT count(*) FROM securities_quotes').fetchone()[0] == quote_count
            assert db.execute('SELECT count(*) FROM securities_orders').fetchone()[0] == 1


def test_explicit_share_quantity():
    args = {'orders': [{'symbol': '000001', 'shares': 100}]}
    assert not quantity_authorized('帮我购买自选', args)
    assert quantity_authorized('每只一手', args)
    assert quantity_authorized('平安银行100股', args)
    assert not quantity_authorized('平安银行200股', args)
