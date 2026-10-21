import json,time
import httpx,pytest
from banking.main import create_app
from banking.bank import BankError
from banking.security import pin_matches,token_hash
from conftest import ScriptedModel,tool
from test_security import register

async def test_pin_change_requires_old_pin_sms_binding_and_invalidates_other_sessions(settings):
 settings.app_auth_enabled=True;app=create_app(settings,ScriptedModel())
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
  await register(c);body={'pin':'1234','repeat_pin':'1234','current_pin':'2468'}
  assert (await c.post('/api/auth/pin',json=body)).status_code==400
  assert (await c.post('/api/auth/pin/sms',json={**body,'current_pin':'0000'})).status_code==400
  sms=await c.post('/api/auth/pin/sms',json=body);assert sms.status_code==200,sms.text
  code=sms.json()['demo_code']
  async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as other:
   await other.post('/api/auth/open',json={});await other.post('/api/auth/unlock',json={'pin':'2468'})
   assert (await other.post('/api/auth/pin',json={**body,'sms_code':code})).status_code==400
  assert (await c.post('/api/auth/pin/sms',json=body)).status_code==400
  assert (await c.post('/api/auth/pin',json={**body,'pin':'4321','repeat_pin':'4321','sms_code':code})).status_code==400
  assert (await c.post('/api/auth/pin',json={**body,'sms_code':'999999' if code!='999999' else '000000'})).status_code==400
  with app.state.database.transaction() as db:db.execute("INSERT INTO app_sessions VALUES('other',1,1)")
  result=await c.post('/api/auth/pin',json={**body,'sms_code':code});assert result.status_code==200 and result.json()['ready'],result.text
  with app.state.database.connection() as db:
   assert pin_matches('1234',db.execute('SELECT pin_hash FROM app_identity').fetchone()[0])
   assert tuple(db.execute("SELECT authenticated,unlocked FROM app_sessions WHERE token_hash='other'").fetchone())==(0,0)
   assert not db.execute('SELECT 1 FROM pin_change_sms').fetchone()
   assert db.execute("SELECT 1 FROM safety_events WHERE kind='pin_changed'").fetchone()
  assert (await c.post('/api/auth/pin',json={**body,'current_pin':'1234','sms_code':code})).status_code==400

async def test_pin_change_sms_expiry_and_five_errors_trip_existing_circuit(settings):
 settings.app_auth_enabled=True;app=create_app(settings,ScriptedModel())
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
  await register(c);body={'pin':'1234','repeat_pin':'1234','current_pin':'2468'}
  code=(await c.post('/api/auth/pin/sms',json=body)).json()['demo_code']
  with app.state.database.transaction() as db:db.execute('UPDATE pin_change_sms SET expires=0')
  assert (await c.post('/api/auth/pin',json={**body,'sms_code':code})).status_code==400
  with app.state.database.transaction() as db:db.execute('UPDATE pin_change_sms SET expires=?',(time.time()+300,))
  for _ in range(5):assert (await c.post('/api/auth/pin',json={**body,'sms_code':'999999' if code!='999999' else '000000'})).status_code==400
  assert (await c.post('/api/auth/pin',json={**body,'sms_code':code})).status_code==400
  with app.state.database.connection() as db:assert pin_matches('2468',db.execute('SELECT pin_hash FROM app_identity').fetchone()[0])

@pytest.mark.parametrize('name,args',[('list_subscriptions',{}),('quote_subscriptions',{'services':['office'],'months':24})])
async def test_subscription_queries_execute_without_confirmation(settings,name,args):
 settings.app_auth_enabled=True;model=ScriptedModel([[tool(name,**args)],'查询已完成。']);app=create_app(settings,model)
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
  await register(c);boot=(await c.get('/api/bootstrap')).json();conv=boot['conversation_id']
  assert len(boot['bank']['subscriptions'])==6
  with app.state.database.connection() as db:before=[tuple(r) for r in db.execute('SELECT * FROM subscriptions')]
  assert app.state.agent.bank.query(name,args)
  task=await app.state.agent.submit('我有哪些订阅' if name=='list_subscriptions' else '轻办公会员订阅两年多少钱',conversation_id=conv)
  await app.state.agent.queue.join()
  with app.state.database.connection() as db:
   assert not db.execute('SELECT 1 FROM approvals WHERE task_id=?',(task,)).fetchone()
   assert db.execute("SELECT 1 FROM messages WHERE task_id=? AND kind='data'",(task,)).fetchone()
   assert [tuple(r) for r in db.execute('SELECT * FROM subscriptions')]==before

async def test_panel_subscription_query_is_green_but_cancellation_requires_confirmation(settings):
 settings.app_auth_enabled=True;app=create_app(settings,ScriptedModel())
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
  await register(c);conv=(await c.get('/api/bootstrap')).json()['conversation_id']
  result=await c.post('/api/sandbox/inspect',json={'tool':'list_subscriptions','conversation_id':conv})
  assert result.status_code==200 and result.json()['kind']=='data'
  assert result.json()['data']['result']['active_monthly_minor']==17900
  result=await c.post('/api/subscriptions/office/cancel',json={'conversation_id':conv})
  assert result.status_code==200 and result.json()['kind']=='approval'
  with app.state.database.connection() as db:
   assert db.execute("SELECT status FROM subscriptions WHERE id='office'").fetchone()[0]=='active'
