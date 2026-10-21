import asyncio
import sqlite3

import httpx
from banking.main import create_app
from banking.securities_broker import SCHEMA
from banking.securities_common import secret, token_hash
from banking.stock_site import create_stock_app
from test_stock_accounts import stock_login


async def test_pairing_expiry_single_use_isolation_and_throttle(tmp_path):
    path = tmp_path / 'broker.sqlite3'
    key = tmp_path / 'key'
    app = create_stock_app(tmp_path / 'cache.sqlite3', broker_path=path, key_path=key)
    headers = {'x-securities-key': secret(key)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8001') as client:
        await stock_login(client, '11111111111')
        assert (await client.post('/internal/securities/pairing-code', json={'owner': 'alice', 'label': 'Alice'})).status_code == 403
        code = (await client.post('/internal/securities/pairing-code', headers=headers, json={'owner': 'alice', 'label': 'Alice'})).json()['code']
        with sqlite3.connect(path) as db:
            assert db.execute('SELECT code_hash FROM pairing_codes').fetchone()[0] != code
            db.execute('UPDATE pairing_codes SET expires=0')
        assert (await client.post('/api/securities/connect', json={'symbols': [], 'code': code})).status_code == 400
        code = (await client.post('/internal/securities/pairing-code', headers=headers, json={'owner': 'alice', 'label': 'Alice'})).json()['code']
        results = await asyncio.gather(*(client.post('/api/securities/connect', json={'symbols': ['000001'], 'code': code}) for _ in range(2)))
        assert sorted(r.status_code for r in results) == [200, 400]
        other = (await client.post('/internal/securities/pairing-code', headers=headers, json={'owner': 'bob', 'label': 'Bob'})).json()['code']
        assert (await client.post('/api/securities/connect', json={'symbols': ['600036'], 'code': other})).status_code == 409
        watch = (await client.get('/internal/securities/alice/watchlist', headers=headers)).json()
        assert [r['symbol'] for r in watch['watchlist']] == ['000001']
        with sqlite3.connect(path) as db:
            db.execute('UPDATE pairing_attempts SET attempts=10')
        assert (await client.post('/api/securities/connect', json={'symbols': [], 'code': other})).status_code == 429


async def test_legacy_connections_require_code_but_keep_watchlist(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    key = tmp_path / 'key'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO investors VALUES ('alice','Alice')")
        db.execute("INSERT INTO sessions VALUES (?,'alice',9999999999)", (token_hash('old-cookie'),))
        db.execute("INSERT INTO watchlist VALUES ('alice','000001',0)")
    app = create_stock_app(tmp_path / 'cache.sqlite3', broker_path=path, key_path=key)
    headers = {'x-securities-key': secret(key)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8001', cookies={'securities_session': 'old-cookie'}) as client:
        assert not (await client.get('/api/securities/session')).json()['connected']
        assert not (await client.get('/internal/securities/alice/watchlist', headers=headers)).json()['connected']
        await stock_login(client, '11111111111')
        code = (await client.post('/internal/securities/pairing-code', headers=headers, json={'owner': 'alice', 'label': 'Alice'})).json()['code']
        linked = (await client.post('/api/securities/connect', json={'symbols': [], 'code': code})).json()
        assert [r['symbol'] for r in linked['watchlist']] == ['000001']


async def test_repeated_risk_assessment_preserves_assets_and_requires_confirmation(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
        boot = (await client.get('/api/bootstrap')).json()
        conv = boot['conversation_id']
        previous = None
        for answers, expected, decision in [
            ({'horizon': 'long', 'loss_tolerance': 'high', 'experience': 'experienced'}, 4, 'confirm'),
            ({'horizon': 'short', 'loss_tolerance': 'none', 'experience': 'none'}, 1, 'cancel'),
            ({'horizon': 'short', 'loss_tolerance': 'none', 'experience': 'none'}, 1, 'confirm'),
        ]:
            message = (await client.post('/api/investments/approvals', json={'conversation_id': conv, 'tool': 'save_risk_profile', 'arguments': answers})).json()
            assert (await client.get('/api/investments')).json()['risk_level'] == previous
            await client.post(f"/api/approvals/{message['data']['approval_id']}/decision", json={'conversation_id': conv, 'decision': decision})
            if decision == 'confirm':
                previous = expected
            assert (await client.get('/api/investments')).json()['risk_level'] == previous
        after = (await client.get('/api/bank')).json()
        assert after['total_assets_minor'] == boot['bank']['total_assets_minor']
        assert after['holdings'] == boot['bank']['holdings']
