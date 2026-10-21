import json
import time
from pathlib import Path

import httpx
import pytest
from banking import account_lock, safety
from banking.bank import BankError
from banking.db import Database
from banking.main import create_app
from conftest import ScriptedModel, ask, balance, pending, state, tool
from test_security import register


def episode(db,kind):
    safety.success(db,kind)
    for _ in range(3 if kind=='suspicious' else 5): safety.failure(db,kind)


def test_full_episodes_are_separate_persistent_and_do_not_reset_with_cooldown(database):
    with database.transaction() as db:
        episode(db,'pin')
        for _ in range(5): safety.failure(db,'pin')
        assert account_lock.status(db)['credential_trips']==1
        episode(db,'suspicious')
        assert not account_lock.locked(db)
        # Restoring the ordinary gate must not clear escalation history.
        db.execute("UPDATE safety_circuits SET requires_reauth=0,blocked_until=? WHERE scope='transactions'",(time.time()-1,))
        safety.expire_user(db)
        episode(db,'sms')
        assert account_lock.status(db)['credential_trips']==2
        assert not account_lock.locked(db)
        episode(db,'pin')
        assert account_lock.locked(db)
    with Database(database.path).connection() as db:
        assert account_lock.status(db)['credential_trips']==3
        with pytest.raises(BankError,match='仅可申请人工解锁'): safety.check_user(db)


async def test_locked_system_exposes_only_manual_requests_no_sms_or_reset_bypass(settings):
    settings.app_auth_enabled=True
    app=create_app(settings,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        await register(c)
        conv=(await c.get('/api/bootstrap')).json()['conversation_id']
        task=await ask(app.state.agent,[[tool('transfer_own_funds',target_account_id='savings',amount_yuan='100')]],'从日常账户转100元到储蓄账户')
        approval=pending(app.state.agent)['id'];before=balance(app.state.agent)
        with app.state.database.transaction() as db:
            for _ in range(3): episode(db,'suspicious')
        assert state(app.state.agent,task)['status']=='failed'
        for path,body in [('/bank',None),('/bootstrap',None),('/contacts',None),('/safety/status',None),('/safety/recovery-sms',{}),('/safety/recover',{'code':'000000'}),('/sandbox/reset',{'confirmation':'RESET'}),('/messages',{'conversation_id':conv,'text':'继续'}),('/auth/unlock',{'pin':'2468'})]:
            result=await (c.get('/api'+path) if body is None else c.post('/api'+path,json=body))
            assert result.status_code==423,(path,result.text)
        status=(await c.get('/api/auth/status')).json()
        assert status['system_locked'] and not status['ready']
        request=(await c.post('/api/safety/unlock-request',json={'note':'误操作，申请人工核验'})).json()
        again=(await c.post('/api/safety/unlock-request',json={'note':'再次提交'})).json()
        assert request['request']['id']==again['request']['id']
        assert request['system_locked'] and request['request']['status']=='pending'
        # No public HTTP approval or user-facing self-unlock endpoint exists.
        assert (await c.post('/api/safety/unlock-request/approve',json={})).status_code==423
        assert balance(app.state.agent)==before
        with app.state.database.connection() as db:
            assert db.execute('SELECT status FROM approvals WHERE id=?',(approval,)).fetchone()[0]=='failed'
        # A local human decision is required; rejection does not unlock.
        account_lock.review(app.state.database,request['request']['id'],False,'测试审核员','资料待核实')
        assert (await c.get('/api/safety/guard')).json()['system_locked']
        new=(await c.post('/api/safety/unlock-request',json={'note':'补充申请'})).json()
        account_lock.review(app.state.database,new['request']['id'],True,'测试审核员','已人工核实身份')
        assert not (await c.get('/api/safety/guard')).json()['system_locked']
        assert (await c.post('/api/auth/unlock',json={'pin':'2468'})).status_code==200
        assert (await c.get('/api/bank')).status_code==200
        assert balance(app.state.agent)==before


async def test_locked_account_cannot_run_agent_or_linked_portfolio(agent):
    with agent.db.transaction() as db:
        for _ in range(3): episode(db,'pin')
    with pytest.raises(BankError,match='仅可申请人工解锁'): await agent.submit('查询账户')
    from banking.securities_bridge import portfolio
    from banking.food_bridge import orders
    with agent.db.connection() as db:
        with pytest.raises(BankError): portfolio(db)
        with pytest.raises(BankError): orders(db)


def test_repeated_suspicious_errors_in_same_window_are_one_episode(database):
    with database.transaction() as db:
        for _ in range(20): safety.failure(db,'suspicious')
        assert account_lock.status(db)['suspicious_trips']==1
        assert not account_lock.locked(db)


async def test_third_real_pin_episode_locks_on_fifteenth_wrong_pin_and_survives_restart(settings):
    settings.app_auth_enabled=True
    app=create_app(settings,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        await register(c)
        for episode_no in range(3):
            for _ in range(5):
                response=await c.post('/api/auth/unlock',json={'pin':'0000'})
                assert response.status_code==400
            if episode_no<2:
                with app.state.database.transaction() as db:
                    db.execute("UPDATE safety_circuits SET blocked_until=? WHERE scope='transactions'",(time.time()-1,))
                await c.get('/api/safety/status')
        assert response.headers['X-System-Locked']=='1'
        with Database(app.state.database.path).connection() as db:
            assert account_lock.status(db)['credential_trips']==3 and account_lock.locked(db)
        assert (await c.post('/api/safety/recover',json={'code':'000000'})).status_code==423
        with app.state.database.transaction() as db:db.execute('UPDATE sms_challenges SET sent_at=0')
        sms=(await c.post('/api/auth/sms',json={'phone':'13800123456','purpose':'login'})).json()['demo_code']
        response=await c.post('/api/auth/login',json={'phone':'13800123456','purpose':'login','code':sms})
        assert response.json()['system_locked'] and not response.json()['ready']


async def test_hard_lock_is_per_user_not_all_users(settings):
    from test_accounts import sms_login
    settings.app_auth_enabled=True;settings.multi_user_accounts=True
    app=create_app(settings,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as one,httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as two:
        await register(one)
        await sms_login(two,'13900123456','第二位用户')
        await two.post('/api/auth/contacts-done',json={});await two.post('/api/auth/pin',json={'pin':'2468','repeat_pin':'2468'})
        conv=(await one.get('/api/bootstrap')).json()['conversation_id']
        # Complete and recover two suspicious protection episodes, then trip third.
        for number in range(3):
            for i in range(3):await one.post(f'/api/approvals/missing{number}{i}/decision',json={'conversation_id':conv,'decision':'confirm'})
            if number<2:
                # Use trusted fixture access to model an ordinary completed recovery.
                with app.state.directory.connection() as db: user=dict(db.execute("SELECT * FROM users WHERE phone='13800123456'").fetchone())
                with Database(Path(user['database_path'])).transaction() as db:
                    db.execute("UPDATE safety_circuits SET requires_reauth=0,blocked_until=0 WHERE scope='transactions'")
                    db.execute('DELETE FROM safety_counters')
        assert (await one.get('/api/bank')).status_code==423
        assert (await one.get('/api/safety/guard')).json()['system_locked']
        assert (await two.get('/api/bank')).status_code==200
        assert not (await two.get('/api/safety/guard')).json()['system_locked']
