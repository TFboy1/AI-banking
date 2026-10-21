"""Read travel APIs; commit the entire mock booking and debit atomically."""
import json
import time
import httpx
from fastapi import HTTPException, Request
from .bank import BankError, table, yuan
from .db import dumps, now, uid
from .travel_common import Trip, owner, require_bridge
from .travel_catalog import validate_trip
from .securities_common import secret

def orders(db):
    from .account_lock import check
    check(db)
    from .travel_changes import current,POLICY
    values=[current(db,r['id']) for r in db.execute('SELECT id FROM travel_orders ORDER BY created_at DESC LIMIT 50')]
    for value in values:
        value['status']='已免费退订' if value['status_code']=='cancelled' else '模拟预订成功'
        value['net_paid_minor']=sum(i['amount_minor'] for i in value['funding'])
    return {'orders':values,'policy':POLICY,'simulated':True}

def item_label(i):
    if i['kind']=='hotel':return f"{i['name']} · {i['checkin']}至{i['checkout']} · {i['nights']}晚 × {i['quantity']}间"
    return f"{'去程' if i['leg']=='outbound' else '返程'} {i['name']} · {i['origin']}→{i['destination']} · {i['date']} {i['departure']}–{i['arrival']} · {i['quantity']}人 · {i['class_name']}"

class TravelBridge:
    def __init__(self,database,settings):self.db,self.settings,self.transport=database,settings,None

    async def request(self,path,body=None):
        from .safety import platform_request
        async def operation():
            async with httpx.AsyncClient(base_url=self.settings.travel_url,transport=self.transport,timeout=12,trust_env=False,verify=not self.settings.travel_url.startswith('http://')) as client:
                return await client.request('GET' if body is None else 'POST',path,json=body,headers={'x-travel-key':secret(self.settings.travel_bridge_key_path)})
        return await platform_request(self.db,'travel',operation)

    async def query(self,name,args,conversation_id):
        with self.db.transaction() as db:
            traveler=owner(db)
            if name=='quote_travel_change':
                from .travel_changes import quote_change
                return quote_change(db,args,conversation_id)
            if name=='get_travel_orders':
                value=orders(db)
                return {**value,'display':table('我的旅行演示订单',['订单','行程','日期','实付','状态'],[[i['id'],i['trip']['origin']+'→'+i['trip']['destination'],i['trip']['departure_date']+'至'+i['trip']['return_date'],yuan(i['amount_minor']),i['status']] for i in value['orders']],note='仅扣虚拟账户，没有真实出票或酒店预订。')}
        current=await self.request(f'/internal/travel/{traveler}/context')
        if not current['connected']:
            answer='请在有数应用设置生成通用同步码，在独立旅行站 http://127.0.0.1:8004/ 手机号登录后输入该码关联；尚未下单或扣款。'
            return {**current,'answer':answer,'outcome':{'status':'needs_clarification'},'display':table('连接旅行演示账户',[],[],note=answer)}
        if name=='get_travel_context':
            return {**current,'display':table('旅行演示城市与住宿区域',['城市','住宿区域'],[[k,'、'.join(v)] for k,v in current['cities'].items()],note='全部为虚构数据。请提供出发城市、目的地、出行与返程日期、住宿区域；默认报价1人1间，请核对人数和间数。')}
        if name=='search_travel_options':
            value=await self.request(f'/internal/travel/{traveler}/search',args)
            trip=value['trip']
            rows=[]
            for leg in ('outbound','inbound'):
                rows.extend([[i['id'],'去程' if leg=='outbound' else '返程','飞机' if i['kind']=='flight' else '动车',i['name'],i['date']+' '+i['departure']+'–'+i['arrival'],i['class_name'],yuan(i['price_minor'])+'/人'] for i in value[leg]])
            rows.extend([[i['id'],'住宿',i['area'],i['name'],f"{i['checkin']}至{i['checkout']} / {i['nights']}晚",i['room_type'],yuan(i['price_minor'])+'/间夜'] for i in value['hotels']])
            return {**value,'display':table('机票、动车与酒店推荐候选',['编号','方向','类型/区域','名称','日期/时刻','席位/房型','单价'],rows,note=f"{trip['origin']}→{trip['destination']}；{trip['passengers']}人、{trip['rooms']}间。票价含演示税费，酒店按实际晚数计价。每程飞机和动车二选一；按预算、时段、区域解释推荐理由，先让用户选择合适方案，不能默认下单。搜索30分钟有效。")}
        value=await self.request(f'/internal/travel/{traveler}/quote',args)
        if value.get('owner')!=traveler or len(value['items'])!=3 or value['amount_minor']<=0 or value['amount_minor']!=sum(i['amount_minor'] for i in value['items']) or any(i['amount_minor']!=i['price_minor']*i['quantity']*i.get('nights',1) for i in value['items']):
            raise BankError('旅行报价不完整，未下单或扣款。')
        with self.db.transaction() as db:
            db.execute('INSERT INTO travel_quotes VALUES (?,?,?,?)',(value['quote_id'],conversation_id,dumps(value),value['expires']))
        return {**value,'display':table('旅行方案合计报价',['项目','单价','小计'],[[item_label(i),yuan(i['price_minor']),yuan(i['amount_minor'])] for i in value['items']],note=f"合计 {yuan(value['amount_minor'])}，5分钟有效。请用place_travel_order生成确认卡，再由用户确认并验证支付；未支付。全部为虚构班次和酒店，不真实出票。")}

def checked_quote(db,quote_id,conversation_id=None):
    row=db.execute('SELECT * FROM travel_quotes WHERE id=?',(quote_id,)).fetchone()
    if not row or row['expires']<=time.time():raise BankError('旅行报价已过期或不存在，请重新搜索并报价。')
    if conversation_id is not None and row['conversation_id']!=conversation_id:raise BankError('旅行报价不属于当前会话。')
    value=json.loads(row['payload'])
    if value['owner']!=owner(db):raise BankError('旅行报价不属于当前用户。')
    if db.execute('SELECT 1 FROM travel_orders WHERE quote_id=?',(quote_id,)).fetchone():raise BankError('该方案已预订，不能重复扣款。')
    if db.execute('SELECT 1 FROM travel_quote_claims WHERE quote_id=?',(quote_id,)).fetchone():raise BankError('该报价已经用于退改或预订，请重新报价。')
    try:validate_trip(Trip.model_validate(value['trip']))
    except HTTPException as exc:raise BankError(str(exc.detail)) from None
    return value

def scoped_quote(db,name,quote_id,conversation_id):
    if name=='change_travel_order':
        from .travel_changes import checked_change
        return checked_change(db,quote_id,conversation_id)
    return checked_quote(db,quote_id,conversation_id)

def summary(bank,db,args):
    value=checked_quote(db,args['quote_id'])
    account=bank._check_debit(db,args['account_id'],value['amount_minor'])
    fields=[(item_label(i),yuan(i['amount_minor'])) for i in value['items']]
    fields.extend([('合计',yuan(value['amount_minor'])),('付款账户',account['name']),('退改规则','免费退改，手续费为0，仅补退实际差价。'),('演示说明','仅扣虚拟余额，不真实出票、不真实预订酒店。')])
    return {'title':'确认旅行演示预订','fields':[{'label':k,'value':v} for k,v in fields],'amount_minor':value['amount_minor'],'simulated':True}

def execute(bank,db,args,action_id):
    value=checked_quote(db,args['quote_id'])
    amount,order_id=value['amount_minor'],uid('travelorder_')
    db.execute('INSERT INTO travel_orders VALUES (?,?,?,?,?,?,?)',(order_id,action_id,args['quote_id'],args['account_id'],amount,dumps(value),now()))
    db.execute('UPDATE accounts SET balance_minor=balance_minor-? WHERE id=?',(amount,args['account_id']))
    bank._ledger(db,action_id+':travel',args['account_id'],'有数旅行演示','out',amount,'旅行','虚拟机酒车预订 '+order_id)
    return {'order_id':order_id,'amount_minor':amount,'items':value['items'],'trip':value['trip'],'simulated':True,'external_real_order':False}

def register_bank_travel(app,database,agent,settings,session):
    @app.post('/internal/travel/orders')
    async def internal_orders(request:Request):
        from .travel_common import Owner
        require_bridge(request,settings.travel_bridge_key_path)
        body=Owner.model_validate(await request.json())
        with database.connection() as db:
            row=db.execute("SELECT value FROM meta WHERE key='travel_owner'").fetchone()
            if not row or row[0]!=body.owner:raise HTTPException(404,'未找到关联账户。')
            return orders(db)

def refresh_scheduled_quote(db,args,settings,database=None):
    """Obtain current inventory before the worker takes its write lock."""
    from .safety import admit_platform, finish_platform
    row=db.execute('SELECT payload FROM travel_quotes WHERE id=?',(args['quote_id'],)).fetchone()
    if not row:raise BankError('原旅行报价不存在，请重新授权。')
    old=json.loads(row[0])
    ticket=admit_platform(database,'travel') if database else None
    finished=False
    try:
        with httpx.Client(base_url=settings.travel_url,timeout=12,trust_env=False,verify=not settings.travel_url.startswith('http://')) as client:
            headers={'x-travel-key':secret(settings.travel_bridge_key_path)}
            search=client.post(f"/internal/travel/{old['owner']}/search",json=old['trip'],headers=headers)
            if search.status_code>=500 or search.status_code==429:search.raise_for_status()
            if not search.is_success:raise BankError('当前行程无法预订，请重新规划并授权。')
            selection={**old['selection'],'search_id':search.json()['search_id']}
            response=client.post(f"/internal/travel/{old['owner']}/quote",json=selection,headers=headers)
            if response.status_code>=500 or response.status_code==429:response.raise_for_status()
        if not response.is_success:raise BankError('旅行平台无法重新报价，预约停止；尚未扣款。')
        fresh=response.json()
        if fresh['expires']<=time.time():raise ValueError('expired')
        if database:finish_platform(database,'travel',ticket,False)
        finished=True
        if any(fresh.get(k)!=old.get(k) for k in ('owner','trip','items','amount_minor')):
            raise BankError('旅行价格或方案内容变化，请重新授权；尚未扣款。')
        old.update(expires=fresh['expires'],selection=selection)
        return 'travel_quotes',dumps(old),fresh['expires'],args['quote_id']
    except BankError:
        if database and not finished:finish_platform(database,'travel',ticket,False)
        raise
    except (httpx.HTTPError,ValueError,KeyError,TypeError) as exc:
        if database and not finished:finish_platform(database,'travel',ticket,True)
        raise BankError('旅行平台暂时不可用，预约停止；尚未扣款。') from exc
