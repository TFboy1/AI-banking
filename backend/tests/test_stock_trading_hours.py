import json
from datetime import datetime,timedelta
import httpx
import pytest
from banking.db import TZ,dumps,now
from banking.main import create_app
from banking.stock_trading import status
from banking.bank import BankError,CAPABILITIES
from banking import scheduled_transfers as scheduled
from conftest import ScriptedModel
from test_security import register
from test_scheduled_actions import approval,seed_quotes
from test_scheduled_transfers import funds

@pytest.mark.parametrize('value,opened',[
 ('2026-10-16T09:29:59',False),('2026-10-16T09:30:00',True),
 ('2026-10-16T11:29:59',True),('2026-10-16T11:30:00',False),
 ('2026-10-16T12:59:59',False),('2026-10-16T13:00:00',True),
 ('2026-10-16T14:59:59',True),('2026-10-16T15:00:00',False),
 ('2026-10-17T10:00:00',False),('2026-10-18T10:00:00',False),
 ('2026-10-01T10:00:00',False),('2026-02-23T10:00:00',False),
 ('2027-01-04T10:00:00',False),('2026-10-16T01:30:00+00:00',True)])
def test_session_boundaries_and_calendar(value,opened):
 assert status(datetime.fromisoformat(value))['open']==opened

@pytest.mark.parametrize('name',['purchase_stock_basket','sell_stock_basket'])
async def test_closed_confirm_and_direct_executor_do_not_move_money(settings,monkeypatch,name):
 settings.app_auth_enabled=True;settings.scheduled_worker_enabled=False
 app=create_app(settings,ScriptedModel())
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
  await register(client);conv,key=await approval(app,client,name,{'quote_id':'stockquote','account_id':'daily'})
  before=funds(app.state.database)
  sms=(await client.post(f'/api/approvals/{key}/sms',json={'conversation_id':conv,'account_id':'daily'})).json()['demo_code']
  monkeypatch.setattr('banking.stock_trading.clock',lambda:datetime(2026,10,16,15,0,tzinfo=TZ))
  result=await client.post(f'/api/approvals/{key}/decision',json={'decision':'confirm','conversation_id':conv,'pin':'2468','sms_code':sms,'account_id':'daily'})
  assert result.status_code==200 and result.json()['status']=='failed',result.text
  assert '已收盘' in result.json()['error']
  with app.state.database.transaction() as db:
   with pytest.raises(BankError,match='已收盘'):app.state.agent.bank.execute(db,name,{'quote_id':'stockquote','account_id':'daily'},'op_forbidden')
   assert db.execute('SELECT COUNT(*) FROM securities_orders').fetchone()[0]==0
  assert funds(app.state.database)==before

async def test_buy_no_longer_offers_or_accepts_timing_and_old_appointments_stop(settings):
 settings.app_auth_enabled=True;settings.scheduled_worker_enabled=False
 app=create_app(settings,ScriptedModel())
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
  await register(client);conv,key=await approval(app,client,'purchase_stock_basket',{'quote_id':'stockquote'})
  context=(await client.get(f'/api/approvals/{key}/payment',params={'conversation_id':conv})).json()
  assert context['schedule_allowed'] is False
  assert 'execute_at' not in CAPABILITIES['purchase_stock_basket'].input_model.model_fields
  due=(scheduled.clock()+timedelta(hours=1)).isoformat()
  result=await client.post(f'/api/approvals/{key}/sms',json={'conversation_id':conv,'account_id':'daily','execute_at':due})
  assert result.status_code==400 and '不能' in result.text
  before=funds(app.state.database)
  # Isolated fixture of an authorization saved by the previous version.
  with app.state.database.transaction() as db:
   from banking.scheduled_actions import snapshot
   source,target=snapshot(app.state.agent.bank,db,'purchase_stock_basket',{'quote_id':'stockquote','account_id':'daily'})
   row={'id':'schedule_legacy','conversation_id':conv,'authorization_id':key,'transfer_tool':'purchase_stock_basket',
        'args_json':dumps({'quote_id':'stockquote','account_id':'daily'}),'source_json':dumps(source),'target_json':dumps(target),
        'security_stamp':scheduled.stamp(db),'execute_at':due,'note':''}
   row['authorization_hash']=scheduled.seal(row)
   columns=list(row)
   db.execute('INSERT INTO scheduled_transfers('+','.join(columns)+',status,authorized_at) VALUES('+','.join('?' for _ in columns)+',?,?)',tuple(row[k] for k in columns)+('scheduled',now()))
   db.execute("UPDATE approvals SET status='executed',receipt_json=? WHERE id=?",(dumps({'schedule':{'id':row['id']}}),key))
  assert scheduled.process_due(app.state.database,settings=settings)==1
  with app.state.database.connection() as db:
   row=db.execute('SELECT * FROM scheduled_transfers').fetchone()
   assert row['status']=='needs_reauthorization' and '取消定时' in row['error'] and not row['execution_id']
  assert funds(app.state.database)==before

async def test_independent_platform_rejects_trade_quotes_but_allows_status_and_catalog(tmp_path,monkeypatch):
 from banking.stock_site import create_stock_app
 from banking.securities_common import secret
 app=create_stock_app(tmp_path/'market.sqlite3',broker_path=tmp_path/'broker.sqlite3',key_path=tmp_path/'key')
 monkeypatch.setattr('banking.stock_trading.clock',lambda:datetime(2026,10,17,10,tzinfo=TZ))
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8001') as client:
  assert (await client.get('/api/securities/trading-status')).json()['reason']=='周末休市'
  assert (await client.get('/api/stocks/catalog')).status_code==200
  for tail in ('quote','sale-quote'):
   response=await client.post('/internal/securities/test/'+tail,json={'orders':[{'symbol':'000001','shares':100}]},headers={'x-securities-key':secret(tmp_path/'key')})
   assert response.status_code==400 and '周末休市' in response.text

async def test_future_buy_intent_is_not_converted_to_immediate_purchase(settings):
 import asyncio
 from conftest import tool
 settings.app_auth_enabled=True;settings.scheduled_worker_enabled=False
 app=create_app(settings,ScriptedModel([[tool('purchase_stock_basket',quote_id='stockquote')]]))
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
  await register(client);conv=(await client.get('/api/bootstrap')).json()['conversation_id']
  with app.state.database.transaction() as db:seed_quotes(db,conv,'purchase_stock_basket')
  await client.post('/api/messages',json={'conversation_id':conv,'text':'帮我预约明天十点买入100股平安银行'})
  await asyncio.wait_for(app.state.agent.queue.join(),3)
  with app.state.database.connection() as db:
   assert db.execute('SELECT COUNT(*) FROM approvals').fetchone()[0]==0
   assert db.execute('SELECT COUNT(*) FROM securities_orders').fetchone()[0]==0
   assert '不支持定时' in db.execute("SELECT text FROM messages WHERE role='assistant' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
