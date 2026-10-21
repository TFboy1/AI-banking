"""Bank consumes the delivery API; order and debit commit atomically here."""
import json
import time
from urllib.parse import quote as urlquote

import httpx
from fastapi import Depends, HTTPException, Request

from .bank import BankError, table, yuan
from .db import dumps, now, uid
from .food_common import owner
from .securities_common import secret


def orders(db):
    from .account_lock import check
    check(db)
    return {'orders': [{**json.loads(r['payload']), 'id': r['id'], 'amount_minor': r['amount_minor'], 'created_at': r['created_at'], 'status': '模拟已下单'} for r in db.execute('SELECT * FROM food_orders ORDER BY created_at DESC LIMIT 50')], 'simulated': True}


class FoodBridge:
    def __init__(self, database, settings):
        self.db, self.settings, self.transport = database, settings, None

    async def request(self, path, body=None):
        from .safety import platform_request
        async def operation():
            async with httpx.AsyncClient(base_url=self.settings.food_url, transport=self.transport, timeout=12, trust_env=False, verify=not self.settings.food_url.startswith('http://')) as client:
                return await client.request('GET' if body is None else 'POST', path, json=body, headers={'x-food-key': secret(self.settings.food_bridge_key_path)})
        return await platform_request(self.db, 'food', operation)

    async def query(self, name, args, conversation_id):
        with self.db.transaction() as db:
            investor = owner(db)
            if name == 'get_food_orders':
                value = orders(db)
                return {**value, 'answer': '仅为虚拟订单，没有真实商家履约。', 'display': table('我的外卖演示订单', ['订单', '店铺', '份数', '实付', '状态'], [[i['id'], i['shop_name'], str(i['quantity']), yuan(i['amount_minor']), i['status']] for i in value['orders']], note='仅扣虚拟账户，不真实下单。')}
        current = await self.request(f'/internal/food/{investor}/context')
        if not current['connected']:
            answer = '请先在有数应用设置生成通用同步码，在独立外卖演示站 http://127.0.0.1:8002/ 登录并连接。尚未下单或扣款。'
            return {**current, 'answer': answer, 'outcome': {'status': 'needs_clarification'}, 'display': table('连接外卖演示账户', [], [], note=answer)}
        if name == 'set_food_location':
            current = await self.request(f'/internal/food/{investor}/location', args)
        if name in {'get_food_context', 'set_food_location'} or not current['place']:
            place = current['place']
            answer = f"当前演示位置：{place['name']}。{place['address']}。" if place else '尚未保存演示位置。若用户原话已明确提及下表唯一位置（例如陆家嘴附近），直接调用set_food_location保存，不要重复询问确认；未给出地点时才请用户选择。不能假装已获取真实定位。'
            return {**current, 'answer': answer, 'outcome': {'status': 'verified' if place else 'needs_clarification'}, 'display': table('外卖演示位置', ['位置编号', '可选位置'], [[p['id'], p['name']] for p in current['places']], note=answer)}
        if name == 'search_food_shops':
            value = await self.request(f'/internal/food/{investor}/shops?q='+urlquote(args['query'], safe=''))
            answer = '请选择店铺和菜品；没有明确选择时不要替用户任意下单。' if value['shops'] else '这个演示位置没有匹配的可配送店铺，请换关键词或演示位置。'
            return {**value, 'answer': answer, 'display': table('附近可配送的演示店铺', ['店铺编号', '店铺', '距离', '预计送达', '配送费', '起送'], [[s['id'], s['name'], str(s['distance_km'])+'km', str(s['minutes'])+'分钟', yuan(s['delivery_minor']), yuan(s['minimum_minor'])] for s in value['shops']], note=value['place']['name']+'；全部为演示店铺。'+answer)}
        if name == 'get_food_menu':
            value = await self.request(f"/internal/food/{investor}/menu/{urlquote(args['shop_id'], safe='')}")
            return {**value, 'answer': '按真实返回的菜品编号与数量报价，不得编造菜单或价格。', 'display': table(value['name']+' · 演示菜单', ['菜品编号', '菜品', '单价', '每份打包费'], [[i['id'], i['name'], yuan(i['price_minor']), yuan(i['packing_minor'])] for i in value['menu']], note=f"起送 {yuan(value['minimum_minor'])}；配送费 {yuan(value['delivery_minor'])}。")}
        quote = await self.request(f'/internal/food/{investor}/quote', args)
        if quote.get('owner') != investor or quote['amount_minor'] != quote['subtotal_minor']+quote['packing_minor']+quote['delivery_minor'] or quote['subtotal_minor'] != sum(i['price_minor']*i['quantity'] for i in quote['items']):
            raise BankError('外卖报价不完整，尚未下单或扣款。')
        with self.db.transaction() as db:
            birthday=db.execute("SELECT id FROM birthday_plans WHERE reminder_conversation_id=? AND status='active'",(conversation_id,)).fetchone()
            if birthday:quote['birthday_plan_id']=birthday['id']
            db.execute('INSERT INTO food_quotes VALUES (?,?,?,?)', (quote['quote_id'], conversation_id, dumps(quote), quote['expires']))
        answer = f"共 {quote['quantity']} 份，餐费 {yuan(quote['subtotal_minor'])}，打包费 {yuan(quote['packing_minor'])}，配送费 {yuan(quote['delivery_minor'])}，合计 {yuan(quote['amount_minor'])}。报价5分钟有效，请用place_food_order生成确认卡，付款账户在密码弹窗选择。"
        return {**quote, 'answer': answer, 'display': table('外卖演示订单报价', ['菜品', '数量', '单价', '餐费'], [[i['name'], str(i['quantity']), yuan(i['price_minor']), yuan(i['amount_minor'])] for i in quote['items']], note=answer+' 配送至：'+quote['place']['address'])}


def checked_quote(db, quote_id, conversation_id=None):
    row = db.execute('SELECT * FROM food_quotes WHERE id=?', (quote_id,)).fetchone()
    if not row or row['expires'] <= time.time():
        raise BankError('外卖报价已过期或不存在，请重新查询菜单并报价。')
    if conversation_id is not None and row['conversation_id'] != conversation_id:
        raise BankError('外卖报价不属于当前会话。')
    value = json.loads(row['payload'])
    if value['owner'] != owner(db):
        raise BankError('外卖报价不属于当前用户。')
    if db.execute('SELECT 1 FROM food_orders WHERE quote_id=?', (quote_id,)).fetchone():
        raise BankError('这份报价已下单，不能重复扣款。')
    return value


def summary(bank, db, args):
    value = checked_quote(db, args['quote_id'])
    key=args.get('birthday_plan_id') or value.get('birthday_plan_id')
    if value.get('birthday_plan_id') and args.get('birthday_plan_id') and key!=value['birthday_plan_id']:
        raise BankError('订单与生日计划不匹配，请重新报价。')
    commitment=None
    if key:
        from .birthdays import validate_plan
        plan=validate_plan(db,key,args['account_id'],value['amount_minor'])
        commitment=plan['commitment_id'];args['birthday_plan_id']=key
    account = bank._check_debit(db, args['account_id'], value['amount_minor'],commitment)
    fields = [('店铺', value['shop_name']), ('配送地址', value['place']['address'])]
    fields += [(i['name'], f"{i['quantity']}份 × {yuan(i['price_minor'])} = {yuan(i['amount_minor'])}") for i in value['items']]
    fields += [('餐费', yuan(value['subtotal_minor'])), ('打包费', yuan(value['packing_minor'])), ('配送费', yuan(value['delivery_minor'])), ('实付', yuan(value['amount_minor'])), ('付款账户', account['name']), ('备注', value['note'] or '无'), ('演示说明', '仅扣虚拟余额，不向真实美团或商家下单；地址按本确认卡固定。')]
    if key:fields += [('生日计划',plan['contact_name']+' · '+plan['event_date']),('预算说明','使用已预留预算；付款后释放未用余额')]
    return {'title': '确认外卖演示订单', 'fields': [{'label': a, 'value': b} for a,b in fields], 'amount_minor': value['amount_minor'], 'simulated': True}


def execute(bank, db, args, action_id):
    value = checked_quote(db, args['quote_id'])
    amount, order_id = value['amount_minor'], uid('foodorder_')
    db.execute('INSERT INTO food_orders VALUES (?,?,?,?,?,?,?)', (order_id, action_id, args['quote_id'], args['account_id'], amount, dumps(value), now()))
    db.execute('UPDATE accounts SET balance_minor=balance_minor-? WHERE id=?', (amount, args['account_id']))
    bank._ledger(db, action_id+':food', args['account_id'], value['shop_name'], 'out', amount, '餐饮', '外卖演示订单 '+order_id)
    birthday=None
    if args.get('birthday_plan_id'):
        from .birthdays import pay
        birthday=pay(db,args['birthday_plan_id'],order_id,amount)
    return {**({'birthday_plan':birthday} if birthday else {}), 'order_id': order_id, 'amount_minor': amount, 'shop_name': value['shop_name'], 'quantity': value['quantity'], 'items': value['items'], 'delivery_address': value['place']['address'], 'simulated': True, 'external_real_order': False}


def register_bank_food(app, database, agent, settings, session):
    @app.post('/api/food/pairing-code', dependencies=[Depends(session)])
    async def pairing_code():
        from .connections import generate
        return generate(database)

    @app.post('/internal/food/orders')
    async def internal_orders(request: Request):
        from .food_common import Owner, require_bridge
        require_bridge(request, settings.food_bridge_key_path)
        body = Owner.model_validate(await request.json())
        with database.connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key='food_owner'").fetchone()
            if not row or row[0] != body.owner:
                raise HTTPException(404, '未找到关联账户。')
            return orders(db)
