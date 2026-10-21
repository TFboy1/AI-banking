import asyncio
import json
import time

import httpx
import pytest
from banking import safety
from banking.bank import BankError
from banking.db import Database
from banking.main import create_app
from conftest import ScriptedModel, ask, balance, pending, state, tool
from test_security import register


async def test_platform_circuit_persists_and_isolates_and_probes_one_request(database):
    calls = 0
    async def down():
        nonlocal calls
        calls += 1
        raise httpx.ConnectError('sensitive upstream details')
    for _ in range(3):
        with pytest.raises(BankError): await safety.platform_request(database, 'securities', down)
    with pytest.raises(BankError, match='服务已暂时暂停'):
        await safety.platform_request(Database(database.path), 'securities', down)
    assert calls == 3
    async def good(): return httpx.Response(200, json={'ok': True})
    assert (await safety.platform_request(database, 'food', good))['ok']
    with database.transaction() as db:
        safety.check_user(db)
        db.execute("UPDATE safety_circuits SET blocked_until=? WHERE scope='securities'", (time.time()-1,))
    entered = asyncio.Event();release = asyncio.Event()
    async def probe():
        entered.set();await release.wait();return httpx.Response(200,json={'ok':True})
    first = asyncio.create_task(safety.platform_request(database,'securities',probe))
    await entered.wait()
    with pytest.raises(BankError, match='恢复情况'):
        await safety.platform_request(database,'securities',good)
    release.set();await first
    with database.connection() as db:
        row=db.execute("SELECT * FROM safety_circuits WHERE scope='securities'").fetchone()
        assert row['blocked_until']==row['failures']==0
        assert 'sensitive upstream details' not in json.dumps([dict(r) for r in db.execute('SELECT * FROM safety_events')])


async def test_platform_business_errors_and_cancellation_do_not_count(database):
    async def business(): return httpx.Response(400,json={'detail':'报价过期'})
    for _ in range(5):
        with pytest.raises(BankError, match='报价过期'):
            await safety.platform_request(database,'food',business)
    async def cancelled(): raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError): await safety.platform_request(database,'food',cancelled)
    with database.connection() as db:
        assert db.execute("SELECT failures FROM safety_circuits WHERE scope='food'").fetchone()[0]==0


async def test_stale_success_cannot_close_opened_platform_and_failed_probe_reopens(database):
    tickets=[safety.admit_platform(database,'securities') for _ in range(4)]
    for ticket in tickets[:3]: safety.finish_platform(database,'securities',ticket,True)
    safety.finish_platform(database,'securities',tickets[3],False)
    with database.transaction() as db:
        assert safety.circuit(db,'securities')['blocked_until'] > time.time()
        db.execute("UPDATE safety_circuits SET blocked_until=? WHERE scope='securities'",(time.time()-1,))
    ticket=safety.admit_platform(database,'securities')
    safety.finish_platform(database,'securities',ticket,True)
    with database.transaction() as db:
        assert safety.circuit(db,'securities')['blocked_until'] > time.time()


async def test_pin_locks_all_money_but_queries_and_cancel_remain_available(settings):
    settings.app_auth_enabled=True
    app=create_app(settings,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        await register(c)
        conv=(await c.get('/api/bootstrap')).json()['conversation_id']
        agent=app.state.agent
        task=await ask(agent,[[tool('transfer_own_funds',target_account_id='savings',amount_yuan='100')]],'从日常账户转100元到储蓄账户')
        aid=pending(agent)['id'];before=balance(agent)
        for _ in range(5):
            assert (await c.post(f'/api/approvals/{aid}/decision',json={'conversation_id':conv,'decision':'confirm','pin':'0000'})).status_code==400
        assert (await c.get('/api/bank')).status_code==200
        status=(await c.get('/api/safety/status')).json()
        assert next(r for r in status['scopes'] if r['scope']=='transactions')['blocked_until'] > time.time()
        assert any(e['kind']=='opened' for e in status['events'])
        assert (await c.post(f'/api/approvals/{aid}/decision',json={'conversation_id':conv,'decision':'confirm','pin':'2468'})).status_code==400
        with app.state.database.transaction() as db,pytest.raises(BankError,match='安全锁定'):
            agent.bank.execute(db,'transfer_own_funds',{'account_id':'daily','target_account_id':'savings','amount_yuan':'100'},'direct-bypass')
        assert balance(agent)==before
        assert (await c.post(f'/api/approvals/{aid}/decision',json={'conversation_id':conv,'decision':'cancel'})).json()['status']=='cancelled'
        with app.state.database.transaction() as db:
            db.execute("UPDATE safety_circuits SET blocked_until=? WHERE scope='transactions'",(time.time()-1,))
        status=(await c.get('/api/safety/status')).json()
        assert next(r for r in status['scopes'] if r['scope']=='transactions')['blocked_until']==0


async def test_suspicious_lock_requires_new_sms_login_and_recovery_code(settings):
    settings.app_auth_enabled=True
    app=create_app(settings,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        assert (await c.get('/api/safety/status')).status_code==401
        await register(c)
        conv=(await c.get('/api/bootstrap')).json()['conversation_id']
        for i in range(3):
            assert (await c.post(f'/api/approvals/forged{i}/decision',json={'conversation_id':conv,'decision':'confirm'})).status_code==400
        assert (await c.get('/api/safety/status')).json()['scopes'][0]['requires_reauth']==1
        assert (await c.post('/api/safety/recovery-sms',json={})).status_code==400
        await c.post('/api/auth/logout',json={})
        with app.state.database.transaction() as db: db.execute('UPDATE sms_challenges SET sent_at=0')
        sms=(await c.post('/api/auth/sms',json={'phone':'13800123456','purpose':'login'})).json()['demo_code']
        assert (await c.post('/api/auth/login',json={'phone':'13800123456','purpose':'login','code':sms})).status_code==200
        recover=(await c.post('/api/safety/recovery-sms',json={})).json()['demo_code']
        assert (await c.post('/api/safety/recover',json={'code':'000000' if recover!='000000' else '111111'})).status_code==400
        result=(await c.post('/api/safety/recover',json={'code':recover})).json()
        assert result['scopes'][0]['requires_reauth']==0 and result['scopes'][0]['blocked_until']==0
        assert any('重新短信登录并验证恢复' in e['detail'] for e in result['events'])
        assert recover not in json.dumps(result) and sms not in json.dumps(result)


async def test_locked_resume_keeps_checkpoint_and_business_failures_not_counted(agent):
    first=await ask(agent,[[tool('transfer_own_funds',target_account_id='savings',amount_yuan='999999')]],'从日常账户转999999元到储蓄账户')
    assert state(agent,first)['status']=='failed'
    with agent.db.transaction() as db:
        assert db.execute('SELECT count(*) FROM safety_events').fetchone()[0]==0
        for _ in range(5): safety.failure(db,'sms')
    before=balance(agent)
    with pytest.raises(BankError,match='安全锁定'): await agent.submit('继续')
    assert balance(agent)==before
    assert 'resumed_by' not in json.loads(state(agent,first)['state_json'])


async def test_payment_sms_five_errors_pause_other_payments(settings):
    settings.app_auth_enabled=True
    app=create_app(settings,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        await register(c)
        conv=(await c.get('/api/bootstrap')).json()['conversation_id']
        await ask(app.state.agent,[[tool('transfer_own_funds',target_account_id='savings',amount_yuan='1001')]],'从日常账户转1001元到储蓄账户')
        aid=pending(app.state.agent)['id'];before=balance(app.state.agent)
        sms=(await c.post(f'/api/approvals/{aid}/sms',json={'conversation_id':conv,'account_id':'daily'})).json()['demo_code']
        wrong='000000' if sms!='000000' else '111111'
        for _ in range(5):
            assert (await c.post(f'/api/approvals/{aid}/decision',json={'conversation_id':conv,'decision':'confirm','account_id':'daily','pin':'2468','sms_code':wrong})).status_code==400
        with app.state.database.connection() as db:
            assert db.execute('SELECT consumed FROM payment_sms WHERE approval_id=?',(aid,)).fetchone()[0]==1
            assert db.execute("SELECT blocked_until FROM safety_circuits WHERE scope='transactions'").fetchone()[0]>time.time()
        assert balance(app.state.agent)==before


async def test_safety_state_is_isolated_between_users(settings):
    from test_accounts import sms_login
    settings.app_auth_enabled=True;settings.multi_user_accounts=True
    app=create_app(settings,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as one,httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as two:
        await register(one)
        await sms_login(two,'13900123456','另一个用户')
        await two.post('/api/auth/contacts-done',json={})
        await two.post('/api/auth/pin',json={'pin':'2468','repeat_pin':'2468'})
        conv=(await one.get('/api/bootstrap')).json()['conversation_id']
        for i in range(3): await one.post(f'/api/approvals/forged{i}/decision',json={'conversation_id':conv,'decision':'confirm'})
        assert (await one.get('/api/safety/status')).json()['scopes'][0]['requires_reauth']==1
        status=(await two.get('/api/safety/status')).json()
        assert status['scopes'][0]['requires_reauth']==0
        assert all(e['kind']=='pin_created' for e in status['events'])
