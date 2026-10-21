import asyncio
import json
from datetime import date,datetime,timedelta

import httpx
import pytest
from banking import daily_reports as reports
from banking.db import TZ,Database,dumps
from banking.main import create_app
from banking.security import encode_pin
from banking.report_history import snapshot,reset_history
from conftest import ScriptedModel
from test_security import register

AT=datetime(2026,10,17,9,tzinfo=TZ)

def seed(database):
 with database.transaction() as db:
  db.execute("INSERT INTO app_identity(id,username,phone,pin_hash,contacts_done) VALUES(1,'日报用户','11111111111',?,1)",(encode_pin('2468'),))
  for table in ('transactions','accounts','holdings','securities_positions','subscriptions','contacts','contact_profiles','contact_details'):
   db.execute('DELETE FROM '+table)
  db.executemany('INSERT INTO accounts VALUES(?,?,?,?)',[('daily','日常账户','checking',100000),('savings','储蓄账户','savings',200000),('card','演示银行卡尾号1234','bank_card',50000),('removed','已解绑','bank_card_unlinked',90000)])
  db.execute("UPDATE products SET nav='1.0000' WHERE id='cash'")
  db.execute("INSERT INTO holdings VALUES('cash','100.0000')")
  db.execute("INSERT INTO securities_positions VALUES('000001','平安银行',100,10000)")
  db.execute("INSERT OR REPLACE INTO meta VALUES('securities_owner','report_owner')")
  db.executemany('INSERT INTO subscriptions VALUES(?,?,?,?,?,?)',[
   ('music','音乐会员','music',1000,'2026-10-20','active'),('next','下月会员','next',2000,'2026-11-05','active'),
   ('cancel','已取消会员','cancel',3000,'2026-10-19','cancelled'),('old','待核对会员','old',500,'2026-10-15','active')])
  for key,birthday,attention in [('lover','1998-10-20',1),('limit','2000-11-17',1),('later','2000-11-18',1),('normal','2000-10-19',0)]:
   db.execute('INSERT INTO contacts VALUES(?,?,?,?,?)',(key,key,'','演示银行',''))
   db.execute('INSERT INTO contact_details VALUES(?,?,?)',(key,'测试备注',''))
   db.execute('INSERT INTO contact_profiles VALUES(?,?,?,?,?)',(key,'家人',birthday,'未设置',attention))
  # A historical fixture of the state already recorded before yesterday ended.
  reset_history(db)
  db.execute("UPDATE report_history SET changed_at='2026-10-15T16:00:00.000+00:00'")
  db.execute("UPDATE report_history_meta SET started_at='2026-10-15T16:00:00.000+00:00'")
  txs=[('meal','out',1000,'餐饮','2026-10-16T12:00:00+08:00'),('utc','out',500,'购物','2026-10-16T15:59:59+00:00'),
       ('income','in',5000,'工资','2026-10-16T17:00:00+08:00'),('internal','in',9000,'内部转账','2026-10-16T17:00:00+08:00'),
       ('stock','out',10000,'证券投资','2026-10-16T17:00:00+08:00'),('sale','in',12000,'证券卖出','2026-10-16T17:00:00+08:00'),
       ('fund','out',4000,'理财','2026-10-16T17:00:00+08:00'),('today','out',700,'餐饮','2026-10-17T00:00:00+08:00'),
       ('todayutc','in',900,'工资','2026-10-16T16:00:00+00:00'),('older','out',600,'餐饮','2026-10-15T23:59:59+08:00')]
  for key,direction,amount,category,stamp in txs:
   db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)',(key,key,'daily',key,direction,amount,category,stamp,None,key))
  reset_history(db)
 # Backdate only the isolated fixture, after its commit; production cannot do so.
 with database.connection() as db:
  db.execute("UPDATE report_history SET changed_at='2026-10-15T16:00:00.000+00:00'")
  db.execute("UPDATE report_history_meta SET started_at='2026-10-15T16:00:00.000+00:00'")
 with database.transaction() as db:
  # Morning changes must not leak into a midnight report.
  db.execute("UPDATE accounts SET balance_minor=999999 WHERE id='daily'")
  db.execute("INSERT INTO accounts VALUES('todaycard','今天新增银行卡','bank_card',500000)")
  db.execute("UPDATE securities_positions SET shares=200,cost_minor=20000")
  db.execute("UPDATE contact_profiles SET special_attention=0 WHERE person_id='lover'")
  db.execute("UPDATE subscriptions SET status='cancelled' WHERE id='music'")

async def prices(database,settings,fact):
 assert fact['report_day']=='2026-10-17'
 positions=fact['history']['securities_positions']
 assert positions[0]['shares']==100
 return [{**p,'market_value_minor':12000,'profit_minor':2000,'profit_percent':20,'price_minor':120,'quote_date':'2026-10-16','source':'isolated historical close'} for p in positions]

async def test_midnight_facts_full_yesterday_and_no_current_day_leak(database,settings,monkeypatch):
 seed(database);monkeypatch.setattr(reports,'stock_values',prices)
 with database.connection() as db:
  original=db.execute("SELECT balance_minor FROM accounts WHERE id='daily'").fetchone()[0]
 result=await reports.generate_due(database,settings,AT)
 assert result['coverage_complete'] and result['expense_minor']==1500 and result['income_minor']==5000
 assert result['subscription_reserve_minor']==1500 and result['investment_minor']==22000
 assert [p['name'] for p in result['birthdays']]==['lover','limit']
 assert [p['days'] for p in result['birthdays']]==[3,31]
 account=result['sections'][0]
 assert account['metrics'][0]['value']=='¥3,500.00' and account['metrics'][1]['value']=='¥3,720.00'
 assert not any('今天新增' in str(row) for row in account['rows'])
 assert result['cutoff_at']=='2026-10-17T00:00:00+08:00'
 with database.connection() as db:
  messages=[dict(r) for r in db.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid',(result['conversation_id'],))]
  assert len(messages)==1 and messages[0]['kind']=='data'
  digest=json.loads(messages[0]['data_json'])['result']['daily_digest']
  assert digest['sections']==result['sections']
  assert digest['greeting']=='尊敬的“日报用户”，早上好！'
  assert '数据截至 2026-10-17 00:00（北京时间）' in digest['subtitle']
  assert messages[0]['text'].startswith('尊敬的“日报用户”，早上好！')
  assert '00:00–24:00' in messages[0]['text'] and '2026-10-16' in messages[0]['text']
  assert not any('你好，我是你的银行助手' in r['text'] for r in messages)
  assert db.execute("SELECT balance_minor FROM accounts WHERE id='daily'").fetchone()[0]==original
  assert db.execute('SELECT COUNT(*) FROM tasks WHERE conversation_id=?',(result['conversation_id'],)).fetchone()[0]==0
 assert await reports.generate_due(database,settings,AT+timedelta(hours=1)) is None
 # Deleting the generated conversation must not recreate a duplicate report.
 with database.transaction() as db:db.execute('UPDATE conversations SET deleted_at=? WHERE id=?',(AT.isoformat(),result['conversation_id']))
 assert await reports.generate_due(database,settings,AT) is None

async def test_concurrent_workers_commit_only_one_conversation(database,settings,monkeypatch):
 seed(database)
 async def paused(*args):await asyncio.sleep(.03);return await prices(*args)
 monkeypatch.setattr(reports,'stock_values',paused)
 results=await asyncio.gather(reports.generate_due(database,settings,AT),reports.generate_due(database,settings,AT))
 assert sum(r is not None for r in results)==1
 with database.connection() as db:assert db.execute('SELECT COUNT(*) FROM daily_report_runs').fetchone()[0]==1

async def test_preference_change_during_quotes_cancels_pending_report(database,settings,monkeypatch):
 seed(database)
 async def disabled(*args):
  with database.transaction() as db:db.execute('UPDATE daily_report_preferences SET enabled=0,version=version+1')
  return []
 monkeypatch.setattr(reports,'stock_values',disabled)
 assert await reports.generate_due(database,settings,AT) is None
 with database.connection() as db:assert not db.execute('SELECT 1 FROM daily_report_runs').fetchone()

@pytest.mark.parametrize('stamp,scheduled,enabled,expected',[
 ('08:59','09:00',1,False),('09:00','09:00',1,True),('11:59','09:00',1,True),
 ('12:00','12:00',1,True),('12:01','09:00',1,False),('00:00','00:00',1,True),('09:00','09:00',0,False)])
async def test_delivery_window_and_disable(database,settings,monkeypatch,stamp,scheduled,enabled,expected):
 seed(database);monkeypatch.setattr(reports,'stock_values',prices)
 with database.transaction() as db:db.execute('UPDATE daily_report_preferences SET enabled=?,delivery_time=?',(enabled,scheduled))
 hour,minute=map(int,stamp.split(':'))
 assert (await reports.generate_due(database,settings,AT.replace(hour=hour,minute=minute)) is not None)==expected

async def test_missing_history_and_prices_are_explicit(database,settings,monkeypatch):
 seed(database)
 async def unavailable(*args):return [{**p,'market_value_minor':None,'price_minor':None,'profit_minor':None} for p in args[2]['history']['securities_positions']]
 monkeypatch.setattr(reports,'stock_values',unavailable)
 report=await reports.generate_due(database,settings,AT)
 assert report['investment_minor'] is None and '无法估值' in str(report['sections'])
 with database.transaction() as db:
  db.execute('DELETE FROM daily_report_runs')
  db.execute("UPDATE report_history_meta SET started_at='2026-10-17T01:00:00.000+00:00'")
 async def no_positions(*args):return []
 monkeypatch.setattr(reports,'stock_values',no_positions)
 report=await reports.generate_due(database,settings,AT)
 assert not report['coverage_complete'] and '不以当前数据代替' in str(report['sections'])
 assert report['expense_minor']==1500

async def test_settings_validation_auth_and_persistence(settings):
 settings.app_auth_enabled=True
 app=create_app(settings,ScriptedModel())
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
  assert (await client.get('/api/daily-report/settings')).status_code==401
  await register(client)
  assert (await client.get('/api/daily-report/settings')).json()['time']=='09:00'
  for value in ('00:00','11:59','12:00'):
   response=await client.patch('/api/daily-report/settings',json={'enabled':True,'time':value})
   assert response.status_code==200,response.text
  for value in ('12:01','23:00','9:00','00:00:00','24:00'):
   assert (await client.patch('/api/daily-report/settings',json={'enabled':True,'time':value})).status_code==422
  assert (await client.patch('/api/daily-report/settings',json={'enabled':'true','time':'09:00'})).status_code==422
  assert (await client.patch('/api/daily-report/settings',json={'enabled':False,'time':'09:15'})).status_code==200
 with app.state.database.connection() as db:
  pref=reports.preference(db,AT);assert pref['time']=='09:15' and not pref['enabled'] and pref['next_run'] is None

def test_history_triggers_roll_back_and_preserve_cutoff_state(database):
 seed(database)
 with database.transaction() as db:
  count=db.execute('SELECT COUNT(*) FROM report_history').fetchone()[0]
  db.execute('SAVEPOINT deliberate_failure')
  db.execute("UPDATE accounts SET balance_minor=1 WHERE id='daily'")
  db.execute('ROLLBACK TO deliberate_failure');db.execute('RELEASE deliberate_failure')
  assert db.execute('SELECT COUNT(*) FROM report_history').fetchone()[0]==count
  old=snapshot(db,'2026-10-16T16:00:00.000+00:00')
  assert next(a for a in old['accounts'] if a['id']=='daily')['balance_minor']==100000
  # No sensitive credentials/card numbers were copied into the journal.
  assert not any('card_number' in row[0] or 'pin_hash' in row[0] for row in db.execute('SELECT payload FROM report_history WHERE payload IS NOT NULL'))

def test_whole_transaction_has_one_historical_boundary(database):
 with database.connection() as db:before=db.execute('SELECT COALESCE(MAX(seq),0) FROM report_history').fetchone()[0]
 with database.transaction() as db:
  db.execute("UPDATE accounts SET balance_minor=balance_minor-100 WHERE id='daily'")
  db.execute("UPDATE accounts SET balance_minor=balance_minor+100 WHERE id='savings'")
 with database.connection() as db:
  versions=[dict(r) for r in db.execute('SELECT * FROM report_history WHERE seq>?',(before,))]
 assert len(versions)==2 and len({r['changed_at'] for r in versions})==1

async def test_backdated_row_added_after_cutoff_is_not_yesterday_data(database,settings,monkeypatch):
 seed(database);monkeypatch.setattr(reports,'stock_values',prices)
 with database.transaction() as db:
  db.execute("INSERT INTO transactions VALUES('late','late','daily','补记', 'out',99999,'购物','2026-10-16T12:00:00+08:00',NULL,'补记')")
 result=await reports.generate_due(database,settings,AT)
 assert result['expense_minor']==1500

def test_birthday_calendar_month_new_year_and_leap_day():
 assert reports.next_birthday('2000-02-29',date(2027,2,1))==date(2027,2,28)
 history={'contacts':[{'id':'x','name':'测试'}],'contact_details':[],
          'contact_profiles':[{'person_id':'x','special_attention':1,'birthday':'2000-01-15','relationship':'朋友'}]}
 assert reports.birthdays(history,date(2026,12,15))[0]['days']==31
 assert reports.birthdays(history,date(2026,12,14))==[]

def test_dormant_pre_securities_schema_migrates_without_seeding_money(database):
 with database.connection() as db:
  before=[tuple(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')]
  db.execute('DROP TABLE securities_positions')
  db.execute('DROP TABLE securities_quotes')
  db.execute('DROP TABLE securities_orders')
 reports.initialize_offline(database)
 with database.connection() as db:
  assert [tuple(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')]==before
  assert db.execute('SELECT COUNT(*) FROM securities_positions').fetchone()[0]==0
  assert db.execute('SELECT started_at FROM report_history_meta WHERE id=1').fetchone()
  assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'report_%'").fetchone()[0]==30

async def test_platform_valuation_filters_out_today_open_and_close():
 from banking.stock_valuation import value_positions
 class Service:
  async def get(self,*args):return {'status':'ok','source':'isolated-source','rows':[
   {'date':'2026-10-16','open':1,'close':1.2}, {'date':'2026-10-17','open':9,'close':10}]}
 values=await value_positions(Service(),[{'symbol':'000001','name':'测试','shares':100,'cost_minor':10000}],cutoff_date='2026-10-17')
 assert values[0]['price_minor']==120 and values[0]['market_value_minor']==12000 and values[0]['quote_date']=='2026-10-16'

async def test_report_valuation_endpoint_uses_linked_owner_and_cutoff(settings,tmp_path):
 from banking.stock_site import create_stock_app
 from banking.securities_common import secret,owner
 from test_stock_accounts import stock_login
 settings.app_auth_enabled=True;settings.securities_bridge_key_path=tmp_path/'report-key'
 bank=create_app(settings,ScriptedModel())
 stocks=create_stock_app(tmp_path/'market.sqlite3',broker_path=tmp_path/'broker.sqlite3',key_path=settings.securities_bridge_key_path)
 stocks.state.bank_transport=httpx.ASGITransport(app=bank)
 async def history(*args):return {'status':'ok','source':'isolated-cutoff-test','rows':[
  {'date':'2026-10-16','open':1,'close':1.2},{'date':'2026-10-17','open':9,'close':10}]}
 stocks.state.stock_service.get=history
 async with bank.router.lifespan_context(bank),httpx.AsyncClient(transport=httpx.ASGITransport(app=bank),base_url='http://127.0.0.1:8000') as client,httpx.AsyncClient(transport=httpx.ASGITransport(app=stocks),base_url='http://127.0.0.1:8001') as market:
  await register(client);await stock_login(market,'11111111111')
  code=(await client.post('/api/securities/pairing-code',json={})).json()['code']
  assert (await market.post('/api/securities/connect',json={'symbols':[],'code':code})).status_code==200
  with bank.state.database.connection() as db:investor=owner(db)
  body={'cutoff_date':'2026-10-17','positions':[{'symbol':'000001','name':'平安银行','shares':100,'cost_minor':10000}]}
  path=f'/internal/securities/{investor}/valuation-at'
  assert (await market.post(path,json=body)).status_code==403
  headers={'x-securities-key':secret(settings.securities_bridge_key_path)}
  result=await market.post(path,json=body,headers=headers)
  assert result.status_code==200,result.text
  assert result.json()['positions'][0]['price_minor']==120
  assert result.json()['positions'][0]['quote_date']=='2026-10-16'
  assert (await market.post('/internal/securities/foreign/valuation-at',json=body,headers=headers)).status_code==400

async def test_multiuser_settings_isolated_and_offline_restart_creates_separate_reports(settings,monkeypatch):
 from test_accounts import sms_login
 settings.multi_user_accounts=True;settings.app_auth_enabled=True;settings.daily_report_worker_enabled=False;settings.scheduled_worker_enabled=False
 app=create_app(settings,ScriptedModel());transport=httpx.ASGITransport(app=app)
 async with app.router.lifespan_context(app),httpx.AsyncClient(transport=transport,base_url='http://127.0.0.1:8000') as a,httpx.AsyncClient(transport=transport,base_url='http://127.0.0.1:8000') as b:
  for client,phone,name in [(a,'13800111111','用户甲'),(b,'13800222222','用户乙')]:
   await sms_login(client,phone,name)
   await client.post('/api/auth/contacts-done',json={})
   await client.post('/api/auth/pin',json={'pin':'2468','repeat_pin':'2468'})
  assert (await a.patch('/api/daily-report/settings',json={'enabled':True,'time':'08:30'})).status_code==200
  assert (await b.get('/api/daily-report/settings')).json()['time']=='09:00'
  with app.state.directory.connection() as db:users=[dict(r) for r in db.execute('SELECT * FROM users')]
 # The next worker must not need logged-in tenant apps or any language model.
 settings.daily_report_worker_enabled=True
 monkeypatch.setattr(reports,'clock',lambda:AT)
 restarted=create_app(settings,ScriptedModel())
 async with restarted.router.lifespan_context(restarted):
  for _ in range(100):
   complete=[]
   for user in users:
    with Database(user['database_path'],seed_demo=False).connection() as db:
     complete.append(bool(db.execute('SELECT 1 FROM daily_report_runs').fetchone()))
   if all(complete):break
   await asyncio.sleep(.05)
  assert all(complete) and restarted.state.directory.apps=={}
  convs=[]
  for user in users:
   with Database(user['database_path'],seed_demo=False).connection() as db:
    row=db.execute('SELECT * FROM daily_report_runs').fetchone();convs.append(row['conversation_id'])
    greeting=db.execute('SELECT text FROM messages WHERE conversation_id=? ORDER BY rowid LIMIT 1',(row['conversation_id'],)).fetchone()[0]
    assert user['username'] in greeting
  assert len(set(convs))==len(users)
