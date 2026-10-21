import asyncio,json
from pathlib import Path

import httpx
import pytest

from banking.db import Database,add_message,dumps,message_dict,queue_outbox
from banking.feishu import card_for_message,OutboxSender
from banking.feishu_actions import handle
from banking.feishu_channels import recipient,enqueue
from banking.main import FeishuBody,create_app
from banking.security import encode_pin,token_hash
from banking import human_service
from conftest import ScriptedModel,tool
from test_accounts import sms_login
from test_feishu_payment import enable
from test_partial_completion import partial,report


def bind(db):
    db.execute("INSERT INTO feishu_binding VALUES(1,'tenant','owner','chat')")


def body(**kwargs):
    return FeishuBody(**({'event_id':'event','kind':'message','tenant_key':'tenant','open_id':'owner','chat_id':'chat','chat_type':'p2p','text':''}|kwargs))


async def test_multiuser_binding_routing_dedup_rebind_and_cross_account_callbacks(settings):
    settings.app_auth_enabled=True;settings.multi_user_accounts=True
    database=Database(settings.database_path);database.initialize()
    with database.transaction() as db:db.execute("INSERT INTO app_identity(id,username,phone,pin_hash,contacts_done) VALUES(1,'甲','11111111111',?,1)",(encode_pin('0000'),))
    app=create_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as first,httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as second:
        await sms_login(first,'11111111111');await sms_login(second,'13800123456','乙')
        await second.post('/api/auth/contacts-done',json={});await second.post('/api/auth/pin',json={'pin':'2468','repeat_pin':'2468'})
        enable(settings)
        for tenant in app.state.directory.apps.values():enable(tenant.state.agent.settings)
        auth={'Authorization':'Bearer '+settings.feishu_bridge_token}
        code1=(await first.post('/api/feishu/binding-code',json={})).json()['code']
        code2=(await second.post('/api/feishu/binding-code',json={})).json()['code']
        payload=body(text='绑定 '+code1,event_id='bind1').model_dump()
        response=await first.post('/api/internal/feishu',headers=auth,json=payload)
        assert '绑定成功' in response.json()['reply'],response.text
        assert (await first.post('/api/internal/feishu',headers=auth,json=payload)).json()['duplicate']
        stolen=await first.post('/api/internal/feishu',headers=auth,json={**payload,'event_id':'steal','text':'绑定 '+code2})
        assert '已关联其他' in stolen.json()['reply']
        other={**payload,'open_id':'other','chat_id':'other_chat','event_id':'bind2','text':'绑定 '+code2}
        assert '绑定成功' in (await second.post('/api/internal/feishu',headers=auth,json=other)).json()['reply']
        users=[]
        for client in [first,second]:
            user=app.state.directory.user_for(token_hash(client.cookies['bank_session']))
            tenant=await app.state.directory.application(user);users.append(tenant)
            tenant.state.agent.model=ScriptedModel([[tool('get_accounts')],'查询完毕。'])
        results=[]
        for payload0 in [payload,other]:
            response=await first.post('/api/internal/feishu',headers=auth,json={**payload0,'event_id':'query','text':'查询余额'})
            results.append(response.json())
        for tenant in users:await asyncio.wait_for(tenant.state.agent.queue.join(),5)
        assert results[0]['task_id']!=results[1]['task_id']
        for tenant,expected in zip(users,['owner','other']):
            with tenant.state.database.connection() as db:
                messages=[json.loads(r[0]) for r in db.execute('SELECT payload_json FROM feishu_outbox')]
                assert messages and all(m['_feishu_recipient']['open_id']==expected for m in messages)
        foreign={**other,'kind':'card','event_id':'forged','action':'stop','conversation_id':'foreign','task_id':results[0]['task_id']}
        assert '不属于' in (await second.post('/api/internal/feishu',headers=auth,json=foreign)).json()['reply']
        assert (await first.post('/api/feishu/preferences',json={'notifications':False})).status_code==200
        assert (await second.get('/api/feishu/preferences')).json()['notifications']
        assert (await first.post('/api/feishu/unbind',json={})).json()['bound'] is False
        assert '先在有数' in (await first.post('/api/internal/feishu',headers=auth,json={**payload,'text':'余额','event_id':'unbound'})).json()['reply']
        code=(await second.post('/api/feishu/binding-code',json={})).json()['code']
        assert '绑定成功' in (await first.post('/api/internal/feishu',headers=auth,json={**payload,'text':'绑定 '+code,'event_id':'switch'})).json()['reply']


async def test_human_two_way_rating_private_web_and_locked_contact(agent):
    with agent.db.transaction() as db:bind(db)
    result=await handle(agent,agent.db,body(text='转人工'))
    with agent.db.transaction() as db:
        service=dict(db.execute('SELECT * FROM human_sessions').fetchone());conv=service['conversation_id']
        human_service.claim(db,service['id'],'测试客服')
        reply=human_service.reply(db,service['id'],'这是一条人工回复。','测试客服')
        old_count=db.execute('SELECT COUNT(*) FROM feishu_outbox').fetchone()[0]
        add_message(db,conv,None,'assistant','text','普通网页私人回复',{})
        assert db.execute('SELECT COUNT(*) FROM feishu_outbox').fetchone()[0]==old_count
        assert db.execute('SELECT 1 FROM feishu_outbox WHERE message_id=?',(reply['id'],)).fetchone()
    await handle(agent,agent.db,body(text='用户补充信息',event_id='human-followup'))
    with agent.db.connection() as db:assert db.execute("SELECT 1 FROM messages WHERE text='用户补充信息'").fetchone()
    end=await handle(agent,agent.db,body(kind='card',text='',action='human_end',message_id=reply['id'],conversation_id=conv,event_id='end'))
    assert '已结束' in end['reply']
    with agent.db.connection() as db:
        rating=message_dict(db.execute("SELECT * FROM messages WHERE json_extract(data_json,'$.handoff_rating') IS NOT NULL").fetchone())
        assert db.execute('SELECT 1 FROM feishu_outbox WHERE message_id=?',(rating['id'],)).fetchone()
    card=card_for_message(rating)
    buttons=[a for element in card['elements'] for a in element.get('actions',[])]
    assert [a['value']['half_stars'] for a in buttons]==list(range(1,11))
    rated=await handle(agent,agent.db,body(kind='card',text='',action='rating',message_id=rating['id'],conversation_id=conv,half_stars=9,event_id='rating'))
    assert '4.5星' in rated['reply']
    with agent.db.transaction() as db:db.execute('UPDATE account_guard SET locked=1')
    assert '锁定' in (await handle(agent,agent.db,body(text='帮我转账',event_id='locked-business')))['reply']
    assert '人工' in (await handle(agent,agent.db,body(text='转人工',event_id='locked-human')))['reply']


async def test_running_stop_cancels_model_and_dedup_stops_money(agent):
    class Blocking:
        started=asyncio.Event();cancelled=False
        async def complete(self,messages):
            self.started.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:self.cancelled=True;raise
    model=Blocking();agent.model=model
    with agent.db.transaction() as db:bind(db)
    started=await handle(agent,agent.db,body(text='帮我转账',event_id='start'))
    await asyncio.wait_for(model.started.wait(),5)
    with agent.db.connection() as db:
        task=db.execute('SELECT * FROM tasks WHERE id=?',(started['task_id'],)).fetchone();conv=task['conversation_id']
        before=db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
    stopped_body=body(kind='card',action='stop',task_id=task['id'],conversation_id=conv,event_id='stop')
    response=await handle(agent,agent.db,stopped_body)
    assert '已停止' in response['reply']
    assert (await handle(agent,agent.db,stopped_body))['duplicate']
    await asyncio.wait_for(agent.queue.join(),5)
    assert model.cancelled
    with agent.db.connection() as db:
        assert db.execute('SELECT status FROM tasks WHERE id=?',(task['id'],)).fetchone()[0]=='failed'
        assert db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]==before
        progress=json.loads(db.execute('SELECT payload_json FROM feishu_outbox WHERE message_id=?',('fsprogress_'+task['id'],)).fetchone()[0])
    assert any(a['value']['action']=='resume' for e in card_for_message(progress)['elements'] for a in e.get('actions',[]))


@pytest.mark.parametrize('choice',['keep','replan','stale'])
async def test_partial_completion_callbacks_keep_or_replan_preserve_money(agent,choice):
    with agent.db.transaction() as db:bind(db)
    task,conv=await partial(agent);value=report(agent,task)
    with agent.db.transaction() as db:
        state=json.loads(db.execute('SELECT state_json FROM tasks WHERE id=?',(task,)).fetchone()[0]);state['feishu_recipient']=recipient(db)
        db.execute("UPDATE tasks SET channel='feishu',state_json=? WHERE id=?",(dumps(state),task))
        message=message_dict(db.execute("SELECT * FROM messages WHERE task_id=? AND json_extract(data_json,'$.tool')='partial_completion'",(task,)).fetchone());queue_outbox(db,message)
    before=agent.bank.snapshot()
    if choice=='replan':agent.model.responses=['剩余步骤请提供新的金额。']
    request=body(kind='card',action='partial',choice='keep' if choice=='stale' else choice,task_id=task,conversation_id=conv,version=999 if choice=='stale' else value['version'])
    response=await handle(agent,agent.db,request)
    assert agent.bank.snapshot()==before
    if choice=='stale':assert '变化' in response['reply']
    elif choice=='keep':assert report(agent,task)['decision']=='kept'
    else:
        await asyncio.wait_for(agent.queue.join(),5)
        with agent.db.connection() as db:assert db.execute('SELECT channel FROM tasks WHERE id=?',(response['task_id'],)).fetchone()[0]=='feishu'
    assert (await handle(agent,agent.db,request))['duplicate']


async def test_proactive_notifications_focus_preference_and_unbind_generation(agent):
    with agent.db.transaction() as db:
        bind(db);conv=agent.db.create_conversation(db)
        message=add_message(db,conv,None,'assistant','text','特别关心生日提醒',{'_feishu_notify':True})
        assert db.execute('SELECT 1 FROM feishu_outbox WHERE message_id=?',(message['id'],)).fetchone()
        db.execute("INSERT INTO meta VALUES('feishu_notifications','0')")
        hidden=add_message(db,conv,None,'assistant','text','已关闭通知',{'_feishu_notify':True})
        assert not db.execute('SELECT 1 FROM feishu_outbox WHERE message_id=?',(hidden['id'],)).fetchone()
    request=body(kind='card',action='focus',message_id=message['id'],conversation_id=conv)
    assert '加入“飞书”对话' in (await handle(agent,agent.db,request))['reply']
    with agent.db.connection() as db:
        chat=db.execute("SELECT value FROM meta WHERE key='feishu_chat_conversation'").fetchone()[0]
        assert db.execute('SELECT COUNT(*) FROM messages WHERE conversation_id=? AND text LIKE ?',(chat,'%你在飞书选择继续处理这条通知%')).fetchone()[0]==1
    with agent.db.connection() as db:assert db.execute("SELECT value FROM meta WHERE key='feishu_conversation'").fetchone()[0]==conv
    with agent.db.transaction() as db:db.execute("INSERT INTO meta VALUES('feishu_binding_nonce','new-generation')")
    assert '失效' in (await handle(agent,agent.db,request.model_copy(update={'event_id':'new-event'})))['reply']


async def test_sender_mock_api_delivers_correct_chat_and_rejects_old_recipient(database,settings,monkeypatch):
    requests=[];sent=asyncio.Event()
    with database.transaction() as db:
        bind(db);conv=database.conversation_id(db)
        first=add_message(db,conv,None,'assistant','text','旧绑定消息',{'_feishu_notify':True})
        db.execute("INSERT INTO meta VALUES('feishu_binding_nonce','new')")
        second=add_message(db,conv,None,'assistant','text','新绑定消息',{'_feishu_notify':True})
    database.initialize()  # Pending outbox survives backend restart.
    def respond(request):
        requests.append(request)
        if '/auth/' in str(request.url):return httpx.Response(200,json={'code':0,'tenant_access_token':'test-token','expire':7200})
        payload=json.loads(request.content);assert payload['receive_id']=='chat'
        assert '新绑定消息' in payload['content'] and '旧绑定消息' not in payload['content']
        sent.set();return httpx.Response(200,json={'code':0,'data':{'message_id':'external'}})
    original=httpx.AsyncClient
    monkeypatch.setattr('banking.feishu.httpx.AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(respond),**kwargs))
    worker=asyncio.create_task(OutboxSender(database,settings,asyncio.Condition()).run())
    try:await asyncio.wait_for(sent.wait(),5)
    finally:worker.cancel();await asyncio.gather(worker,return_exceptions=True)
    with database.connection() as db:
        assert db.execute('SELECT status FROM feishu_outbox WHERE message_id=?',(first['id'],)).fetchone()[0]=='failed'
        assert db.execute('SELECT status FROM feishu_outbox WHERE message_id=?',(second['id'],)).fetchone()[0]=='sent'
    assert len(requests)==2


async def test_real_daily_report_enqueues_only_once_with_complete_summary(database,settings,monkeypatch):
    from test_daily_reports import seed,prices,AT,reports
    seed(database)
    with database.transaction() as db:bind(db)
    monkeypatch.setattr(reports,'stock_values',prices)
    result=await reports.generate_due(database,settings,AT)
    assert result
    assert await reports.generate_due(database,settings,AT) is None
    with database.connection() as db:
        entries=[json.loads(r[0]) for r in db.execute('SELECT payload_json FROM feishu_outbox')]
    assert len(entries)==1
    card=card_for_message(entries[0]);text=dumps(card)
    assert '昨日支出' in text and '数据截至' in text
    assert any(a['value']['action']=='focus' for e in card['elements'] for a in e.get('actions',[]))


async def test_partial_travel_refund_in_feishu_creates_verified_payment_only(settings,tmp_path):
    from test_travel import setup,client,connected,quote_for
    from test_partial_completion import travel_partial
    from banking.feishu_payment import payment_link
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web);quote=await quote_for(app,conv)
        task,value=await travel_partial(app,bank,conv,quote);item=value['completed'][0]
        enable(settings)
        with app.state.database.transaction() as db:
            bind(db);state=json.loads(db.execute('SELECT state_json FROM tasks WHERE id=?',(task,)).fetchone()[0]);state['feishu_recipient']=recipient(db)
            db.execute("UPDATE tasks SET channel='feishu',state_json=? WHERE id=?",(dumps(state),task))
        before=app.state.agent.bank.snapshot()
        result=await handle(app.state.agent,app.state.database,body(kind='card',action='partial',choice='cancel_order',task_id=task,conversation_id=conv,version=value['version'],order_id=item['order_id']))
        assert 'message' in result,result
        approval=result['message']['data']['approval_id']
        assert app.state.agent.bank.snapshot()==before
        with app.state.database.connection() as db:
            assert payment_link(db,approval,settings)
            assert db.execute('SELECT channel FROM tasks WHERE id=(SELECT task_id FROM approvals WHERE id=?)',(approval,)).fetchone()[0]=='feishu'
        assert (await bank.post('/api/approvals/'+approval+'/decision',json={'decision':'confirm','conversation_id':conv})).status_code==428
        assert app.state.agent.bank.snapshot()==before


async def test_locked_http_bridge_allows_only_bound_human_contact(settings):
    from test_security import register
    settings.app_auth_enabled=True
    app=create_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client);enable(settings)
        with app.state.database.transaction() as db:bind(db);db.execute('UPDATE account_guard SET locked=1')
        auth={'Authorization':'Bearer '+settings.feishu_bridge_token}
        response=await client.post('/api/internal/feishu',headers=auth,json=body(text='转人工').model_dump())
        assert response.status_code==200 and '人工' in response.json()['reply']
        blocked=await client.post('/api/internal/feishu',headers=auth,json=body(text='转账100元',event_id='blocked').model_dump())
        # Messages are handed to the existing human service, never to banking tools.
        assert blocked.status_code==200
        forged=await client.post('/api/internal/feishu',headers=auth,json=body(kind='card',action='resume',task_id='foreign',conversation_id='foreign',event_id='forged').model_dump())
        assert '锁定' in forged.json()['reply']
        assert (await client.post('/api/internal/feishu',json=body(text='转人工').model_dump())).status_code==403
