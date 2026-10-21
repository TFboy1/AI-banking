from face_test_support import bind_test_face, grant
import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest

from banking.db import TZ, Database, dumps, message_dict, queue_outbox
from banking.feishu import card_for_message
from banking.feishu_payment import payment_link, public_origin
from banking.main import create_app
from banking.security import encode_pin
from test_accounts import sms_login
from test_red_security import create_payment
from test_security import register


def enable(settings):
    settings.feishu_enabled = True
    settings.feishu_app_id = 'fake'
    settings.feishu_app_secret = 'fake'
    settings.feishu_bridge_token = 'testing-only-secret-' * 3


def issue(app, approval):
    with app.state.database.transaction() as db:
        db.execute("INSERT OR IGNORE INTO feishu_binding VALUES(1,'tenant','owner','chat')")
        db.execute("UPDATE tasks SET channel='feishu' WHERE id=(SELECT task_id FROM approvals WHERE id=?)", (approval,))
        row = db.execute('SELECT m.* FROM messages m JOIN approvals a ON a.message_id=m.id WHERE a.id=?', (approval,)).fetchone()
        queue_outbox(db, message_dict(row))
        return payment_link(db, approval, app.state.agent.settings).split('#')[1]


def headers(token):
    return {'X-Feishu-Payment': token}


async def test_page_yellow_payment_auth_pin_idempotency_and_card_update(settings):
    settings.app_auth_enabled = True
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
        await register(client)
        enable(settings)  # Never start a real Feishu sender in tests.
        conversation = (await client.get('/api/bootstrap')).json()['conversation_id']
        approval = await create_payment(app, conversation, '100')
        token = issue(app, approval)
        assert '13800123456' not in token
        path = '/api/feishu-pay/'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as anonymous:
            assert (await anonymous.get(path+'context', headers=headers(token))).status_code == 401
        context = (await client.get(path+'context', headers=headers(token))).json()
        assert context['amount_minor'] == 10000 and not context['sms_required'] and context['account_id'] == ''
        assert context['schedule_allowed'] is False
        before = (await client.get('/api/bank')).json()
        body = {'decision':'confirm','account_id':'daily','pin':'0000'}
        assert (await client.post(path+'decision', headers=headers(token), json=body)).status_code == 400
        assert (await client.get('/api/bank')).json() == before
        assert (await client.post(path+'decision', headers=headers(token), json={'decision':'confirm'})).status_code == 428
        body['pin'] = '2468'
        response = await client.post(path+'decision', headers=headers(token), json=body)
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'executed'
        after = (await client.get('/api/bank')).json()
        assert next(a for a in after['accounts'] if a['id']=='daily')['balance_minor'] == next(a for a in before['accounts'] if a['id']=='daily')['balance_minor'] - 10000
        assert (await client.post(path+'decision', headers=headers(token), json=body)).json()['status'] == 'executed'
        assert (await client.get('/api/bank')).json() == after
        assert (await client.get(path+'context', headers=headers(token))).json()['receipt']['action_id'] == approval
        with app.state.database.connection() as db:
            outbox=json.loads(db.execute('SELECT payload_json FROM feishu_outbox WHERE message_id=(SELECT message_id FROM approvals WHERE id=?)',(approval,)).fetchone()[0])
        assert outbox['data']['status'] == 'executed'
        assert outbox['data']['receipt']['action_id'] == approval


async def test_red_sms_account_binding_cancel_and_malformed_requests(settings):
    settings.app_auth_enabled = True
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
        await register(client); bind_test_face(app); enable(settings)
        conversation = (await client.get('/api/bootstrap')).json()['conversation_id']
        approval = await create_payment(app, conversation, '1001')
        token = issue(app, approval); h=headers(token); path='/api/feishu-pay/'
        assert (await client.get(path+'context',headers=h)).json()['sms_required']
        body={'decision':'confirm','pin':'2468','account_id':'daily'}
        assert (await client.post(path+'decision',headers=h,json=body)).status_code == 400
        sms=(await client.post(path+'sms',headers=h,json={'account_id':'daily'})).json()
        assert len(sms['demo_code']) == 6
        assert (await client.post(path+'decision',headers=h,json={**body,'account_id':'savings','sms_code':sms['demo_code']})).status_code == 400
        assert (await client.post(path+'decision',headers=h,json={**body,'sms_code':sms['demo_code'],'execute_at':'2026-12-01T10:00:00+08:00'})).status_code == 422
        assert (await client.post(path+'decision',headers=h,json={**body,'sms_code':sms['demo_code'],'face_token':grant(app,client,approval,'daily')})).json()['status']=='executed'
        other = await create_payment(app,conversation,'1')
        other_token=issue(app,other)
        assert (await client.post(path+'decision',headers=headers(other_token),json={'decision':'cancel'})).json()['status']=='cancelled'
        assert (await client.get(path+'context',headers=headers(other_token))).json()['status']=='cancelled'
        for bad in ['',token[:-1]+'x','a.b.c','%%%.'+'0'*64]:
            assert (await client.get(path+'context',headers=headers(bad))).status_code in {403,404}


@pytest.mark.parametrize('change', ['args','summary','binding','rebind','identity','expired','web','deleted','locked'])
async def test_link_revocation_and_safety(settings,change):
    settings.app_auth_enabled=True
    app=create_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client);enable(settings)
        conversation=(await client.get('/api/bootstrap')).json()['conversation_id']
        approval=await create_payment(app,conversation,'10');token=issue(app,approval)
        with app.state.database.transaction() as db:
            if change=='args':db.execute("UPDATE approvals SET args_json=json_set(args_json,'$.amount_minor',99999) WHERE id=?",(approval,))
            if change=='summary':db.execute("UPDATE approvals SET summary_json=json_set(summary_json,'$.amount_minor',99999) WHERE id=?",(approval,))
            if change=='binding':db.execute("UPDATE feishu_binding SET open_id='different'")
            if change=='rebind':db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('feishu_binding_nonce','new-binding')")
            if change=='identity':db.execute("UPDATE app_identity SET pin_hash=?",(encode_pin('9876'),))
            if change=='expired':
                db.execute('UPDATE approvals SET expires_at=? WHERE id=?',((datetime.now(TZ)-timedelta(seconds=1)).isoformat(),approval))
                token=payment_link(db,approval,settings).split('#')[1]
            if change=='web':db.execute("UPDATE tasks SET channel='web'")
            if change=='deleted':db.execute('UPDATE conversations SET deleted_at=? WHERE id=?',(datetime.now(TZ).isoformat(),conversation))
            if change=='locked':db.execute("UPDATE app_identity SET failures=5,blocked_until=9999999999")
        if change=='locked':
            response=await client.post('/api/feishu-pay/decision',headers=headers(token),json={'decision':'confirm','account_id':'daily','pin':'2468'})
            assert response.status_code==400 and '锁定' in response.text
        else:
            response=await client.get('/api/feishu-pay/context',headers=headers(token))
            assert response.status_code in {403,404,409,410}, response.text
        with app.state.database.connection() as db:
            status=db.execute('SELECT status FROM approvals WHERE id=?',(approval,)).fetchone()[0]
            assert status in ({'pending','expired'} if change=='expired' else {'pending'})
            assert not db.execute('SELECT 1 FROM transactions WHERE action_id=?',(approval,)).fetchone()


async def test_multiuser_wrong_account_cannot_open_payment(settings):
    settings.app_auth_enabled=True;settings.multi_user_accounts=True
    database=Database(settings.database_path);database.initialize()
    with database.transaction() as db:
        db.execute("INSERT INTO app_identity(id,username,phone,pin_hash,contacts_done) VALUES(1,'演示','11111111111',?,1)",(encode_pin('0000'),))
    app=create_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as owner,httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as other:
        await sms_login(owner,'11111111111')
        user=app.state.directory.user_for(__import__('banking.security',fromlist=['token_hash']).token_hash(owner.cookies['bank_session']))
        tenant=await app.state.directory.application(user)
        enable(tenant.state.agent.settings)
        conversation=(await owner.get('/api/bootstrap')).json()['conversation_id']
        approval=await create_payment(tenant,conversation,'10');token=issue(tenant,approval)
        await sms_login(other,'13800123456','另一个用户')
        await other.post('/api/auth/contacts-done',json={})
        await other.post('/api/auth/pin',json={'pin':'2468','repeat_pin':'2468'})
        assert (await other.get('/api/feishu-pay/context',headers=headers(token))).status_code in {403,404}
        assert (await owner.get('/api/feishu-pay/context',headers=headers(token))).json()['status']=='pending'


def test_cards_links_and_public_origin(settings):
    message={'kind':'approval','data':{'title':'交易','status':'pending','fields':[],'approval_id':'op_test'}}
    card=card_for_message(message,'https://bank.example/feishu-pay.html#token',True)
    button=card['elements'][1]['actions'][0]
    assert button['url'].startswith('https://bank.example/') and 'value' not in button
    assert card['elements'][1]['actions'][1]['value']['decision']=='cancel'
    settings.feishu_public_url='https://bank.example'
    assert public_origin(settings)=='https://bank.example'
    for bad in ['http://bank.example','https://user:pass@bank.example','https://bank.example/path','javascript:alert(1)']:
        settings.feishu_public_url=bad
        with pytest.raises(ValueError):public_origin(settings)
    for name in ['partial_completion','daily_digest']:
        assert card_for_message({'kind':'data','text':'报告','data':{'result':{name:{}}}})['header']['title']['content']=='报告'
