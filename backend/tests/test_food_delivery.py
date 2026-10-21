import asyncio
import json
import time

import httpx
import pytest
from banking.bank import BankError
from banking.food_common import owner
from banking.food_site import create_food_app
from banking.main import create_app
from banking.securities_common import secret
from conftest import ScriptedModel, pending, tool
from test_security import register


async def food_login(client, phone='11111111111'):
    response = await client.post('/api/food/auth/sms', json={'phone': phone})
    assert response.status_code == 200, response.text
    code = response.json()['demo_code']
    result = await client.post('/api/food/auth/login', json={'phone': phone, 'code': code})
    assert result.status_code == 200, result.text
    return code


class QuoteModel(ScriptedModel):
    async def complete(self, messages):
        if self.responses and isinstance(self.responses[0], list):
            for call in self.responses[0]:
                if call['function']['name'] == 'place_food_order':
                    quote = next(json.loads(m['content']) for m in reversed(messages) if m.get('role') == 'tool' and 'quote_id' in json.loads(m['content']))
                    call['function']['arguments'] = json.dumps({'quote_id': quote['quote_id']})
        return await super().complete(messages)


async def test_food_chat_payment_atomic_order_and_external_sync(settings, tmp_path):
    settings.app_auth_enabled = True
    settings.food_bridge_key_path = tmp_path/'food-key'
    model = QuoteModel()
    app = create_app(settings, model)
    food = create_food_app(tmp_path/'food.sqlite3', settings.food_bridge_key_path)
    app.state.agent.food.transport = httpx.ASGITransport(app=food)
    food.state.bank_transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as bank, httpx.AsyncClient(transport=httpx.ASGITransport(app=food), base_url='http://127.0.0.1:8002') as site:
        assert (await site.get('/api/food/orders')).status_code == 401
        assert (await bank.post('/api/food/pairing-code', json={})).status_code == 401
        await register(bank)
        await food_login(site)
        context = await app.state.agent.food.query('get_food_context', {}, 'test')
        assert not context['connected']
        code = (await bank.post('/api/food/pairing-code', json={})).json()['code']
        response = await site.post('/api/food/connect', json={'code': code})
        assert response.status_code == 200, response.text
        assert (await site.post('/api/food/connect', json={'code': code})).status_code == 400
        conv = (await bank.get('/api/bootstrap')).json()['conversation_id']
        bridge = app.state.agent.food
        no_location = await bridge.query('search_food_shops', {'query': '黄焖鸡'}, conv)
        assert no_location['outcome']['status'] == 'needs_clarification'
        await site.post('/api/food/location', json={'place_id': 'lujiazui'})
        shops = await bridge.query('search_food_shops', {'query': '黄焖鸡'}, conv)
        assert shops['shops'] and all(s['distance_km'] <= s['radius_km'] for s in shops['shops'])
        body = {'shop_id': 'lujiazui_0', 'items': [{'item_id': 'lujiazui_0_0', 'quantity': 2}], 'note': '少辣'}
        model.responses = [[tool('get_food_context')], [tool('search_food_shops', query='黄焖鸡')], [tool('get_food_menu', shop_id='lujiazui_0')], [tool('quote_food_order', **body)], [tool('place_food_order', quote_id='LATEST')], '模拟外卖订单已下单。']
        before = (await bank.get('/api/bank')).json()
        assert (await bank.post('/api/messages', json={'conversation_id': conv, 'text': '帮我在小满饭堂陆家嘴店下单两份招牌黄焖鸡饭，少辣'})).status_code == 202
        await asyncio.wait_for(app.state.agent.queue.join(), 5)
        approval = pending(app.state.agent)
        assert approval['tool_name'] == 'place_food_order'
        summary = json.loads(approval['summary_json'])
        assert summary['amount_minor'] == 5700
        assert (await bank.get('/api/bank')).json() == before
        assert (await site.get('/api/food/orders')).json()['orders'] == []
        path = '/api/approvals/'+approval['id']
        payment = (await bank.get(path+'/payment?conversation_id='+conv)).json()
        assert payment['level'] == 'yellow'
        decision = {'conversation_id': conv, 'decision': 'confirm', 'account_id': 'daily'}
        assert (await bank.post(path+'/decision', json=decision)).status_code == 428
        decision['pin'] = '2468'
        response = await bank.post(path+'/decision', json=decision)
        assert response.status_code == 200, response.text
        assert response.json()['status'] == 'executed'
        await asyncio.wait_for(app.state.agent.queue.join(), 5)
        after = (await bank.get('/api/bank')).json()
        amount = lambda value: next(a['balance_minor'] for a in value['accounts'] if a['id']=='daily')
        assert amount(before)-amount(after) == 5700
        with app.state.database.connection() as db:
            task = db.execute('SELECT task_id FROM approvals WHERE id=?', (approval['id'],)).fetchone()[0]
        report = (await app.state.agent.interrupt(task, conv))['interruption']
        assert report['debit_minor'] == 5700 and report['credit_minor'] == 0
        assert (await bank.post(path+'/decision', json=decision)).json()['status'] == 'executed'
        assert (await bank.get('/api/bank')).json() == after
        orders = (await site.get('/api/food/orders')).json()['orders']
        assert len(orders) == 1 and orders[0]['quantity'] == 2 and orders[0]['amount_minor'] == 5700
        assert orders[0]['place']['id'] == 'lujiazui'
        # Another quote uses the same existing payment policy, including red SMS.
        assert (await bank.patch('/api/payment-policy', json={'threshold_yuan': '50', 'pin': '2468'})).status_code == 200
        expensive = {**body, 'items': [{'item_id':'lujiazui_0_0','quantity':20},{'item_id':'lujiazui_0_1','quantity':20}]}
        quote = await bridge.query('quote_food_order', expensive, conv)
        with app.state.database.transaction() as db:
            new = app.state.agent.create_direct_approval(db, conv, 'place_food_order', {'quote_id': quote['quote_id']})
        red_path = '/api/approvals/'+new['data']['approval_id']
        assert (await bank.get(red_path+'/payment?conversation_id='+conv)).json()['sms_required']
        assert (await bank.post(red_path+'/decision', json=decision)).status_code == 400
        sms = (await bank.post(red_path+'/sms', json={'conversation_id': conv, 'account_id': 'daily'})).json()
        assert (await bank.post(red_path+'/decision', json={**decision, 'sms_code': sms['demo_code']})).json()['status'] == 'executed'
        with app.state.database.transaction() as db, pytest.raises(BankError, match='当前会话'):
            app.state.agent.create_direct_approval(db, 'other', 'place_food_order', {'quote_id': quote['quote_id']})


async def test_food_quote_restrictions_cancel_expiry_and_locations(settings, tmp_path):
    settings.app_auth_enabled = True
    settings.food_bridge_key_path = tmp_path/'key'
    app = create_app(settings, ScriptedModel())
    food = create_food_app(tmp_path/'food.sqlite3', settings.food_bridge_key_path)
    app.state.agent.food.transport = httpx.ASGITransport(app=food)
    food.state.bank_transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as bank, httpx.AsyncClient(transport=httpx.ASGITransport(app=food), base_url='http://127.0.0.1:8002') as site:
        await register(bank)
        await food_login(site)
        code = (await bank.post('/api/food/pairing-code', json={})).json()['code']
        await site.post('/api/food/connect', json={'code':code})
        assert (await site.post('/api/food/location', json={'place_id':'bad'})).status_code == 400
        await site.post('/api/food/location', json={'place_id':'xujiahui'})
        assert (await site.get('/api/food/shops/lujiazui_0')).status_code == 400
        assert (await site.get('/api/food/shops?q=不存在菜品')).json()['shops'] == []
        await site.post('/api/food/location', json={'place_id':'lujiazui'})
        with app.state.database.transaction() as db:
            investor = owner(db)
        headers = {'x-food-key': secret(settings.food_bridge_key_path)}
        path = f'/internal/food/{investor}/quote'
        assert (await site.post(path,json={})).status_code in {403,422}
        for items, status in [([{'item_id':'lujiazui_0_4','quantity':1}],400), ([{'item_id':'lujiazui_1_0','quantity':1}],400), ([{'item_id':'lujiazui_0_0','quantity':0}],422), ([{'item_id':'lujiazui_0_0','quantity':21}],422)]:
            response = await site.post(path, headers=headers, json={'shop_id':'lujiazui_0','items':items})
            assert response.status_code == status, response.text
        conv = (await bank.get('/api/bootstrap')).json()['conversation_id']
        quote = await app.state.agent.food.query('quote_food_order', {'shop_id':'lujiazui_0','items':[{'item_id':'lujiazui_0_0','quantity':1}]},conv)
        before = (await bank.get('/api/bank')).json()
        with app.state.database.transaction() as db:
            approval = app.state.agent.create_direct_approval(db,conv,'place_food_order',{'quote_id':quote['quote_id']})
        assert (await bank.post('/api/approvals/'+approval['data']['approval_id']+'/decision',json={'conversation_id':conv,'decision':'cancel'})).json()['status'] == 'cancelled'
        assert (await bank.get('/api/bank')).json() == before
        with app.state.database.transaction() as db:
            db.execute('UPDATE food_quotes SET expires=?', (time.time()-1,))
            with pytest.raises(BankError,match='过期'):
                app.state.agent.create_direct_approval(db,conv,'place_food_order',{'quote_id':quote['quote_id']})


async def test_food_users_one_to_one_binding_and_isolation(tmp_path):
    key = tmp_path/'key'
    food = create_food_app(tmp_path/'food.sqlite3', key)
    headers = {'x-food-key': secret(key)}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=food),base_url='http://127.0.0.1:8002') as a, httpx.AsyncClient(transport=httpx.ASGITransport(app=food),base_url='http://127.0.0.1:8002') as b:
        await food_login(a)
        await food_login(b,'12222222222')
        code = (await a.post('/internal/food/pairing-code',headers=headers,json={'owner':'bank-a','label':'A'})).json()['code']
        assert (await a.post('/api/food/connect',json={'code':code})).status_code == 200
        await a.post('/api/food/location',json={'place_id':'lujiazui'})
        assert (await b.get('/api/food/session')).json()['place'] is None
        assert not (await b.get('/api/food/session')).json()['connected']
        code = (await a.post('/internal/food/pairing-code',headers=headers,json={'owner':'bank-a','label':'A'})).json()['code']
        assert (await b.post('/api/food/connect',json={'code':code})).status_code == 409
        assert (await b.get('/api/food/orders')).json()['orders'] == []
        assert (await a.post('/api/food/location',json={'place_id':'lujiazui'},headers={'origin':'https://other.test'})).status_code == 403


async def test_food_orders_route_to_correct_bank_tenant(settings, tmp_path):
    from banking.db import Database, dumps, now
    from banking.security import encode_pin
    from test_accounts import sms_login
    settings.app_auth_enabled = True
    settings.multi_user_accounts = True
    settings.food_bridge_key_path = tmp_path/'key'
    legacy = Database(settings.database_path)
    legacy.initialize()
    with legacy.transaction() as db:
        db.execute("INSERT INTO app_identity(id,username,phone,pin_hash,contacts_done) VALUES (1,'演示文档','11111111111',?,1)", (encode_pin('0000'),))
    app = create_app(settings, ScriptedModel())
    food = create_food_app(tmp_path/'food.sqlite3', settings.food_bridge_key_path)
    food.state.bank_transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as bank_a, httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as bank_b, httpx.AsyncClient(transport=httpx.ASGITransport(app=food),base_url='http://127.0.0.1:8002') as site_a, httpx.AsyncClient(transport=httpx.ASGITransport(app=food),base_url='http://127.0.0.1:8002') as site_b:
        await sms_login(bank_a,'11111111111')
        await bank_a.get('/api/bootstrap')
        app.state.directory.apps['legacy'].state.agent.food.transport=httpx.ASGITransport(app=food)
        await food_login(site_a)
        code=(await bank_a.post('/api/food/pairing-code',json={})).json()['code']
        assert (await site_a.post('/api/food/connect',json={'code':code})).status_code==200
        with legacy.transaction() as db:
            db.execute('INSERT INTO food_orders VALUES (?,?,?,?,?,?,?)',('tenant-order','approval','quote','daily',100,dumps({'shop_name':'A的演示店铺','quantity':1,'items':[],'place':{}}),now()))
        assert (await site_a.get('/api/food/orders')).json()['orders'][0]['id']=='tenant-order'
        await sms_login(bank_b,'13800123456','独立用户')
        await bank_b.post('/api/auth/contacts-done',json={})
        await bank_b.post('/api/auth/pin',json={'pin':'2468','repeat_pin':'2468'})
        await bank_b.get('/api/bootstrap')
        for child in app.state.directory.apps.values():
            child.state.agent.food.transport=httpx.ASGITransport(app=food)
        await food_login(site_b,'12222222222')
        code=(await bank_b.post('/api/food/pairing-code',json={})).json()['code']
        assert (await site_b.post('/api/food/connect',json={'code':code})).status_code==200
        assert (await site_b.get('/api/food/orders')).json()['orders']==[]
        assert (await site_a.get('/api/food/orders')).json()['orders'][0]['id']=='tenant-order'
