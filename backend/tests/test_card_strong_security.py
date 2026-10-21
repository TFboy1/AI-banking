from face_test_support import bind_test_face, grant
import asyncio
import json

import httpx
import pytest
from banking.main import create_app
from conftest import ScriptedModel, tool
from test_security import register


@pytest.mark.parametrize('channel',['page','chat'])
async def test_add_card_always_requires_both_factors(settings,channel):
    settings.app_auth_enabled=True
    number='6222000012344455'
    app=create_app(settings,ScriptedModel([[tool('add_bank_card',bank_name='双验银行',card_number=number)],'银行卡已添加。']))
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        await register(c)
        bind_test_face(app)
        conv=(await c.get('/api/bootstrap')).json()['conversation_id']
        with app.state.database.transaction() as db:
            db.execute('UPDATE app_identity SET lock_enabled=0')
            db.execute('UPDATE payment_policy SET threshold_minor=99999999')
        before=(await c.get('/api/bank')).json()
        if channel=='page':
            message=(await c.post('/api/cards',json={'bank_name':'双验银行','card_number':number,'conversation_id':conv})).json()
        else:
            await c.post('/api/messages',json={'conversation_id':conv,'text':f'帮我添加双验银行银行卡，卡号{number}'})
            await asyncio.wait_for(app.state.agent.queue.join(),3)
            message=next(m for m in reversed((await c.get('/api/bootstrap')).json()['messages']) if m['kind']=='approval')
        approval=message['data']['approval_id']
        assert number not in json.dumps(message['data'])
        assert (await c.get('/api/bank')).json()==before
        ctx=(await c.get(f'/api/approvals/{approval}/payment?conversation_id={conv}')).json()
        assert ctx['level']=='red' and ctx['sms_required'] and not ctx['account_required']
        path=f'/api/approvals/{approval}/decision';body={'decision':'confirm','conversation_id':conv}
        assert (await c.post(path,json=body)).status_code==428
        assert (await c.post(path,json={**body,'pin':'2468'})).status_code==400
        sms=(await c.post(f'/api/approvals/{approval}/sms',json={'conversation_id':conv})).json()['demo_code']
        assert (await c.post(path,json={**body,'sms_code':sms})).status_code==428
        assert (await c.post(path,json={**body,'pin':'0000','sms_code':sms})).status_code==400
        wrong='000000' if sms!='000000' else '111111'
        assert (await c.post(path,json={**body,'pin':'2468','sms_code':wrong})).status_code==400
        assert (await c.get('/api/bank')).json()==before
        result=(await c.post(path,json={**body,'pin':'2468','sms_code':sms,'face_token':grant(app,c,approval)})).json()
        assert result['status']=='executed'
        after=(await c.get('/api/bank')).json()
        assert after['account_balance_minor']==before['account_balance_minor']+500000
        assert (await c.post(path,json={**body,'pin':'2468','sms_code':sms})).json()['status']=='executed'
        assert (await c.get('/api/bank')).json()==after


async def test_repeated_small_transfers_stay_yellow_under_single_threshold(settings):
    from test_red_security import create_payment
    settings.app_auth_enabled=True
    app=create_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        await register(c)
        bind_test_face(app)
        conv=(await c.get('/api/bootstrap')).json()['conversation_id']
        for amount,red in [('600',False),('600',False),('1000',False),('0.01',False)]:
            approval=await create_payment(app,conv,amount)
            context=(await c.get(f'/api/approvals/{approval}/payment?conversation_id={conv}')).json()
            assert context['sms_required']==red and context['level']==('red' if red else 'yellow')
            body={'conversation_id':conv,'decision':'confirm','account_id':'daily','pin':'2468'}
            if red:
                sms=(await c.post(f'/api/approvals/{approval}/sms',json={'conversation_id':conv,'account_id':'daily'})).json()
                body['sms_code']=sms['demo_code']
            assert (await c.post(f'/api/approvals/{approval}/decision',json=body)).json()['status']=='executed'
        approval=await create_payment(app,conv,'1000.01')
        assert (await c.get(f'/api/approvals/{approval}/payment?conversation_id={conv}')).json()['sms_required']
