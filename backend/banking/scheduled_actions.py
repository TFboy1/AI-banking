"""Shared optional scheduling for the existing verified transaction handlers.

Authorization fixes the transaction terms. A changed price requires a new consent.
No model, arbitrary code, or saved password runs in the due worker.
"""
import json
from datetime import timedelta

from .db import dumps,event,now,uid
from .scheduled_transfers import clock,when,stamp,seal,public

TOOLS={'transfer','transfer_own_funds','purchase_product','redeem_product',
       'sell_stock_basket','place_food_order','place_travel_order',
       'simulate_expense','save_to_goal','add_bank_card'}
RULE='到期重新核对余额、对象、风险和报价；价格或订单内容变化则停止并要求重新授权。现在不扣款、不冻结余额。'

def time_value(value):
    from .bank import BankError
    due=when(value)
    if not clock()<due<=clock()+timedelta(days=365):
        raise BankError('请选择未来365天内的明确执行时间；当前没有扣款。')
    return due.isoformat(timespec='seconds')

def base_args(args):return {k:v for k,v in args.items() if k!='execute_at'}

def timing(name,args,value):
    from .bank import BankError
    args=dict(args)
    if value is not None:
        if name not in TOOLS:raise BankError('这项操作不能在此修改执行时间。')
        if value:args['execute_at']=time_value(value)
        else:args.pop('execute_at',None)
    elif args.get('execute_at'):
        if name not in TOOLS and name!='schedule_transfer':raise BankError('这项操作已不支持定时，请重新发起即时交易。')
        args['execute_at']=time_value(args['execute_at'])
    return args

def summary(bank,db,name,args):
    from .bank import BankError
    if name not in TOOLS:raise BankError('这项操作不支持定时。')
    args['execute_at']=time_value(args['execute_at'])
    if name=='place_travel_order':
        from .travel_bridge import checked_quote
        quote=checked_quote(db,args['quote_id'])
        if when(args['execute_at']).date().isoformat()>=quote['trip']['departure_date']:
            raise BankError('旅行预约支付需在出发日期之前完成，请选择更早的支付时间。')
    if name=='sell_stock_basket':
        from .stock_trading import status
        if not status(when(args['execute_at']))['open']:
            raise BankError('股票卖出预约必须选择已公布交易日的09:30–11:30或13:00–15:00（北京时间）。')
    actual=base_args(args)
    result=bank._check_action(db,name,actual)
    # Recipient names must resolve to a fixed entity before consent.
    args.update(actual)
    return {**result,'title':result['title']+' · 定时授权','scheduled':True,'execute_at':args['execute_at'],
            'fields':[*result['fields'],{'label':'执行时间（北京时间）','value':args['execute_at']},
                      {'label':'定时规则','value':RULE}, {'label':'离线规则','value':'错过执行时间5分钟后停止，不自动补扣。'}]}

def snapshot(bank,db,name,args):
    from .scheduled_transfers import snapshot as transfer_snapshot
    account=db.execute('SELECT id,name,kind FROM accounts WHERE id=?',(args.get('account_id'),)).fetchone()
    source={**dict(account),'cards':[dict(r) for r in db.execute('SELECT id,name,last4 FROM cards WHERE account_id=? ORDER BY id',(account['id'],))]} if account else {'id':'','name':'无需资金账户','kind':'none'}
    extra={}
    if name=='transfer':
        source,extra=transfer_snapshot(bank,db,args['recipient'],args['account_id'])
    if name=='transfer_own_funds':
        _,extra=transfer_snapshot(bank,db,args['target_account_id'],args['account_id'])
    result=bank._check_action(db,name,args)
    # The valuation date can advance without changing the authorized price.
    terms={k:result.get(k) for k in ('amount_minor','valuation_nav')}
    terms['fields']=[f for f in result['fields'] if f['label'] not in {'成交说明'}]
    if name in {'purchase_stock_basket','sell_stock_basket'}:
        quote=json.loads(db.execute('SELECT payload FROM securities_quotes WHERE id=?',(args['quote_id'],)).fetchone()[0])
        terms={'amount_minor':quote['amount_minor'],'owner':quote['owner'],'side':quote.get('side','buy'),
               'items':[{k:i[k] for k in ('symbol','shares','price_minor')} for i in quote['items']]}
    return source,{'kind':'scheduled_action','tool':name,'terms':terms,'target':extra,
                   'id':args.get('recipient') or args.get('target_account_id') or args.get('product_id') or name,
                   'name':result['title'].removeprefix('确认'),'detail':RULE,'amount_minor':result.get('amount_minor',0)}

def authorize(bank,db,name,args,authorization_id):
    from .bank import BankError,validate
    from .payment_security import risk
    original=db.execute('SELECT a.*,t.conversation_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?',(authorization_id,)).fetchone()
    if not original or original['status']!='pending' or original['tool_name']!=name or validate(name,json.loads(original['args_json']))!=args:
        raise BankError('定时授权参数与当前确认卡不一致。')
    for kind in ['payment.pin_verified']+(['payment.sms_verified'] if risk(db,original,args)['sms_required'] else []):
        if not db.execute("SELECT 1 FROM events WHERE type=? AND json_extract(data_json,'$.approval_id')=?",(kind,authorization_id)).fetchone():
            raise BankError('请先完成支付密码和所需短信验证。')
    summary(bank,db,name,args)
    actual=base_args(args);source,target=snapshot(bank,db,name,actual)
    row={'id':uid('schedule_'),'conversation_id':original['conversation_id'],'authorization_id':authorization_id,
         'transfer_tool':name,'args_json':dumps(actual),'source_json':dumps(source),'target_json':dumps(target),
         'security_stamp':stamp(db),'execute_at':args['execute_at'],'note':args.get('note','')}
    row['authorization_hash']=seal(row)
    db.execute('INSERT INTO scheduled_transfers(id,conversation_id,authorization_id,transfer_tool,args_json,source_json,target_json,security_stamp,authorization_hash,execute_at,status,authorized_at,note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
               tuple(row[k] for k in ('id','conversation_id','authorization_id','transfer_tool','args_json','source_json','target_json','security_stamp','authorization_hash','execute_at'))+('scheduled',now(),row['note']))
    event(db,row['conversation_id'],'scheduled.authorized',{'approval_id':authorization_id,'schedule_id':row['id'],'execute_at':row['execute_at'],'tool':name})
    return public(db.execute('SELECT * FROM scheduled_transfers WHERE id=?',(row['id'],)).fetchone())

def refresh_quote(db,name,args,settings,database=None):
    """Fetch a fresh platform quote, compare fixed terms, then replace the expired cache.

    This endpoint only quotes; actual orders remain local atomic bank transactions.
    The original quote ID stays reserved, preventing any immediate duplicate purchase.
    """
    if name=='place_travel_order':
        from .travel_bridge import refresh_scheduled_quote
        return refresh_scheduled_quote(db,args,settings,database)
    if name not in {'purchase_stock_basket','sell_stock_basket','place_food_order'}:return
    import httpx
    from .bank import BankError
    from .securities_common import secret
    food=name=='place_food_order';table='food_quotes' if food else 'securities_quotes'
    row=db.execute(f'SELECT * FROM {table} WHERE id=?',(args['quote_id'],)).fetchone()
    if not row:raise BankError('原报价不存在，请重新授权。')
    old=json.loads(row['payload'])
    if food:
        body={'shop_id':old['shop_id'],'items':[{'item_id':i['id'],'quantity':i['quantity']} for i in old['items']], 'note':old['note']}
        path=f"/internal/food/{old['owner']}/quote";url=settings.food_url;header='x-food-key';key=settings.food_bridge_key_path
        keys=('owner','shop_id','items','place','note','subtotal_minor','packing_minor','delivery_minor','amount_minor')
    else:
        body={'orders':[{'symbol':i['symbol'],'shares':i['shares']} for i in old['items']]}
        path=f"/internal/securities/{old['owner']}/{'sale-quote' if name=='sell_stock_basket' else 'quote'}"
        url=settings.securities_url;header='x-securities-key';key=settings.securities_bridge_key_path
        keys=('owner','amount_minor')
    from .safety import admit_platform,finish_platform
    scope='food' if food else 'securities'
    ticket=admit_platform(database,scope) if database else None
    finished=False
    try:
        with httpx.Client(base_url=url,timeout=12,trust_env=False,verify=not url.startswith('http://')) as client:
            response=client.post(path,json=body,headers={header:secret(key)})
        if response.status_code>=500 or response.status_code==429:response.raise_for_status()
        fresh=response.json()
        if not isinstance(fresh,dict):raise ValueError('Invalid platform response')
        if database:finish_platform(database,scope,ticket,False)
        finished=True
        if not response.is_success:raise BankError('到期平台报价未完成，请重新授权；未执行交易。')
        if any(fresh.get(k)!=old.get(k) for k in keys):raise BankError('平台价格、配送地址或订单内容已变化，请重新授权。')
        if not food and [{k:i[k] for k in ('symbol','shares','price_minor')} for i in fresh['items']]!=[{k:i[k] for k in ('symbol','shares','price_minor')} for i in old['items']]:
            raise BankError('股票报价已变化，请重新授权；未买卖或扣款。')
        import time
        if fresh['expires']<=time.time():raise ValueError('expired')
        old.update(expires=fresh['expires'])
        # Keep the consented budget fields but update the genuine quote date/source.
        if not food:
            for saved,current in zip(old['items'],fresh['items']):
                for field in ('quote_date','source','price_kind'):
                    if field in current:saved[field]=current[field]
        return table,dumps(old),fresh['expires'],args['quote_id']
    except BankError:raise
    except (httpx.HTTPError,ValueError,KeyError,TypeError) as exc:
        if database and not finished:finish_platform(database,scope,ticket,True)
        raise BankError('到期无法取得有效平台报价，预约停止，未执行交易；请重新授权。') from exc
