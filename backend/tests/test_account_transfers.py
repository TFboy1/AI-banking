import httpx
from banking.main import create_app
from card_support import add_card_for_test
from conftest import ScriptedModel
from test_security import register


async def test_card_account_both_directions_boundary_and_idempotency(settings):
    settings.app_auth_enabled=True
    app=create_app(settings,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        await register(c)
        conv=(await c.get('/api/bootstrap')).json()['conversation_id']
        card=await add_card_for_test(c,{'bank_name':'划转测试银行','card_number':'6222000000015566'})
        bankcard=card['account_id']
        before=(await c.get('/api/bank')).json()
        for source,target,amount,red in [(bankcard,'savings','100',False),('savings',bankcard,'900',False),(bankcard,'daily','1000.01',True)]:
            prior=(await c.get('/api/bank')).json()
            response=await c.post('/api/account-transfers',json={'conversation_id':conv,'account_id':source,'target_account_id':target,'amount_yuan':amount})
            assert response.status_code==200,response.text
            approval=response.json()['data']['approval_id']
            assert (await c.get('/api/bank')).json()['account_balance_minor']==before['account_balance_minor']
            ctx=(await c.get(f'/api/approvals/{approval}/payment?conversation_id={conv}')).json()
            assert ctx['sms_required']==red
            body={'conversation_id':conv,'decision':'confirm','account_id':source}
            path=f'/api/approvals/{approval}/decision'
            assert (await c.post(path,json=body)).status_code==428
            body['pin']='2468'
            if red:
                assert (await c.post(path,json=body)).status_code==400
                sms=(await c.post(f'/api/approvals/{approval}/sms',json={'conversation_id':conv,'account_id':source})).json()
                body['sms_code']=sms['demo_code']
            result=await c.post(path,json=body)
            assert result.json()['status']=='executed',result.text
            after=(await c.get('/api/bank')).json()
            balance=lambda state,key:next(a['balance_minor'] for a in state['accounts'] if a['id']==key)
            moved=round(float(amount)*100)
            assert balance(prior,source)-balance(after,source)==moved
            assert balance(after,target)-balance(prior,target)==moved
            assert after['account_balance_minor']==before['account_balance_minor']
            assert after['total_assets_minor']==before['total_assets_minor']
            assert (await c.post(path,json=body)).json()['status']=='executed'
            assert (await c.get('/api/bank')).json()==after
        for source,target,amount in [(bankcard,bankcard,'1'),('daily','daily','1'),(bankcard,'investment_summary','1'),('foreign-card','daily','1'),(bankcard,'daily','999999')]:
            assert (await c.post('/api/account-transfers',json={'conversation_id':conv,'account_id':source,'target_account_id':target,'amount_yuan':amount})).status_code==400
        with app.state.database.transaction() as db:
            db.execute("UPDATE cards SET status='frozen' WHERE id=?",(card['id'],))
        for source,target in [(bankcard,'daily'),('daily',bankcard)]:
            assert (await c.post('/api/account-transfers',json={'conversation_id':conv,'account_id':source,'target_account_id':target,'amount_yuan':'1'})).status_code==400


async def test_unlinked_or_deleted_target_rechecked_before_transfer(settings):
    app=create_app(settings,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        conv=(await c.get('/api/bootstrap')).json()['conversation_id']
        card=await add_card_for_test(c,{'bank_name':'删除前校验','card_number':'6222000000093344'})
        message=(await c.post('/api/account-transfers',json={'conversation_id':conv,'account_id':card['account_id'],'target_account_id':'daily','amount_yuan':'10'})).json()
        await c.request('DELETE',f"/api/cards/{card['id']}",json={'confirmation':'DELETE'})
        before=(await c.get('/api/bank')).json()
        result=await c.post(f"/api/approvals/{message['data']['approval_id']}/decision",json={'conversation_id':conv,'decision':'confirm','account_id':card['account_id']})
        assert result.json()['status']=='failed'
        assert (await c.get('/api/bank')).json()['account_balance_minor']==before['account_balance_minor']
