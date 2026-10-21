import asyncio,json,sqlite3
from datetime import datetime,timedelta,date
import httpx,pytest,pytest_asyncio
from banking.main import create_app
from banking.bank import BankError
from banking.db import TZ,Database
from banking.food_site import create_food_app
from banking.food_common import owner
from banking import birthdays
from test_security import register
from conftest import ScriptedModel,ask,tool,state

FIXED=datetime(2026,10,17,10,tzinfo=TZ)
@pytest_asyncio.fixture
async def setup(settings,tmp_path,monkeypatch):
    monkeypatch.setattr(birthdays,'clock',lambda:FIXED)
    settings.app_auth_enabled=True;settings.birthday_worker_enabled=False;settings.scheduled_worker_enabled=False
    settings.food_bridge_key_path=tmp_path/'food-key'
    app=create_app(settings,ScriptedModel())
    food=create_food_app(tmp_path/'food.sqlite3',settings.food_bridge_key_path)
    app.state.agent.food.transport=httpx.ASGITransport(app=food)
    food.state.bank_transport=httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        await register(c)
        conv=(await c.get('/api/bootstrap')).json()['conversation_id']
        with app.state.database.transaction() as db:
            db.execute("INSERT OR REPLACE INTO contact_profiles VALUES('zhang_san','爱人','2000-11-08','未设置',1)")
            investor=owner(db)
        with sqlite3.connect(tmp_path/'food.sqlite3') as db:
            db.execute("INSERT INTO food_bindings VALUES(?,'food_demo','生日测试')",(investor,))
            db.execute("INSERT INTO food_places VALUES('food_demo','lujiazui')")
        yield app,c,conv

def funds(app):
    with app.state.database.connection() as db:return [tuple(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')]

async def pending(app,conv,name,args):
    with app.state.database.transaction() as db:msg=app.state.agent.create_direct_approval(db,conv,name,args)
    return msg['data']['approval_id']

async def approve(c,conv,key,money=False,account='daily'):
    body={'decision':'confirm','conversation_id':conv}
    if money:
        body.update(pin='2468',account_id=account)
        context=(await c.get(f'/api/approvals/{key}/payment',params={'conversation_id':conv})).json()
        if context['sms_required']:
            sms=await c.post(f'/api/approvals/{key}/sms',json={'conversation_id':conv,'account_id':account})
            assert sms.status_code==200,sms.text
            body['sms_code']=sms.json()['demo_code']
    response=await c.post(f'/api/approvals/{key}/decision',json=body)
    assert response.status_code==200 and response.json()['status']=='executed',response.text
    return response.json()['receipt'],body

async def create(app,c,conv,budget='1000',**changes):
    key=await pending(app,conv,'create_birthday_plan',{'contact_id':'zhang_san','budget_yuan':budget,'occasion_month':'2026-11',**changes})
    receipt,_=await approve(c,conv,key)
    return receipt['birthday_plan']

async def manage(app,c,conv,p,action):
    key=await pending(app,conv,'manage_birthday_plan',{'plan_id':p['id'],'version':p['version'],'action':action})
    receipt,_=await approve(c,conv,key)
    return receipt['birthday_plan']

def get(app,key):
    with app.state.database.connection() as db:return birthdays.public(db,birthdays.get(db,key))

async def test_preview_confirmation_hold_without_debit_and_duplicate_prevention(setup):
    app,c,conv=setup;before=funds(app)
    result=app.state.agent.bank.query('get_birthday_context',{'keyword':'爱人'})
    assert len(result['contacts'])==1 and result['contacts'][0]['event_date']=='2026-11-08'
    assert not result['needs_clarification']
    preview=app.state.agent.bank.query('preview_birthday_plan',{'contact_id':'zhang_san','budget_yuan':'1000','occasion_month':'2026-11'})
    assert preview['affordable'] and preview['remind_at']=='2026-11-06T09:00:00+08:00'
    with app.state.database.connection() as db:assert not db.execute('SELECT 1 FROM birthday_plans').fetchone()
    key=await pending(app,conv,'create_birthday_plan',{'contact_id':'zhang_san','budget_yuan':'1000'})
    assert funds(app)==before
    with app.state.database.connection() as db:assert not db.execute('SELECT 1 FROM birthday_plans').fetchone()
    receipt,body=await approve(c,conv,key);p=receipt['birthday_plan']
    assert p['reserved_minor']==100000 and p['status']=='active' and funds(app)==before
    assert (await c.post(f'/api/approvals/{key}/decision',json=body)).json()['status']=='executed'
    with app.state.database.connection() as db:
        assert db.execute('SELECT count(*) FROM birthday_plans').fetchone()[0]==1
        assert db.execute("SELECT count(*) FROM commitments WHERE kind='birthday'").fetchone()[0]==1
    with pytest.raises(BankError,match='已有计划'):await create(app,c,conv)

async def test_missing_ambiguous_birthday_month_mismatch_insufficient_and_leap(setup):
    app,c,conv=setup
    with app.state.database.transaction() as db:
        db.execute("INSERT OR REPLACE INTO contact_profiles VALUES('liu_yang','爱人',NULL,'未设置',0)")
    assert app.state.agent.bank.query('get_birthday_context',{'keyword':'爱人'})['needs_clarification']
    with pytest.raises(BankError,match='尚未设置生日'):app.state.agent.bank.query('preview_birthday_plan',{'contact_id':'liu_yang','budget_yuan':'1000'})
    with pytest.raises(BankError,match='指定月份'):await create(app,c,conv,occasion_month='2026-12')
    with pytest.raises(BankError,match='不一致'):await create(app,c,conv,event_date='2026-11-09')
    with pytest.raises(BankError,match='资金不足'):await create(app,c,conv,budget='9999999')
    assert birthdays.next_birthday('2000-02-29',date(2027,2,1))==date(2027,2,28)
    assert birthdays.next_birthday('2000-02-29',date(2028,2,1))==date(2028,2,29)

async def test_due_is_durable_idempotent_search_only_and_does_not_switch_chat(setup):
    app,c,conv=setup;p=await create(app,c,conv);before=funds(app)
    with app.state.database.connection() as db:
        old_sessions=[tuple(r) for r in db.execute('SELECT * FROM sessions')]
        old_transactions=db.execute('SELECT count(*) FROM transactions').fetchone()[0]
    due=datetime(2026,11,6,9,0,tzinfo=TZ)
    assert await birthdays.process_due(app.state.database,app.state.settings,due-timedelta(seconds=1),app.state.agent.food)==0
    results=await asyncio.gather(*(birthdays.process_due(app.state.database,app.state.settings,due,app.state.agent.food) for _ in range(2)))
    assert sum(results)==1
    p=get(app,p['id']);assert p['reminder_conversation_id'] and p['reminder_conversation_id']!=conv
    assert p['recommendation']['menus'][0]['shop_id']=='lujiazui_3'
    assert p['recommendation']['menus'][0]['menu'][0]['price_minor']==13800
    assert p['reserved_minor']==100000 and p['order_id'] is None and funds(app)==before
    with app.state.database.connection() as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM sessions')]==old_sessions
        assert db.execute('SELECT count(*) FROM transactions').fetchone()[0]==old_transactions
        assert not db.execute('SELECT 1 FROM food_orders').fetchone()
    restarted=Database(app.state.database.path);restarted.initialize()
    assert await birthdays.process_due(restarted,app.state.settings,due,app.state.agent.food)==0
    notices=(await c.get('/api/birthday-plans/notices')).json()['items'];assert len(notices)==1
    assert (await c.post('/api/birthday-plans/'+p['id']+'/seen',json={})).status_code==200
    assert (await c.get('/api/birthday-plans/notices')).json()['items']==[]

async def test_pause_resume_cancel_stale_version_and_no_reminder_after_cancel(setup):
    app,c,conv=setup;p=await create(app,c,conv);before=funds(app)
    paused=await manage(app,c,conv,p,'pause');assert paused['status']=='paused' and paused['reserved_minor']==100000
    due=datetime(2026,11,6,9,tzinfo=TZ)
    assert await birthdays.process_due(app.state.database,app.state.settings,due,app.state.agent.food)==0
    with pytest.raises(BankError,match='已变化'):await manage(app,c,conv,p,'cancel')
    resumed=await manage(app,c,conv,paused,'resume');assert resumed['status']=='active'
    stopped=await manage(app,c,conv,resumed,'cancel');assert stopped['status']=='cancelled' and stopped['reserved_minor']==0
    assert funds(app)==before and await birthdays.process_due(app.state.database,app.state.settings,due,app.state.agent.food)==0

async def test_order_uses_reserved_budget_and_releases_only_its_unused_balance(setup):
    app,c,conv=setup
    with app.state.database.transaction() as db:
        db.execute("UPDATE accounts SET balance_minor=100000 WHERE id='daily'")
        db.execute("INSERT INTO commitments VALUES('rent','standalone',?,'daily','rent','房租',20000,20000,'2026-11-10','active','now','now')",(conv,))
    p=await create(app,c,conv,budget='800')
    with app.state.database.connection() as db,pytest.raises(BankError,match='已安排资金'):
        app.state.agent.bank._check_debit(db,'daily',1)
    quote=await app.state.agent.food.query('quote_food_order',{'shop_id':'lujiazui_3','items':[{'item_id':'lujiazui_3_0','quantity':1}]},conv)
    assert quote['amount_minor']==14500
    key=await pending(app,conv,'place_food_order',{'quote_id':quote['quote_id'],'birthday_plan_id':p['id'],'account_id':'daily'})
    assert (await c.post(f'/api/approvals/{key}/decision',json={'conversation_id':conv,'decision':'confirm','account_id':'daily'})).status_code==428
    receipt,body=await approve(c,conv,key,money=True)
    assert receipt['birthday_plan']['status']=='completed' and receipt['birthday_plan']['reserved_minor']==0
    assert receipt['birthday_plan']['spent_minor']==14500
    with app.state.database.connection() as db:
        assert db.execute("SELECT balance_minor FROM accounts WHERE id='daily'").fetchone()[0]==85500
        assert db.execute("SELECT remaining_minor FROM commitments WHERE id='rent'").fetchone()[0]==20000
        assert db.execute('SELECT count(*) FROM food_orders').fetchone()[0]==1
    assert (await c.post(f'/api/approvals/{key}/decision',json=body)).json()['status']=='executed'
    with app.state.database.connection() as db:assert db.execute("SELECT balance_minor FROM accounts WHERE id='daily'").fetchone()[0]==85500

async def test_reminder_quote_auto_binds_plan_red_payment_and_pending_cancel_protection(setup):
    app,c,conv=setup;p=await create(app,c,conv,budget='2000')
    await birthdays.process_due(app.state.database,app.state.settings,datetime(2026,11,6,9,tzinfo=TZ),app.state.agent.food)
    p=get(app,p['id']);reminder=p['reminder_conversation_id']
    quote=await app.state.agent.food.query('quote_food_order',{'shop_id':'lujiazui_3','items':[{'item_id':'lujiazui_3_0','quantity':8}]},reminder)
    assert quote['birthday_plan_id']==p['id'] and quote['amount_minor']==111800
    key=await pending(app,reminder,'place_food_order',{'quote_id':quote['quote_id'],'account_id':'daily'})
    ctx=(await c.get(f'/api/approvals/{key}/payment',params={'conversation_id':reminder})).json()
    assert ctx['sms_required'] and not next(a for a in ctx['accounts'] if a['id']=='savings')['eligible']
    body={'conversation_id':reminder,'decision':'confirm','account_id':'daily','pin':'2468'}
    assert (await c.post(f'/api/approvals/{key}/decision',json=body)).status_code==400
    sms=(await c.post(f'/api/approvals/{key}/sms',json={'conversation_id':reminder,'account_id':'daily'})).json()['demo_code']
    before=funds(app)
    # Cancelling from the original conversation invalidates an already displayed payment.
    stopped=await manage(app,c,conv,p,'cancel');assert stopped['reserved_minor']==0
    response=await c.post(f'/api/approvals/{key}/decision',json={**body,'sms_code':sms})
    assert response.status_code==200 and response.json()['status']=='failed',response.text
    assert funds(app)==before
    with app.state.database.connection() as db:assert not db.execute('SELECT 1 FROM food_orders').fetchone()

async def test_red_birthday_payment_requires_both_factors_then_completes(setup):
    app,c,conv=setup;p=await create(app,c,conv,budget='2000');before=funds(app)
    quote=await app.state.agent.food.query('quote_food_order',{'shop_id':'lujiazui_3','items':[{'item_id':'lujiazui_3_0','quantity':8}]},conv)
    key=await pending(app,conv,'place_food_order',{'quote_id':quote['quote_id'],'birthday_plan_id':p['id'],'account_id':'daily'})
    path=f'/api/approvals/{key}'
    assert (await c.post(path+'/decision',json={'decision':'confirm','conversation_id':conv,'pin':'2468','account_id':'daily'})).status_code==400
    assert funds(app)==before
    receipt,_=await approve(c,conv,key,money=True)
    assert receipt['birthday_plan']['spent_minor']==111800 and receipt['birthday_plan']['status']=='completed'
    assert receipt['birthday_plan']['reserved_minor']==0

async def test_profile_change_during_recommendations_does_not_create_stale_reminder(setup):
    app,c,conv=setup;p=await create(app,c,conv)
    class ChangingBridge:
        async def query(self,*args):
            with app.state.database.transaction() as db:db.execute("UPDATE contact_profiles SET birthday='2000-11-09' WHERE person_id='zhang_san'")
            return {'connected':False,'answer':'未关联'}
    due=datetime(2026,11,6,9,tzinfo=TZ)
    assert await birthdays.process_due(app.state.database,app.state.settings,due,ChangingBridge())==0
    assert get(app,p['id'])['reminder_conversation_id'] is None
    await birthdays.process_due(app.state.database,app.state.settings,due,app.state.agent.food)
    assert get(app,p['id'])['status']=='needs_review'

async def test_platform_failure_still_sends_one_reminder_and_keeps_funds(setup):
    app,c,conv=setup;p=await create(app,c,conv);before=funds(app)
    class BrokenBridge:
        async def query(self,*args):raise BankError('临时不可用')
    due=datetime(2026,11,6,9,tzinfo=TZ)
    assert await birthdays.process_due(app.state.database,app.state.settings,due,BrokenBridge())==1
    p=get(app,p['id']);assert p['reminder_conversation_id'] and not p['recommendation']['menus']
    assert '暂不可用' in p['recommendation']['message'] and p['reserved_minor']==100000 and funds(app)==before
    assert await birthdays.process_due(app.state.database,app.state.settings,due,BrokenBridge())==0

async def test_non_favorite_skips_automatic_reminder_without_changing_budget_or_profile(setup):
    app,c,conv=setup
    with app.state.database.transaction() as db:db.execute("UPDATE contact_profiles SET special_attention=0 WHERE person_id='zhang_san'")
    p=await create(app,c,conv);before=funds(app)
    assert not p['notify_enabled']
    preview=app.state.agent.bank.query('preview_birthday_plan',{'contact_id':'zhang_san','budget_yuan':'1000'})
    assert not preview['notify_enabled'] and '不自动提醒' in str(preview['display'])
    class NeverCalled:
        async def query(self,*args):raise AssertionError('Non-favorite must not trigger platform access')
    due=datetime(2026,11,6,9,tzinfo=TZ)
    assert await birthdays.process_due(app.state.database,app.state.settings,due,NeverCalled())==0
    current=get(app,p['id'])
    assert current['reserved_minor']==100000 and current['reminder_conversation_id'] is None
    assert funds(app)==before and (await c.get('/api/birthday-plans/notices')).json()['items']==[]
    with app.state.database.connection() as db:assert db.execute("SELECT special_attention FROM contact_profiles WHERE person_id='zhang_san'").fetchone()[0]==0
    # Explicitly starring later enables one catch-up reminder before the birthday.
    with app.state.database.transaction() as db:db.execute("UPDATE contact_profiles SET special_attention=1 WHERE person_id='zhang_san'")
    assert await birthdays.process_due(app.state.database,app.state.settings,due,app.state.agent.food)==1
    reminder=get(app,p['id'])['reminder_conversation_id']
    with app.state.database.transaction() as db:db.execute("UPDATE contact_profiles SET special_attention=0 WHERE person_id='zhang_san'")
    assert (await c.get('/api/birthday-plans/notices')).json()['items']==[]
    with app.state.database.connection() as db:assert db.execute('SELECT 1 FROM conversations WHERE id=?',(reminder,)).fetchone()
    assert get(app,p['id'])['reserved_minor']==100000 and funds(app)==before

async def test_unfavorite_during_menu_fetch_prevents_notification(setup):
    app,c,conv=setup;p=await create(app,c,conv)
    class ChangingBridge:
        async def query(self,*args):
            with app.state.database.transaction() as db:db.execute("UPDATE contact_profiles SET special_attention=0 WHERE person_id='zhang_san'")
            return {'connected':False,'answer':'未关联'}
    assert await birthdays.process_due(app.state.database,app.state.settings,datetime(2026,11,6,9,tzinfo=TZ),ChangingBridge())==0
    assert get(app,p['id'])['reminder_conversation_id'] is None
    assert not get(app,p['id'])['notify_enabled']

async def test_non_favorite_expiry_releases_budget_without_posting_a_notification(setup):
    app,c,conv=setup;p=await create(app,c,conv)
    with app.state.database.transaction() as db:
        db.execute("UPDATE contact_profiles SET special_attention=0 WHERE person_id='zhang_san'")
        before=db.execute('SELECT count(*) FROM messages').fetchone()[0]
    await birthdays.process_due(app.state.database,app.state.settings,datetime(2026,11,9,9,tzinfo=TZ),app.state.agent.food)
    assert get(app,p['id'])['status']=='expired' and get(app,p['id'])['reserved_minor']==0
    with app.state.database.connection() as db:assert db.execute('SELECT count(*) FROM messages').fetchone()[0]==before

async def test_over_budget_paused_wrong_source_and_fake_budget_spend_blocked(setup):
    app,c,conv=setup;p=await create(app,c,conv,budget='100')
    quote=await app.state.agent.food.query('quote_food_order',{'shop_id':'lujiazui_3','items':[{'item_id':'lujiazui_3_0','quantity':1}]},conv)
    with pytest.raises(BankError,match='超过生日预算'):await pending(app,conv,'place_food_order',{'quote_id':quote['quote_id'],'birthday_plan_id':p['id']})
    quote=await app.state.agent.food.query('quote_food_order',{'shop_id':'lujiazui_0','items':[{'item_id':'lujiazui_0_0','quantity':1}]},conv)
    with pytest.raises(BankError,match='预算来源账户'):await pending(app,conv,'place_food_order',{'quote_id':quote['quote_id'],'birthday_plan_id':p['id'],'account_id':'savings'})
    p=await manage(app,c,conv,p,'pause')
    with pytest.raises(BankError,match='已暂停'):await pending(app,conv,'place_food_order',{'quote_id':quote['quote_id'],'birthday_plan_id':p['id']})
    with app.state.database.connection() as db:commit=birthdays.get(db,p['id'])['commitment_id']
    with pytest.raises(BankError,match='生日预算'):await pending(app,conv,'adjust_commitment',{'commitment_id':commit,'remaining_yuan':'0'})
    with pytest.raises(BankError,match='生日预算'):await pending(app,conv,'simulate_expense',{'merchant':'假蛋糕','amount_yuan':'1','budget_commitment_id':commit})

@pytest.mark.parametrize('reason',['expiry','birthday-change','budget-change','lock'])
async def test_expiry_contact_changes_and_hard_lock(setup,reason):
    app,c,conv=setup;p=await create(app,c,conv);before=funds(app)
    with app.state.database.transaction() as db:
        if reason=='birthday-change':db.execute("UPDATE contact_profiles SET birthday='2000-11-09' WHERE person_id='zhang_san'")
        if reason=='budget-change':db.execute('UPDATE commitments SET remaining_minor=100 WHERE id=(SELECT commitment_id FROM birthday_plans WHERE id=?)',(p['id'],))
        if reason=='lock':db.execute('UPDATE account_guard SET locked=1 WHERE id=1')
    at=datetime(2026,11,9,9,tzinfo=TZ) if reason=='expiry' else datetime(2026,11,6,9,tzinfo=TZ)
    await birthdays.process_due(app.state.database,app.state.settings,at,app.state.agent.food)
    p=get(app,p['id'])
    assert p['status']==('active' if reason=='lock' else 'expired' if reason=='expiry' else 'needs_review')
    assert p['reserved_minor']==(100000 if reason=='lock' else 0) and funds(app)==before
    assert p['reminder_conversation_id'] is None
