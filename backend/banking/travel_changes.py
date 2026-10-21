"""Versioned free cancellation/rebooking. All differences settle atomically."""
import copy
import json
import time
from .bank import BankError,table,yuan
from .db import dumps,now,uid

POLICY={'cancel_fee_minor':0,'change_fee_minor':0,'label':'免费退改；只补退实际差价，退款按剩余支付记录原路退回。'}

def current(db,order_id,version=None):
    row=db.execute('SELECT * FROM travel_orders WHERE id=?',(order_id,)).fetchone()
    if not row:raise BankError('当前账户没有这笔旅行订单，请先查询我的旅行订单。')
    state=db.execute('SELECT * FROM travel_order_state WHERE order_id=?',(order_id,)).fetchone()
    value=json.loads(state['payload'] if state else row['payload'])
    value.update(id=row['id'],version=state['version'] if state else 1,
        status_code=state['status'] if state else 'active',created_at=row['created_at'],
        updated_at=state['updated_at'] if state else row['created_at'])
    value.setdefault('funding',[{'account_id':row['account_id'],'amount_minor':value['amount_minor']}])
    value['policy']=POLICY
    if version is not None and version!=value['version']:raise BankError('订单已发生变化，请重新查询订单并确认退改方案。')
    return value

def active(value):
    if value['status_code']!='active':raise BankError('订单已退订，不能再次退订或改签。')

def refundable(value):
    active(value)
    # The demo deliberately offers free changes even after the simulated date:
    # there is no fulfillment or inventory outside this application.
    if sum(i['amount_minor'] for i in value['funding'])!=value['amount_minor']:
        raise BankError('订单支付记录需要核对，尚未退改或扣款。')

def state_save(db,value,status):
    value=copy.deepcopy(value)
    value['version']+=1;value['status_code']=status;value['updated_at']=now()
    db.execute('INSERT OR REPLACE INTO travel_order_state VALUES (?,?,?,?,?)',
        (value['id'],value['version'],status,dumps(value),value['updated_at']))
    return value

def refund_targets(db,funding,amount):
    left=amount;remaining=copy.deepcopy(funding);refunds=[]
    for lot in reversed(remaining):
        part=min(left,lot['amount_minor'])
        if not part:continue
        row=db.execute('SELECT id,name,kind FROM accounts WHERE id=?',(lot['account_id'],)).fetchone()
        fallback=not row or row['kind']=='bank_card_unlinked'
        account=db.execute("SELECT id,name FROM accounts WHERE id='daily'").fetchone() if fallback else row
        if not account:raise BankError('找不到可用退款账户，请联系人工核对。')
        refunds.append({'account_id':account['id'],'account_name':account['name'],'amount_minor':part,
            'original_account_id':lot['account_id'],'fallback':fallback})
        lot['amount_minor']-=part;left-=part
        if not left:break
    if left:raise BankError('退款金额超过订单剩余实付金额，未执行。')
    return [lot for lot in remaining if lot['amount_minor']],refunds

def refund_description(db,value,amount):
    _,refunds=refund_targets(db,value['funding'],amount)
    return '；'.join(r['account_name']+' '+yuan(r['amount_minor'])+('（原账户已删除/解绑，退回日常账户）' if r['fallback'] else '') for r in refunds) or '无需退款'

def settle_refund(bank,db,value,amount,action_id):
    remaining,refunds=refund_targets(db,value['funding'],amount)
    for n,refund in enumerate(refunds):
        db.execute('UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?',(refund['amount_minor'],refund['account_id']))
        bank._ledger(db,action_id+f':travel-refund:{n}',refund['account_id'],'有数旅行演示','in',refund['amount_minor'],'旅行退款','免费退改退款 '+value['id'])
    value['funding']=remaining
    return refunds

def quote_change(db,args,conversation_id):
    from .travel_bridge import checked_quote,item_label
    old=current(db,args['order_id']);refundable(old)
    proposal=checked_quote(db,args['new_quote_id'],conversation_id)
    parts=set(args['change_parts'])
    if len(parts)!=len(args['change_parts']):raise BankError('退改项目不能重复。')
    old_items={i['leg']:i for i in old['items']};new_items={i['leg']:i for i in proposal['items']}
    if not parts<=old_items.keys() or old_items.keys()!=new_items.keys():raise BankError('改签需保持原订单项目类型；请核对去程、返程和酒店。')
    items=[copy.deepcopy(new_items[i['leg']] if i['leg'] in parts else i) for i in old['items']]
    by_leg={i['leg']:i for i in items}
    out,back,stay=by_leg['outbound'],by_leg['inbound'],by_leg['hotels']
    if out['origin']!=back['destination'] or out['destination']!=back['origin'] or stay['name'].split(stay['area'])[0]!=out['destination']:
        raise BankError('去程、返程与酒店城市不一致；更换目的地需同时调整相关项目。')
    if out['date']>back['date'] or stay['checkout']<=out['date'] or stay['checkin']>back['date']:
        raise BankError('新交通日期与住宿日期不匹配，请同时调整所需酒店日期。')
    if all(items[n]==old['items'][n] for n in range(len(items))):raise BankError('所选方案与原订单相同，无需改签。')
    amount=sum(i['amount_minor'] for i in items);delta=amount-old['amount_minor']
    trip={**old['trip'],'origin':out['origin'],'destination':out['destination'],'departure_date':out['date'],
        'return_date':back['date'],'area':stay['area'],'passengers':out['quantity'],'rooms':stay['quantity']}
    if out['quantity']!=back['quantity']:raise BankError('往返人数不一致，请同时调整往返交通。')
    value={'quote_id':uid('travelchangequote_'),'owner':old['owner'],'order_id':old['id'],'order_version':old['version'],
        'proposal_id':proposal['quote_id'],'trip':trip,'items':items,'old_amount_minor':old['amount_minor'],
        'new_amount_minor':amount,'delta_minor':delta,'change_parts':sorted(parts),'fee_minor':0,'expires':min(time.time()+300,proposal['expires'])}
    db.execute('INSERT INTO travel_change_quotes VALUES (?,?,?,?)',(value['quote_id'],conversation_id,dumps(value),value['expires']))
    return {**value,'display':table('免费改签差价核对',['项目','调整后方案','金额'],[[i['leg'],item_label(i),yuan(i['amount_minor'])] for i in items],
        note=f"原订单 {yuan(old['amount_minor'])}，新方案 {yuan(amount)}，改签手续费 ¥0.00；"+(f"需补款 {yuan(delta)}。" if delta>0 else f"原路退款 {yuan(-delta)}。" if delta<0 else '无需补款或退款。')+'未执行改签；使用change_travel_order生成确认卡，验证后办理。')}

def checked_change(db,quote_id,conversation_id=None):
    from .travel_common import owner
    from .travel_bridge import checked_quote
    row=db.execute('SELECT * FROM travel_change_quotes WHERE id=?',(quote_id,)).fetchone()
    if not row or row['expires']<=time.time():raise BankError('改签报价已过期或不存在，请重新报价。')
    if conversation_id is not None and row['conversation_id']!=conversation_id:raise BankError('改签报价不属于当前会话。')
    value=json.loads(row['payload'])
    if value['owner']!=owner(db):raise BankError('改签报价不属于当前账户。')
    old=current(db,value['order_id'],value['order_version']);refundable(old)
    checked_quote(db,value['proposal_id'],row['conversation_id'])
    return value,old

def summary(bank,db,name,args):
    if name=='cancel_travel_order':
        value=current(db,args['order_id'],args['order_version']);refundable(value)
        amount=value['amount_minor']
        fields=[('订单',value['id']),('原实付',yuan(amount)),('退订手续费','¥0.00'),('退款金额',yuan(amount)),
            ('退款去向',refund_description(db,value,amount)),('说明','免费退订整个演示订单，确认并验证后退款；不会保留其中的机票或酒店。')]
        return {'title':'确认免费退订旅行订单','amount_minor':amount,'debit_minor':0,'fields':[{'label':a,'value':b} for a,b in fields]}
    from .travel_bridge import item_label
    quote,old=checked_change(db,args['quote_id']);delta=quote['delta_minor']
    fields=[('订单',old['id']),('原实付',yuan(old['amount_minor'])),('调整后总价',yuan(quote['new_amount_minor'])),('改签手续费','¥0.00')]
    fields.extend([(item_label(i),yuan(i['amount_minor'])) for i in quote['items']])
    if delta>0:
        account=bank._check_debit(db,args['account_id'],delta)
        fields.extend([('需补差价',yuan(delta)),('付款账户',account['name'])])
    else:fields.extend([('退回差价',yuan(-delta)),('退款去向',refund_description(db,old,-delta))])
    return {'title':'确认免费改签旅行订单','amount_minor':abs(delta),'debit_minor':max(delta,0),'fields':[{'label':a,'value':b} for a,b in fields]}

def execute(bank,db,name,args,action_id):
    if name=='cancel_travel_order':
        value=current(db,args['order_id'],args['order_version']);refundable(value)
        amount=value['amount_minor'];refunds=settle_refund(bank,db,value,amount,action_id)
        value['refunded_minor']=value.get('refunded_minor',0)+amount
        value=state_save(db,value,'cancelled')
        result={'order_id':value['id'],'order_version':value['version'],'refund_minor':amount,'debit_minor':0,'refunds':refunds,'fee_minor':0}
    else:
        quote,value=checked_change(db,args['quote_id']);delta=quote['delta_minor'];refunds=[]
        if delta>0:
            bank._check_debit(db,args['account_id'],delta)
            db.execute('UPDATE accounts SET balance_minor=balance_minor-? WHERE id=?',(delta,args['account_id']))
            bank._ledger(db,action_id+':travel-change',args['account_id'],'有数旅行演示','out',delta,'旅行','免费改签补差价 '+value['id'])
            value['funding'].append({'account_id':args['account_id'],'amount_minor':delta})
        elif delta<0:refunds=settle_refund(bank,db,value,-delta,action_id)
        value.update(trip=quote['trip'],items=quote['items'],amount_minor=quote['new_amount_minor'])
        value=state_save(db,value,'active')
        db.execute('INSERT INTO travel_quote_claims VALUES (?,?)',(quote['proposal_id'],action_id))
        result={'order_id':value['id'],'order_version':value['version'],'refund_minor':max(-delta,0),'debit_minor':max(delta,0),'refunds':refunds,'fee_minor':0,'items':value['items'],'trip':value['trip']}
    db.execute('INSERT INTO travel_changes VALUES (?,?,?,?,?,?)',(uid('travelchange_'),action_id,value['id'],name,dumps(result),now()))
    return {**result,'simulated':True,'external_real_order':False}
