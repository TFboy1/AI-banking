import json

from banking import payment_security as security
from banking.db import Database
import httpx
from banking.main import create_app
from test_security import register
from test_red_security import create_payment


def ledger(db, key, amount, occurred, status='executed', tool='transfer', category='转账'):
    db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,?,?,?)",(key,'task','message',tool,'{}','{}',status,None,occurred,occurred))
    db.execute("INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)",(key,key+':out','daily','测试','out',amount,category,occurred,None,''))


def assessment(db, amount, tool='transfer', args=None):
    return security.risk(db,{'tool_name':tool,'summary_json':json.dumps({'amount_minor':amount})},args or {})


def test_beijing_midnight_success_only_own_transfers_once_and_boundary(database, monkeypatch):
    monkeypatch.setattr(security,'now',lambda:'2026-10-18T09:00:00+08:00')
    with database.transaction() as db:
        ledger(db,'yesterday',990000,'2026-10-17T15:59:59Z')
        ledger(db,'midnight',30000,'2026-10-17T16:00:00Z')
        ledger(db,'own',20000,'2026-10-18T10:00:00+08:00',tool='transfer_own_funds',category='内部转账')
        db.execute("INSERT INTO transactions VALUES('own-in','own:in','savings','测试','in',20000,'内部转账','2026-10-18T10:00:00+08:00',NULL,'')")
        ledger(db,'cancel',900000,'2026-10-18T09:00:00+08:00',status='cancelled')
        ledger(db,'failed',900000,'2026-10-18T09:00:00+08:00',status='failed')
        ledger(db,'schedule',900000,'2026-10-18T09:00:00+08:00',tool='schedule_transfer')
        ledger(db,'stock',900000,'2026-10-18T09:00:00+08:00',tool='purchase_stock_basket',category='证券投资')
        assert security.daily_transferred(db)==50000
        assert assessment(db,50000)['level']=='yellow'
        assert assessment(db,50001)['level']=='yellow'
        assert assessment(db,100000)['level']=='yellow'
        assert assessment(db,100001)['level']=='red'
        assert assessment(db,1,'schedule_transfer')['sms_required']
        assert assessment(db,1,args={'execute_at':'2026-10-19T09:00:00+08:00'})['sms_required']
        assert assessment(db,1,'purchase_product')['sms_required']
        assert 'daily_transfer_minor' not in assessment(db,1,'purchase_product')
        monkeypatch.setattr(security,'now',lambda:'2026-10-19T00:00:00+08:00')
        assert security.daily_transferred(db)==0


def test_migration_fixed_once_preserves_funds_and_user_isolation(database, tmp_path):
    with database.transaction() as db:
        db.execute('DROP TABLE payment_policy_mode')
        db.execute('UPDATE payment_policy SET threshold_minor=999999,version=7')
        balances=db.execute('SELECT * FROM accounts ORDER BY id').fetchall()
    database.initialize()
    with database.connection() as db:
        assert security.policy(db)['threshold_minor']==999999
        assert security.policy(db)['version']==7
        assert db.execute('SELECT * FROM accounts ORDER BY id').fetchall()==balances
    database.initialize()
    other=Database(tmp_path/'other.sqlite3');other.initialize()
    with database.transaction() as db:
        ledger(db,'one-user',100000,security.now())
        assert not assessment(db,1)['sms_required']
        assert security.policy(db)['version']==7
    with other.connection() as db:
        assert security.daily_transferred(db)==0
        assert assessment(db,1)['level']=='yellow'


async def test_each_transaction_uses_its_own_amount_across_conversations(settings):
    settings.app_auth_enabled=True
    app=create_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client)
        first=(await client.get('/api/bootstrap')).json()['conversation_id']
        second=(await client.post('/api/conversations',json={})).json()['conversation_id']
        one=await create_payment(app,first,'600')
        two=await create_payment(app,second,'600')
        assert not (await client.get(f'/api/approvals/{two}/payment',params={'conversation_id':second})).json()['sms_required']
        body={'decision':'confirm','pin':'2468','account_id':'daily'}
        assert (await client.post(f'/api/approvals/{one}/decision',json={**body,'conversation_id':first})).json()['status']=='executed'
        path=f'/api/approvals/{two}'
        ctx=(await client.get(path+'/payment',params={'conversation_id':second})).json()
        assert not ctx['sms_required'] and ctx['basis']=='single_transaction'
        assert (await client.post(path+'/decision',json={**body,'conversation_id':second})).json()['status']=='executed'
        assert (await client.get('/api/payment-policy')).json()['daily_transfer_minor']==120000
