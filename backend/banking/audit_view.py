"""Read-only audit projections. Receipts and exact ledger namespaces are authoritative."""
import csv
import hashlib
import io
import json
import re
from datetime import date

from fastapi import Depends, HTTPException, Query
from fastapi.responses import Response

from .db import now
from .task_dag import TITLES

STATES={'queued':'等待开始','running':'处理中','waiting_confirmation':'等待确认','completed':'已完成',
        'failed':'失败','cancelled':'已取消','pending':'待确认','executed':'执行成功','expired':'已过期',
        'succeeded':'已完成','blocked':'后续已停止','needs_input':'等待补充信息','uncertain':'结果待核查'}
EVENTS={'operation.decision':'用户提交确认或取消','payment.pin_verified':'支付密码验证通过',
        'scheduled.authorized':'定时交易已提前授权（未扣款）','scheduled.executed':'定时交易到点执行',
        'scheduled.failed':'定时交易未执行','scheduled.cancelled':'定时交易已取消',
        'scheduled.needs_reauthorization':'定时交易需要重新授权',
        'payment.sms_sent':'已发送模拟交易短信','payment.sms_verified':'交易短信验证通过',
        'payment.verification_failed':'交易验证未通过','payment.account_selected':'已选择付款账户',
        'payment.policy_updated':'安全金额门槛已修改'}
SAFETY={'pin_changed':'支付密码已修改','pin_created':'首次设置支付密码','pin_change_sms_sent':'发送修改密码短信','pin_change_denied':'修改密码验证拒绝','pin_failure':'支付密码验证失败','sms_failure':'短信验证失败','suspicious_failure':'拒绝可疑请求',
        'opened':'触发安全保护','recovered':'安全保护已恢复','system_locked':'系统已人工锁定',
        'manual_requested':'申请人工解锁','manual_approved':'人工审核通过','manual_rejected':'人工审核拒绝',
        'service_failure':'平台请求失败','probe':'平台恢复探测'}
ADMIN={'login':'管理员登录','login_failed':'管理员登录失败','view_history':'查看人工对话',
       'claim':'接入人工对话','reply':'发送客服回复','close':'结束人工服务',
       'unlock_approved':'审核通过并解锁','unlock_rejected':'拒绝解锁申请',
       'audit_view':'查看操作审计','audit_export':'导出操作审计','audit_security':'查看账户安全记录',
       'audit_integrity':'校验审计完整性','audit_checkpoint':'提交签名检查点','audit_integrity_export':'导出签名校验凭据'}
SECRET=re.compile(r'pin|password|passwd|secret|token|fingerprint|code|api.?key|authorization|cookie|验证码|密码',re.I)

def text(value):
    value=str(value or '')
    # Receipt references are UUIDs, not phone/card numbers. Long decimal runs
    # inside a UUID must not break links between an authorization and its trade.
    if re.fullmatch(r'(?:op|task|conv|msg|schedule|call|call_direct|exec|tx|txn)_[0-9a-f]{32}(?::[a-z_]+)?',value):return value
    value=re.sub(r'(?<!\d)(\d{3})\d{4,13}(\d{4})(?!\d)',r'\1****\2',value)
    value=re.sub(r'((?:支付密码|交易密码|密码|验证码|password|pin|api[_ -]?key|token)\s*(?:是|为|[:：=])?\s*)[A-Za-z0-9_\-\.]{4,}',r'\1[已隐藏]',value,flags=re.I)
    return value

def safe(value):
    if isinstance(value,dict):
        if isinstance(value.get('label'),str) and SECRET.search(value['label']):
            value={**value,'value':'[已隐藏]'}
        return {k:safe(v) for k,v in value.items() if k in {'authorization_id','authorization_approval_id'} or not SECRET.search(k)}
    if isinstance(value,list):
        return [safe(v) for v in value]
    return text(value) if isinstance(value,str) else value

def parsed(value,default=None):
    fallback={} if default is None else default
    try:
        result=json.loads(value) if value else fallback
        return result if isinstance(result,type(fallback)) else fallback
    except (TypeError,ValueError):return fallback

def table(db,name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(name,)).fetchone())

def ledger(db,approval_id):
    prefix=approval_id+':'
    return [dict(r) for r in db.execute('SELECT id,account_id,counterparty,direction,amount_minor,category,occurred_at FROM transactions WHERE action_id=? OR substr(action_id,1,?)=? ORDER BY rowid',(approval_id,len(prefix),prefix))]

def operation(db,row):
    receipt=parsed(row['receipt_json']);summary=parsed(row['summary_json'])
    # A receipt without a matching ledger entry is not inferred to be a debit.
    entries=ledger(db,row['id'])
    debit=sum(r['amount_minor'] for r in entries if r['direction']=='out')
    credit=sum(r['amount_minor'] for r in entries if r['direction']=='in')
    warnings=[]
    if row['status']=='executed' and not receipt:warnings.append('状态为执行成功，但缺少回执，请人工核查。')
    if entries and row['status']!='executed':warnings.append('存在已记账流水，但操作状态未成功，请人工核查。')
    if receipt and receipt.get('action_id')!=row['id']:warnings.append('回执编号与确认操作不一致，请人工核查。')
    return safe({'id':row['id'],'task_id':row['task_id'],'tool':row['tool_name'],
                 'title':summary.get('title',TITLES.get(row['tool_name'],'业务操作')),'status':row['status'],
                 'status_label':'已保存定时授权，尚未执行交易' if receipt.get('scheduled') and receipt.get('schedule') else STATES.get(row['status'],row['status']),'created_at':row['created_at'],
                 'amount_minor':summary.get('amount_minor'),'fields':summary.get('fields',[]),
                 'arguments':parsed(row['args_json']),'receipt':receipt or None,'ledger':entries,
                 'debit_minor':debit,'credit_minor':credit,'warnings':warnings,
                 'settlement':'已保存定时授权，尚未执行交易或发生资金变动。' if receipt.get('scheduled') and receipt.get('schedule') else
                              '已取得模拟转账成功回执；未提供收款方独立到账流水。' if row['tool_name']=='transfer' and row['status']=='executed' else
                              '按本系统已落账流水核对，金额不代表消费统计。'})

def overview(db,conversation):
    approvals=db.execute('SELECT a.* FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE t.conversation_id=? ORDER BY a.rowid',(conversation,)).fetchall()
    operations=[operation(db,r) for r in approvals]
    return operations,{'task_count':db.execute('SELECT COUNT(*) FROM tasks WHERE conversation_id=?',(conversation,)).fetchone()[0],
                       'operation_count':len(operations),'executed_count':sum(o['status']=='executed' for o in operations),
                       'pending_count':sum(o['status']=='pending' for o in operations),
                       'debit_minor':sum(o['debit_minor'] for o in operations),'credit_minor':sum(o['credit_minor'] for o in operations)}

def detail(db,user,conversation):
    row=db.execute('SELECT * FROM conversations WHERE id=? AND deleted_at IS NULL',(conversation,)).fetchone()
    if not row:raise HTTPException(404,'找不到该用户的对话。')
    operations,summary=overview(db,conversation)
    task_rows=db.execute('SELECT * FROM tasks WHERE conversation_id=? ORDER BY rowid',(conversation,)).fetchall()
    tasks=[];timeline=[];node_owners={}
    def add(key,at,kind,title,description='',task_id=None,approval_id=None,data=None):
        timeline.append(safe({'id':key,'at':at,'kind':kind,'title':title,'description':description,
                              'task_id':task_id,'approval_id':approval_id,'data':data or {}}))
    for r in db.execute("SELECT * FROM messages WHERE conversation_id=? AND role='user' ORDER BY rowid",(conversation,)):
        add(r['id'],r['created_at'],'request','用户请求',r['text'],r['task_id'])
    for r in task_rows:
        state=parsed(r['state_json']);nodes=[dict(n) for n in state.get('dag',{}).get('nodes',[])]
        for n in nodes:
            key=(n['id'],n.get('tool'),n.get('finished_at'),n.get('receipt_id'))
            if state.get('recovery') and n.get('status')=='succeeded' and key in node_owners:
                n['carried_from_task']=node_owners[key]
            elif n.get('status')=='succeeded':node_owners[key]=r['id']
        checkpoints={k:state[k] for k in ('interruption','restart_checkpoint','manual_lock_checkpoint','recovery','scheduled_id','authorization_id') if k in state}
        tasks.append(safe({'id':r['id'],'status':r['status'],'status_label':STATES.get(r['status'],r['status']),
                           'created_at':r['created_at'],'error':r['error'],'dag':{'nodes':nodes},'checkpoints':checkpoints}))
        add(r['id'],r['created_at'],'task','任务创建记录','当前状态：'+STATES.get(r['status'],r['status']),r['id'],data={'channel':r['channel']})
        for n in nodes:
            if n.get('carried_from_task'):continue
            add(r['id']+':'+n['id'],n.get('finished_at') or n.get('started_at') or n.get('created_at') or r['created_at'],
                'step',n.get('title',TITLES.get(n.get('tool'),'业务步骤')),STATES.get(n['status'],n['status']),r['id'],
                data={'tool':n.get('tool'),'depends_on':n.get('depends_on',[]),'started_at':n.get('started_at'),
                      'finished_at':n.get('finished_at'),'error':n.get('error'),'receipt_id':n.get('receipt_id')})
        known={n['id'] for n in nodes}
        for i,m in enumerate(state.get('messages',[])):
            if m.get('role')!='tool' or m.get('tool_call_id') not in known:continue
            result=parsed(m.get('content'));display=result.get('display')
            if display:
                node=next(n for n in nodes if n['id']==m['tool_call_id'])
                add(f"{r['id']}:result:{i}",node.get('finished_at') or r['created_at'],'source',display.get('title','后台查询结果'),
                    display.get('note',''),r['id'],data={'display':display})
        for name,label in (('interruption','任务中断与资金核对'),('restart_checkpoint','服务停止时保存进度'),('manual_lock_checkpoint','安全锁定时保存进度'),('recovery','恢复任务并重新核对')):
            if name in state:
                value=state[name]
                add(r['id']+':'+name,value.get('requested_at') or value.get('saved_at') or r['created_at'],'control',label,
                    value.get('stage') or value.get('reason',''),r['id'],data=value)
        if r['error']:
            error_message=db.execute("SELECT created_at FROM messages WHERE task_id=? AND kind='error' ORDER BY rowid DESC LIMIT 1",(r['id'],)).fetchone()
            error_at=state.get('interruption',{}).get('requested_at') or (error_message[0] if error_message else None) or state.get('restart_checkpoint',{}).get('saved_at') or r['created_at']
            add(r['id']+':error',error_at,'error','任务异常或停止',r['error'],r['id'])
    approvals={o['id']:o for o in operations}; task_ids={r['id'] for r in task_rows}
    for op in operations:
        add(op['id']+':created',op['created_at'],'approval','生成确认卡',op['title'],op['task_id'],op['id'],{'fields':op['fields'],'status':op['status']})
        if op['receipt']:
            add(op['id']+':receipt',op['receipt'].get('executed_at') or op['created_at'],'receipt','执行回执',op['receipt'].get('title',op['title']),op['task_id'],op['id'],op['receipt'])
        for entry in op['ledger']:
            add(entry['id'],entry['occurred_at'],'ledger','已记账扣款' if entry['direction']=='out' else '已记账入账',entry['category'],op['task_id'],op['id'],entry)
    for r in db.execute("SELECT * FROM events WHERE conversation_id IN (?, '*') ORDER BY id",(conversation,)):
        data=parsed(r['data_json']);op_id=data.get('approval_id')
        if r['type']=='task' and data.get('id') in task_ids:
            add('event:'+str(r['id']),r['created_at'],'task','任务状态变更',STATES.get(data.get('status'),data.get('status','')),data['id'],data={'status':data.get('status'),'error':data.get('error')})
        if r['type'] in EVENTS and (op_id in approvals or r['conversation_id']==conversation):
            add('event:'+str(r['id']),r['created_at'],'verification' if r['type'].startswith('payment.') else 'confirmation',EVENTS[r['type']],data.get('reason',''),approvals.get(op_id,{}).get('task_id'),op_id,data)
    if table(db,'safety_events'):
        for r in db.execute('SELECT * FROM safety_events ORDER BY id'):
            ref=r['reference']
            if ref in approvals or ref in task_ids:
                add('safety:'+str(r['id']),r['created_at'],'security',SAFETY.get(r['kind'],'安全事件'),r['detail'],ref if ref in task_ids else approvals[ref]['task_id'],ref if ref in approvals else None,{'scope':r['scope']})
    if table(db,'human_sessions'):
        for r in db.execute('SELECT h.* FROM human_sessions h WHERE conversation_id=? ORDER BY rowid',(conversation,)):
            add(r['id'],r['created_at'],'human','转交人工客服','状态：'+{'waiting':'等待接入','active':'服务中','closed':'已结束'}.get(r['status'],r['status']),data={'operator':r['operator']})
            for action in db.execute('SELECT * FROM human_audit WHERE session_id=? ORDER BY rowid',(r['id'],)):
                add(action['id'],action['created_at'],'human',{'claimed':'人工接入','closed':'人工服务结束','replied':'人工回复','requested':'提交人工请求'}.get(action['action'],'人工处理'),action['operator'])
    timeline.sort(key=lambda e:(e['at'],e['id']))
    return {'user':{'id':user['id'],'username':text(user['username'])},'conversation':{'id':conversation,'title':text(row['title']),'created_at':row['created_at']},
            'summary':summary,'tasks':tasks,'operations':operations,'timeline':timeline,
            'note':'金额只来自本对话确认操作关联的已落账流水，恢复任务不重复累计。本人划转包含扣款和入账，不等于消费或收入；旧记录未留存的验证结果不推断为已通过。'}

def csv_file(rows,headers):
    out=io.StringIO(newline='');writer=csv.writer(out);writer.writerow(headers)
    for row in rows:
        # Avoid spreadsheet formulas when exported user text starts with =,+,-,@.
        writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in row])
    return '\ufeff'+out.getvalue()

def register(app,authenticated,users,tenant,log,console):
    @app.get('/api/admin/audit/conversations')
    def conversations(user_id: str | None=None,query: str=Query('',max_length=100),status: str | None=None,
                      from_date: date | None=None,to_date: date | None=None,offset: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=100),operator=Depends(authenticated)):
        if status and status not in {'queued','running','waiting_confirmation','completed','failed','cancelled','none'}:
            raise HTTPException(422,'任务状态无效。')
        if from_date and to_date and from_date>to_date:raise HTTPException(422,'开始日期不能晚于结束日期。')
        items=[];options=[];found=user_id is None
        for user,database in users():
            options.append({'id':user['id'],'username':text(user['username'])})
            if user_id and user_id!=user['id']:continue
            found=True
            with database.transaction() as db:
                for r in db.execute("SELECT c.*,COALESCE((SELECT status FROM tasks WHERE conversation_id=c.id ORDER BY rowid DESC LIMIT 1),'none') AS task_status FROM conversations c WHERE c.deleted_at IS NULL ORDER BY updated_at DESC"):
                    day=(r['updated_at'] or r['created_at'])[:10]
                    if (query.lower() not in r['title'].lower() or (status and status!=r['task_status']) or
                        (from_date and day<from_date.isoformat()) or (to_date and day>to_date.isoformat())):continue
                    _,summary=overview(db,r['id'])
                    items.append({'user_id':user['id'],'username':text(user['username']),'id':r['id'],'title':text(r['title']),
                                  'updated_at':r['updated_at'] or r['created_at'],'status':r['task_status'],
                                  'status_label':STATES.get(r['task_status'],'暂无任务'),**summary})
        if not found:raise HTTPException(404,'用户不存在。')
        items.sort(key=lambda r:(r['updated_at'],r['user_id'],r['id']),reverse=True)
        return {'items':items[offset:offset+limit],'total':len(items),'users':options,'offset':offset,'limit':limit}

    def load(user_id,conversation_id):
        user,database=tenant(user_id)
        with database.transaction() as db:return detail(db,user,conversation_id)

    @app.get('/api/admin/audit/users/{user_id}/conversations/{conversation_id}')
    def inspect(user_id: str,conversation_id: str,operator=Depends(authenticated)):
        result=load(user_id,conversation_id);log('audit_view',user_id,conversation_id)
        return result

    @app.get('/api/admin/audit/users/{user_id}/conversations/{conversation_id}/export')
    def export(user_id: str,conversation_id: str,format: str=Query('json',pattern='^(json|csv)$'),operator=Depends(authenticated)):
        result=load(user_id,conversation_id)
        canonical=json.dumps(result,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        checksum=hashlib.sha256(canonical).hexdigest()
        if format=='json':
            content=json.dumps({'exported_at':now(),'sha256':checksum,'checksum_note':'SHA-256用于核对本次导出内容完整性，不代表数据库历史不可篡改。','data':result},ensure_ascii=False,indent=2)
            media='application/json'
        else:
            content=csv_file([[r['at'],r['title'],r['description'],r['task_id'] or '',r['approval_id'] or '',json.dumps(r['data'],ensure_ascii=False)] for r in result['timeline']],['时间','事件','说明','任务编号','操作编号','依据'])
            media='text/csv'
            checksum=hashlib.sha256(content.encode()).hexdigest()
        log('audit_export',user_id,conversation_id)
        return Response(content,media_type=media,headers={'Content-Disposition':f'attachment; filename="audit-{hashlib.sha256((user_id+conversation_id).encode()).hexdigest()[:16]}.{format}"','X-Audit-SHA256':checksum})

    @app.get('/api/admin/audit/users/{user_id}/security')
    def security(user_id: str,operator=Depends(authenticated)):
        _,database=tenant(user_id)
        with database.connection() as db:
            records=[safe({**dict(r),'title':SAFETY.get(r['kind'],'安全事件')}) for r in db.execute('SELECT * FROM safety_events ORDER BY id DESC LIMIT 300')] if table(db,'safety_events') else []
        log('audit_security',user_id,None)
        return {'items':records,'note':'账户级记录可能来自其他对话；未关联操作编号的事件不归入所选对话。'}

    @app.get('/api/admin/audit/administrators')
    def administrators(user_id: str | None=None,operator=Depends(authenticated)):
        with console() as db:
            records=[{**dict(r),'title':ADMIN.get(r['action'],'管理员操作')} for r in db.execute('SELECT * FROM admin_audit WHERE (? IS NULL OR user_id=?) ORDER BY rowid DESC LIMIT 300',(user_id,user_id))]
        return {'items':records}
