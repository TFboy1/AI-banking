"""Durable birthday plans. Reminders can search; only existing payment confirmation can pay."""
import asyncio,calendar,json,logging
from datetime import date,datetime,time,timedelta
from pathlib import Path
from decimal import Decimal
from pydantic import BaseModel,ConfigDict,Field
from fastapi import Depends
from .db import TZ,Database,add_message,dumps,event,now,uid

SCHEMA='''
CREATE TABLE IF NOT EXISTS birthday_plans(
 id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,contact_id TEXT NOT NULL,contact_name TEXT NOT NULL,
 birthday_snapshot TEXT NOT NULL,event_date TEXT NOT NULL,remind_at TEXT NOT NULL,account_id TEXT NOT NULL,
 budget_minor INTEGER NOT NULL,commitment_id TEXT NOT NULL UNIQUE,status TEXT NOT NULL,version INTEGER NOT NULL,
 reminder_conversation_id TEXT,reminder_read_at TEXT,recommendation_json TEXT,order_id TEXT,spent_minor INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS birthday_active_person_date ON birthday_plans(contact_id,event_date) WHERE status IN ('active','paused','completed');
'''
STATUSES={'active':'进行中','paused':'已暂停','completed':'已完成','cancelled':'已取消','expired':'已过期','needs_review':'资料已变化，需重新确认'}
log=logging.getLogger(__name__)
def clock():return datetime.now(TZ)
def error(text):
    from .bank import BankError
    raise BankError(text)
def yuan(amount):return f'¥{Decimal(amount)/100:,.2f}'
def anniversary(birthday,year):
    born=date.fromisoformat(birthday)
    return date(year,born.month,min(born.day,calendar.monthrange(year,born.month)[1]))
def next_birthday(birthday,today):
    result=anniversary(birthday,today.year)
    return anniversary(birthday,today.year+1) if result<today else result
def get(db,key):
    row=db.execute('SELECT * FROM birthday_plans WHERE id=?',(key,)).fetchone()
    if not row:error('生日计划不存在，请重新查询。')
    return dict(row)
def person(db,key):
    from .contacts import person_key
    key=person_key(db,key)
    row=db.execute('SELECT c.id,c.name,p.birthday,p.special_attention FROM contacts c LEFT JOIN contact_profiles p ON p.person_id=c.id WHERE c.id=?',(key,)).fetchone()
    if not row or not row['birthday']:error('该联系人尚未设置生日，请先让用户补充并在联系人资料中保存，不能猜测生日。')
    return dict(row)
def available(db,account):
    row=db.execute("SELECT * FROM accounts WHERE id=? AND kind NOT IN ('bank_card_unlinked','investment')",(account,)).fetchone()
    if not row:error('预算来源账户不存在或不可用。')
    if db.execute("SELECT 1 FROM cards WHERE account_id=? AND status<>'active'",(account,)).fetchone():error('预算来源银行卡已冻结，请选择可用账户。')
    reserved=db.execute("SELECT COALESCE(SUM(remaining_minor),0) FROM commitments WHERE account_id=? AND status='active'",(account,)).fetchone()[0]
    return dict(row),max(0,row['balance_minor']-reserved)
def preview(db,args,at=None):
    at=at or clock();p=person(db,args['contact_id']);event_day=next_birthday(p['birthday'],at.date())
    if args.get('event_date'):
        try:event_day=date.fromisoformat(args['event_date'])
        except ValueError:error('生日日期无效。')
        if event_day!=anniversary(p['birthday'],event_day.year):error('指定日期与联系人生日不一致，请先核对联系人资料。')
    if not 0<=(event_day-at.date()).days<=366:error('请选择今天起366天内的生日，本次没有预留资金。')
    if args.get('occasion_month') and event_day.strftime('%Y-%m')!=args['occasion_month']:error('联系人生日不在指定月份，请核对日期，尚未建立计划。')
    amount=int(Decimal(args['budget_yuan'])*100)
    if amount<=0:error('生日预算必须大于0元。')
    account,free=available(db,args['account_id'])
    due=datetime.combine(event_day-timedelta(days=args['remind_days']),time.fromisoformat(args['remind_time']),TZ)
    return {'contact_id':p['id'],'contact_name':p['name'],'birthday_snapshot':p['birthday'],'event_date':event_day.isoformat(),
        'remind_at':due.isoformat(),'account_id':account['id'],'account_name':account['name'],'budget_minor':amount,
        'available_minor':free,'shortfall_minor':max(0,amount-free),'affordable':free>=amount,'reminder_overdue':due<=at,'notify_enabled':bool(p['special_attention'])}
def public(db,row):
    result={k:row[k] for k in ('id','contact_id','contact_name','event_date','remind_at','account_id','budget_minor','status','version','reminder_conversation_id','order_id','spent_minor','created_at','updated_at')}
    account=db.execute('SELECT name FROM accounts WHERE id=?',(row['account_id'],)).fetchone()
    commitment=db.execute('SELECT remaining_minor,status FROM commitments WHERE id=?',(row['commitment_id'],)).fetchone()
    result.update(status_label=STATUSES[row['status']],account_name=account[0] if account else '账户已移除',
                  reserved_minor=commitment['remaining_minor'] if commitment and commitment['status']=='active' else 0)
    profile=db.execute('SELECT special_attention FROM contact_profiles WHERE person_id=?',(row['contact_id'],)).fetchone()
    result['notify_enabled']=bool(profile and profile[0])
    recommendation=json.loads(row['recommendation_json']) if row['recommendation_json'] else None
    result['recommendation']=recommendation
    done=row['status']=='completed';inactive=row['status'] not in {'active','paused'}
    result['steps']=[{'id':'contact','title':'核对联系人和生日','depends_on':[],'status':'succeeded'},
        {'id':'budget','title':'预留生日预算','depends_on':['contact'],'status':'succeeded' if not inactive or done else 'cancelled'},
        {'id':'reminder','title':'到期新建提醒会话','depends_on':['budget'],'status':'disabled' if not result['notify_enabled'] else 'succeeded' if row['reminder_conversation_id'] else 'blocked' if inactive or row['status']=='paused' else 'waiting'},
        {'id':'recommend','title':'查询蛋糕与餐饮菜单','depends_on':['reminder'],'status':'succeeded' if recommendation and recommendation.get('menus') else 'needs_input' if row['reminder_conversation_id'] else 'waiting'},
        {'id':'order','title':'选择商品并验证后下单','depends_on':['recommend'],'status':'succeeded' if done else 'blocked' if inactive or row['status']=='paused' else 'waiting'}]
    return result
def listing(db):return [public(db,dict(r)) for r in db.execute('SELECT * FROM birthday_plans ORDER BY event_date,created_at')]
def query(bank,db,name,args):
    from .bank import table
    if name=='get_birthday_context':
        values=bank._list_contacts(db,{'keyword':args['keyword']})['contacts']
        for p in values:
            p['event_date']=next_birthday(p['birthday'],clock().date()).isoformat() if p['birthday'] else None
        return {'contacts':values,'needs_clarification':len(values)!=1 or not values[0]['birthday'] if values else True,
            'display':table('生日联系人核对',['姓名','关系','生日','下次生日'],[[p['name'],p['relationship'],p['birthday'] or '未设置',p['event_date'] or '需补充资料'] for p in values],note='按人员去重，多人须明确选择；生日缺失不能猜测。闰日生日在非闰年按2月末处理。')}
    if name=='preview_birthday_plan':
        p=preview(db,args)
        return {**p,'display':table('生日计划预览',['联系人','生日','提醒时间','预算来源'],[[p['contact_name'],p['event_date'],p['remind_at'] if p['notify_enabled'] else '未特别关注，不自动提醒',p['account_name']]],metrics=[{'label':'生日预算','value':yuan(p['budget_minor'])},{'label':'可自由安排','value':yuan(p['available_minor'])},{'label':'预算缺口','value':yuan(p['shortfall_minor'])}],note='这只是预览，尚未建立计划或预留资金。仅特别关注联系人自动提醒，不自动下单；其他计划可手动查看和办理。'+('原提醒时点已过，保存后尽快提醒。' if p['reminder_overdue'] and p['notify_enabled'] else ''))}
    plans=listing(db)
    return {'birthday_plans':plans,'display':table('生日计划',['联系人','生日','状态','剩余预留'],[[p['contact_name'],p['event_date'],p['status_label'],yuan(p['reserved_minor'])] for p in plans],note='暂停保留预算；取消或过期释放未用预算。已完成订单不因取消计划而退款。')}
def summary(db,name,args):
    if name=='create_birthday_plan':
        p=preview(db,args)
        if not p['affordable']:error('可自由安排资金不足，生日预算还差'+yuan(p['shortfall_minor'])+'；请减少预算或选择其他账户。')
        if db.execute("SELECT 1 FROM birthday_plans WHERE contact_id=? AND event_date=? AND status IN ('active','paused','completed')",(p['contact_id'],p['event_date'])).fetchone():error('这个联系人该次生日已有计划，请先查看生日计划，不能重复预留。')
        args.update(contact_id=p['contact_id'],event_date=p['event_date'])
        args['plan_id']=args.get('plan_id') or uid('birthday_')
        fields=[('联系人',p['contact_name']),('生日',p['event_date']),('预算来源',p['account_name']),('预留预算',yuan(p['budget_minor'])),('提醒时间',p['remind_at']+'（北京时间）' if p['notify_enabled'] else '未特别关注，不自动提醒'),('提醒策略','只通知特别关注的联系人，不自动更改关注状态'),('执行方式','选好商品后再确认并验证付款'),('预留说明','不扣款、不转账；暂停保留预算，取消或过期释放；下单后释放剩余预算')]
        if p['reminder_overdue'] and p['notify_enabled']:fields.append(('提醒补发','原提醒时点已过，保存后尽快提醒'))
        return {'title':'确认生日计划与预算预留','fields':[{'label':k,'value':v} for k,v in fields]}
    p=get(db,args['plan_id'])
    if p['version']!=args['version']:error('生日计划已变化，请先查询最新计划再操作。')
    allowed={'pause':{'active'},'resume':{'paused'},'cancel':{'active','paused','needs_review'}}
    if p['status'] not in allowed[args['action']]:error('当前状态不支持这项操作，请查看最新生日计划。')
    if args['action']=='resume':validate_plan(db,p['id'])
    label={'pause':'暂停','resume':'继续','cancel':'取消'}[args['action']]
    return {'title':'确认'+label+'生日计划','fields':[{'label':'联系人','value':p['contact_name']},{'label':'生日','value':p['event_date']},{'label':'预算处理','value':'释放未使用预算；已完成订单保留' if args['action']=='cancel' else '继续保留预算，不自动扣款'}]}
def release(db,p):
    db.execute("UPDATE commitments SET remaining_minor=0,status='released',updated_at=? WHERE id=? AND status='active'",(now(),p['commitment_id']))
def validate_plan(db,key,account_id=None,amount=None):
    p=get(db,key)
    if p['status'] not in {'active','paused'}:error('生日计划已结束或需重新核对，不能使用该预算付款。')
    if clock().date()>date.fromisoformat(p['event_date']):error('生日已过，不能继续使用这项预算，请重新安排。')
    current=person(db,p['contact_id'])
    if current['birthday']!=p['birthday_snapshot']:error('联系人生日已变化，请取消旧计划并重新确认。')
    c=db.execute('SELECT * FROM commitments WHERE id=?',(p['commitment_id'],)).fetchone()
    if not c or c['status']!='active' or c['remaining_minor']!=p['budget_minor'] or c['account_id']!=p['account_id']:error('生日预留资金已变化，请先核对计划。')
    available(db,p['account_id'])
    if account_id is not None:
        if p['status']!='active':error('生日计划已暂停，请先恢复后下单。')
        if account_id!=p['account_id']:error('请使用生日计划的预算来源账户；换来源需取消旧计划后重新建立。')
        if amount is not None and amount>c['remaining_minor']:error('订单合计超过生日预算，请减少数量或重新安排预算。')
    return p
def execute(bank,db,name,args,action_id):
    if name=='create_birthday_plan':
        p=preview(db,args);created=now();commit=uid('commit_')
        conversation=db.execute('SELECT t.conversation_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?',(action_id,)).fetchone()
        conv=conversation[0] if conversation else bank.database.conversation_id(db)
        db.execute('INSERT INTO commitments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(commit,args['plan_id'],conv,p['account_id'],'birthday',p['contact_name']+'生日预算',p['budget_minor'],p['budget_minor'],p['event_date'],'active',created,created))
        db.execute('INSERT INTO birthday_plans(id,conversation_id,contact_id,contact_name,birthday_snapshot,event_date,remind_at,account_id,budget_minor,commitment_id,status,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)',
                   (args['plan_id'],conv,p['contact_id'],p['contact_name'],p['birthday_snapshot'],p['event_date'],p['remind_at'],p['account_id'],p['budget_minor'],commit,'active',created,created))
        key=args['plan_id']
    else:
        key=args['plan_id'];p=get(db,key)
        if args['action']=='cancel':release(db,p)
        db.execute('UPDATE birthday_plans SET status=?,version=version+1,updated_at=? WHERE id=?',({'pause':'paused','resume':'active','cancel':'cancelled'}[args['action']],now(),key))
    event(db,'*','birthday.updated',{'plan_id':key})
    return public(db,get(db,key))
def pay(db,key,order_id,amount):
    p=get(db,key);release(db,p)
    db.execute("UPDATE birthday_plans SET status='completed',order_id=?,spent_minor=?,version=version+1,updated_at=? WHERE id=?",(order_id,amount,now(),key))
    event(db,'*','birthday.updated',{'plan_id':key,'order_id':order_id})
    return public(db,get(db,key))

async def recommendations(bridge,conversation):
    try:
        current=await bridge.query('get_food_context',{},conversation)
        if not current.get('connected') or not current.get('place'):
            return {'menus':[],'message':current.get('answer','请先关联外卖账户并选择演示收货位置。')}
        found=await bridge.query('search_food_shops',{'query':'蛋糕'},conversation)
        menus=[]
        for shop in found.get('shops',[])[:3]:
            menu=await bridge.query('get_food_menu',{'shop_id':shop['id']},conversation)
            menus.append({'shop_id':shop['id'],'name':shop['name'],'delivery_minor':shop['delivery_minor'],
                          'menu':menu.get('menu',[]),'place':found['place']})
        return {'menus':menus,'message':'已读取演示平台蛋糕菜单，价格仅为当前查询结果，选择后重新报价；未下单或扣款。' if menus else '当前位置没有匹配蛋糕店，可在有数中查询其他餐饮。'}
    except Exception:
        log.warning('Birthday recommendation unavailable',exc_info=True)
        return {'menus':[],'message':'演示外卖平台暂不可用。生日提醒和预算仍保留，可稍后在对话中重新查询；未下单或扣款。'}

async def process_due(database,settings,at=None,bridge=None):
    at=at or clock();count=0
    with database.connection() as db:
        db.executescript(SCHEMA)
        if db.execute('SELECT locked FROM account_guard WHERE id=1').fetchone()[0]:return 0
        plans=[dict(r) for r in db.execute("SELECT * FROM birthday_plans WHERE status IN ('active','paused')")]
    for p in plans:
        invalid=at.date()>date.fromisoformat(p['event_date'])
        reason='生日已过，已释放未用预算。' if invalid else ''
        if not invalid:
            with database.connection() as db:
                try:validate_plan(db,p['id'])
                except ValueError as exc:invalid=True;reason=str(exc)
        if invalid:
            with database.transaction() as db:
                current=get(db,p['id'])
                if current['version']!=p['version'] or current['status'] not in {'active','paused'}:continue
                release(db,current)
                status='expired' if at.date()>date.fromisoformat(p['event_date']) else 'needs_review'
                db.execute('UPDATE birthday_plans SET status=?,version=version+1,updated_at=? WHERE id=?',(status,now(),p['id']))
                profile=db.execute('SELECT special_attention FROM contact_profiles WHERE person_id=?',(p['contact_id'],)).fetchone()
                if profile and profile[0]:add_message(db,p['conversation_id'],None,'assistant','text',reason+' 未执行生日订单。',{'_feishu_notify':True})
                event(db,'*','birthday.updated',{'plan_id':p['id']});event(db,'*','bank.updated',{})
            count+=1;continue
        if p['status']!='active' or p['reminder_conversation_id'] or datetime.fromisoformat(p['remind_at'])>at:continue
        with database.connection() as db:
            if not person(db,p['contact_id'])['special_attention']:continue
        if bridge is None:
            from .food_bridge import FoodBridge
            bridge=FoodBridge(database,settings)
        rec=await recommendations(bridge,p['conversation_id'])
        with database.transaction() as db:
            current=get(db,p['id'])
            if current['version']!=p['version'] or current['status']!='active' or current['reminder_conversation_id']:continue
            if db.execute('SELECT locked FROM account_guard WHERE id=1').fetchone()[0]:continue
            try:validate_plan(db,p['id'])
            except ValueError:continue
            if not person(db,p['contact_id'])['special_attention']:continue
            conv=uid('conv_');created=now();title=p['contact_name']+'的生日提醒'
            db.execute('INSERT INTO conversations(id,created_at,title,updated_at) VALUES(?,?,?,?)',(conv,created,title,created))
            db.execute('UPDATE birthday_plans SET reminder_conversation_id=?,recommendation_json=?,version=version+1,updated_at=? WHERE id=?',(conv,dumps(rec),created,p['id']))
            add_message(db,conv,None,'assistant','text',f"{p['contact_name']}的生日是{p['event_date']}，生日预算{yuan(p['budget_minor'])}已预留。{rec['message']}",{'_feishu_notify':True})
            result=query(None,db,'list_birthday_plans',{})
            result['birthday_plans']=[public(db,get(db,p['id']))]
            result['display'].update(title=title,rows=[],metrics=[])
            add_message(db,conv,None,'assistant','data',title,{'tool':'birthday_reminder','result':result,'_feishu_notify':True})
            event(db,'*','birthday.ready',{'plan_id':p['id'],'conversation_id':conv,'title':title})
            event(db,'*','conversations.updated',{'conversation_id':conv})
        count+=1
    return count

async def loop(database,settings,notify):
    while True:
        try:
            if await process_due(database,settings):await notify()
        except Exception:log.exception('Birthday reminder processing failed')
        await asyncio.sleep(10)
async def directory_loop(directory,settings):
    while True:
        try:
            with directory.connection() as db:users=[dict(r) for r in db.execute('SELECT database_path FROM users')]
            for user in users:
                try:
                    if await process_due(Database(Path(user['database_path']),seed_demo=False),settings):
                        for app in list(directory.apps.values()):await app.state.agent.notify()
                except Exception:log.exception('Birthday reminder tenant unavailable')
        except Exception:log.exception('Birthday reminder directory unavailable')
        await asyncio.sleep(10)

class ManageBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    action:str=Field(pattern=r'^(pause|resume|cancel)$')
    version:int=Field(ge=1)
    conversation_id:str=Field(min_length=1,max_length=100)
def register(app,database,agent,session,resolve_conversation):
    @app.get('/api/birthday-plans',dependencies=[Depends(session)])
    def plans():
        with database.connection() as db:return {'items':listing(db)}
    @app.post('/api/birthday-plans/{key}/actions')
    async def manage(key:str,body:ManageBody,session_key:str=Depends(session)):
        with database.transaction() as db:
            conv=resolve_conversation(db,session_key,body.conversation_id)
            message=agent.create_direct_approval(db,conv,'manage_birthday_plan',{'plan_id':key,'version':body.version,'action':body.action})
        await agent.notify();return message
    @app.get('/api/birthday-plans/notices',dependencies=[Depends(session)])
    def notices():
        with database.connection() as db:return {'items':[{'plan_id':r['id'],'conversation_id':r['reminder_conversation_id'],'title':r['contact_name']+'的生日提醒'} for r in db.execute("SELECT p.* FROM birthday_plans p JOIN conversations c ON c.id=p.reminder_conversation_id JOIN contact_profiles f ON f.person_id=p.contact_id WHERE f.special_attention=1 AND p.reminder_read_at IS NULL AND p.status='active' AND c.deleted_at IS NULL ORDER BY p.remind_at")]}
    @app.post('/api/birthday-plans/{key}/seen',dependencies=[Depends(session)])
    def seen(key:str):
        with database.transaction() as db:
            get(db,key);db.execute('UPDATE birthday_plans SET reminder_read_at=? WHERE id=?',(now(),key))
        return {'ok':True}
