import asyncio
import sqlite3
import pytest
import httpx
from banking.admin_site import create_app as admin_app
from banking.main import create_app
from banking import human_service as human, account_lock
from banking.db import Database, add_message
from conftest import ScriptedModel

async def test_operator_full_history_chat_close_and_resume(settings):
    model=ScriptedModel(['AI恢复工作'])
    bank=create_app(settings,model);admin=admin_app(settings)
    async with bank.router.lifespan_context(bank),admin.router.lifespan_context(admin),httpx.AsyncClient(transport=httpx.ASGITransport(app=bank),base_url='http://127.0.0.1:8000') as user,httpx.AsyncClient(transport=httpx.ASGITransport(app=admin),base_url='http://127.0.0.1:8003') as operator:
        conv=(await user.get('/api/bootstrap')).json()['conversation_id']
        with bank.state.database.transaction() as db:
            add_message(db,conv,None,'user','text','之前的问题：余额是多少',{})
        assert (await operator.get('/api/admin/queue')).status_code==401
        assert (await operator.post('/api/admin/login',json={'password':'bad'})).status_code==401
        assert (await operator.post('/api/admin/login',json={'password':'000000'})).status_code==200
        assert (await user.post('/api/messages',json={'text':'转人工','conversation_id':conv})).status_code==202
        await bank.state.agent.queue.join()
        queue=(await operator.get('/api/admin/queue')).json()
        ticket=queue['sessions'][0];assert ticket['status']=='waiting'
        path=f"/api/admin/users/{ticket['user_id']}/sessions/{ticket['id']}"
        history=(await operator.get(path)).json()
        assert any(m['text']=='之前的问题：余额是多少' for m in history['messages'])
        assert (await operator.post(path+'/messages',json={'text':'未接入不能发'})).status_code==400
        assert (await operator.post(path+'/claim',json={})).json()['status']=='active'
        for text in ('你好，需要查一下','后续补充'):
            assert (await user.post('/api/messages',json={'text':text,'conversation_id':conv})).status_code==202
        await bank.state.agent.queue.join()
        assert model.requests==[]
        assert (await operator.post(path+'/messages',json={'text':'您好，我已看到您的问题。'})).status_code==200
        messages=(await user.get('/api/handoff',params={'conversation_id':conv})).json()['messages']
        assert messages[-1]['data']['source']=='human'
        assert messages[-1]['text']=='您好，我已看到您的问题。'
        assert (await operator.post(path+'/messages',json={'text':'   '})).status_code==400
        assert (await operator.post(path+'/close',json={})).json()['status']=='closed'
        assert (await operator.post(path+'/close',json={})).status_code==200
        assert (await operator.post(path+'/messages',json={'text':'不能再回复'})).status_code==400
        history=(await user.get('/api/handoff',params={'conversation_id':conv})).json()['messages']
        assert sum('handoff_rating' in m['data'] for m in history)==1
        assert (await user.post('/api/messages',json={'text':'查询余额','conversation_id':conv})).status_code==202
        await bank.state.agent.queue.join();assert len(model.requests)==1
        assert (await operator.post('/api/admin/logout',json={})).status_code==200
        assert (await operator.get(path)).status_code==401

async def test_admin_isolation_origin_and_persistence(settings):
    bank=create_app(settings,ScriptedModel());admin=admin_app(settings)
    async with bank.router.lifespan_context(bank),admin.router.lifespan_context(admin):
        database=bank.state.database
        with database.transaction() as db:
            conv=database.conversation_id(db);s=human.open_session(db,conv)
        other=Database(settings.database_path.parent/'other.sqlite3');other.initialize()
        with other.transaction() as db:
            conv2=other.conversation_id(db);s2=human.open_session(db,conv2)
        with sqlite3.connect(settings.database_path.parent/'accounts.sqlite3') as db:
            db.execute('CREATE TABLE users(id TEXT,username TEXT,phone TEXT,database_path TEXT)')
            db.executemany('INSERT INTO users VALUES(?,?,?,?)',[('a','用户甲','11111111111',str(settings.database_path)),('b','用户乙','13800123456',str(other.path))])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin),base_url='http://127.0.0.1:8003') as client:
            assert (await client.post('/api/admin/login',json={'password':'000000'},headers={'origin':'https://bad.example'})).status_code==403
            await client.post('/api/admin/login',json={'password':'000000'})
            assert len((await client.get('/api/admin/queue')).json()['sessions'])==2
            assert (await client.get(f"/api/admin/users/b/sessions/{s['id']}")).status_code==400
            assert (await client.post(f"/api/admin/users/a/sessions/{s2['id']}/claim",json={})).status_code==400
            assert (await client.get('/api/admin/users/missing/sessions/missing')).status_code==404
            recreated=admin_app(settings)
            async with recreated.router.lifespan_context(recreated),httpx.AsyncClient(transport=httpx.ASGITransport(app=recreated),base_url='http://127.0.0.1:8003',cookies=client.cookies) as after:
                assert len((await after.get('/api/admin/queue')).json()['sessions'])==2
            with database.transaction() as db:
                db.execute('UPDATE conversations SET deleted_at=? WHERE id=?',('deleted',conv))
            assert (await client.get(f"/api/admin/users/a/sessions/{s['id']}")).status_code==400

async def test_locked_account_can_chat_but_only_admin_reviews(settings):
    bank=create_app(settings,ScriptedModel());admin=admin_app(settings)
    async with bank.router.lifespan_context(bank),admin.router.lifespan_context(admin),httpx.AsyncClient(transport=httpx.ASGITransport(app=bank),base_url='http://127.0.0.1:8000') as user,httpx.AsyncClient(transport=httpx.ASGITransport(app=admin),base_url='http://127.0.0.1:8003') as operator:
        await user.get('/api/bootstrap')
        with bank.state.database.transaction() as db:
            for _ in range(3):account_lock.trip(db,'credential')
        assert (await user.post('/api/messages',json={'text':'查询余额'})).status_code==423
        response=await user.post('/api/safety/unlock-request',json={'note':'请审核我的锁定原因'})
        assert response.status_code==200 and response.json()['system_locked']
        assert (await user.post('/api/handoff/messages',json={'text':'我在等待审核'})).status_code==200
        await operator.post('/api/admin/login',json={'password':'000000'})
        queue=(await operator.get('/api/admin/queue')).json();ticket=queue['sessions'][0];review=queue['reviews'][0]
        path=f"/api/admin/users/{ticket['user_id']}/sessions/{ticket['id']}"
        await operator.post(path+'/claim',json={});await operator.post(path+'/messages',json={'text':'已经收到您的申请'})
        assert (await user.get('/api/handoff')).json()['messages'][-1]['text']=='已经收到您的申请'
        assert (await user.post('/api/admin/users/legacy/reviews/'+review['id'],json={'approve':True,'note':'自助解锁'})).status_code==423
        response=await operator.post('/api/admin/users/legacy/reviews/'+review['id'],json={'approve':True,'note':'人工核对完成'})
        assert response.status_code==200 and not response.json()['system_locked']
        assert not (await user.get('/api/safety/guard')).json()['system_locked']

async def test_admin_password_failures_persist(settings):
    app=admin_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8003') as client:
        for _ in range(5):assert (await client.post('/api/admin/login',json={'password':'wrong'})).status_code==401
        assert (await client.post('/api/admin/login',json={'password':'000000'})).status_code==429

async def test_real_multiuser_routes_and_locked_support_require_user_login(settings):
    from test_accounts import sms_login
    from banking.security import token_hash
    settings.multi_user_accounts=True;settings.app_auth_enabled=True
    bank=create_app(settings,ScriptedModel());admin=admin_app(settings)
    async with bank.router.lifespan_context(bank),admin.router.lifespan_context(admin),httpx.AsyncClient(transport=httpx.ASGITransport(app=bank),base_url='http://127.0.0.1:8000') as first,httpx.AsyncClient(transport=httpx.ASGITransport(app=bank),base_url='http://127.0.0.1:8000') as second,httpx.AsyncClient(transport=httpx.ASGITransport(app=admin),base_url='http://127.0.0.1:8003') as operator:
        for client,phone,name in ((first,'13800123456','用户甲'),(second,'13900123456','用户乙')):
            await sms_login(client,phone,name)
            await client.post('/api/auth/contacts-done',json={})
            assert (await client.post('/api/auth/pin',json={'pin':'2468','repeat_pin':'2468'})).status_code==200
        conv=(await first.get('/api/bootstrap')).json()['conversation_id']
        assert (await first.post('/api/messages',json={'text':'转人工','conversation_id':conv})).status_code==202
        assert (await second.get('/api/handoff',params={'conversation_id':conv})).status_code==404
        user=bank.state.directory.user_for(token_hash(first.cookies['bank_session']))
        tenant=await bank.state.directory.application(user)
        await tenant.state.agent.queue.join()
        with tenant.state.database.transaction() as db:
            for _ in range(3):account_lock.trip(db,'suspicious')
        assert (await first.post('/api/safety/unlock-request',json={'note':'请人工协助'})).status_code==200
        assert (await first.post('/api/handoff/messages',json={'text':'锁定后联系人工','conversation_id':conv})).status_code==200
        assert (await first.get('/api/bank')).status_code==423
        assert (await second.get('/api/bank')).status_code==200
        await operator.post('/api/admin/login',json={'password':'000000'})
        queue=(await operator.get('/api/admin/queue')).json();ticket=queue['sessions'][0]
        assert ticket['username']=='用户甲' and ticket['user_id']==user['id']
        path=f"/api/admin/users/{ticket['user_id']}/sessions/{ticket['id']}"
        await operator.post(path+'/claim',json={})
        assert (await operator.post(path+'/messages',json={'text':'人工正在核对'})).status_code==200
        assert (await first.get('/api/handoff',params={'conversation_id':conv})).json()['messages'][-1]['text']=='人工正在核对'
        await first.post('/api/auth/logout',json={})
        assert (await first.get('/api/handoff',params={'conversation_id':conv})).status_code==401
