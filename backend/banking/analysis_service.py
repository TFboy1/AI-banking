"""Snapshot -> de-identified dataset -> bounded code worker -> independent reconciliation."""
import asyncio,hashlib,json,os,sys,tempfile,time
from collections import defaultdict
from datetime import date,datetime,timedelta
from pathlib import Path
from .db import TZ,dumps,now,uid
from .bill_reports import EXCLUDED,instant,money

SCHEMA='''
CREATE TABLE IF NOT EXISTS analysis_datasets(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,args_json TEXT NOT NULL,
 payload TEXT NOT NULL,input_hash TEXT NOT NULL,created_at TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS analysis_runs(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,dataset_id TEXT NOT NULL,
 code TEXT NOT NULL,code_hash TEXT NOT NULL,input_hash TEXT NOT NULL,status TEXT NOT NULL,result_json TEXT,
 error TEXT,elapsed_ms INTEGER,created_at TEXT NOT NULL);
'''
TOOLS={'get_bill_analysis_data','run_bill_analysis_code'}
_slots=asyncio.Semaphore(2)

def error(text):
    from .bank import BankError
    raise BankError(text)

def clock():return datetime.now(TZ)
def digest(value):return hashlib.sha256(dumps(value).encode('utf-8')).hexdigest()

def bounds(start,end):
    try:a=date.fromisoformat(start);b=date.fromisoformat(end)
    except ValueError:error('分析日期无效。')
    if not 0<=(b-a).days<=1096:error('每段分析范围须为1天至3年。')
    try:
        return datetime.combine(a,datetime.min.time(),TZ),datetime.combine(b+timedelta(days=1),datetime.min.time(),TZ)
    except OverflowError:error('分析日期超出支持范围。')

def rows_for(db,args,at):
    periods=[('本期',args['start_date'],args['end_date'])]
    if args['kind']=='comparison':periods.append(('对比期',args['compare_start'],args['compare_end']))
    records=[];references=set();groups=set();totals=defaultdict(int);coverage=[];observed=[]
    for label,start,end in periods:
        begin,finish=bounds(start,end)
        source=[dict(r) for r in db.execute('SELECT id,occurred_at,category,direction,amount_minor,counterparty,description FROM transactions WHERE julianday(occurred_at)>=julianday(?) AND julianday(occurred_at)<julianday(?) AND julianday(occurred_at)<=julianday(?) ORDER BY julianday(occurred_at),id LIMIT 12001',(begin.isoformat(),finish.isoformat(),at.isoformat()))]
        if len(source)>12000:error('该范围流水超过分析上限，请缩短时间后重试。')
        count=0;first=None;last=None
        for row in source:
            if row['category'] in EXCLUDED:continue
            if args['categories'] and row['category'] not in args['categories']:continue
            if args.get('merchant_contains') and not any(args['merchant_contains'] in (row[k] or '') for k in ('counterparty','description')):continue
            if args['measure']=='expense' and row['direction']!='out':continue
            if args['measure']=='income' and row['direction']!='in':continue
            day=instant(row['occurred_at']).date();first=first or day;last=day
            bucket=row['category'] if args['kind'] in {'categories','comparison'} else day.strftime('%Y-%m') if args['group_by']=='month' else (day-timedelta(days=day.weekday())).isoformat() if args['group_by']=='week' else day.isoformat()
            # '|': reserved only for the verified key/series boundary.
            if '|' in bucket or len(bucket)>100:error('账单分类格式不适合分析，请先核对资料。')
            amount=row['amount_minor']*(-1 if args['measure']=='net' and row['direction']=='out' else 1)
            series=label if args['kind']=='comparison' else {'expense':'支出','income':'收入','net':'净收支'}[args['measure']]
            records.append({'bucket':bucket,'series':series,'amount_minor':amount})
            references.add(row['id']);groups.add(bucket);totals[label]+=amount;count+=1
        coverage.append({'label':label,'start':start,'end':end,'count':count,'total_minor':totals[label],
                         'first_record':first.isoformat() if first else None,'last_record':last.isoformat() if last else None,
                         'partial':finish>at})
        observed.append(count)
    if len(records)>16000 or len(groups)>1000:error('分析数据或分组过多，请缩短范围或按月统计。')
    expected=defaultdict(int)
    for row in records:expected[row['bucket']+'|'+row['series']]+=row['amount_minor']
    return {'rows':records,'expected':dict(expected),'references':sorted(references),'coverage':coverage,'as_of':at.isoformat(),'args':args}

async def run_worker(rows,code):
    async with _slots:
        with tempfile.TemporaryDirectory(prefix='youshu-analysis-') as folder:
            env={k:v for k,v in os.environ.items() if k.upper() in {'SYSTEMROOT','WINDIR'}}
            process=await asyncio.create_subprocess_exec(sys.executable,'-I','-S',str(Path(__file__).with_name('analysis_worker.py')),
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,cwd=folder,env=env)
            try:
                data=dumps({'rows':rows,'code':code}).encode('utf-8')
                if len(data)>2_000_000:error('分析输入过大，请缩短范围。')
                raw,_=await asyncio.wait_for(process.communicate(data),5)
                if process.returncode or len(raw)>400000:error('分析执行失败或结果过大。')
                try:value=json.loads(raw.decode('utf-8'))
                except (ValueError,UnicodeError):error('分析进程返回无效结果，已停止展示，请重试。')
                if type(value) is not dict or value.get('ok') is not True:error('分析代码未通过受限执行检查，未展示未经核对的结果。')
                result=value.get('result');steps=value.get('steps')
                if (type(result) is not dict or len(result)>4000 or
                    any(type(k) is not str or len(k)>512 or type(v) is not int or abs(v)>10**15 for k,v in result.items()) or
                    type(steps) is not int or not 0<steps<=600000):
                    error('分析进程结果格式不符合要求，已停止展示，请重试。')
                return value
            except TimeoutError:
                error('分析执行超时，已停止计算；请缩短范围后重试，未修改资金。')
            finally:
                if process.returncode is None:
                    process.kill();await process.wait()

def presentation(data,run_id,code_hash,elapsed,steps):
    args=data['args'];expected=data['expected'];buckets=sorted({r['bucket'] for r in data['rows']})
    if args['kind']!='trend':buckets.sort(key=lambda b:(-sum(v for k,v in expected.items() if k.rsplit('|',1)[0]==b),b))
    series=['本期','对比期'] if args['kind']=='comparison' else [{'expense':'支出','income':'收入','net':'净收支'}[args['measure']]]
    values=[{'name':name,'values':[expected.get(b+'|'+name,0) for b in buckets]} for name in series]
    chart='line' if args['kind']=='trend' else 'donut' if args['kind']=='categories' and args['measure']!='net' else 'bar'
    labels={'trend':'收支趋势分析','categories':'收支分类分析','comparison':'两期账单对比'}
    metrics=[{'label':p['label']+{'expense':'支出','income':'收入','net':'净收支'}[args['measure']],'value':money(p['total_minor'])} for p in data['coverage']]
    difference=None
    if args['kind']=='comparison':
        one,two=data['coverage'];change=one['total_minor']-two['total_minor']
        difference={'amount_minor':change,'percent':round(change/two['total_minor']*100,2) if two['total_minor']>0 else None}
        metrics.append({'label':'两期差额','value':money(change)})
    note='仅使用当前账号已有账单，内部划转和理财、证券本金不计入日常收支。无记录不代表实际零消费；空缺时间不补造流水。'
    if args['kind']=='trend' and args['group_by']=='week':note+=' 周分组以周一标记，首尾周可能不足7天。'
    if any(p['partial'] for p in data['coverage']):note+=' 范围含尚未结束的时间，本次只统计到查询时刻。'
    return {'analysis':{'title':labels[args['kind']],'chart_type':chart,'labels':buckets,'series':values,'metrics':metrics,
                'as_of':data['as_of'],'coverage':data['coverage'],'difference':difference,'record_count':len(data['references']),
                'note':note,'verified':True,'sandbox':{'run_id':run_id,'code_hash':code_hash,'steps':steps,'elapsed_ms':elapsed,'mode':'bounded_python_subset'},
                'rows':[{'label':b,'values':[expected.get(b+'|'+s,0) for s in series]} for b in buckets]},
            'display':{'title':labels[args['kind']],'columns':[],'rows':[],'metrics':[],'note':''},
            'answer':'分析已完成，金额已与后台独立汇总逐项核对。请依据图表、统计区间与数据覆盖解释，不把差额当作同等时长的增长，不推断没有记录的消费。',
            'explanation':{'as_of':data['as_of'],'sources':[{'label':'当前账号脱敏账单','references':data['references']}],
                'checks':[{'label':'计算结果核对','status':'pass','detail':'生成代码的每个分组及金额与后台独立计算完全一致。'}],
                'conclusion':'分析不修改余额，不产生交易。'}}

class BillAnalysis:
    def __init__(self,database):self.db=database
    async def query(self,name,args,conversation_id):
        with self.db.connection() as db:
            if not db.execute('SELECT 1 FROM conversations WHERE id=? AND deleted_at IS NULL',(conversation_id,)).fetchone():error('分析会话不存在。')
        if name=='get_bill_analysis_data':
            with self.db.transaction() as db:
                data=rows_for(db,args,clock());key=uid('dataset_');hashed=digest(data)
                db.execute('INSERT INTO analysis_datasets VALUES(?,?,?,?,?,?,?)',(key,conversation_id,dumps(args),dumps(data),hashed,now(),time.time()+900))
                db.execute('DELETE FROM analysis_datasets WHERE expires<?',(time.time()-86400,))
            return {'dataset_id':key,'row_count':len(data['rows']),'as_of':data['as_of'],'sample':data['rows'][:12],
                'code_contract':'输入rows的每行只有bucket、series、amount_minor。创建totals={}；for row in rows，用key=row["bucket"]+"|"+row["series"]；totals[key]=get(totals,key,0)+row["amount_minor"]；最后result=totals。不能导入模块或使用属性/方法。下一步必须run_bill_analysis_code，不能自行编造已分析或已画图。',
                'display':{'title':'已准备脱敏分析数据','columns':[],'rows':[],'metrics':[{'label':'可分析流水','value':str(len(data['references']))+'笔'}],
                           'note':'接下来运行受限计算并核对结果。尚未修改任何资金或订阅。'}}
        with self.db.transaction() as db:
            row=db.execute('SELECT * FROM analysis_datasets WHERE id=? AND conversation_id=?',(args['dataset_id'],conversation_id)).fetchone()
            if not row or row['expires']<=time.time():error('分析数据已过期或不属于本对话，请重新查询。')
            data=json.loads(row['payload'])
            if digest(data)!=row['input_hash']:error('分析数据校验失败，请重新查询。')
            run_id=uid('analysis_');hashed=digest(args['code']);started=time.monotonic()
            db.execute('INSERT INTO analysis_runs VALUES(?,?,?,?,?,?,?,NULL,NULL,NULL,?)',(run_id,conversation_id,row['id'],args['code'],hashed,row['input_hash'],'running',now()))
        try:
            value=await run_worker(data['rows'],args['code'])
            if value['result']!=data['expected'] or any(type(v) is not int for v in value['result'].values()):error('分析结果与后台账单汇总不一致，已拦截；不会展示或据此操作资金。')
            result=presentation(data,run_id,hashed,int((time.monotonic()-started)*1000),value['steps'])
            with self.db.transaction() as db:
                if not db.execute('SELECT 1 FROM conversations WHERE id=? AND deleted_at IS NULL',(conversation_id,)).fetchone():error('对话已关闭，停止展示分析结果。')
                db.execute("UPDATE analysis_runs SET status='completed',result_json=?,elapsed_ms=? WHERE id=?",(dumps(result),result['analysis']['sandbox']['elapsed_ms'],run_id))
            return result
        except BaseException:
            with self.db.transaction() as db:db.execute("UPDATE analysis_runs SET status='failed',error='计算未完成或未通过核对，未展示结果。',elapsed_ms=? WHERE id=?",(int((time.monotonic()-started)*1000),run_id))
            raise
