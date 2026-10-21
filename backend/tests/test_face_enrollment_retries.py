import pytest
from banking import face_engine
from banking.safety import failure
from test_face import environment, finish_face


@pytest.mark.parametrize('purpose',['enroll','replace_enroll'])
async def test_repeated_capture_mismatch_does_not_change_security(settings,monkeypatch,purpose):
    async with environment(settings,monkeypatch) as (app,client,state):
        with app.state.database.transaction() as db:
            failure(db,'face')
        # Start through the public enrollment API; replacement capture is already
        # authorized by the old-face check, tested separately in test_face.py.
        sms=(await client.post('/api/face/sms',json={'purpose':'enroll','pin':'2468'})).json()['demo_code']
        challenge=(await client.post('/api/face/challenge',json={'purpose':'enroll','pin':'2468','sms_code':sms,'consent':True})).json()
        with app.state.database.transaction() as db:
            db.execute('UPDATE face_challenges SET purpose=? WHERE id=?',(purpose,challenge['challenge_id']))
        for index in range(8):
            state['pose']=0.;state['vector']=[1.]+[0.]*127
            result=await client.post('/api/face/challenges/'+challenge['challenge_id']+'/frame',content=b'\xff\xd8'+bytes([index])*150,headers={'Content-Type':'image/jpeg'})
            assert result.status_code==200,result.text
            with app.state.database.transaction() as db:db.execute('UPDATE face_challenges SET last_at=0 WHERE id=?',(challenge['challenge_id'],))
            state['vector']=[0.,1.]+[0.]*126
            retry=await client.post('/api/face/challenges/'+challenge['challenge_id']+'/frame',content=b'\xff\xd8'+bytes([index+20])*150,headers={'Content-Type':'image/jpeg'})
            assert retry.status_code==200 and retry.json()['step']==0 and retry.json()['retry']
            with app.state.database.connection() as db:
                assert db.execute("SELECT failures FROM safety_counters WHERE kind='face'").fetchone()[0]==1
                assert db.execute('SELECT status FROM face_challenges WHERE id=?',(challenge['challenge_id'],)).fetchone()[0]=='pending'
        state['vector']=[1.]+[0.]*127
        assert (await finish_face(app,client,state,challenge))['bound']
        with app.state.database.connection() as db:
            assert db.execute("SELECT failures FROM safety_counters WHERE kind='face'").fetchone()[0]==1


async def test_enrollment_engine_rejection_does_not_count_security_failure(settings,monkeypatch):
    async with environment(settings,monkeypatch) as (app,client,state):
        sms=(await client.post('/api/face/sms',json={'purpose':'enroll','pin':'2468'})).json()['demo_code']
        challenge=(await client.post('/api/face/challenge',json={'purpose':'enroll','pin':'2468','sms_code':sms,'consent':True})).json()
        def reject(*args):raise face_engine.FaceCheckError('请重试',security=True)
        monkeypatch.setattr(face_engine,'inspect',reject)
        for _ in range(6):
            result=await client.post('/api/face/challenges/'+challenge['challenge_id']+'/frame',content=b'\xff\xd8'+b'x'*150,headers={'Content-Type':'image/jpeg'})
            assert result.status_code==200 and result.json()['retry']
        with app.state.database.connection() as db:
            assert db.execute("SELECT COALESCE(SUM(failures),0) FROM safety_counters WHERE kind='face'").fetchone()[0]==0
