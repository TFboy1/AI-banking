import httpx
from banking.main import create_app
from card_support import add_card_for_test
from test_security import register


async def test_card_funding_payment_delete_and_rebind(settings):
    settings.app_auth_enabled = True
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as c:
        card = {'bank_name': '测试银行', 'card_number': '6222000012345678'}
        assert (await c.post('/api/cards', json=card)).status_code == 401
        await register(c)
        boot = (await c.get('/api/bootstrap')).json()
        initial = boot['bank']['total_assets_minor']
        assert (await c.post('/api/cards', json={**card, 'balance_minor': 9999999})).status_code == 422
        assert (await c.post('/api/cards', json={**card, 'card_number': '123'})).status_code == 422
        added = await add_card_for_test(c,card)
        assert added['balance_minor'] == 500000
        account, card_id = added['account_id'], added['id']
        assert (await c.post('/api/cards', json=card)).status_code == 400
        snapshot = (await c.get('/api/bank')).json()
        assert snapshot['total_assets_minor'] == initial + 500000
        assert snapshot['account_balance_minor'] == sum(a['balance_minor'] for a in snapshot['accounts'])
        assert snapshot['account_balance_minor'] == snapshot['category_balance_minor'] + snapshot['card_balance_minor']
        assert snapshot['total_assets_minor'] == snapshot['account_balance_minor'] + snapshot['investment_assets_minor']
        assert next(v for v in snapshot['cards'] if v['id'] == card_id)['balance_minor'] == 500000
        with app.state.database.transaction() as db:
            msg = app.state.agent.create_direct_approval(db, boot['conversation_id'], 'transfer', {'recipient': 'zhang_san', 'amount_yuan': '1200'})
        approval = msg['data']['approval_id']
        path = f'/api/approvals/{approval}/decision'
        context = (await c.get(f'/api/approvals/{approval}/payment?conversation_id={boot["conversation_id"]}')).json()
        option = next(v for v in context['accounts'] if v['id'] == account)
        assert option['eligible'] and '5678' in option['name']
        args = {'decision': 'confirm', 'conversation_id': boot['conversation_id'], 'account_id': account}
        assert (await c.post(path, json=args)).status_code == 428
        assert (await c.post(path, json={**args, 'pin': '2468'})).status_code == 400
        sms = (await c.post(f'/api/approvals/{approval}/sms', json={'conversation_id': boot['conversation_id'], 'account_id': account})).json()
        assert (await c.post(path, json={**args, 'pin': '2468', 'sms_code': sms['demo_code']})).json()['status'] == 'executed'
        snapshot = (await c.get('/api/bank')).json()
        assert next(v for v in snapshot['cards'] if v['id'] == card_id)['balance_minor'] == 380000
        assert snapshot['total_assets_minor'] == initial + 380000
        # A pending debit cannot keep using a card after it is removed.
        with app.state.database.transaction() as db:
            msg = app.state.agent.create_direct_approval(db, boot['conversation_id'], 'transfer', {'recipient': 'zhang_san', 'amount_yuan': '10', 'account_id': account})
        second = msg['data']['approval_id']
        assert (await c.post(f'/api/approvals/{second}/sms',json={'conversation_id':boot['conversation_id'],'account_id':account})).status_code==400
        second_sms=None  # Single-transaction yellow payments do not need SMS.
        assert (await c.request('DELETE', f'/api/cards/{card_id}', json={'confirmation': 'DELETE'})).status_code == 428
        assert (await c.request('DELETE', f'/api/cards/{card_id}', json={'confirmation': 'DELETE', 'pin': '0000'})).status_code == 400
        deleted = await c.request('DELETE', f'/api/cards/{card_id}', json={'confirmation': 'DELETE', 'pin': '2468'})
        assert deleted.json()['returned_minor'] == 0
        assert (await c.request('DELETE', f'/api/cards/{card_id}', json={'confirmation': 'DELETE', 'pin': '2468'})).status_code == 400
        after = (await c.get('/api/bank')).json()
        assert after['total_assets_minor'] == snapshot['total_assets_minor'] - 380000
        assert next(v for v in after['accounts'] if v['id']=='daily')['balance_minor'] == next(v for v in snapshot['accounts'] if v['id']=='daily')['balance_minor']
        assert not any(v['id'] == account for v in after['accounts'])
        assert (await c.post(f'/api/approvals/{second}/decision', json={'decision': 'confirm', 'conversation_id': boot['conversation_id'], 'account_id': account, 'pin': '2468','sms_code':second_sms})).json()['status'] == 'failed'
        rebound = await add_card_for_test(c,card)
        assert rebound['account_id'] == account and rebound['balance_minor'] == 380000
        assert (await c.get('/api/bank')).json()['total_assets_minor'] == snapshot['total_assets_minor']


async def test_frozen_card_rebind_and_category_route_protection(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as c:
        await c.get('/api/bootstrap')
        body = {'bank_name': '另一银行', 'card_number': '6222000099995678'}
        card = await add_card_for_test(c,body)
        with app.state.database.transaction() as db:
            db.execute("UPDATE cards SET status='frozen',daily_limit_minor=10000 WHERE id=?", (card['id'],))
        assert (await c.request('DELETE', f'/api/account-categories/{card["account_id"]}', json={'confirmation':'DELETE'})).status_code == 400
        assert (await c.patch(f'/api/account-categories/{card["account_id"]}', json={'name':'绕过卡片管理'})).status_code == 400
        assert (await c.request('DELETE', f'/api/cards/{card["id"]}', json={'confirmation':'DELETE'})).status_code == 200
        rebound = await add_card_for_test(c,body)
        snapshot = (await c.get('/api/bank')).json()
        item = next(v for v in snapshot['cards'] if v['id'] == rebound['id'])
        assert item['status'] == 'frozen' and item['daily_limit_minor'] == 10000


async def test_first_card_for_empty_user_and_restart(settings):
    settings.seed_demo_data = False
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as c:
        await c.get('/api/bootstrap')
        body = {'bank_name': '新用户银行', 'card_number': '6222000011117777'}
        card = await add_card_for_test(c,body)
        app.state.database.initialize()
        snapshot = (await c.get('/api/bank')).json()
        assert snapshot['total_assets_minor'] == 500000
        assert next(v for v in snapshot['accounts'] if v['id'] == 'daily')['balance_minor'] == 0
        assert (await c.request('DELETE', f'/api/cards/{card["id"]}', json={'confirmation':'DELETE'})).status_code == 200
        app.state.database.initialize()
        rebound = await add_card_for_test(c,body)
        assert rebound['balance_minor'] == 500000
        assert (await c.get('/api/bank')).json()['total_assets_minor'] == 500000


async def test_account_totals_deduplicate_shared_card_and_keep_unlinked_balance(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        await c.get('/api/bootstrap')
        with app.state.database.transaction() as db:
            db.execute("UPDATE accounts SET balance_minor=100000 WHERE id='daily'")
            db.execute("UPDATE accounts SET balance_minor=200000 WHERE id='savings'")
        body={'bank_name':'余额核对银行','card_number':'6222000000008899'}
        card=await add_card_for_test(c,body)
        before=(await c.get('/api/bank')).json()
        assert before['account_balance_minor']==800000
        assert before['category_balance_minor']==300000 and before['card_balance_minor']==500000
        with app.state.database.connection() as db:
            count=db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
        await c.request('DELETE',f"/api/cards/{card['id']}",json={'confirmation':'DELETE'})
        after=(await c.get('/api/bank')).json()
        assert after['account_balance_minor']==300000
        assert next(a['balance_minor'] for a in after['accounts'] if a['id']=='daily')==100000
        with app.state.database.connection() as db:
            assert db.execute('SELECT balance_minor FROM accounts WHERE id=?',(card['account_id'],)).fetchone()[0]==500000
            assert db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]==count
        rebound=await add_card_for_test(c,body)
        assert rebound['balance_minor']==500000
        assert (await c.get('/api/bank')).json()['account_balance_minor']==800000
