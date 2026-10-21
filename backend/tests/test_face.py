import json
from contextlib import asynccontextmanager

import httpx
import numpy as np
import pytest

from banking.main import create_app
from banking import face, face_engine
from test_security import register
from test_red_security import create_payment


@asynccontextmanager
async def environment(settings,monkeypatch):
    settings.app_auth_enabled=True
    app=create_app(settings)
    state={'pose':0.,'vector':[1.]+[0.]*127}
    monkeypatch.setattr(face_engine,'ready',lambda directory:True)
    monkeypatch.setattr(face_engine,'inspect',lambda data,directory:dict(state))
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client)
        yield app,client,state


async def finish_face(app,client,state,challenge):
    identifier=challenge['challenge_id']
    for index in range(2*challenge['steps']):
        with app.state.database.transaction() as db:
            row=db.execute('SELECT * FROM face_challenges WHERE id=?',(identifier,)).fetchone()
            pose=json.loads(row['sequence'])[row['step']]
            state['pose']={'center':0.,'left':.3,'right':-.3}[pose]
            db.execute('UPDATE face_challenges SET last_at=0 WHERE id=?',(identifier,))
        result=await client.post(f'/api/face/challenges/{identifier}/frame',content=b'\xff\xd8'+bytes([index])*150,headers={'Content-Type':'image/jpeg'})
        assert result.status_code==200,result.text
    return result.json()


async def enroll(app,client,state):
    sms=(await client.post('/api/face/sms',json={'purpose':'enroll','pin':'2468'})).json()
    result=await client.post('/api/face/challenge',json={'purpose':'enroll','pin':'2468','sms_code':sms['demo_code'],'consent':True})
    assert result.status_code==200,result.text
    assert result.json()['steps']==4
    with app.state.database.connection() as db:
        sequence=json.loads(db.execute('SELECT sequence FROM face_challenges WHERE id=?',(result.json()['challenge_id'],)).fetchone()[0])
        assert sequence[0]==sequence[-1]=='center' and set(sequence[1:3])=={'left','right'}
    return await finish_face(app,client,state,result.json())


async def payment_face(app,client,state,identifier,conversation,red=False):
    body={'purpose':'payment','approval_id':identifier,'conversation_id':conversation,'account_id':'daily'}
    sms=None
    if red:
        sms=(await client.post(f'/api/approvals/{identifier}/sms',json={'conversation_id':conversation,'account_id':'daily'})).json()['demo_code']
        body.update(pin='2468',sms_code=sms)
    result=await client.post('/api/face/challenge',json=body)
    assert result.status_code==200,result.text
    assert result.json()['steps']==1
    with app.state.database.connection() as db:
        assert json.loads(db.execute('SELECT sequence FROM face_challenges WHERE id=?',(result.json()['challenge_id'],)).fetchone()[0])==['center']
    proof=(await finish_face(app,client,state,result.json()))['face_token']
    return proof,sms


async def test_enrollment_encrypted_and_not_a_payment(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        with app.state.database.connection() as db:before=db.execute('SELECT SUM(balance_minor) FROM accounts').fetchone()[0]
        assert not (await client.get('/api/face/status')).json()['bound']
        assert (await enroll(app,client,state))['bound']
        assert (await client.get('/api/face/status')).json()['bound']
        with app.state.database.connection() as db:
            stored=db.execute('SELECT template FROM face_identity').fetchone()[0]
            assert '1.0' not in stored and json.dumps(state['vector']) not in stored
            assert db.execute('SELECT SUM(balance_minor) FROM accounts').fetchone()[0]==before
            assert db.execute('SELECT COUNT(*) FROM face_challenges WHERE vector IS NOT NULL').fetchone()[0]==0
        assert app.state.database.path.with_suffix('.face.key').exists()


async def test_yellow_face_or_password_red_requires_all_three(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        await enroll(app,client,state)
        conversation=(await client.get('/api/bootstrap')).json()['conversation_id']
        yellow=await create_payment(app,conversation,'100')
        token,_=await payment_face(app,client,state,yellow,conversation)
        result=await client.post(f'/api/approvals/{yellow}/decision',json={'decision':'confirm','conversation_id':conversation,'account_id':'daily','face_token':token})
        assert result.json()['status']=='executed',result.text
        yellow=await create_payment(app,conversation,'100')
        result=await client.post(f'/api/approvals/{yellow}/decision',json={'decision':'confirm','conversation_id':conversation,'account_id':'daily','pin':'2468'})
        assert result.json()['status']=='executed'
        red=await create_payment(app,conversation,'1001')
        token,sms=await payment_face(app,client,state,red,conversation,red=True)
        body={'decision':'confirm','conversation_id':conversation,'account_id':'daily','pin':'2468','sms_code':sms}
        assert (await client.post(f'/api/approvals/{red}/decision',json=body)).status_code==400
        # A face token cannot substitute for a red operation's password.
        assert (await client.post(f'/api/approvals/{red}/decision',json={k:v for k,v in {**body,'face_token':token}.items() if k!='pin'})).status_code==400
        result=await client.post(f'/api/approvals/{red}/decision',json={**body,'face_token':token})
        assert result.json()['status']=='executed',result.text
        with app.state.database.connection() as db:assert db.execute('SELECT consumed FROM face_grants WHERE token_hash=?',(face.token_hash(token),)).fetchone()[0]==1


async def test_grant_bound_to_operation_account_policy_and_expiry(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        await enroll(app,client,state);conversation=(await client.get('/api/bootstrap')).json()['conversation_id']
        approval=await create_payment(app,conversation,'1200');token,sms=await payment_face(app,client,state,approval,conversation,True)
        with app.state.database.connection() as db:
            row=db.execute('SELECT * FROM approvals WHERE id=?',(approval,)).fetchone();args=json.loads(row['args_json']);args['account_id']='daily';binding=face.payment_binding(db,row,args)
            assert face.verify_grant(db,'wrong-session',token,binding,False)
            args['account_id']='savings';assert face.verify_grant(db,db.execute('SELECT session_key FROM face_grants').fetchone()[0],token,face.payment_binding(db,row,args),False)
        with app.state.database.transaction() as db:db.execute('UPDATE face_grants SET expires=0')
        body={'decision':'confirm','conversation_id':conversation,'account_id':'daily','pin':'2468','sms_code':sms,'face_token':token}
        assert (await client.post(f'/api/approvals/{approval}/decision',json=body)).status_code==400
        with app.state.database.connection() as db:assert db.execute('SELECT status FROM approvals WHERE id=?',(approval,)).fetchone()[0]=='pending'


async def test_wrong_face_denied_and_failure_count_persists(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        await enroll(app,client,state);conversation=(await client.get('/api/bootstrap')).json()['conversation_id']
        approval=await create_payment(app,conversation,'100')
        challenge=(await client.post('/api/face/challenge',json={'purpose':'payment','approval_id':approval,'conversation_id':conversation,'account_id':'daily'})).json()
        state['vector']=[0.,1.]+[0.]*126;state['pose']=0.
        result=await client.post('/api/face/challenges/'+challenge['challenge_id']+'/frame',content=b'\xff\xd8'+b'x'*150,headers={'Content-Type':'image/jpeg'})
        assert result.status_code==400 and '不匹配' in result.text
        with app.state.database.connection() as db:
            assert db.execute("SELECT failures FROM safety_counters WHERE kind='face'").fetchone()[0]==1
            assert db.execute('SELECT COUNT(*) FROM face_grants').fetchone()[0]==0


async def test_unbound_yellow_password_still_available_and_no_red_bypass(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        conversation=(await client.get('/api/bootstrap')).json()['conversation_id']
        approval=await create_payment(app,conversation,'100')
        result=await client.post('/api/face/challenge',json={'purpose':'payment','approval_id':approval,'conversation_id':conversation,'account_id':'daily'})
        assert result.status_code==400
        result=await client.post(f'/api/approvals/{approval}/decision',json={'decision':'confirm','conversation_id':conversation,'account_id':'daily','pin':'2468'})
        assert result.json()['status']=='executed'
        assert (await client.get('/api/payment-policy')).status_code==200


@pytest.mark.parametrize('mode',['unbound','account_off'])
async def test_red_without_active_bound_face_uses_pin_and_sms(settings,monkeypatch,mode):
    async with environment(settings,monkeypatch) as (app,client,state):
        if mode=='account_off':
            await enroll(app,client,state)
            with app.state.database.transaction() as db:db.execute('UPDATE face_preferences SET enabled=0')
        conversation=(await client.get('/api/bootstrap')).json()['conversation_id']
        approval=await create_payment(app,conversation,'1001')
        context=(await client.get(f'/api/approvals/{approval}/payment?conversation_id={conversation}')).json()
        assert not context['face_required'] and not context['face_enabled'] and context['sms_required']
        body={'decision':'confirm','conversation_id':conversation,'account_id':'daily','pin':'2468'}
        assert (await client.post(f'/api/approvals/{approval}/decision',json=body)).status_code==400
        code=(await client.post(f'/api/approvals/{approval}/sms',json={'conversation_id':conversation,'account_id':'daily'})).json()['demo_code']
        result=await client.post(f'/api/approvals/{approval}/decision',json={**body,'sms_code':code})
        assert result.json()['status']=='executed',result.text


async def test_delete_requires_old_face_and_invalidates_grants(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        await enroll(app,client,state)
        with app.state.database.transaction() as db:db.execute('UPDATE face_sms SET sent=0')
        sms=(await client.post('/api/face/sms',json={'purpose':'delete','pin':'2468'})).json()['demo_code']
        body={'purpose':'delete','pin':'2468','sms_code':sms,'consent':True}
        result=await client.post('/api/face/challenge',json=body)
        assert (await client.get('/api/face/status')).json()['bound']
        assert (await finish_face(app,client,state,result.json()))['bound'] is False
        assert (await client.post('/api/face/challenge',json=body)).status_code==400


async def test_cancel_missing_consent_upload_limits_and_no_login(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        sms=(await client.post('/api/face/sms',json={'purpose':'enroll','pin':'2468'})).json()['demo_code']
        body={'purpose':'enroll','pin':'2468','sms_code':sms}
        assert (await client.post('/api/face/challenge',json=body)).status_code==400
        challenge=(await client.post('/api/face/challenge',json={**body,'consent':True})).json()['challenge_id']
        path=f'/api/face/challenges/{challenge}/frame'
        assert (await client.post(path,content=b'x'*250001,headers={'Content-Type':'image/jpeg'})).status_code==413
        assert (await client.post(path,json={'face_passed':True})).status_code==415
        assert (await client.delete('/api/face/challenges/'+challenge)).status_code==200
        assert (await client.post(path,content=b'\xff\xd8'+b'x'*100,headers={'Content-Type':'image/jpeg'})).status_code==400
        await client.post('/api/auth/logout',json={})
        assert (await client.get('/api/face/status')).status_code==401


def test_actual_models_load_and_reject_empty_frame():
    import cv2
    from banking.config import ROOT
    if not face_engine.ready(ROOT/'data/face'):pytest.skip('Local models are optional in fresh source checkouts')
    engine=face_engine.Engine(ROOT/'data/face')
    _,encoded=cv2.imencode('.jpg',np.zeros((480,640,3),np.uint8))
    with pytest.raises(face_engine.FaceCheckError):engine.inspect(encoded.tobytes())


async def test_replace_requires_old_face_then_new_capture(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        await enroll(app,client,state)
        with app.state.database.transaction() as db:db.execute('UPDATE face_sms SET sent=0')
        sms=(await client.post('/api/face/sms',json={'purpose':'replace','pin':'2468'})).json()['demo_code']
        challenge=(await client.post('/api/face/challenge',json={'purpose':'replace','pin':'2468','sms_code':sms,'consent':True})).json()
        next_challenge=await finish_face(app,client,state,challenge)
        assert next_challenge['replacing'] and not next_challenge['done']
        state['vector']=[0.,1.]+[0.]*126
        assert (await finish_face(app,client,state,next_challenge))['bound']
        with app.state.database.connection() as db:
            profile=db.execute('SELECT * FROM face_identity').fetchone()
            assert profile['version']==2 and face.decrypt(app.state.database,profile['template'])==state['vector']


async def test_password_change_requires_sms_and_old_bound_face(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        await enroll(app,client,state)
        body={'pin':'1234','repeat_pin':'1234','current_pin':'2468'}
        sms=(await client.post('/api/auth/pin/sms',json=body)).json()['demo_code']
        assert (await client.post('/api/auth/pin',json={**body,'sms_code':sms})).status_code==400
        challenge=(await client.post('/api/face/challenge',json={'purpose':'pin_change','pin':'2468','sms_code':sms})).json()
        token=(await finish_face(app,client,state,challenge))['face_token']
        response=await client.post('/api/auth/pin',json={**body,'sms_code':sms,'face_token':token})
        assert response.status_code==200,response.text


async def test_account_disable_requires_face_and_survives_server_restart(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        await enroll(app,client,state)
        with app.state.database.connection() as db:original=db.execute('SELECT template FROM face_identity').fetchone()[0]
        # There is no password-only disable API.
        assert (await client.post('/api/face/disable',json={'pin':'2468'})).status_code in {404,405}
        with app.state.database.transaction() as db:db.execute('UPDATE face_sms SET sent=0')
        sms=(await client.post('/api/face/sms',json={'purpose':'disable','pin':'2468'})).json()['demo_code']
        challenge=(await client.post('/api/face/challenge',json={'purpose':'disable','pin':'2468','sms_code':sms,'consent':True})).json()
        assert (await client.get('/api/face/status')).json()['enabled']
        result=await finish_face(app,client,state,challenge)
        assert result['enabled'] is False
        face.configure(app.state.database,True)
        status=(await client.get('/api/face/status')).json()
        assert status['server_enabled'] and not status['enabled'] and status['bound']
        with app.state.database.connection() as db:assert db.execute('SELECT template FROM face_identity').fetchone()[0]==original
        assert (await client.post('/api/face/enable',json={'pin':'0000'})).status_code==400
        assert (await client.post('/api/face/enable',json={'pin':'2468'})).status_code==400
        sms=(await client.post('/api/face/sms',json={'purpose':'enable','pin':'2468'})).json()['demo_code']
        challenge=(await client.post('/api/face/challenge',json={'purpose':'enable','pin':'2468','sms_code':sms,'consent':True})).json()
        assert challenge['steps']==1
        assert not (await client.get('/api/face/status')).json()['enabled']
        assert (await finish_face(app,client,state,challenge))['enabled']
        assert (await client.get('/api/face/status')).json()['enabled']


async def test_unbound_enable_via_enrollment_and_unbind_disables(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        with app.state.database.transaction() as db:db.execute('UPDATE face_preferences SET enabled=0')
        assert (await enroll(app,client,state))['bound']
        assert (await client.get('/api/face/status')).json()['verification_enabled']
        with app.state.database.transaction() as db:db.execute('UPDATE face_sms SET sent=0')
        sms=(await client.post('/api/face/sms',json={'purpose':'delete','pin':'2468'})).json()['demo_code']
        challenge=(await client.post('/api/face/challenge',json={'purpose':'delete','pin':'2468','sms_code':sms,'consent':True})).json()
        assert challenge['steps']==1
        assert not (await finish_face(app,client,state,challenge))['bound']
        status=(await client.get('/api/face/status')).json()
        assert not status['bound'] and not status['enabled'] and not status['verification_enabled']


async def test_enable_rejects_different_person(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        await enroll(app,client,state)
        with app.state.database.transaction() as db:db.execute('UPDATE face_preferences SET enabled=0')
        sms=(await client.post('/api/face/sms',json={'purpose':'enable','pin':'2468'})).json()['demo_code']
        challenge=(await client.post('/api/face/challenge',json={'purpose':'enable','pin':'2468','sms_code':sms,'consent':True})).json()
        state['vector']=[0.,1.]+[0.]*126;state['pose']=0.
        result=await client.post('/api/face/challenges/'+challenge['challenge_id']+'/frame',content=b'\xff\xd8'+b'x'*150,headers={'Content-Type':'image/jpeg'})
        assert result.status_code==400 and '不匹配' in result.text
        assert not (await client.get('/api/face/status')).json()['enabled']


async def test_stationary_face_does_not_complete_random_turn_challenge(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        sms=(await client.post('/api/face/sms',json={'purpose':'enroll','pin':'2468'})).json()['demo_code']
        challenge=(await client.post('/api/face/challenge',json={'purpose':'enroll','pin':'2468','sms_code':sms,'consent':True})).json()['challenge_id']
        state['pose']=0.;state['passive_score']=.02
        for index in range(4):
            with app.state.database.transaction() as db:db.execute('UPDATE face_challenges SET last_at=0 WHERE id=?',(challenge,))
            result=await client.post(f'/api/face/challenges/{challenge}/frame',content=b'\xff\xd8'+bytes([index])*150,headers={'Content-Type':'image/jpeg'})
            assert result.status_code==200 and not result.json()['done']
        with app.state.database.connection() as db:
            assert db.execute('SELECT step FROM face_challenges WHERE id=?',(challenge,)).fetchone()[0]==1
            assert db.execute('SELECT template FROM face_identity').fetchone()[0] is None


def test_passive_score_is_advisory_not_an_immediate_veto():
    from types import SimpleNamespace
    import cv2,torch
    engine=face_engine.Engine.__new__(face_engine.Engine)
    face_row=np.array([[200,100,140,190,235,150,285,150,260,185,240,220,280,220,.99]],dtype=np.float32)
    engine.detector=SimpleNamespace(setInputSize=lambda size:None,detect=lambda image:(None,face_row))
    engine.recognizer=SimpleNamespace(alignCrop=lambda image,face:np.zeros((112,112,3),np.uint8),feature=lambda image:np.ones((1,128),np.float32))
    engine.antispoof=[(lambda tensor:torch.tensor([[4.,0.,-4.]]),2.7),(lambda tensor:torch.tensor([[4.,0.,-4.]]),4.)]
    image=np.random.default_rng(42).integers(70,190,(480,640,3),dtype=np.uint8)
    _,encoded=cv2.imencode('.jpg',image)
    result=engine.inspect(encoded.tobytes())
    assert result['passive_score']<.80 and result['pose']==0.
    assert abs(np.linalg.norm(result['vector'])-1)<1e-5
