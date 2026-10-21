"""Deterministic daily summaries, using midnight history rather than live balances."""
import asyncio
import calendar
import json
import logging
from datetime import date,datetime,time,timedelta,timezone
from decimal import Decimal,ROUND_HALF_UP
from pathlib import Path

from fastapi import Depends
from pydantic import BaseModel,ConfigDict,Field
from .db import Database,TZ,add_message,dumps,event,now,uid

SCHEMA='''
CREATE TABLE IF NOT EXISTS daily_report_preferences(
 id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL DEFAULT 1,
 delivery_time TEXT NOT NULL DEFAULT '09:00',version INTEGER NOT NULL DEFAULT 1);
INSERT OR IGNORE INTO daily_report_preferences VALUES(1,1,'09:00',1);
CREATE TABLE IF NOT EXISTS daily_report_runs(
 report_day TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,cutoff_at TEXT NOT NULL,
 period_start TEXT NOT NULL,created_at TEXT NOT NULL,summary_json TEXT NOT NULL);
'''
EXCLUDED={'内部转账','理财','证券投资','证券卖出'}
log=logging.getLogger(__name__)

def initialize_offline(database):
    """Schema-only upgrade for dormant accounts from older app versions.

    Do not seed demo money, contacts or products, and do not update market NAVs.
    """
    from .db import SCHEMA as BANK_SCHEMA
    from .securities_common import BANK_SCHEMA as STOCK_SCHEMA
    from .security import SCHEMA as AUTH_SCHEMA
    from .account_lock import SCHEMA as GUARD_SCHEMA
    from .safety import SCHEMA as SAFETY_SCHEMA
    from .report_history import install
    with database.connection() as db:
        db.executescript(BANK_SCHEMA+STOCK_SCHEMA+AUTH_SCHEMA+GUARD_SCHEMA+SAFETY_SCHEMA+SCHEMA)
        install(db)

def clock():return datetime.now(TZ)
def money(value):return f'¥{Decimal(value)/100:,.2f}'
def local(value):
    moment=datetime.fromisoformat(value)
    return (moment.replace(tzinfo=TZ) if moment.tzinfo is None else moment.astimezone(TZ))

def preference(db,at=None):
    at=at or clock();row=db.execute('SELECT * FROM daily_report_preferences WHERE id=1').fetchone()
    result={'enabled':bool(row['enabled']),'time':row['delivery_time'],'version':row['version'],'next_run':None}
    if result['enabled']:
        due=datetime.combine(at.date(),time.fromisoformat(result['time']),TZ)
        done=db.execute('SELECT 1 FROM daily_report_runs WHERE report_day=?',(at.date().isoformat(),)).fetchone()
        if done or at.strftime('%H:%M')>'12:00':due+=timedelta(days=1)
        result['next_run']=max(at,due).isoformat()
    return result

def due(db,at):
    prefs=preference(db,at)
    if not prefs['enabled'] or not prefs['time']<=at.strftime('%H:%M')<='12:00':return None
    if db.execute('SELECT 1 FROM daily_report_runs WHERE report_day=?',(at.date().isoformat(),)).fetchone():return None
    if not db.execute('SELECT 1 FROM app_identity WHERE id=1 AND pin_hash IS NOT NULL AND contacts_done=1').fetchone():return None
    if db.execute('SELECT locked FROM account_guard WHERE id=1').fetchone()[0]:return None
    return prefs

def period(at):
    cutoff=datetime.combine(at.date(),time(0),TZ)
    return cutoff-timedelta(days=1),cutoff

def facts(database,at):
    from .report_history import snapshot
    start,cutoff=period(at)
    utc=cutoff.astimezone(timezone.utc).isoformat(timespec='milliseconds')
    with database.connection() as db:
        # One SQLite read transaction keeps all report sections on one revision.
        db.execute('BEGIN')
        prefs=due(db,at)
        if not prefs:return None
        person=db.execute('SELECT username FROM app_identity WHERE id=1').fetchone()
        history=snapshot(db,utc)
        transactions=([t for t in history['transactions'] if local(t['occurred_at']).date()==start.date()] if history is not None else
                      [dict(r) for r in db.execute("SELECT * FROM transactions WHERE date(occurred_at,'+8 hours')=? ORDER BY occurred_at,rowid",(start.date().isoformat(),))])
        transactions.sort(key=lambda t:(local(t['occurred_at']),t['id']))
        coverage=db.execute('SELECT started_at FROM report_history_meta WHERE id=1').fetchone()[0]
        owner=db.execute("SELECT value FROM meta WHERE key='securities_owner'").fetchone()
        db.rollback()
    return {'report_day':at.date().isoformat(),'username':person['username'],'period_start':start.isoformat(),
            'cutoff_at':cutoff.isoformat(),'history':history,'transactions':transactions,
            'history_started_at':coverage,'preference_version':prefs['version'],'stock_owner':owner[0] if owner else None}

async def stock_values(database,settings,fact):
    positions=(fact['history'] or {}).get('securities_positions',[])
    empty=[{**p,'market_value_minor':None,'profit_minor':None,'profit_percent':None,'price_minor':None,'quote_date':None,'source':None} for p in positions]
    if not positions:return []
    if not fact['stock_owner']:return empty
    from .securities_bridge import SecuritiesBridge
    bridge=SecuritiesBridge(database,settings)
    try:
        result=await asyncio.wait_for(bridge.request(f"/internal/securities/{fact['stock_owner']}/valuation-at",{'positions':positions,'cutoff_date':fact['report_day']}),timeout=15)
        values=result['positions']
        if [(p['symbol'],p['shares'],p['cost_minor']) for p in values]!=[(p['symbol'],p['shares'],p['cost_minor']) for p in positions]:return empty
        # Never accept a price dated on/after the cutoff day.
        for value in values:
            if value.get('market_value_minor') is not None and (not value.get('quote_date') or value['quote_date']>=fact['report_day']):return empty
        return values
    except (TimeoutError,KeyError,TypeError,ValueError):return empty
    except Exception as exc:
        from .bank import BankError
        if isinstance(exc,BankError):return empty
        raise

def next_birthday(value,today):
    born=date.fromisoformat(value)
    def anniversary(year):return date(year,born.month,min(born.day,calendar.monthrange(year,born.month)[1]))
    result=anniversary(today.year)
    return anniversary(today.year+1) if result<today else result

def birthdays(history,today):
    month=today.month%12+1;year=today.year+(today.month==12)
    horizon=date(year,month,min(today.day,calendar.monthrange(year,month)[1]))
    names={p['id']:p['name'] for p in history['contacts']}
    notes={p['contact_id']:p['note'] for p in history['contact_details']}
    result=[]
    for person in history['contact_profiles']:
        if not person['special_attention'] or not person['birthday'] or person['person_id'] not in names:continue
        try:day=next_birthday(person['birthday'],today)
        except (ValueError,TypeError):continue
        if day>horizon:continue
        result.append({'name':names[person['person_id']],'note':notes.get(person['person_id'],''),
                       'relationship':person['relationship'],'birthday':day.isoformat(),'days':(day-today).days})
    return sorted(result,key=lambda p:(p['days'],p['name']))

def build(fact,positions):
    """All totals are integers from persisted facts; no model-written amounts."""
    history=fact['history'];today=date.fromisoformat(fact['report_day']);sections=[]
    def section(title,columns,rows,note='',metrics=None):
        sections.append({'title':title,'columns':columns,'rows':rows,'note':note,'metrics':metrics or []})
    spending=[t for t in fact['transactions'] if t['direction']=='out' and t['category'] not in EXCLUDED]
    income=[t for t in fact['transactions'] if t['direction']=='in' and t['category'] not in EXCLUDED]
    expense=sum(t['amount_minor'] for t in spending);earned=sum(t['amount_minor'] for t in income)
    if history is None:
        section('截止时点的账户与投资',[],[],'该截止时点尚未建立历史快照，无法可靠还原余额、持仓、订阅或生日资料；不以当前数据代替。')
        accounts=[]
    else:
        accounts=[a for a in history['accounts'] if a['kind']!='bank_card_unlinked']
        cash=sum(a['balance_minor'] for a in accounts)
        section('账户余额',['账户','余额'],[[a['name'],money(a['balance_minor'])] for a in accounts],
                '含已绑定银行卡余额；投资资产单独列示。',[{'label':'资金账户合计','value':money(cash)}])
    names={a['id']:a['name'] for a in accounts}
    for title,items,total in [('昨日支出',spending,expense),('昨日收入',income,earned)]:
        section(title,['时间','项目','类别','账户','金额'],[
            [local(t['occurred_at']).strftime('%H:%M'),t['counterparty'],t['category'],names.get(t['account_id'],'历史账户'),money(t['amount_minor'])] for t in items],
            '本人账户互转、理财和证券本金流转不计入日常收支。'+('无记录。' if not items else ''),[{'label':title+'合计','value':money(total)}])
    movements=[t for t in fact['transactions'] if t['category'] in EXCLUDED-{'内部转账'}]
    if movements:
        section('昨日投资资金流转',['项目','方向','金额'],[[t['counterparty'],'投入' if t['direction']=='out' else '回笼',money(t['amount_minor'])] for t in movements],'投资本金进出单列，不计入昨日消费或收入。')
    reserve=0;birthday_items=[];investment=None
    if history is not None:
        products={p['id']:p for p in history['products']};funds=[]
        for h in history['holdings']:
            if Decimal(h['units'])<=0 or h['product_id'] not in products:continue
            product=products[h['product_id']];value=int((Decimal(h['units'])*Decimal(product['nav'])*100).quantize(Decimal(1),rounding=ROUND_HALF_UP))
            funds.append({'name':product['name'],'units':h['units'],'nav':product['nav'],'value_minor':value})
        rows=[[p['name'],'理财',p['units']+'份',p['nav'],money(p['value_minor']),'—'] for p in funds]
        rows += [[p['name']+' '+p['symbol'],'股票',str(p['shares'])+'股',money(p['price_minor']) if p.get('price_minor') is not None else '暂无历史行情',
                  money(p['market_value_minor']) if p.get('market_value_minor') is not None else '无法估值',
                  money(p['profit_minor'])+' / '+str(p['profit_percent'])+'%' if p.get('profit_minor') is not None else '无法计算'] for p in positions]
        complete=all(p.get('market_value_minor') is not None for p in positions)
        fund_value=sum(p['value_minor'] for p in funds)
        investment=fund_value+sum(p['market_value_minor'] for p in positions) if complete else None
        quote_note='；'.join(dict.fromkeys(f"股票行情 {p['quote_date']} · {p.get('source') or '证券平台'}" for p in positions if p.get('quote_date')))
        section('投资账户详情',['产品','类型','持有数量','截止价格/净值','持仓估值','股票浮盈亏'],rows,
                '持仓按截止时点还原；股票使用截止前最近收盘价，理财为截止时点模拟净值。'+quote_note,
                [{'label':'投资资产合计','value':money(investment) if investment is not None else '历史行情不完整，暂不汇总'},
                 {'label':'股票买入成本','value':money(sum(p['cost_minor'] for p in positions))}])
        if investment is not None:
            sections[0]['metrics'].append({'label':'含投资总资产','value':money(sum(a['balance_minor'] for a in accounts)+investment)})
        subscriptions=[]
        for s in history['subscriptions']:
            if s['status']!='active':continue
            try:charged=date.fromisoformat(s['next_charge_date'])
            except (ValueError,TypeError):continue
            days=(charged-today).days
            in_month=charged.year==today.year and charged.month==today.month
            if in_month:reserve+=s['amount_minor']
            subscriptions.append([s['name'],money(s['amount_minor']),charged.isoformat(),('今天' if days==0 else f'{days}天后' if days>0 else '已过期，需核对')])
        section('订阅续费提醒',['服务','预计扣费','下次扣费日期','距离扣费'],sorted(subscriptions,key=lambda r:(r[2],r[0])),
                '按截止时点仍有效的协议估算；本月逾期协议也预留一次费用，实际是否已扣费需核对。只作提醒，不自动扣款。',
                [{'label':'本月订阅建议留存','value':money(reserve)}])
        birthday_items=birthdays(history,today)
        if birthday_items:
            section('特别关注的生日',['联系人','关系','生日','还有多久'],[
                [p['name']+(f"（{p['note']}）" if p['note'] else ''),p['relationship'],p['birthday'],'今天生日' if p['days']==0 else str(p['days'])+'天'] for p in birthday_items],
                '仅提醒未来一个自然月内的生日；天数从日报当天计算。闰日生日在非闰年按2月末提醒。')
    return {'report_day':fact['report_day'],'cutoff_at':fact['cutoff_at'],'period_start':fact['period_start'],
            'coverage_complete':history is not None,'expense_minor':expense,'income_minor':earned,
            'investment_minor':investment,'subscription_reserve_minor':reserve,'birthdays':birthday_items,'sections':sections}

async def generate_due(database,settings,at=None):
    explicit=at is not None
    at=at or clock()
    fact=await asyncio.to_thread(facts,database,at)
    if fact is None:return None
    values=await stock_values(database,settings,fact)
    result=build(fact,values)
    with database.transaction() as db:
        # A setting changed or another worker finished while quotes were loading.
        moment=at if explicit else clock()
        if moment.date()!=at.date():return None
        prefs=due(db,moment)
        if not prefs or prefs['version']!=fact['preference_version']:return None
        conversation=uid('conv_');title=f"日报 · {fact['report_day']}"
        username=db.execute('SELECT username FROM app_identity WHERE id=1').fetchone()[0]
        db.execute('INSERT INTO conversations(id,created_at,title,updated_at) VALUES(?,?,?,?)',(conversation,now(),title,now()))
        # No welcome template, model request, payment, or session switch.
        greeting=f'尊敬的“{username}”，早上好！'
        subtitle=f"数据截至 {fact['report_day']} 00:00（北京时间）；昨日收支统计 {local(fact['period_start']).date()} 00:00–24:00，不含今日交易。"
        add_message(db,conversation,None,'assistant','data',greeting+'\n'+subtitle,
                    {'tool':'daily_report','source':'sandbox','daily_report':True,'_feishu_notify':True,
                     'result':{'daily_digest':{'greeting':greeting,'subtitle':subtitle,'sections':result['sections']}}})
        db.execute('INSERT INTO daily_report_runs VALUES(?,?,?,?,?,?)',(fact['report_day'],conversation,fact['cutoff_at'],fact['period_start'],now(),dumps(result)))
        event(db,'*','daily_report.ready',{'conversation_id':conversation,'report_day':fact['report_day']})
    return {'conversation_id':conversation,**result}

class PreferenceBody(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    enabled:bool
    time:str=Field(pattern=r'^(?:(?:0\d|1[01]):[0-5]\d|12:00)$')

def register(app,database,agent,session):
    @app.get('/api/daily-report/settings',dependencies=[Depends(session)])
    def read_settings():
        with database.connection() as db:return preference(db)

    @app.patch('/api/daily-report/settings',dependencies=[Depends(session)])
    async def save_settings(body:PreferenceBody):
        with database.transaction() as db:
            db.execute('UPDATE daily_report_preferences SET enabled=?,delivery_time=?,version=version+1 WHERE id=1',(int(body.enabled),body.time))
            event(db,'*','daily_report.settings_changed',{'enabled':body.enabled,'delivery_time':body.time})
            result=preference(db)
        await agent.notify();return result

    @app.get('/api/daily-report/today',dependencies=[Depends(session)])
    def current():
        with database.connection() as db:
            row=db.execute('SELECT r.conversation_id,r.report_day FROM daily_report_runs r JOIN conversations c ON c.id=r.conversation_id WHERE r.report_day=? AND c.deleted_at IS NULL',(clock().date().isoformat(),)).fetchone()
            return dict(row) if row else {'conversation_id':None}

async def loop(database,settings,notify):
    while True:
        try:
            if await generate_due(database,settings):await notify()
        except Exception:log.exception('Daily report generation failed')
        await asyncio.sleep(10)

async def directory_loop(directory,settings):
    initialized=set()
    while True:
        try:
            with directory.connection() as db:users=[dict(r) for r in db.execute('SELECT database_path FROM users')]
            for user in users:
                try:
                    database=Database(Path(user['database_path']),seed_demo=False)
                    if user['database_path'] not in initialized:
                        # Schema/history migration for offline tenants, without
                        # creating a model or a logged-in application instance.
                        await asyncio.to_thread(initialize_offline,database)
                        initialized.add(user['database_path'])
                    if await generate_due(database,settings):
                        for app in list(directory.apps.values()):await app.state.agent.notify()
                except Exception:log.exception('Daily report tenant unavailable')
        except Exception:log.exception('Daily report account directory unavailable')
        await asyncio.sleep(10)
