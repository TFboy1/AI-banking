"""One-shot preauthorized transfers; authorization is not a payment receipt."""
import asyncio
import hashlib
import json
import sqlite3
from decimal import Decimal
from datetime import datetime,timedelta
from pathlib import Path

from fastapi import Depends
from pydantic import BaseModel,ConfigDict,Field

from .db import TZ,Database,add_message,dumps,event,now,uid
from .scheduled_models import ScheduleTransfer

SCHEMA='''
CREATE TABLE IF NOT EXISTS scheduled_transfers(
 id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,authorization_id TEXT NOT NULL UNIQUE,
 transfer_tool TEXT NOT NULL,args_json TEXT NOT NULL,source_json TEXT NOT NULL,target_json TEXT NOT NULL,
 security_stamp TEXT NOT NULL,authorization_hash TEXT NOT NULL,execute_at TEXT NOT NULL,status TEXT NOT NULL,
 authorized_at TEXT NOT NULL,finished_at TEXT,execution_id TEXT,receipt_json TEXT,error TEXT,note TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS scheduled_due ON scheduled_transfers(status,execute_at);
'''
LABELS={'scheduled':'已授权，等待执行','executed':'已执行','cancelled':'已取消','failed':'执行失败','needs_reauthorization':'需要重新授权'}
GRACE=timedelta(minutes=5)

def clock():return datetime.now(TZ)

def migrate(db):
    if 'note' not in {r[1] for r in db.execute('PRAGMA table_info(scheduled_transfers)')}:
        db.execute("ALTER TABLE scheduled_transfers ADD COLUMN note TEXT NOT NULL DEFAULT ''")

def when(value):
    from .bank import BankError
    try:
        result=datetime.fromisoformat(value)
        return (result.replace(tzinfo=TZ) if result.tzinfo is None else result.astimezone(TZ))
    except (ValueError,TypeError):raise BankError('请提供明确的执行日期和时间，例如2026-10-20T09:00:00+08:00。') from None

def stamp(db):
    person=db.execute('SELECT phone,pin_hash FROM app_identity WHERE id=1').fetchone()
    policy=db.execute('SELECT threshold_minor,version FROM payment_policy WHERE id=1').fetchone()
    circuit=db.execute("SELECT generation FROM safety_circuits WHERE scope='transactions'").fetchone()
    return hashlib.sha256(dumps({'identity':list(person) if person else None,'policy':list(policy),'generation':circuit[0] if circuit else 0}).encode()).hexdigest()

def snapshot(bank,db,recipient,account):
    from .bank import BankError
    source=db.execute('SELECT id,name,kind FROM accounts WHERE id=?',(account,)).fetchone()
    candidates=bank._recipients(db,recipient)
    if not source or len(candidates)!=1:raise BankError('付款账户不存在，或收款人不唯一，请先核对账户和收款银行卡。')
    target=candidates[0]
    source_data={**dict(source),'cards':[dict(r) for r in db.execute('SELECT id,name,last4 FROM cards WHERE account_id=? ORDER BY id',(account,))]}
    private={'id':target['id'],'kind':target['kind'],'name':target['name'],'detail':target['detail']}
    if target['kind']=='contact':
        row=db.execute('SELECT c.bank,c.last4,d.card_number FROM contacts c LEFT JOIN contact_details d ON d.contact_id=c.id WHERE c.id=?',(target['id'],)).fetchone()
        private.update(dict(row))
    else:
        row=db.execute('SELECT id,name,kind FROM accounts WHERE id=?',(target['id'],)).fetchone()
        private['account']=dict(row)
        private['cards']=[dict(r) for r in db.execute('SELECT id,name,last4 FROM cards WHERE account_id=? ORDER BY id',(target['id'],))]
    return source_data,private

def transfer(bank,db,args):
    from .bank import BankError
    candidates=bank._recipients(db,args['recipient'])
    if len(candidates)!=1:raise BankError('收款人有多个匹配或不存在，请先选择具体收款银行卡。')
    target=candidates[0]
    if target['kind']=='own_account':
        tool='transfer_own_funds';actual={'account_id':args['account_id'],'target_account_id':target['id'],'amount_yuan':args['amount_yuan']}
    else:
        tool='transfer';actual={'account_id':args['account_id'],'recipient':target['id'],'amount_yuan':args['amount_yuan'],'note':args.get('note',''),'plan_id':None}
    return tool,actual,bank._check_action(db,tool,actual)

def replacement(db,args):
    from .bank import BankError
    if args.get('replaces_id'):
        row=db.execute('SELECT * FROM scheduled_transfers WHERE id=?',(args['replaces_id'],)).fetchone()
        if not row or row['status'] not in {'scheduled','failed','needs_reauthorization'}:raise BankError('原预约已执行或取消，不能替换；请查询最新预约状态。')
        return row

def summary(bank,db,args):
    from .bank import BankError
    due=when(args['execute_at'])
    if due<=clock():raise BankError('执行时间已到或已过去，请重新选择未来时间；当前没有扣款。')
    if due>clock()+timedelta(days=365):raise BankError('单次预约最多提前365天。')
    old=replacement(db,args)
    _,_,base=transfer(bank,db,args)
    args['execute_at']=due.isoformat(timespec='seconds')
    args['recipient']=bank._recipients(db,args['recipient'])[0]['id']
    fields=[*base['fields'],{'label':'执行时间（北京时间）','value':due.strftime('%Y-%m-%d %H:%M:%S')},
            {'label':'提前授权','value':'现在验证并授权，到点自动执行；现在不扣款、不冻结余额。'},
            {'label':'到期规则','value':'重新检查余额、对象和安全状态；离线错过5分钟窗口则停止，不自动补扣或重试。'}]
    if args.get('note') and not any(f['label']=='备注' for f in fields):fields.append({'label':'备注','value':args['note']})
    if old:
        old_public=public(old)
        fields.append({'label':'替换原预约','value':f"{old['id']} · {old_public['recipient_name']} · ¥{old_public['amount_yuan']} · {when(old['execute_at']).strftime('%Y-%m-%d %H:%M:%S')}"})
        fields.append({'label':'修改预约','value':'新授权成功后替换原预约；验证完成前原预约仍有效。'})
    return {'title':'确认定时转账授权','amount_minor':base['amount_minor'],'fields':fields,'scheduled':True,'execute_at':args['execute_at']}

def seal(row):
    keys=('id','conversation_id','authorization_id','transfer_tool','args_json','source_json','target_json','security_stamp','execute_at','note')
    return hashlib.sha256(dumps({k:row[k] for k in keys}).encode()).hexdigest()

def public(row):
    target=json.loads(row['target_json']);source=json.loads(row['source_json']);args=json.loads(row['args_json'])
    amount=target.get('amount_minor',int(Decimal(args.get('amount_yuan','0'))*100))
    purpose=[f['value'] for f in target.get('terms',{}).get('fields',[]) if f['label'] in {'收款人','商户','产品','店铺','到账账户'}]
    if target.get('kind')!='scheduled_action':purpose.append(target['name'])
    if row['note']:purpose.append(row['note'])
    return {'id':row['id'],'conversation_id':row['conversation_id'],'authorization_id':row['authorization_id'],
            'purpose':' · '.join(dict.fromkeys(purpose)),
            'generic':target.get('kind')=='scheduled_action','tool':row['transfer_tool'],
            'transaction_name':'转账' if row['transfer_tool'] in {'transfer','transfer_own_funds'} else target['name'],
            'recipient':target['id'],'recipient_name':target['name'],'recipient_detail':target['detail'],
            'account_id':source['id'],'account_name':source['name'],'amount_yuan':str(Decimal(amount)/100),
            'amount_minor':amount,'note':row['note'],
            'execute_at':row['execute_at'],'status':row['status'],'status_label':LABELS.get(row['status'],row['status']),
            'authorized_at':row['authorized_at'],'finished_at':row['finished_at'],'execution_id':row['execution_id'],
            'error':row['error'],'receipt':json.loads(row['receipt_json']) if row['receipt_json'] else None}

def authorize(bank,db,args,authorization_id):
    from .bank import BankError
    from .payment_security import risk
    original=db.execute('SELECT a.*,t.conversation_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?',(authorization_id,)).fetchone()
    if not original or original['tool_name']!='schedule_transfer' or original['status']!='pending':raise BankError('没有有效的定时授权确认。')
    from .bank import validate
    if validate('schedule_transfer',json.loads(original['args_json']))!=args:
        raise BankError('授权参数与确认卡不一致，请重新核对并验证。')
    for kind in ['payment.pin_verified']+(['payment.sms_verified'] if risk(db,original,args)['sms_required'] else []):
        if not db.execute("SELECT 1 FROM events WHERE type=? AND json_extract(data_json,'$.approval_id')=?",(kind,authorization_id)).fetchone():
            raise BankError('定时授权必须提前完成支付密码及所需短信验证，不能由模型跳过。')
    summary(bank,db,args)
    tool,actual,_=transfer(bank,db,args)
    source,target=snapshot(bank,db,args['recipient'],args['account_id'])
    row={'id':uid('schedule_'),'conversation_id':original['conversation_id'],'authorization_id':authorization_id,
         'transfer_tool':tool,'args_json':dumps(actual),'source_json':dumps(source),'target_json':dumps(target),
         'security_stamp':stamp(db),'execute_at':args['execute_at'],'note':args.get('note','')}
    row['authorization_hash']=seal(row)
    if args.get('replaces_id'):cancel(db,args['replaces_id'],'重新授权后替换原预约。')
    db.execute('INSERT INTO scheduled_transfers(id,conversation_id,authorization_id,transfer_tool,args_json,source_json,target_json,security_stamp,authorization_hash,execute_at,status,authorized_at,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               tuple(row[k] for k in ('id','conversation_id','authorization_id','transfer_tool','args_json','source_json','target_json','security_stamp','authorization_hash','execute_at'))+('scheduled',now(),row['note']))
    event(db,row['conversation_id'],'scheduled.authorized',{'approval_id':authorization_id,'schedule_id':row['id'],'execute_at':row['execute_at']})
    return public(db.execute('SELECT * FROM scheduled_transfers WHERE id=?',(row['id'],)).fetchone())

def cancel(db,key,reason='用户取消预约，后续不再扣款。'):
    from .bank import BankError
    row=db.execute('SELECT * FROM scheduled_transfers WHERE id=?',(key,)).fetchone()
    if not row:raise BankError('找不到当前用户的定时交易。')
    if row['status']=='cancelled':return public(row)
    if row['status'] not in {'scheduled','needs_reauthorization','failed'}:raise BankError('交易已经执行，不能取消或撤销已成功的交易。')
    db.execute("UPDATE scheduled_transfers SET status='cancelled',finished_at=?,error=? WHERE id=?",(now(),reason,key))
    event(db,row['conversation_id'],'scheduled.cancelled',{'approval_id':row['authorization_id'],'schedule_id':key,'reason':reason})
    return public(db.execute('SELECT * FROM scheduled_transfers WHERE id=?',(key,)).fetchone())

def listing(db):
    return [public(r) for r in db.execute('SELECT * FROM scheduled_transfers ORDER BY authorized_at DESC,rowid DESC')]

def query(db):
    from .bank import table,yuan
    items=listing(db)
    return {'schedules':items,'display':table('定时交易预约',['编号','交易','资金账户','金额','北京时间','状态'],
           [[r['id'],r['recipient_name'],r['account_name'],yuan(r['amount_minor']),when(r['execute_at']).strftime('%Y-%m-%d %H:%M:%S'),r['status_label']] for r in items],
           note='提前授权不扣款、不冻结余额。到点重新检查后执行，失败不自动重试；修改需重新验证。')}

def process_due(database,as_of=None,settings=None):
    """Atomic per schedule: one transaction commits the transfer, receipt and terminal state."""
    from .bank import Bank,BankError
    from .safety import check_business
    bank=Bank(database);instant=as_of or clock();changed=0
    with database.connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduled_transfers'").fetchone():db.executescript(SCHEMA)
        migrate(db)
        keys=[r[0] for r in db.execute("SELECT id FROM scheduled_transfers WHERE status='scheduled' AND (execute_at<=? OR transfer_tool='purchase_stock_basket') ORDER BY execute_at,id",(instant.isoformat(),))]
    for key in keys:
        # Fetch quotes without a write lock. A user may cancel while the
        # independent platform responds; the final transaction rechecks status.
        refreshed=None;quote_error=None
        with database.connection() as db:
            planned=db.execute('SELECT * FROM scheduled_transfers WHERE id=?',(key,)).fetchone()
            if planned and planned['transfer_tool']!='purchase_stock_basket' and planned['status']=='scheduled' and planned['authorization_hash']==seal(planned) and (as_of or clock())<=when(planned['execute_at'])+GRACE and json.loads(planned['target_json']).get('kind')=='scheduled_action':
                from .scheduled_actions import refresh_quote
                from .config import Settings
                try:refreshed=refresh_quote(db,planned['transfer_tool'],json.loads(planned['args_json']),settings or Settings(),database)
                except BankError as exc:quote_error=str(exc)
        with database.transaction() as db:
            row=db.execute('SELECT * FROM scheduled_transfers WHERE id=?',(key,)).fetchone()
            moment=as_of or clock()
            if not row or row['status']!='scheduled' or (when(row['execute_at'])>moment and row['transfer_tool']!='purchase_stock_basket'):continue
            error=None;status='failed';execution_id=None;receipt=None
            try:
                if row['authorization_hash']!=seal(row):
                    status='needs_reauthorization';raise BankError('授权参数校验失败，需要重新验证。')
                generic=json.loads(row['target_json']).get('kind')=='scheduled_action'
                original=db.execute('SELECT status,receipt_json FROM approvals WHERE id=? AND tool_name=?',(row['authorization_id'],row['transfer_tool'] if generic else 'schedule_transfer')).fetchone()
                if not original or original['status']!='executed' or json.loads(original['receipt_json'] or '{}').get('schedule',{}).get('id')!=key:
                    status='needs_reauthorization';raise BankError('找不到对应的成功授权回执，预约已停止。')
                if row['transfer_tool']=='purchase_stock_basket':
                    status='needs_reauthorization';raise BankError('股票买入已取消定时功能，原预约失效；请在交易时段重新发起即时买入。')
                if moment>when(row['execute_at'])+GRACE:
                    status='needs_reauthorization';raise BankError('后台离线或延迟已超过5分钟执行窗口，未补扣，请重新授权。')
                check_business(db,row['transfer_tool'])
                if row['security_stamp']!=stamp(db):
                    status='needs_reauthorization';raise BankError('密码、手机号、安全门槛或安全保护状态已变化，原授权失效。')
                if not db.execute('SELECT 1 FROM conversations WHERE id=? AND deleted_at IS NULL',(row['conversation_id'],)).fetchone():raise BankError('原对话已删除，预约已停止，未扣款。')
                actual=json.loads(row['args_json']);target=json.loads(row['target_json'])
                if generic:
                    from .scheduled_actions import snapshot as action_snapshot
                    if quote_error:
                        status='needs_reauthorization';raise BankError(quote_error)
                    if refreshed:
                        quote_table,payload,expiry,quote_id=refreshed
                        db.execute(f'UPDATE {quote_table} SET payload=?,expires=? WHERE id=?',(payload,expiry,quote_id))
                    source,current=action_snapshot(bank,db,row['transfer_tool'],actual)
                else:
                    source,current=snapshot(bank,db,target['id'],actual['account_id'])
                if dumps(source)!=row['source_json'] or dumps(current)!=row['target_json']:
                    status='needs_reauthorization';raise BankError('资金账户、交易对象、价格或内容已变化，原授权失效。')
                summary=bank._check_action(db,row['transfer_tool'],actual)
                from .payment_security import risk
                assessment=risk(db,{'tool_name':row['transfer_tool'],'summary_json':dumps(summary)},actual)
                if assessment['sms_required'] and not db.execute("SELECT 1 FROM events WHERE type='payment.sms_verified' AND json_extract(data_json,'$.approval_id')=?",(row['authorization_id'],)).fetchone():
                    status='needs_reauthorization';raise BankError('执行时已升级红色验证，原授权没有短信验证，停止交易；请重新授权。')
                task_id=uid('task_');execution_id=uid('op_')
                db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(task_id,row['conversation_id'],'running','scheduled',dumps({'scheduled_id':key,'authorization_id':row['authorization_id']}),None,now()))
                message=add_message(db,row['conversation_id'],task_id,'assistant','approval','按已验证的定时授权执行。',{'approval_id':execution_id,'status':'pending',**summary})
                db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?)',(execution_id,task_id,message['id'],row['transfer_tool'],row['args_json'],dumps(summary),'pending',None,now(),now()))
                db.execute('SAVEPOINT scheduled_payment')
                try:
                    receipt=bank.execute(db,row['transfer_tool'],actual,execution_id)
                    if row['transfer_tool']=='transfer_own_funds' and row['note']:
                        db.execute('UPDATE transactions SET description=? WHERE action_id IN (?,?)',(row['note'],execution_id+':out',execution_id+':in'))
                        receipt['fields'].append({'label':'备注','value':row['note']})
                except BankError:
                    db.execute('ROLLBACK TO scheduled_payment');db.execute('RELEASE scheduled_payment');raise
                db.execute('RELEASE scheduled_payment')
                from .db import update_message
                db.execute("UPDATE approvals SET status='executed',receipt_json=? WHERE id=?",(dumps(receipt),execution_id))
                update_message(db,message['id'],{'approval_id':execution_id,'status':'executed',**summary,'receipt':receipt})
                db.execute("UPDATE tasks SET status='completed' WHERE id=?",(task_id,))
                status='executed'
            except BankError as exc:
                error=str(exc)
                if execution_id:
                    db.execute("UPDATE approvals SET status='failed' WHERE id=?",(execution_id,))
                    db.execute("UPDATE tasks SET status='failed',error=? WHERE id=(SELECT task_id FROM approvals WHERE id=?)",(error,execution_id))
                    from .db import update_message
                    update_message(db,message['id'],{'approval_id':execution_id,'status':'failed',**summary,'error':error})
            db.execute('UPDATE scheduled_transfers SET status=?,finished_at=?,execution_id=?,receipt_json=?,error=? WHERE id=?',(status,now(),execution_id,dumps(receipt) if receipt else None,error,key))
            event(db,row['conversation_id'],'scheduled.'+status,{'approval_id':execution_id or row['authorization_id'],'authorization_id':row['authorization_id'],'schedule_id':key,'reason':error or ''})
            add_message(db,row['conversation_id'],None,'assistant','text',('定时交易已执行，请以执行回执和账户流水为准。' if status=='executed' else '定时交易未执行，未发生资金变动：'+error),{'schedule_id':key,'_feishu_notify':True})
            event(db,'*','bank.updated',{});changed+=1
    return changed

async def loop(database,notify,settings=None):
    while True:
        try:
            if await asyncio.to_thread(process_due,database,settings=settings):await notify()
        except Exception:
            # A rolled-back database failure can be retried safely, using the same schedule ID.
            import logging
            logging.getLogger(__name__).exception('Scheduled transfer database unavailable')
        await asyncio.sleep(1)

async def directory_loop(directory,settings=None):
    while True:
        try:
            with directory.connection() as db:users=[dict(r) for r in db.execute('SELECT database_path FROM users')]
        except sqlite3.Error:
            import logging
            logging.getLogger(__name__).exception('Scheduled transfer directory unavailable')
            await asyncio.sleep(1);continue
        for user in users:
            try:
                if await asyncio.to_thread(process_due,Database(Path(user['database_path']),seed_demo=False),settings=settings):
                    for app in list(directory.apps.values()):await app.state.agent.notify()
            except Exception:
                import logging
                logging.getLogger(__name__).exception('Scheduled transfer tenant database unavailable')
        await asyncio.sleep(1)

class ScheduleBody(ScheduleTransfer):
    conversation_id: str=Field(min_length=1,max_length=100)
class CancelBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    confirmation: str=Field(pattern='^CANCEL$')

def register(app,database,agent,session,resolve_conversation):
    @app.get('/api/scheduled-transfers')
    def schedules(session_key: str=Depends(session)):
        with database.connection() as db:return {'items':listing(db)}

    @app.get('/api/scheduled-transfers/recipients')
    def recipients(query: str,session_key: str=Depends(session)):
        return agent.bank.query('find_recipients',{'query':query})

    @app.post('/api/scheduled-transfers')
    async def create(body: ScheduleBody,session_key: str=Depends(session)):
        with database.transaction() as db:
            conversation=resolve_conversation(db,session_key,body.conversation_id)
            result=agent.create_direct_approval(db,conversation,'schedule_transfer',body.model_dump(exclude={'conversation_id'}))
        await agent.notify();return result

    @app.post('/api/scheduled-transfers/{key}/cancel')
    async def remove(key: str,body: CancelBody,session_key: str=Depends(session)):
        with database.transaction() as db:result=cancel(db,key)
        await agent.notify();return result
