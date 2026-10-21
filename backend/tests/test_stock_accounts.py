import sqlite3

import httpx
from banking.securities_common import secret
from banking.stock_site import create_stock_app


async def stock_login(client, phone):
    sms = await client.post('/api/securities/auth/sms', json={'phone': phone})
    assert sms.status_code == 200, sms.text
    code = sms.json()['demo_code']
    result = await client.post('/api/securities/auth/login', json={'phone': phone, 'code': code})
    assert result.status_code == 200, result.text
    return code


async def test_stock_users_watchlist_and_one_to_one_links(tmp_path):
    key = tmp_path / 'key'
    app = create_stock_app(tmp_path/'cache.sqlite3', broker_path=tmp_path/'users.sqlite3', key_path=key)
    headers = {'x-securities-key': secret(key)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8001') as alice, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8001') as bob:
        assert (await alice.put('/api/securities/watchlist', json={'symbols':['000001']})).status_code == 401
        assert (await alice.post('/api/securities/connect',json={'symbols':[], 'code':'12345678'})).status_code == 401
        code = await stock_login(alice, '11111111111')
        assert (await alice.post('/api/securities/auth/login',json={'phone':'11111111111','code':code})).status_code == 400
        assert (await alice.get('/api/securities/session')).json()['watchlist'] == []
        await alice.put('/api/securities/watchlist',json={'symbols':['000001']})
        await stock_login(bob, '13800138000')
        assert (await bob.get('/api/securities/session')).json()['watchlist'] == []
        await bob.put('/api/securities/watchlist',json={'symbols':['600036']})
        alice_id = (await alice.get('/api/securities/session')).json()['user_id']
        assert (await bob.put('/api/securities/watchlist',json={'symbols':[]},headers={'x-stock-account':alice_id})).status_code == 409
        pair = (await alice.post('/internal/securities/pairing-code',headers=headers,json={'owner':'bank-alice','label':'Alice银行'})).json()['code']
        assert (await alice.post('/api/securities/connect',json={'symbols':[],'code':pair})).status_code == 200
        pair = (await bob.post('/internal/securities/pairing-code',headers=headers,json={'owner':'bank-alice','label':'Alice银行'})).json()['code']
        assert (await bob.post('/api/securities/connect',json={'symbols':[],'code':pair})).status_code == 409
        pair = (await alice.post('/internal/securities/pairing-code',headers=headers,json={'owner':'bank-bob','label':'Bob银行'})).json()['code']
        assert (await alice.post('/api/securities/connect',json={'symbols':[],'code':pair})).status_code == 409
        assert (await bob.post('/api/securities/connect',json={'symbols':[],'code':pair})).status_code == 200
        assert [r['symbol'] for r in (await alice.get('/internal/securities/bank-alice/watchlist',headers=headers)).json()['watchlist']] == ['000001']
        assert [r['symbol'] for r in (await bob.get('/internal/securities/bank-bob/watchlist',headers=headers)).json()['watchlist']] == ['600036']
        token = alice.cookies.get('securities_auth')
        await alice.post('/api/securities/auth/logout',json={})
        assert not (await alice.get('/api/securities/session')).json()['authenticated']
        alice.cookies.set('securities_auth',token)
        assert (await alice.get('/api/securities/portfolio')).status_code == 401
        assert (await bob.get('/api/securities/session')).json()['connected']


async def test_sms_browser_binding_failure_limit_and_expiry(tmp_path):
    path = tmp_path/'users.sqlite3'
    app = create_stock_app(tmp_path/'cache.sqlite3',broker_path=path,key_path=tmp_path/'key')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8001') as client, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8001') as other:
        sms = (await client.post('/api/securities/auth/sms',json={'phone':'11111111111'})).json()
        body = {'phone':'11111111111','code':sms['demo_code']}
        assert (await other.post('/api/securities/auth/login',json=body)).status_code == 400
        assert (await client.post('/api/securities/auth/sms',json={'phone':'11111111111'})).status_code == 429
        wrong = '000000' if sms['demo_code'] != '000000' else '111111'
        for _ in range(5):
            assert (await client.post('/api/securities/auth/login',json={**body,'code':wrong})).status_code == 400
        assert (await client.post('/api/securities/auth/login',json=body)).status_code == 400

        with sqlite3.connect(path) as db:
            db.execute('UPDATE stock_sms SET attempts=0,expires=0')
        assert (await client.post('/api/securities/auth/login',json=body)).status_code == 400


async def test_multiuser_portfolios_follow_binding_without_bank_cookie(settings, tmp_path):
    from banking.db import Database
    from banking.main import create_app
    from banking.security import encode_pin
    from test_accounts import sms_login

    settings.app_auth_enabled = True
    settings.multi_user_accounts = True
    settings.securities_bridge_key_path = tmp_path/'key'
    legacy = Database(settings.database_path)
    legacy.initialize()
    with legacy.transaction() as db:
        db.execute("INSERT INTO app_identity(id,username,phone,pin_hash,contacts_done) VALUES (1,'演示文档','11111111111',?,1)", (encode_pin('0000'),))
    app = create_app(settings)
    stocks = create_stock_app(tmp_path/'cache.sqlite3',broker_path=tmp_path/'stock.sqlite3',key_path=settings.securities_bridge_key_path)
    stocks.state.bank_transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as bank_a, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as bank_b:
        await sms_login(bank_a,'11111111111')
        await sms_login(bank_b,'13800138000','新用户')
        await bank_b.post('/api/auth/contacts-done',json={})
        await bank_b.post('/api/auth/pin',json={'pin':'2468','repeat_pin':'2468'})
        directory = app.state.directory
        with directory.connection() as db:
            users = [dict(r) for r in db.execute('SELECT * FROM users ORDER BY phone')]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stocks),base_url='http://127.0.0.1:8001') as stock_a, httpx.AsyncClient(transport=httpx.ASGITransport(app=stocks),base_url='http://127.0.0.1:8001') as stock_b:
            for bank, market, phone, symbol in [(bank_a,stock_a,'11111111111','000001'),(bank_b,stock_b,'13800138000','600036')]:
                user = next(u for u in users if u['phone']==phone)
                tenant = await directory.application(user)
                tenant.state.agent.securities.transport = httpx.ASGITransport(app=stocks)
                with tenant.state.database.transaction() as db:
                    db.execute('INSERT INTO securities_positions VALUES (?,?,100,100000)',(symbol,'隔离测试持仓'))
                code = (await bank.post('/api/securities/pairing-code',json={})).json()['code']
                await stock_login(market,phone)
                assert (await market.post('/api/securities/connect',json={'symbols':[],'code':code})).status_code == 200
            await bank_a.post('/api/auth/logout',json={})
            a = (await stock_a.get('/api/securities/portfolio')).json()
            b = (await stock_b.get('/api/securities/portfolio')).json()
            assert a['positions'][0]['symbol']=='000001'
            assert b['positions'][0]['symbol']=='600036'
            assert (await bank_a.post('/internal/securities/portfolio',json={'owner':'unknown'})).status_code==403
