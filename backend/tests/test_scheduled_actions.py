import copy
import json
import time
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from banking.main import create_app
from banking import scheduled_transfers as scheduled
from banking.db import dumps
from banking.securities_common import owner as stock_owner
from banking.food_common import owner as food_owner
from conftest import ScriptedModel
from test_security import register
from test_scheduled_transfers import funds

@pytest.fixture(autouse=True)
def scheduled_market_clock(monkeypatch,stock_session_clock):
 from banking.stock_trading import clock
 monkeypatch.setattr(scheduled,'clock',clock)
 monkeypatch.setattr('banking.scheduled_actions.clock',clock)

CASES=[
 ('transfer',{'recipient':'zhang_san','amount_yuan':'100'},-10000),
 ('transfer_own_funds',{'account_id':'daily','target_account_id':'savings','amount_yuan':'100'},-10000),
 ('purchase_product',{'product_id':'cash','amount_yuan':'100'},-10000),
 ('redeem_product',{'product_id':'cash','units':'100','account_id':'daily'},10000),
 ('sell_stock_basket',{'quote_id':'stockquote','account_id':'daily'},100000),
 ('place_food_order',{'quote_id':'foodquote'},-2500),
 ('simulate_expense',{'merchant':'演示商户','amount_yuan':'100'},-10000),
 ('save_to_goal',{'total_yuan':'100'},-10000),
 ('add_bank_card',{'bank_name':'测试银行','card_number':'6222000012345678'},0),
]

def seed_quotes(db,conv,name):
 db.execute("INSERT OR REPLACE INTO risk_profile VALUES(1,'{}',4)")
 db.execute("INSERT OR REPLACE INTO holdings VALUES('cash','2000.0000')")
 if name in {'purchase_stock_basket','sell_stock_basket'}:
  value={'quote_id':'stockquote','owner':stock_owner(db),'amount_minor':100000,'side':'sell' if name=='sell_stock_basket' else 'buy',
   'items':[{'symbol':'000001','name':'平安银行','shares':100,'price_minor':1000,'amount_minor':100000,'quote_date':'2026-10-16'}],
   'pricing_mode':'演示开收盘价格','expires':time.time()+300}
  db.execute('INSERT INTO securities_quotes VALUES(?,?,?,?)',('stockquote',conv,dumps(value),value['expires']))
  if name=='sell_stock_basket':db.execute("INSERT INTO securities_positions VALUES('000001','平安银行',200,200000)")
 elif name=='place_food_order':
  value={'quote_id':'foodquote','owner':food_owner(db),'shop_id':'shop','shop_name':'演示餐厅','amount_minor':2500,
   'items':[{'id':'dish','name':'演示午饭','quantity':1,'price_minor':2000,'amount_minor':2000,'packing_minor':100}],
   'quantity':1,'subtotal_minor':2000,'packing_minor':100,'delivery_minor':400,'place':{'id':'place','address':'演示地址'},'note':'','expires':time.time()+300}
  db.execute('INSERT INTO food_quotes VALUES(?,?,?,?)',('foodquote',conv,dumps(value),value['expires']))

async def approval(app,client,name,args,**timing):
 conv=(await client.get('/api/bootstrap')).json()['conversation_id']
 with app.state.database.transaction() as db:
  seed_quotes(db,conv,name)
  result=app.state.agent.create_direct_approval(db,conv,name,args)
 return conv,result['data']['approval_id']

async def consent(client,conv,key,due,**changes):
 path=f'/api/approvals/{key}'
 body={'conversation_id':conv,'decision':'confirm','pin':'2468','account_id':'daily','execute_at':due,**changes}
 context=(await client.get(path+'/payment',params={'conversation_id':conv})).json()
 if context['sms_required'] or (due and context['tool'] in {'transfer','transfer_own_funds','save_to_goal','schedule_transfer'}):
  sms=await client.post(path+'/sms',json={k:body[k] for k in ('conversation_id','account_id','execute_at')})
  assert sms.status_code==200,sms.text
  body['sms_code']=sms.json()['demo_code']
 result=await client.post(path+'/decision',json=body)
 assert result.status_code==200,result.text
 assert result.json()['status']=='executed',result.text
 return result.json()['receipt']

def mock_platform(monkeypatch,dbpath,changed=False):
 real=httpx.Client
 def response(request):
  from banking.db import Database
  body=json.loads(request.content)
  food='/food/' in str(request.url)
  with Database(dbpath).connection() as db:
   row=db.execute('SELECT payload FROM '+('food_quotes' if food else 'securities_quotes')).fetchone()
  value=json.loads(row[0]);value['expires']=time.time()+300
  if changed:value['amount_minor']+=1
  assert body.get('orders') or body.get('items')
  return httpx.Response(200,json=value)
 monkeypatch.setattr(httpx,'Client',lambda **kw:real(**kw,transport=httpx.MockTransport(response)))

@pytest.mark.parametrize('name,args,delta',CASES)
async def test_every_transaction_can_be_authorized_then_executes_once(settings,monkeypatch,name,args,delta):
 settings.app_auth_enabled=True;settings.scheduled_worker_enabled=False
 app=create_app(settings,ScriptedModel())
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
  await register(client)
  conv,key=await approval(app,client,name,args)
  before=funds(app.state.database)
  due=(scheduled.clock()+timedelta(minutes=2)).isoformat()
  receipt=await consent(client,conv,key,due)
  assert receipt['scheduled'] and receipt['schedule']['generic']
  assert funds(app.state.database)==before
  from banking.audit_view import detail
  with app.state.database.connection() as db:
   operation=next(r for r in detail(db,{'id':'test','username':'测试'},conv)['operations'] if r['id']==key)
   assert operation['debit_minor']==operation['credit_minor']==0
   assert '尚未执行' in operation['settlement']
  assert scheduled.process_due(app.state.database,settings=settings)==0
  # Platform quotes expire; the worker must genuinely requery before execution.
  if 'quote_id' in args:
   with app.state.database.transaction() as db:db.execute('UPDATE '+('food_quotes' if name=='place_food_order' else 'securities_quotes')+' SET expires=0')
   mock_platform(monkeypatch,settings.database_path)
  instant=scheduled.when(due)+timedelta(seconds=1)
  with ThreadPoolExecutor(max_workers=2) as pool:
   assert sum(pool.map(lambda _:scheduled.process_due(app.state.database,instant,settings),range(2)))==1
  with app.state.database.connection() as db:
   row=db.execute('SELECT * FROM scheduled_transfers').fetchone()
   assert row['status']=='executed',row['error']
   assert row['execution_id']!=key
   if name=='add_bank_card':assert db.execute("SELECT COUNT(*) FROM cards WHERE name='测试银行'").fetchone()[0]==1
  assert funds(app.state.database)['daily']==before['daily']+delta
  assert scheduled.process_due(app.state.database,instant,settings)==0

async def test_sms_binds_timing_and_immediate_still_works(settings):
 settings.app_auth_enabled=True;settings.scheduled_worker_enabled=False
 app=create_app(settings,ScriptedModel())
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
  await register(client);conv,key=await approval(app,client,'transfer',{'recipient':'zhang_san','amount_yuan':'1001'})
  due=(scheduled.clock()+timedelta(minutes=2)).isoformat()
  sms=(await client.post(f'/api/approvals/{key}/sms',json={'conversation_id':conv,'account_id':'daily','execute_at':due})).json()
  body={'conversation_id':conv,'decision':'confirm','pin':'2468','account_id':'daily','sms_code':sms['demo_code'],'execute_at':''}
  before=funds(app.state.database)
  result=await client.post(f'/api/approvals/{key}/decision',json=body)
  assert result.status_code==400 and '参数' in result.text
  body['execute_at']=due
  result=await client.post(f'/api/approvals/{key}/decision',json=body)
  assert result.status_code==200 and funds(app.state.database)==before
  # A separate ordinary immediate transfer remains available by default.
  conv,key=await approval(app,client,'transfer',{'recipient':'zhang_san','amount_yuan':'10'})
  result=await consent(client,conv,key,'')
  assert not result.get('scheduled') and funds(app.state.database)['daily']==before['daily']-1000

@pytest.mark.parametrize('name,args,delta',[CASES[2],CASES[4],CASES[5]])
async def test_changed_price_stops_without_funds_and_no_retry(settings,monkeypatch,name,args,delta):
 settings.app_auth_enabled=True;settings.scheduled_worker_enabled=False
 app=create_app(settings,ScriptedModel())
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
  await register(client);conv,key=await approval(app,client,name,args)
  due=(scheduled.clock()+timedelta(minutes=2)).isoformat();await consent(client,conv,key,due)
  before=funds(app.state.database)
  if name=='purchase_product':
   with app.state.database.transaction() as db:db.execute("UPDATE products SET nav='1.0100' WHERE id='cash'")
   monkeypatch.setattr('banking.market.sync_prices',lambda db:None)
  else:mock_platform(monkeypatch,settings.database_path,changed=True)
  assert scheduled.process_due(app.state.database,scheduled.when(due)+timedelta(seconds=1),settings)==1
  with app.state.database.connection() as db:
   row=db.execute('SELECT * FROM scheduled_transfers').fetchone();assert row['status']!='executed' and row['error']
  assert funds(app.state.database)==before
  assert scheduled.process_due(app.state.database,scheduled.when(due)+timedelta(seconds=2),settings)==0

async def test_natural_language_schedule_and_switch_back_to_immediate(settings):
 settings.app_auth_enabled=True;settings.scheduled_worker_enabled=False
 due=(scheduled.clock()+timedelta(hours=1)).isoformat()
 from conftest import tool
 model=ScriptedModel([[tool('transfer',recipient='zhang_san',amount_yuan='10',execute_at=due)]])
 app=create_app(settings,model)
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
  import asyncio
  await register(client);conv=(await client.get('/api/bootstrap')).json()['conversation_id']
  response=await client.post('/api/messages',json={'conversation_id':conv,'text':'预约一小时后给张三转10元'})
  assert response.status_code==202
  await asyncio.wait_for(app.state.agent.queue.join(),3)
  with app.state.database.connection() as db:
   row=db.execute("SELECT * FROM approvals WHERE status='pending'").fetchone()
   assert json.loads(row['args_json'])['execute_at']==scheduled.when(due).isoformat(timespec='seconds')
  before=funds(app.state.database)
  result=await consent(client,conv,row['id'],due)
  assert result['schedule']['status']=='scheduled' and funds(app.state.database)==before
  assert len(model.requests)==1
  # An originally deferred inbound operation can explicitly become immediate.
  conv,key=await approval(app,client,'redeem_product',{'product_id':'cash','units':'10','account_id':'daily','execute_at':due})
  receipt=await consent(client,conv,key,'')
  assert not receipt.get('scheduled')
  with app.state.database.connection() as db:
   row=db.execute('SELECT summary_json FROM approvals WHERE id=?',(key,)).fetchone()
   assert not json.loads(row[0]).get('scheduled')

@pytest.mark.parametrize('mode',['cancel','unavailable'])
async def test_quote_wait_can_be_cancelled_and_platform_faults_are_recorded(settings,monkeypatch,mode):
 settings.app_auth_enabled=True;settings.scheduled_worker_enabled=False
 app=create_app(settings,ScriptedModel())
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
  await register(client);conv,key=await approval(app,client,'place_food_order',{'quote_id':'foodquote'})
  due=(scheduled.clock()+timedelta(minutes=2)).isoformat();receipt=await consent(client,conv,key,due)
  before=funds(app.state.database);real=httpx.Client
  def respond(request):
   if mode=='unavailable':raise httpx.ConnectError('isolated outage',request=request)
   with app.state.database.transaction() as db:
    scheduled.cancel(db,receipt['schedule']['id'])
    value=json.loads(db.execute('SELECT payload FROM food_quotes').fetchone()[0])
   value['expires']=time.time()+300
   return httpx.Response(200,json=value)
  monkeypatch.setattr(httpx,'Client',lambda **kw:real(**kw,transport=httpx.MockTransport(respond)))
  count=scheduled.process_due(app.state.database,scheduled.when(due)+timedelta(seconds=1),settings)
  with app.state.database.connection() as db:
   row=db.execute('SELECT * FROM scheduled_transfers').fetchone()
   assert row['status']==('cancelled' if mode=='cancel' else 'needs_reauthorization')
   assert not row['execution_id'] and funds(app.state.database)==before
   if mode=='unavailable':assert db.execute("SELECT failures FROM safety_circuits WHERE scope='food'").fetchone()[0]==1
  assert count==(0 if mode=='cancel' else 1)
