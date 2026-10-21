import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from banking.main import create_app
from banking.db import Database
from banking.bank import Bank,BankError
from banking import scheduled_transfers as scheduled
from conftest import ScriptedModel,ask,tool,balance
from test_security import register

def funds(database):
    with database.connection() as db:return {r['id']:r['balance_minor'] for r in db.execute('SELECT * FROM accounts')}

async def pending(client,conv,**changes):
    args={'conversation_id':conv,'recipient':'zhang_san','amount_yuan':'100','execute_at':(scheduled.clock()+timedelta(minutes=2)).isoformat(),**changes}
    response=await client.post('/api/scheduled-transfers',json=args)
    assert response.status_code==200,response.text
    return response.json()['data']['approval_id']

async def authorize(client,conv,**changes):
    key=await pending(client,conv,**changes)
    context=(await client.get(f'/api/approvals/{key}/payment',params={'conversation_id':conv})).json()
    body={'conversation_id':conv,'decision':'confirm','pin':'2468','account_id':'daily'}
    if context['sms_required']:
        sms=await client.post(f'/api/approvals/{key}/sms',json={'conversation_id':conv,'account_id':'daily'})
        assert sms.status_code==200,sms.text
        body['sms_code']=sms.json()['demo_code']
    response=await client.post(f'/api/approvals/{key}/decision',json=body)
    assert response.status_code==200 and response.json()['status']=='executed',response.text
    return response.json()['receipt']['schedule']

@pytest.fixture
def configured(settings):
    settings.app_auth_enabled=True;settings.scheduled_worker_enabled=False
    return settings

async def test_preauthorization_yellow_and_red_no_early_debit(configured):
    app=create_app(configured,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client);conv=(await client.get('/api/bootstrap')).json()['conversation_id'];before=funds(app.state.database)
        key=await pending(client,conv)
        path=f'/api/approvals/{key}/decision';body={'conversation_id':conv,'decision':'confirm','account_id':'daily'}
        assert (await client.post(path,json=body)).status_code==428
        assert (await client.post(path,json={**body,'pin':'0000'})).status_code==400
        assert (await client.post(path,json={**body,'pin':'2468'})).status_code==400
        sms=(await client.post(f'/api/approvals/{key}/sms',json={'conversation_id':conv,'account_id':'daily'})).json()
        body={**body,'sms_code':sms['demo_code']}
        response=await client.post(path,json={**body,'pin':'2468'})
        row=response.json()['receipt']['schedule'];assert row['status']=='scheduled'
        assert funds(app.state.database)==before
        assert scheduled.process_due(app.state.database)==0
        assert (await client.post(path,json={**body,'pin':'2468'})).json()['receipt']['schedule']['id']==row['id']
        key=await pending(client,conv,amount_yuan='1000.01')
        path=f'/api/approvals/{key}/decision'
        assert (await client.post(path,json={**body,'pin':'2468'})).status_code==400
        sms=(await client.post(f'/api/approvals/{key}/sms',json={'conversation_id':conv,'account_id':'daily'})).json()
        assert (await client.post(path,json={**body,'pin':'2468','sms_code':sms['demo_code']})).json()['status']=='executed'
        assert funds(app.state.database)==before
        assert len((await client.get('/api/scheduled-transfers')).json()['items'])==2

async def test_due_is_atomic_idempotent_and_separate_execution_receipt(configured):
    app=create_app(configured,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client);conv=(await client.get('/api/bootstrap')).json()['conversation_id']
        row=await authorize(client,conv,recipient='savings',note='旅行资金')
        before=funds(app.state.database);due=scheduled.when(row['execute_at'])+timedelta(seconds=1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:scheduled.process_due(app.state.database,due),range(2)))
        assert sum(results)==1
        after=funds(app.state.database);assert after['daily']==before['daily']-10000 and after['savings']==before['savings']+10000
        assert scheduled.process_due(app.state.database,due)==0
        record=(await client.get('/api/scheduled-transfers')).json()['items'][0]
        assert record['status']=='executed' and record['execution_id']!=record['authorization_id']
        assert record['note']=='旅行资金'
        assert record['receipt']['action_id']==record['execution_id']
        from banking.audit_view import detail
        with app.state.database.connection() as db:
            audit=detail(db,{'id':'legacy','username':'测试'},conv)
        assert audit['summary']['debit_minor']==10000 and audit['summary']['credit_minor']==10000
        assert any(t['title']=='定时交易已提前授权（未扣款）' for t in audit['timeline'])
        with app.state.database.connection() as db:
            assert db.execute('SELECT description FROM transactions WHERE action_id=?',(record['execution_id']+':out',)).fetchone()[0]=='旅行资金'

@pytest.mark.parametrize('change,expected',[
    ('balance','failed'),('lock','failed'),('policy','needs_reauthorization'),('pin','needs_reauthorization'),
    ('recipient','needs_reauthorization'),('source','needs_reauthorization'),('tamper','needs_reauthorization'),('late','needs_reauthorization')])
async def test_rechecks_fail_closed_and_never_retry(configured,change,expected):
    app=create_app(configured,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client);conv=(await client.get('/api/bootstrap')).json()['conversation_id'];row=await authorize(client,conv)
        with app.state.database.transaction() as db:
            if change=='balance':db.execute("UPDATE accounts SET balance_minor=0 WHERE id='daily'")
            if change=='lock':db.execute('UPDATE account_guard SET locked=1 WHERE id=1')
            if change=='policy':db.execute('UPDATE payment_policy SET version=version+1 WHERE id=1')
            if change=='pin':db.execute("UPDATE app_identity SET pin_hash='changed' WHERE id=1")
            if change=='recipient':db.execute("UPDATE contact_details SET card_number='6222000012341111' WHERE contact_id='zhang_san'")
            if change=='source':db.execute("UPDATE accounts SET name='改名后账户' WHERE id='daily'")
            if change=='tamper':db.execute("UPDATE scheduled_transfers SET args_json=? WHERE id=?",(json.dumps({'recipient':'zhang_san','account_id':'daily','amount_yuan':'1','note':'','plan_id':None}),row['id']))
        before=funds(app.state.database)
        due=scheduled.when(row['execute_at'])+(timedelta(minutes=6) if change=='late' else timedelta(seconds=1))
        assert scheduled.process_due(app.state.database,due)==1
        with app.state.database.connection() as db:record=db.execute('SELECT * FROM scheduled_transfers WHERE id=?',(row['id'],)).fetchone()
        assert record['status']==expected and record['error'] and not record['execution_id']
        assert scheduled.process_due(app.state.database,due)==0 and funds(app.state.database)==before

async def test_cancel_and_edit_need_new_authorization(configured):
    app=create_app(configured,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client);conv=(await client.get('/api/bootstrap')).json()['conversation_id'];old=await authorize(client,conv)
        key=await pending(client,conv,replaces_id=old['id'],amount_yuan='120',execute_at=(scheduled.when(old['execute_at'])+timedelta(minutes=2)).isoformat())
        assert (await client.get('/api/scheduled-transfers')).json()['items'][0]['status']=='scheduled'
        sms=(await client.post(f'/api/approvals/{key}/sms',json={'conversation_id':conv,'account_id':'savings'})).json()
        result=await client.post(f'/api/approvals/{key}/decision',json={'conversation_id':conv,'decision':'confirm','account_id':'savings','pin':'2468','sms_code':sms['demo_code']})
        new=result.json()['receipt']['schedule']
        items=(await client.get('/api/scheduled-transfers')).json()['items']
        assert next(i for i in items if i['id']==old['id'])['status']=='cancelled'
        assert new['account_id']=='savings' and new['amount_minor']==12000
        assert scheduled.process_due(app.state.database,scheduled.when(old['execute_at'])+timedelta(seconds=1))==0
        before=funds(app.state.database)
        assert (await client.post(f"/api/scheduled-transfers/{new['id']}/cancel",json={'confirmation':'CANCEL'})).status_code==200
        assert scheduled.process_due(app.state.database,scheduled.when(new['execute_at'])+timedelta(seconds=1))==0
        assert funds(app.state.database)==before

async def test_partial_failure_rolls_back_and_restart_can_execute_once(configured,monkeypatch):
    app=create_app(configured,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client);conv=(await client.get('/api/bootstrap')).json()['conversation_id'];row=await authorize(client,conv)
        due=scheduled.when(row['execute_at'])+timedelta(seconds=1);before=funds(app.state.database);real=Bank.execute
        def broken(self,db,name,args,action):
            db.execute("UPDATE accounts SET balance_minor=balance_minor-100 WHERE id='daily'")
            raise RuntimeError('测试模拟进程异常')
        monkeypatch.setattr(Bank,'execute',broken)
        with pytest.raises(RuntimeError):scheduled.process_due(app.state.database,due)
        assert funds(app.state.database)==before
        monkeypatch.setattr(Bank,'execute',real)
    restarted=create_app(configured,ScriptedModel())
    async with restarted.router.lifespan_context(restarted):
        assert scheduled.process_due(restarted.state.database,due)==1
        assert funds(restarted.state.database)['daily']==before['daily']-10000
        assert scheduled.process_due(restarted.state.database,due)==0

async def test_ambiguous_time_and_model_instant_transfer_are_blocked(agent,configured):
    before=balance(agent)
    await ask(agent,[[tool('transfer',recipient='zhang_san',amount_yuan='100')]],'明天下午3点帮我给张三转100')
    assert balance(agent)==before
    with agent.db.connection() as db:assert not db.execute('SELECT 1 FROM approvals').fetchone()
    app=create_app(configured,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client);conv=(await client.get('/api/bootstrap')).json()['conversation_id']
        base={'conversation_id':conv,'recipient':'zhang_san','amount_yuan':'100'}
        for value in ('tomorrow','2020-01-01T09:00:00+08:00',(scheduled.clock()+timedelta(days=366)).isoformat()):
            assert (await client.post('/api/scheduled-transfers',json={**base,'execute_at':value})).status_code in (400,422)

async def test_multiuser_offline_accounts_execute_on_hub_restart(configured,monkeypatch):
    from test_accounts import sms_login
    from banking.security import token_hash
    configured.multi_user_accounts=True
    app=create_app(configured,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as a,httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as b:
        paths=[]
        for client,phone,label in ((a,'13800123456','甲'),(b,'13900123456','乙')):
            await sms_login(client,phone,label);await client.post('/api/auth/contacts-done',json={})
            await client.post('/api/auth/pin',json={'pin':'2468','repeat_pin':'2468'})
            user=app.state.directory.user_for(token_hash(client.cookies['bank_session']))
            tenant=await app.state.directory.application(user);paths.append(tenant.state.database.path)
            with tenant.state.database.transaction() as db:
                db.executemany('INSERT INTO accounts VALUES(?,?,?,?)',[('daily','日常账户','checking',100000),('savings','储蓄账户','savings',0)])
        conv=(await a.get('/api/bootstrap')).json()['conversation_id'];row=await authorize(a,conv,recipient='savings',amount_yuan='10')
        assert (await b.post(f"/api/scheduled-transfers/{row['id']}/cancel",json={'confirmation':'CANCEL'})).status_code==400
        assert (await b.get('/api/scheduled-transfers')).json()['items']==[]
    configured.scheduled_worker_enabled=True
    monkeypatch.setattr(scheduled,'clock',lambda:scheduled.when(row['execute_at'])+timedelta(seconds=1))
    restarted=create_app(configured,ScriptedModel())
    async with restarted.router.lifespan_context(restarted):
        for _ in range(30):
            if funds(Database(paths[0],seed_demo=False))['daily']==99000:break
            await asyncio.sleep(.05)
        assert restarted.state.directory.apps=={},'后台应处理离线用户，无需登录或启动其模型'
        assert funds(Database(paths[0],seed_demo=False))['daily']==99000
        assert funds(Database(paths[1],seed_demo=False))['daily']==100000

async def test_authorization_cannot_be_forged_without_verification(database):
    from banking.db import dumps,now,uid,add_message
    bank=Bank(database)
    with database.transaction() as db:
        conv=database.conversation_id(db);task=uid('task_');op=uid('op_')
        args,summary=bank.prepare_payment('schedule_transfer',{'recipient':'zhang_san','amount_yuan':'100','account_id':'daily','execute_at':(scheduled.clock()+timedelta(minutes=2)).isoformat()},connection=db)
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(task,conv,'waiting_confirmation','web','{}',None,now()))
        message=add_message(db,conv,task,'assistant','approval','核对',{})
        db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?)',(op,task,message['id'],'schedule_transfer',dumps(args),dumps(summary),'pending',None,now(),now()))
        with pytest.raises(BankError,match='提前完成'):bank.execute(db,'schedule_transfer',args,op)
        assert not db.execute('SELECT 1 FROM scheduled_transfers').fetchone()

async def test_chat_schedule_uses_explicit_source_and_never_claims_payment_after_authorization(agent):
    from banking.security import encode_pin
    agent.settings.app_auth_enabled=True
    with agent.db.transaction() as db:
        db.execute("INSERT INTO app_identity(id,username,phone,pin_hash,contacts_done) VALUES(1,'测试','11111111111',?,1)",(encode_pin('2468'),))
    before=funds(agent.db)
    await ask(agent,[[tool('schedule_transfer',recipient='zhang_san',amount_yuan='100',account_id='daily',execute_at=(scheduled.clock()+timedelta(days=1)).isoformat())],'已经转给张三并到账了'],'明天这个时间从储蓄账户给张三转100')
    with agent.db.connection() as db:
        approval=db.execute("SELECT * FROM approvals WHERE status='pending'").fetchone()
        assert approval['tool_name']=='schedule_transfer'
        assert json.loads(approval['args_json'])['account_id']=='savings'
    from banking.payment_security import send_sms
    with agent.db.transaction() as db:
        args=json.loads(approval['args_json']);args['account_id']='savings'
        sms=send_sms(db,approval,args,'local')
    response=await agent.confirm(approval['id'],'confirm',pin='2468',account_id='savings',sms_code=sms['demo_code'],session_key='local')
    await asyncio.wait_for(agent.queue.join(),3)
    assert response['receipt']['schedule']['status']=='scheduled'
    assert funds(agent.db)==before
    with agent.db.connection() as db:
        assert '现在未扣款' in db.execute("SELECT text FROM messages WHERE kind='text' AND role='assistant' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    await ask(agent,[[tool('schedule_transfer',recipient='李明',amount_yuan='100',execute_at=(scheduled.clock()+timedelta(days=1)).isoformat())]],'明天这个时间给李明转100')
    with agent.db.connection() as db:
        assert not db.execute("SELECT 1 FROM approvals WHERE status='pending'").fetchone()
