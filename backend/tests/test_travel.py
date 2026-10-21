import asyncio
import json
import sqlite3
import time
from datetime import timedelta

import httpx
import pytest
from banking.bank import BankError, validate
from banking.main import create_app
from banking.travel_site import create_travel_app
from banking.travel_catalog import clock
from banking.travel_common import owner
from banking.securities_common import secret
from conftest import ScriptedModel, pending, tool
from test_security import register

def trip(**overrides):
    return {'origin':'上海','destination':'杭州','departure_date':(clock().date()+timedelta(days=8)).isoformat(),
        'return_date':(clock().date()+timedelta(days=11)).isoformat(),'area':'西湖区','passengers':2,'rooms':1,**overrides}

async def login(site,phone='11111111111'):
    res=await site.post('/api/travel/auth/sms',json={'phone':phone})
    assert res.status_code==200,res.text
    res=await site.post('/api/travel/auth/login',json={'phone':phone,'code':res.json()['demo_code']})
    assert res.status_code==200,res.text

def setup(settings,tmp_path,model=None):
    settings.app_auth_enabled=True
    settings.scheduled_worker_enabled=False
    settings.travel_bridge_key_path=tmp_path/'travel-key'
    app=create_app(settings,model or ScriptedModel())
    site=create_travel_app(tmp_path/'travel.sqlite3',settings.travel_bridge_key_path)
    app.state.agent.travel.transport=httpx.ASGITransport(app=site)
    site.state.bank_transport=httpx.ASGITransport(app=app)
    return app,site

def client(app,port):return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=f'http://127.0.0.1:{port}')

async def connected(bank,site):
    await register(bank)
    await login(site)
    code=(await bank.post('/api/connections/pairing-code',json={})).json()
    assert code['platforms']==['securities','food','travel']
    response=await site.post('/api/travel/connect',json={'code':code['code']})
    assert response.status_code==200,response.text
    return (await bank.get('/api/bootstrap')).json()['conversation_id']

async def quote_for(app,conv,**overrides):
    search=await app.state.agent.travel.query('search_travel_options',validate('search_travel_options',trip(**overrides)),conv)
    return await app.state.agent.travel.query('quote_travel_order',{'search_id':search['search_id'],
        'outbound_id':search['outbound'][2]['id'],'inbound_id':search['inbound'][2]['id'],'hotel_id':search['hotels'][0]['id']},conv)

class TravelModel(ScriptedModel):
    async def complete(self,messages):
        if self.responses and isinstance(self.responses[0],list):
            for call in self.responses[0]:
                name=call['function']['name']
                values=[json.loads(m['content']) for m in messages if m.get('role')=='tool']
                if name=='quote_travel_order':
                    found=next(v for v in reversed(values) if 'search_id' in v)
                    call['function']['arguments']=json.dumps({'search_id':found['search_id'],'outbound_id':found['outbound'][2]['id'],'inbound_id':found['inbound'][2]['id'],'hotel_id':found['hotels'][0]['id']})
                elif name=='place_travel_order':
                    found=next(v for v in reversed(values) if 'quote_id' in v)
                    call['function']['arguments']=json.dumps({'quote_id':found['quote_id']})
        return await super().complete(messages)

async def test_travel_chat_verified_purchase_receipt_and_site_orders(settings,tmp_path):
    model=TravelModel()
    app,site=setup(settings,tmp_path,model)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        assert (await web.get('/api/travel/orders')).status_code==401
        conv=await connected(bank,web)
        model.responses=[[tool('get_travel_context')],[tool('search_travel_options',**trip())],
            [tool('quote_travel_order',search_id='LATEST',outbound_id='out-train-8',inbound_id='back-train-8',hotel_id='hotel-0-0')],
            [tool('place_travel_order',quote_id='LATEST')],'演示预订已完成，请查看模拟订单。']
        before=(await bank.get('/api/bank')).json()
        assert (await bank.post('/api/messages',json={'conversation_id':conv,'text':'我已选好上海去杭州西湖区的往返早班动车和轻居演示酒店，2人1间住3晚，帮我下单'})).status_code==202
        await asyncio.wait_for(app.state.agent.queue.join(),5)
        approval=pending(app.state.agent)
        assert approval['tool_name']=='place_travel_order'
        amount=json.loads(approval['summary_json'])['amount_minor']
        assert amount>100000
        assert (await bank.get('/api/bank')).json()==before
        assert (await web.get('/api/travel/orders')).json()['orders']==[]
        path='/api/approvals/'+approval['id']
        payment=(await bank.get(path+'/payment?conversation_id='+conv)).json()
        assert payment['sms_required']
        decision={'conversation_id':conv,'decision':'confirm','account_id':'daily','pin':'2468'}
        assert (await bank.post(path+'/decision',json=decision)).status_code==400
        sms=(await bank.post(path+'/sms',json={'conversation_id':conv,'account_id':'daily'})).json()
        response=await bank.post(path+'/decision',json={**decision,'sms_code':sms['demo_code']})
        assert response.status_code==200,response.text
        result=response.json()
        assert result['status']=='executed'
        assert result['receipt']['amount_minor']==amount and result['receipt']['order_id'].startswith('travelorder_')
        await asyncio.wait_for(app.state.agent.queue.join(),5)
        after=(await bank.get('/api/bank')).json()
        cash=lambda v:next(a['balance_minor'] for a in v['accounts'] if a['id']=='daily')
        assert cash(before)-cash(after)==amount
        orders=(await web.get('/api/travel/orders')).json()['orders']
        assert len(orders)==1 and orders[0]['amount_minor']==amount
        assert orders[0]['items'][2]['nights']==3
        assert orders[0]['items'][2]['amount_minor']==22900*3
        assert (await bank.post(path+'/decision',json=decision)).json()['status']=='executed'
        assert (await bank.get('/api/bank')).json()==after
        with app.state.database.connection() as db:
            task=db.execute('SELECT task_id FROM approvals WHERE id=?',(approval['id'],)).fetchone()[0]
        report=(await app.state.agent.interrupt(task,conv))['interruption']
        assert report['debit_minor']==amount and report['credit_minor']==0

@pytest.mark.parametrize('change,status',[
    ({'origin':'火星'},400),({'destination':'上海'},400),({'area':'不支持的区'},400),
    ({'return_date':(clock().date()+timedelta(days=7)).isoformat()},400),
    ({'passengers':0},422),({'rooms':4},422),({'departure_date':clock().date().isoformat()},400),
])
async def test_travel_search_rejects_invalid_parameters(tmp_path,change,status):
    site=create_travel_app(tmp_path/'site.sqlite3',tmp_path/'key')
    async with client(site,8004) as web:
        await login(web)
        response=await web.post('/api/travel/search',json=trip(**change))
        assert response.status_code==status,response.text

async def test_travel_scopes_expiry_and_cancel(settings,tmp_path):
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web)
        value=await quote_for(app,conv)
        before=(await bank.get('/api/bank')).json()
        with app.state.database.transaction() as db:
            with pytest.raises(BankError,match='当前会话'):
                app.state.agent.create_direct_approval(db,'other','place_travel_order',{'quote_id':value['quote_id']})
            message=app.state.agent.create_direct_approval(db,conv,'place_travel_order',{'quote_id':value['quote_id']})
        result=await bank.post('/api/approvals/'+message['data']['approval_id']+'/decision',json={'conversation_id':conv,'decision':'cancel'})
        assert result.json()['status']=='cancelled'
        assert (await bank.get('/api/bank')).json()==before
        with app.state.database.transaction() as db:
            db.execute('UPDATE travel_quotes SET expires=?',(time.time()-1,))
            with pytest.raises(BankError,match='过期'):
                app.state.agent.create_direct_approval(db,conv,'place_travel_order',{'quote_id':value['quote_id']})

async def test_travel_auth_isolation_and_foreign_search(settings,tmp_path):
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as a,client(site,8004) as b:
        conv=await connected(bank,a)
        await login(b,'12222222222')
        assert not (await b.get('/api/travel/session')).json()['connected']
        assert (await b.get('/api/travel/orders')).json()['orders']==[]
        search=(await b.post('/api/travel/search',json=trip())).json()
        with app.state.database.transaction() as db:person=owner(db)
        path=f'/internal/travel/{person}/quote'
        body={'search_id':search['search_id'],'outbound_id':'out-train-8','inbound_id':'back-train-8','hotel_id':'hotel-0-0'}
        assert (await a.post(path,json=body)).status_code==403
        headers={'x-travel-key':secret(settings.travel_bridge_key_path)}
        assert (await a.post(path,json=body,headers=headers)).status_code==400
        search=(await a.post('/api/travel/search',json=trip())).json()
        body['search_id']=search['search_id']
        body['outbound_id']='back-train-8'
        assert (await a.post(path,json=body,headers=headers)).status_code==400
        body['outbound_id']='out-train-8'
        body['amount_minor']=1
        assert (await a.post(path,json=body,headers=headers)).status_code==422
        assert (await a.post('/api/travel/search',json=trip(),headers={'origin':'https://bad.example'})).status_code==403
        assert (await a.get('/api/travel/orders',headers={'x-travel-account':'another-user'})).status_code==409
        # Regenerated codes cannot connect a second travel account to this owner.
        code=(await bank.post('/api/connections/pairing-code',json={})).json()['code']
        assert (await b.post('/api/travel/connect',json={'code':code})).status_code==409
        assert (await b.get('/api/travel/orders')).json()['orders']==[]

async def test_travel_atomic_rollback_and_recovery_semantics(settings,tmp_path,monkeypatch):
    from banking.recovery import action_key,check_fresh_quote
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web)
        first=await quote_for(app,conv)
        second=await quote_for(app,conv)
        with app.state.database.transaction() as db:
            key=lambda q:action_key(app.state.agent.bank,db,'place_travel_order',{'quote_id':q['quote_id']})
            assert key(first)==key(second)
        with pytest.raises(BankError,match='重新查询'):
            check_fresh_quote({'recovery':True,'fresh_quote_ids':[]},'place_travel_order',{'quote_id':first['quote_id']})
        before=app.state.agent.bank.snapshot()
        def fail(*args,**kwargs):raise RuntimeError('ledger failed')
        monkeypatch.setattr(app.state.agent.bank,'_ledger',fail)
        with pytest.raises(RuntimeError),app.state.database.transaction() as db:
            app.state.agent.bank.execute(db,'place_travel_order',{'quote_id':first['quote_id']},'test-rollback')
        assert app.state.agent.bank.snapshot()==before
        assert (await web.get('/api/travel/orders')).json()['orders']==[]

@pytest.mark.parametrize('changed',[False,True])
async def test_travel_scheduled_payment_requotes_and_executes_once(settings,tmp_path,monkeypatch,changed):
    from banking import scheduled_transfers as scheduled
    from test_scheduled_actions import consent
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web)
        value=await quote_for(app,conv)
        before=app.state.agent.bank.snapshot()
        with app.state.database.transaction() as db:
            message=app.state.agent.create_direct_approval(db,conv,'place_travel_order',{'quote_id':value['quote_id']})
        due=(scheduled.clock()+timedelta(minutes=2)).isoformat()
        receipt=await consent(bank,conv,message['data']['approval_id'],due)
        assert receipt['scheduled'] and app.state.agent.bank.snapshot()==before
        assert (await web.get('/api/travel/orders')).json()['orders']==[]
        real=httpx.Client
        calls=[]
        def response(request):
            calls.append(request.url.path)
            if request.url.path.endswith('/search'):return httpx.Response(200,json={'search_id':'fresh-search'})
            fresh={**value,'selection':{**value['selection'],'search_id':'fresh-search'},'expires':time.time()+300}
            if changed:fresh['amount_minor']+=1
            return httpx.Response(200,json=fresh)
        monkeypatch.setattr(httpx,'Client',lambda **kw:real(**kw,transport=httpx.MockTransport(response)))
        instant=scheduled.when(due)+timedelta(seconds=1)
        assert scheduled.process_due(app.state.database,instant,settings)==1
        with app.state.database.connection() as db:
            row=db.execute('SELECT * FROM scheduled_transfers').fetchone()
            assert row['status']==('needs_reauthorization' if changed else 'executed'),row['error']
        assert len(calls)==2
        assert scheduled.process_due(app.state.database,instant,settings)==0
        after=app.state.agent.bank.snapshot()
        cash=lambda v:next(a['balance_minor'] for a in v['accounts'] if a['id']=='daily')
        assert cash(before)-cash(after)==(0 if changed else value['amount_minor'])

async def test_travel_scheduled_payment_cannot_follow_departure(settings,tmp_path):
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web)
        value=await quote_for(app,conv)
        with pytest.raises(BankError,match='出发日期之前'):
            app.state.agent.bank.prepare_payment('place_travel_order',{'quote_id':value['quote_id'],'execute_at':value['trip']['departure_date']+'T08:00:00+08:00'})
