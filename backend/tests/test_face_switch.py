import httpx
from banking.config import Settings
from banking.main import create_app
from banking.face import configure,enabled,encrypt
from test_security import register
from test_red_security import create_payment


def test_default_enabled_and_env_override(monkeypatch):
    monkeypatch.delenv('FACE_ENABLED',raising=False)
    assert Settings(_env_file=None).face_enabled
    monkeypatch.setenv('FACE_ENABLED','false')
    assert not Settings(_env_file=None).face_enabled


async def test_disabled_yellow_pin_red_pin_sms_and_no_face_bypass(settings):
    settings.app_auth_enabled=True;settings.face_enabled=False
    app=create_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client)
        state=(await client.get('/api/face/status')).json()
        assert not state['enabled'] and not state['ready']
        assert (await client.post('/api/face/challenge',json={'purpose':'enroll'})).status_code==403
        assert (await client.post('/api/face/sms',json={'purpose':'enroll','pin':'2468'})).status_code==403
        conversation=(await client.get('/api/bootstrap')).json()['conversation_id']
        for amount in ['100','1001']:
            approval=await create_payment(app,conversation,amount)
            context=(await client.get(f'/api/approvals/{approval}/payment?conversation_id={conversation}')).json()
            assert not context['face_required'] and not context['face_enabled']
            body={'decision':'confirm','conversation_id':conversation,'account_id':'daily','pin':'2468'}
            if amount=='1001':
                assert (await client.post(f'/api/approvals/{approval}/decision',json=body)).status_code==400
                sms=(await client.post(f'/api/approvals/{approval}/sms',json={'conversation_id':conversation,'account_id':'daily'})).json()['demo_code'];body['sms_code']=sms
            assert (await client.post(f'/api/approvals/{approval}/decision',json={**body,'pin':None,'face_token':'forged'})).status_code==400
            response=await client.post(f'/api/approvals/{approval}/decision',json=body)
            assert response.json()['status']=='executed',response.text
        change={'pin':'1234','repeat_pin':'1234','current_pin':'2468'}
        sms=(await client.post('/api/auth/pin/sms',json=change)).json()['demo_code']
        assert (await client.post('/api/auth/pin',json={**change,'sms_code':sms})).status_code==200


def test_switch_revokes_pending_credentials_but_preserves_template(database):
    template=encrypt(database,[1.]+[0.]*127)
    with database.transaction() as db:
        db.execute('UPDATE face_identity SET template=? WHERE id=1',(template,))
        old=db.execute('SELECT version FROM payment_policy').fetchone()[0]
        db.execute("INSERT INTO face_grants VALUES('hash','session','payment','binding',0,9999999999,0)")
    configure(database,False)
    with database.connection() as db:
        assert not enabled(db)
        assert db.execute('SELECT COUNT(*) FROM face_grants').fetchone()[0]==0
        assert db.execute('SELECT template FROM face_identity').fetchone()[0]==template
        assert db.execute('SELECT version FROM payment_policy').fetchone()[0]==old+1
    configure(database,True)
    with database.connection() as db:assert enabled(db)
