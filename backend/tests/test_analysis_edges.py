"""Adversarial and lifecycle regression checks, using disposable databases only."""
import asyncio
import json
from datetime import datetime

import pytest

from banking import analysis_service as service
from banking.analysis_models import AnalysisData
from banking.analysis_worker import Interpreter, Rejected
from banking.bank import BankError
from banking.db import TZ
from test_custom_analysis import CODE, seed


def args(**kw):
    return AnalysisData(start_date='2026-10-01', end_date='2026-10-17', **kw).model_dump()


def insert(db, key, timestamp, amount=1, direction='out', category='餐饮', merchant='商户', description='测试'):
    db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)',
               (key, key, 'daily', merchant, direction, amount, category, timestamp, None, description))


def test_actual_beijing_midnight_boundaries(database):
    with database.transaction() as db:
        db.execute('DELETE FROM transactions')
        for key, timestamp in [('before','2026-09-30T15:59:59Z'),('start','2026-09-30T16:00:00Z'),
                               ('last','2026-10-01T15:59:59Z'),('next','2026-10-01T16:00:00Z')]:
            insert(db,key,timestamp)
        query=args();query['end_date']='2026-10-01'
        data=service.rows_for(db,query,datetime(2026,10,18,tzinfo=TZ))
        assert data['references']==['last','start']
        assert data['coverage'][0]['total_minor']==2
        assert data['rows'][0]['bucket']=='2026-10'


@pytest.mark.parametrize('start,end,valid',[
    ('2024-02-29','2024-02-29',True),('2025-02-29','2025-03-01',False),
    ('2026-10-02','2026-10-01',False),('2020-01-01','2026-01-01',False),
    ('9999-12-31','9999-12-31',False),('2026-13-01','2026-13-02',False)])
def test_date_edge_cases(start,end,valid):
    if valid:
        a,b=service.bounds(start,end);assert (b-a).days==1
    else:
        with pytest.raises(BankError):service.bounds(start,end)


@pytest.mark.parametrize('measure,total',[('expense',3000),('income',5000),('net',2000)])
def test_measures_and_all_investment_exclusions(database,measure,total):
    with database.transaction() as db:
        seed(db)
        for i,cat in enumerate(['内部转账','理财','证券投资','证券卖出']):
            for direction in ['in','out']:
                insert(db,f'ex{i}{direction}','2026-10-02T12:00:00+08:00',999999,direction,cat)
        data=service.rows_for(db,args(measure=measure),datetime(2026,10,18,tzinfo=TZ))
        assert sum(data['expected'].values())==total


def test_merchant_filter_does_not_treat_all_food_as_takeaway(database):
    with database.transaction() as db:
        db.execute('DELETE FROM transactions')
        insert(db,'dine','2026-10-02T12:00:00+08:00',4000,description='堂食')
        insert(db,'delivery','2026-10-02T12:00:00+08:00',2500,description='外卖订单')
        insert(db,'market','2026-10-02T12:00:00+08:00',800,category='购物',merchant='外卖超市')
        data=service.rows_for(db,args(merchant_contains='外卖',categories=['餐饮']),datetime(2026,10,18,tzinfo=TZ))
        assert data['references']==['delivery'] and sum(data['expected'].values())==2500


def test_future_records_cutoff_and_partial_period(database):
    with database.transaction() as db:
        db.execute('DELETE FROM transactions')
        insert(db,'past','2026-10-02T12:00:00+08:00',1)
        insert(db,'future','2026-10-17T12:00:00+08:00',999)
        data=service.rows_for(db,args(),datetime(2026,10,10,tzinfo=TZ))
        assert data['references']==['past'] and data['coverage'][0]['partial']


@pytest.mark.parametrize('code',[
    'if False:\n    x = open(".env")\nresult = {}',
    'if False:\n    x = __import__("os")\nresult = {}',
    'x = [r for r in rows]\nresult = {}',
    'result = eval("1")', 'result = exec("1")', 'result = globals()',
    'result = rows[0].get("bucket")','result = f"{rows}"',
    'result = {}\nresult["x"] = True',
    'x = 1000000000000001\nresult = {}',
    'x = "'+('a'*513)+'"\nresult = {}',
    'try:\n    result = {}\nexcept:\n    result = {}',
    'result = {}\nfor x in rows:\n    break',
    'result = {}\n'+('x = 1\n'*200),
])
def test_sandbox_adversarial_programs(code):
    with pytest.raises((Rejected,SyntaxError,TypeError)):
        Interpreter([{'bucket':'餐饮','series':'支出','amount_minor':1}]).run(code)


async def test_tampered_snapshot_and_deleted_conversation(database):
    with database.transaction() as db:seed(db);conv=database.create_conversation(db)
    api=service.BillAnalysis(database)
    value=await api.query('get_bill_analysis_data',args(),conv)
    with database.transaction() as db:
        row=db.execute('SELECT payload FROM analysis_datasets WHERE id=?',(value['dataset_id'],)).fetchone()
        data=json.loads(row[0]);data['rows'][0]['amount_minor']=999
        db.execute('UPDATE analysis_datasets SET payload=? WHERE id=?',(json.dumps(data),value['dataset_id']))
    with pytest.raises(BankError,match='校验失败'):
        await api.query('run_bill_analysis_code',{'dataset_id':value['dataset_id'],'code':CODE},conv)
    with database.transaction() as db:db.execute("UPDATE conversations SET deleted_at='2026-10-18' WHERE id=?",(conv,))
    with pytest.raises(BankError,match='不存在'):await api.query('get_bill_analysis_data',args(),conv)


async def test_cancel_running_analysis_keeps_money_and_marks_failed(database,monkeypatch):
    with database.transaction() as db:seed(db);conv=database.create_conversation(db)
    api=service.BillAnalysis(database);value=await api.query('get_bill_analysis_data',args(),conv)
    started=asyncio.Event()
    async def slow(*unused):started.set();await asyncio.Event().wait()
    monkeypatch.setattr(service,'run_worker',slow)
    job=asyncio.create_task(api.query('run_bill_analysis_code',{'dataset_id':value['dataset_id'],'code':CODE},conv))
    await asyncio.wait_for(started.wait(),2);job.cancel()
    with pytest.raises(asyncio.CancelledError):await job
    with database.connection() as db:
        assert db.execute('SELECT status FROM analysis_runs').fetchone()[0]=='failed'
        assert db.execute('SELECT count(*) FROM transactions').fetchone()[0]==6


async def test_deleted_while_computing_cannot_publish(database,monkeypatch):
    with database.transaction() as db:seed(db);conv=database.create_conversation(db)
    api=service.BillAnalysis(database);value=await api.query('get_bill_analysis_data',args(),conv)
    async def remove(rows,code):
        with database.transaction() as db:db.execute("UPDATE conversations SET deleted_at='2026-10-18' WHERE id=?",(conv,))
        return Interpreter(rows).run(code)
    monkeypatch.setattr(service,'run_worker',remove)
    with pytest.raises(BankError,match='关闭'):
        await api.query('run_bill_analysis_code',{'dataset_id':value['dataset_id'],'code':CODE},conv)
    with database.connection() as db:assert db.execute('SELECT status FROM analysis_runs').fetchone()[0]=='failed'


@pytest.mark.parametrize('payload',[b'not-json',b'[]',b'{"ok":true}',b'{"ok":true,"result":{},"steps":-1}'])
async def test_malformed_worker_output_is_controlled_error(monkeypatch,payload):
    class Child:
        returncode=0
        async def communicate(self,data):return payload,b''
    async def create(*a,**kw):return Child()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',create)
    with pytest.raises(BankError):await service.run_worker([],CODE)


async def test_worker_cancellation_kills_child_and_strips_secrets(monkeypatch):
    started=asyncio.Event();seen={}
    class Child:
        returncode=None
        async def communicate(self,data):started.set();await asyncio.Event().wait()
        def kill(self):seen['killed']=True;self.returncode=-1
        async def wait(self):seen['waited']=True
    async def create(*a,**kw):seen.update(kw);return Child()
    monkeypatch.setenv('ANALYSIS_SECRET','should-not-reach-child')
    monkeypatch.setattr(asyncio,'create_subprocess_exec',create)
    task=asyncio.create_task(service.run_worker([],CODE));await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert seen['killed'] and seen['waited']
    assert set(seen['env']).issubset({'SYSTEMROOT','WINDIR','SystemRoot','windir'})
    assert 'ANALYSIS_SECRET' not in seen['env']


async def test_worker_timeout_kills_child(monkeypatch):
    seen={}
    class Child:
        returncode=None
        async def communicate(self,data):raise TimeoutError()
        def kill(self):seen['killed']=True;self.returncode=-1
        async def wait(self):seen['waited']=True
    async def create(*a,**kw):return Child()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',create)
    with pytest.raises(BankError,match='超时'):await service.run_worker([],CODE)
    assert seen=={'killed':True,'waited':True}


async def test_max_two_workers_and_cancelled_waiter_releases_capacity(monkeypatch):
    monkeypatch.setattr(service,'_slots',asyncio.Semaphore(2))
    active=0;peak=0;ready=asyncio.Event();release=asyncio.Event()
    class Child:
        returncode=0
        async def communicate(self,data):
            nonlocal active,peak
            active+=1;peak=max(peak,active)
            if active==2:ready.set()
            try:await release.wait();return b'{"ok":true,"result":{},"steps":1}',b''
            finally:active-=1
    async def create(*a,**kw):return Child()
    monkeypatch.setattr(asyncio,'create_subprocess_exec',create)
    jobs=[asyncio.create_task(service.run_worker([],CODE)) for _ in range(4)]
    await asyncio.wait_for(ready.wait(),2)
    jobs[-1].cancel()
    with pytest.raises(asyncio.CancelledError):await jobs[-1]
    release.set();await asyncio.gather(*jobs[:-1])
    assert peak==2 and service._slots._value==2


@pytest.mark.parametrize('changes',[
    {'kind':'comparison'}, {'compare_start':'2026-09-01'},
    {'measure':'profit'}, {'group_by':'hour'}, {'unknown':1},
    {'categories':['x']*13}, {'merchant_contains':'x'*61},
])
def test_invalid_request_contract_rejected(changes):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):AnalysisData(**{'start_date':'2026-10-01','end_date':'2026-10-17',**changes})


def test_empty_comparison_baseline_never_divides_by_zero(database):
    with database.transaction() as db:
        seed(db)
        data=service.rows_for(db,args(kind='comparison',compare_start='2020-01-01',compare_end='2020-01-17'),datetime(2026,10,18,tzinfo=TZ))
    value=service.presentation(data,'test','hash',1,1)['analysis']
    assert value['difference']['percent'] is None
    assert '无记录不代表实际零消费' in value['note']
    assert value['coverage'][1]['count']==0
