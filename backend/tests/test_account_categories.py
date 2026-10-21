import httpx
from banking.main import create_app
from conftest import ScriptedModel
from test_security import register


async def test_category_lifecycle_preserves_money_and_reservations(settings):
    app = create_app(settings, ScriptedModel())
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as c:
        assert (await c.post('/api/account-categories', json={'name': '旅行'})).status_code == 401
        boot = (await c.get('/api/bootstrap')).json()
        original = (await c.get('/api/bank')).json()
        assert (await c.post('/api/account-categories', json={'name': '  '})).status_code == 422
        result = await c.post('/api/account-categories', json={'name': '  旅行基金  '})
        assert result.status_code == 200
        account = result.json()['id']
        assert result.json()['name'] == '旅行基金'
        assert result.json()['balance_minor'] == 0
        assert (await c.post('/api/account-categories', json={'name': '旅行基金'})).status_code == 400
        assert (await c.patch(f'/api/account-categories/{account}', json={'name': '假期'})).status_code == 200
        assert (await c.patch(f'/api/account-categories/{account}', json={'name': '日常账户'})).status_code == 400
        allocation = await c.post(f'/api/account-categories/{account}/allocation', json={'conversation_id': boot['conversation_id'], 'amount_yuan': '100.00', 'direction': 'in'})
        assert allocation.status_code == 200
        # Creating an approval cannot change balances.
        assert (await c.get('/api/bank')).json()['total_assets_minor'] == original['total_assets_minor']
        approval_id = allocation.json()['data']['approval_id']
        executed = await c.post(f'/api/approvals/{approval_id}/decision', json={'decision': 'confirm', 'conversation_id': boot['conversation_id'], 'account_id': 'daily'})
        assert executed.status_code == 200 and executed.json()['status'] == 'executed'
        with app.state.database.transaction() as db:
            assert db.execute("SELECT balance_minor FROM accounts WHERE id=?", (account,)).fetchone()[0] == 10000
            db.execute("INSERT INTO commitments VALUES ('reserve','plan',?,?,'reserve','假期预算',5000,5000,NULL,'active','now','now')", (boot['conversation_id'], account))
        assert (await c.request('DELETE', '/api/account-categories/daily', json={'confirmation': 'DELETE'})).status_code == 400
        assert (await c.request('DELETE', f'/api/account-categories/{account}', json={'confirmation': 'NO'})).status_code == 400
        removed = await c.request('DELETE', f'/api/account-categories/{account}', json={'confirmation': 'DELETE'})
        assert removed.status_code == 200 and removed.json()['returned_minor'] == 10000
        assert (await c.request('DELETE', f'/api/account-categories/{account}', json={'confirmation': 'DELETE'})).status_code == 400
        after = (await c.get('/api/bank')).json()
        assert after['total_assets_minor'] == original['total_assets_minor']
        assert not any(a['id'] == account for a in after['accounts'])
        assert next(a for a in after['accounts'] if a['id'] == 'daily')['balance_minor'] == next(a for a in original['accounts'] if a['id'] == 'daily')['balance_minor']
        with app.state.database.connection() as db:
            assert db.execute("SELECT account_id FROM commitments WHERE id='reserve'").fetchone()[0] == 'daily'
            assert db.execute("SELECT COUNT(*) FROM transactions WHERE description='删除分类账户，余额归回日常账户'").fetchone()[0] == 2
        assert (await c.post(f'/api/account-categories/{account}/allocation', json={'conversation_id': boot['conversation_id'], 'amount_yuan': '1', 'direction': 'in'})).status_code == 400


async def test_removed_salary_endpoints_cannot_create_money(settings):
    app = create_app(settings, ScriptedModel())
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as c:
        await c.get('/api/bootstrap')
        before = (await c.get('/api/bank')).json()['total_assets_minor']
        r = await c.post('/api/sandbox/events', json={'kind': 'salary', 'amount_minor': 1800000, 'account_id': 'daily', 'source_key': 'salary', 'period': '2026-10'})
        assert r.status_code == 410
        assert (await c.post('/api/sandbox/scenario', json={'confirmation': 'LOAD_DEMO'})).status_code == 410
        assert (await c.get('/api/bank')).json()['total_assets_minor'] == before


async def test_deleting_category_requires_correct_payment_pin(settings):
    settings.app_auth_enabled = True
    app = create_app(settings, ScriptedModel())
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as c:
        await register(c)
        await c.get('/api/bootstrap')
        account = (await c.post('/api/account-categories', json={'name': '预算'})).json()['id']
        path = f'/api/account-categories/{account}'
        assert (await c.request('DELETE', path, json={'confirmation': 'DELETE'})).status_code == 428
        assert (await c.request('DELETE', path, json={'confirmation': 'DELETE', 'pin': '0000'})).status_code == 400
        assert any(a['id'] == account for a in (await c.get('/api/bank')).json()['accounts'])
        assert (await c.request('DELETE', path, json={'confirmation': 'DELETE', 'pin': '2468'})).status_code == 200
