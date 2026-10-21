import asyncio,json
from datetime import timedelta
import pytest
from banking.bank import BankError
from banking.travel_changes import current,checked_change
from test_travel import setup,client,connected,quote_for,trip

def cash(snapshot,name='daily'):return next(a['balance_minor'] for a in snapshot['accounts'] if a['id']==name)

async def paid(app,conv,**changes):
    value=await quote_for(app,conv,**changes)
    with app.state.database.transaction() as db:
        receipt=app.state.agent.bank.execute(db,'place_travel_order',{'quote_id':value['quote_id']},'fixture-paid')
    return receipt['order_id'],value

async def consent(app,bank,conv,tool,args):
    with app.state.database.transaction() as db:message=app.state.agent.create_direct_approval(db,conv,tool,args)
    path='/api/approvals/'+message['data']['approval_id']
    context=(await bank.get(path+'/payment',params={'conversation_id':conv})).json()
    body={'conversation_id':conv,'decision':'confirm','pin':'2468'}
    if context['account_required']:body['account_id']='daily'
    assert (await bank.post(path+'/decision',json={'conversation_id':conv,'decision':'confirm'})).status_code==428
    if context['sms_required']:
        sms=await bank.post(path+'/sms',json={'conversation_id':conv,**({'account_id':'daily'} if context['account_required'] else {})})
        assert sms.status_code==200,sms.text
        body['sms_code']=sms.json()['demo_code']
    response=await bank.post(path+'/decision',json=body)
    assert response.status_code==200,response.text
    assert response.json()['status']=='executed',response.text
    await asyncio.wait_for(app.state.agent.queue.join(),5)
    return path,body,response.json()['receipt'],context

@pytest.mark.parametrize('kind',['more','less','equal'])
async def test_free_change_delta_settlement_updates_same_order(settings,tmp_path,kind):
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web)
        order_id,original=await paid(app,conv)
        # Hotel upgrade/downgrade; equal-price change uses the hotel's room
        # count and nights so the total stays equal while dates change.
        found=await app.state.agent.travel.query('search_travel_options',trip(),conv)
        hotel='hotel-0-1' if kind=='more' else 'hotel-0-0'
        proposal=await app.state.agent.travel.query('quote_travel_order',{'search_id':found['search_id'],'outbound_id':'out-train-8','inbound_id':'back-train-8','hotel_id':hotel},conv)
        if kind=='less':
            with app.state.database.transaction() as db:
                value=current(db,order_id)
                value['items'][2]['price_minor']+=10000;value['items'][2]['amount_minor']+=30000
                value['amount_minor']+=30000;value['funding'][0]['amount_minor']+=30000
                db.execute('UPDATE accounts SET balance_minor=balance_minor-30000 WHERE id=?',('daily',))
                db.execute('INSERT INTO travel_order_state VALUES(?,1,?,?,?)',(order_id,'active',json.dumps(value),'test'))
        if kind=='equal':
            with app.state.database.transaction() as db:
                value=json.loads(db.execute('SELECT payload FROM travel_quotes WHERE id=?',(proposal['quote_id'],)).fetchone()[0])
                value['items'][2]['room_type']='演示同价大床房'
                db.execute('UPDATE travel_quotes SET payload=? WHERE id=?',(json.dumps(value),proposal['quote_id']))
        change=await app.state.agent.travel.query('quote_travel_change',{'order_id':order_id,'new_quote_id':proposal['quote_id'],'change_parts':['hotels']},conv)
        before=app.state.agent.bank.snapshot()
        path,body,receipt,context=await consent(app,bank,conv,'change_travel_order',{'quote_id':change['quote_id']})
        assert receipt['fee_minor']==0
        assert context['account_required']==(change['delta_minor']>0)
        after=app.state.agent.bank.snapshot()
        assert cash(before)-cash(after)==change['delta_minor']
        assert receipt['debit_minor']==max(change['delta_minor'],0)
        assert receipt['refund_minor']==max(-change['delta_minor'],0)
        orders=(await web.get('/api/travel/orders')).json()['orders']
        assert len(orders)==1 and orders[0]['id']==order_id and orders[0]['version']==2
        assert orders[0]['net_paid_minor']==orders[0]['amount_minor']==change['new_amount_minor']
        assert orders[0]['items'][:2]==original['items'][:2]
        assert (await bank.post(path+'/decision',json=body)).json()['status']=='executed'
        assert app.state.agent.bank.snapshot()==after
        with app.state.database.connection() as db,pytest.raises(BankError,match='变化|已经用于'):
            checked_change(db,change['quote_id'],conv)
        with app.state.database.connection() as db:
            with pytest.raises(BankError,match='已经用于'):
                from banking.travel_bridge import checked_quote
                checked_quote(db,proposal['quote_id'])

async def test_free_cancellation_refunds_once_and_prevents_changes(settings,tmp_path):
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web);order_id,original=await paid(app,conv)
        before=app.state.agent.bank.snapshot()
        path,body,receipt,_=await consent(app,bank,conv,'cancel_travel_order',{'order_id':order_id,'order_version':1})
        assert receipt['refund_minor']==original['amount_minor'] and receipt['fee_minor']==0
        after=app.state.agent.bank.snapshot()
        assert cash(after)-cash(before)==original['amount_minor']
        order=(await web.get('/api/travel/orders')).json()['orders'][0]
        assert order['status_code']=='cancelled' and order['net_paid_minor']==0
        assert (await bank.post(path+'/decision',json=body)).json()['status']=='executed'
        assert app.state.agent.bank.snapshot()==after
        with pytest.raises(BankError,match='已退订'):
            app.state.agent.bank.prepare('cancel_travel_order',{'order_id':order_id,'order_version':2})

async def test_change_preserves_unselected_hotel_and_rejects_stale_scope(settings,tmp_path):
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web);order_id,original=await paid(app,conv)
        found=await app.state.agent.travel.query('search_travel_options',trip(return_date=(__import__('datetime').date.fromisoformat(original['trip']['return_date'])+timedelta(days=1)).isoformat()),conv)
        proposal=await app.state.agent.travel.query('quote_travel_order',{'search_id':found['search_id'],'outbound_id':'out-train-8','inbound_id':'back-train-13','hotel_id':'hotel-0-0'},conv)
        change=await app.state.agent.travel.query('quote_travel_change',{'order_id':order_id,'new_quote_id':proposal['quote_id'],'change_parts':['inbound']},conv)
        assert change['items'][0]==original['items'][0]
        assert change['items'][2]==original['items'][2]
        with app.state.database.transaction() as db:
            with pytest.raises(BankError,match='当前会话'):
                app.state.agent.create_direct_approval(db,'other','change_travel_order',{'quote_id':change['quote_id']})
            current_value=current(db,order_id)
            db.execute('INSERT INTO travel_order_state VALUES(?,2,?,?,?)',(order_id,'active',json.dumps(current_value),'test'))
        with app.state.database.connection() as db,pytest.raises(BankError,match='变化'):
            checked_change(db,change['quote_id'],conv)

async def test_cancellation_fallback_for_deleted_source_and_atomic_rollback(settings,tmp_path,monkeypatch):
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web);order_id,original=await paid(app,conv)
        with app.state.database.transaction() as db:
            db.execute("UPDATE travel_orders SET account_id='deleted-custom' WHERE id=?",(order_id,))
        before=app.state.agent.bank.snapshot()
        _,summary=app.state.agent.bank.prepare('cancel_travel_order',{'order_id':order_id,'order_version':1})
        assert '原账户已删除' in str(summary)
        def fail(*args,**kwargs):raise RuntimeError('ledger failed')
        monkeypatch.setattr(app.state.agent.bank,'_ledger',fail)
        with pytest.raises(RuntimeError),app.state.database.transaction() as db:
            app.state.agent.bank.execute(db,'cancel_travel_order',{'order_id':order_id,'order_version':1},'rollback')
        assert app.state.agent.bank.snapshot()==before
        assert (await web.get('/api/travel/orders')).json()['orders'][0]['status_code']=='active'

async def test_future_trip_date_does_not_force_scheduled_payment(settings,tmp_path):
    from test_travel import TravelModel
    from conftest import tool,pending
    model=TravelModel();app,site=setup(settings,tmp_path,model)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web)
        model.responses=[[tool('search_travel_options',**trip())],[tool('quote_travel_order',search_id='LATEST',outbound_id='out-train-8',inbound_id='back-train-8',hotel_id='hotel-0-0')],[tool('place_travel_order',quote_id='LATEST')]]
        await bank.post('/api/messages',json={'conversation_id':conv,'text':'现在买明天出发后天返回的往返动车和酒店，旅行日期2026-10-25到2026-10-28，所选方案都合适，请生成普通确认卡'})
        await asyncio.wait_for(app.state.agent.queue.join(),5)
        assert pending(app.state.agent)['tool_name']=='place_travel_order'

async def test_offline_users_get_schema_only_travel_upgrade(settings,tmp_path):
    from banking.db import Database
    from banking.main import create_app
    dormant=Database(tmp_path/'dormant.sqlite3',seed_demo=False);dormant.initialize()
    with dormant.connection() as db:
        db.execute("INSERT INTO meta VALUES('travel_owner','offline-traveler')")
        db.execute("INSERT OR REPLACE INTO accounts VALUES('daily','日常账户','checking',12345)")
        for table in ('travel_order_state','travel_changes','travel_change_quotes','travel_quote_claims'):db.execute(f'DROP TABLE {table}')
    settings.multi_user_accounts=True;settings.app_auth_enabled=True;settings.scheduled_worker_enabled=False
    app=create_app(settings)
    directory=app.state.directory;directory.initialize()
    with directory.connection() as db:
        db.execute('INSERT INTO users VALUES (?,?,?,?)',('offline','12222222222','离线用户',str(dormant.path)))
    async with app.router.lifespan_context(app):
        with dormant.connection() as db:
            assert db.execute("SELECT balance_minor FROM accounts WHERE id='daily'").fetchone()[0]==12345
            assert db.execute('SELECT COUNT(*) FROM travel_order_state').fetchone()[0]==0

async def test_cancellation_returns_multiple_funding_sources_exactly(settings,tmp_path):
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web)
        initial=app.state.agent.bank.snapshot()
        order_id,original=await paid(app,conv)
        found=await app.state.agent.travel.query('search_travel_options',trip(),conv)
        proposal=await app.state.agent.travel.query('quote_travel_order',{'search_id':found['search_id'],'outbound_id':'out-train-8','inbound_id':'back-train-8','hotel_id':'hotel-0-1'},conv)
        change=await app.state.agent.travel.query('quote_travel_change',{'order_id':order_id,'new_quote_id':proposal['quote_id'],'change_parts':['hotels']},conv)
        with app.state.database.transaction() as db:
            app.state.agent.bank.execute(db,'change_travel_order',{'quote_id':change['quote_id'],'account_id':'savings'},'fixture-upgrade')
        _,_,receipt,_=await consent(app,bank,conv,'cancel_travel_order',{'order_id':order_id,'order_version':2})
        assert {r['account_id'] for r in receipt['refunds']}=={'daily','savings'}
        after=app.state.agent.bank.snapshot()
        assert cash(after)==cash(initial) and cash(after,'savings')==cash(initial,'savings')
        assert receipt['refund_minor']==original['amount_minor']+change['delta_minor']
