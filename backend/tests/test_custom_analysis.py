import asyncio,json,time
from datetime import datetime
import pytest
from banking.analysis_models import AnalysisData
from banking.analysis_service import BillAnalysis,rows_for,run_worker
from banking.analysis_worker import Interpreter,Rejected
from banking.bank import BankError
from banking.db import TZ
from conftest import tool,ask,state

CODE='''totals = {}
for row in rows:
    key = row["bucket"] + "|" + row["series"]
    totals[key] = get(totals, key, 0) + row["amount_minor"]
result = totals
'''

@pytest.mark.parametrize('code',[
 'import os\nresult = {}','result = rows.__class__','result = open("C:/secret")','result = __import__("os")',
 'while True:\n    result = {}','x = {}\nx["self"] = x\nresult = x',
 'if False:\n    import socket\nresult = {}','result = {"fake": 999}',
 'x = 10 ** 999999999\nresult = {}', 'rows = {}\nresult = {}',
 'rows[0]["amount_minor"] = 0\nresult = {}','f = lambda: 1\nresult = {}'
])
def test_worker_rejects_unsafe_or_invalid_programs(code):
 if code=='result = {"fake": 999}':
  # Literal nonempty dictionaries are outside the bounded dialect as well.
  pass
 with pytest.raises((Rejected,TypeError)):Interpreter([{'bucket':'餐饮','series':'支出','amount_minor':100}]).run(code)

async def test_real_worker_runs_generated_subset_and_has_no_host_bindings():
 rows=[{'bucket':'餐饮','series':'支出','amount_minor':100},{'bucket':'餐饮','series':'支出','amount_minor':250}]
 value=await run_worker(rows,CODE)
 assert value['result']=={'餐饮|支出':350} and value['steps']>0
 with pytest.raises(BankError):await run_worker(rows,'result = open(".env")')

async def test_worker_enforces_compute_budget():
 rows=[{'bucket':str(i),'series':'支出','amount_minor':1} for i in range(500)]
 with pytest.raises(BankError):await run_worker(rows,'totals = {}\nfor a in rows:\n    for b in rows:\n        key = a["bucket"]\n        totals[key] = get(totals, key, 0) + 1\nresult = totals')

def seed(db):
 db.execute('DELETE FROM transactions')
 for key,amount,category,direction,day in [('a',1000,'餐饮','out','2026-10-01'),('b',2000,'购物','out','2026-10-03'),('c',5000,'工资','in','2026-10-04'),('d',99999,'证券投资','out','2026-10-05'),('e',5000,'内部转账','in','2026-10-05'),('f',800,'餐饮','out','2026-09-01')]:
  db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)',(key,key,'daily','私密姓名13900123456',direction,amount,category,day+'T12:00:00+08:00',None,'私密备注银行卡'))

async def test_scoped_snapshot_reconciles_code_money_and_automatically_selects_chart(database):
 with database.transaction() as db:seed(db);conv=database.create_conversation(db);other=database.create_conversation(db)
 service=BillAnalysis(database)
 for kind,chart in [('trend','line'),('categories','donut'),('comparison','bar')]:
  args=AnalysisData(kind=kind,start_date='2026-10-01',end_date='2026-10-17',**({'compare_start':'2026-09-01','compare_end':'2026-09-17'} if kind=='comparison' else {})).model_dump()
  dataset=await service.query('get_bill_analysis_data',args,conv)
  assert '私密' not in json.dumps(dataset,ensure_ascii=False) and '13900123456' not in str(dataset)
  with pytest.raises(BankError):await service.query('run_bill_analysis_code',{'dataset_id':dataset['dataset_id'],'code':CODE},other)
  result=await service.query('run_bill_analysis_code',{'dataset_id':dataset['dataset_id'],'code':CODE},conv)
  assert result['analysis']['verified'] and result['analysis']['chart_type']==chart
  assert result['analysis']['coverage'][0]['total_minor']==3000
  assert result['analysis']['sandbox']['steps']>0
  if kind=='comparison':assert result['analysis']['difference']=={'amount_minor':2200,'percent':275.0}
 with database.connection() as db:assert db.execute('SELECT count(*) FROM transactions').fetchone()[0]==6

async def test_fabrication_expiry_empty_and_audit(database):
 with database.transaction() as db:seed(db);conv=database.create_conversation(db)
 service=BillAnalysis(database);args=AnalysisData(start_date='2026-10-01',end_date='2026-10-17').model_dump()
 dataset=await service.query('get_bill_analysis_data',args,conv)
 with pytest.raises(BankError,match='不一致'):await service.query('run_bill_analysis_code',{'dataset_id':dataset['dataset_id'],'code':'result = {}'},conv)
 with database.connection() as db:assert db.execute("SELECT status FROM analysis_runs ORDER BY rowid DESC LIMIT 1").fetchone()[0]=='failed'
 with database.transaction() as db:db.execute('UPDATE analysis_datasets SET expires=0')
 with pytest.raises(BankError,match='过期'):await service.query('run_bill_analysis_code',{'dataset_id':dataset['dataset_id'],'code':CODE},conv)
 empty=await service.query('get_bill_analysis_data',AnalysisData(start_date='2020-01-01',end_date='2020-01-02').model_dump(),conv)
 result=await service.query('run_bill_analysis_code',{'dataset_id':empty['dataset_id'],'code':CODE},conv)
 assert not result['analysis']['labels'] and result['analysis']['record_count']==0

def test_beijing_boundary_filter_net_and_invalid_period(database):
 with database.transaction() as db:seed(db)
 with database.connection() as db:
  args=AnalysisData(start_date='2026-10-01',end_date='2026-10-17',measure='net',group_by='week').model_dump()
  result=rows_for(db,args,datetime(2026,10,18,tzinfo=TZ))
  assert sum(result['expected'].values())==2000
  args['end_date']='2026-09-01'
  with pytest.raises(BankError):rows_for(db,args,datetime(2026,10,18,tzinfo=TZ))

async def test_agent_analysis_routes_two_tools_and_preserves_balances(agent):
 before=agent.bank.snapshot()
 # First tool result supplies the dynamically generated dataset ID.
 original=agent.model.complete
 async def complete(messages):
  for m in reversed(messages):
   if m['role']=='tool':
    value=json.loads(m['content'])
    if 'dataset_id' in value:return {'role':'assistant','content':'','tool_calls':[tool('run_bill_analysis_code',dataset_id=value['dataset_id'],code=CODE)]}
    if 'analysis' in value:return {'role':'assistant','content':'已核对图表，请查看数据范围。'}
  return {'role':'assistant','content':'','tool_calls':[tool('get_bill_analysis_data',start_date='2026-10-01',end_date='2026-10-17',kind='trend',group_by='week')]}
 agent.model.complete=complete
 key=await agent.submit('最近的账单按周分析一下');await asyncio.wait_for(agent.queue.join(),10)
 assert state(agent,key)['status']=='completed'
 assert agent.bank.snapshot()==before
 with agent.db.connection() as db:
  assert db.execute("SELECT count(*) FROM analysis_runs WHERE status='completed'").fetchone()[0]==1
  assert not db.execute('SELECT 1 FROM approvals').fetchone()
