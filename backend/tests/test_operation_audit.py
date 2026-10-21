import hashlib
import json
import sqlite3

import httpx
from banking.main import create_app
from banking.admin_site import create_app as admin_app
from banking.db import add_message,dumps,event,uid,now,Database
from banking.audit_view import detail,csv_file,safe
from conftest import ScriptedModel,ask,tool,pending,confirm
from test_security import register
from test_red_security import create_payment

async def test_real_money_verifications_exports_and_read_only(settings):
    settings.app_auth_enabled=True
    bank=create_app(settings,ScriptedModel());admin=admin_app(settings)
    async with bank.router.lifespan_context(bank),admin.router.lifespan_context(admin),httpx.AsyncClient(transport=httpx.ASGITransport(app=bank),base_url='http://127.0.0.1:8000') as user,httpx.AsyncClient(transport=httpx.ASGITransport(app=admin),base_url='http://127.0.0.1:8003') as operator:
        await register(user)
        conv=(await user.get('/api/bootstrap')).json()['conversation_id']
        with bank.state.database.transaction() as db:
            add_message(db,conv,None,'user','text','买入测试；手机号13800123456，银行卡6222000012345678，密码是2468',{})
        approval=await create_payment(bank,conv,'1001')
        body={'conversation_id':conv,'decision':'confirm','account_id':'daily','pin':'0000'}
        path=f'/api/approvals/{approval}/decision'
        assert (await user.post(path,json=body)).status_code==400
        sms=(await user.post(f'/api/approvals/{approval}/sms',json={'conversation_id':conv,'account_id':'daily'})).json()
        body['pin']='2468';body['sms_code']=sms['demo_code']
        assert (await user.post(path,json=body)).json()['status']=='executed'
        assert (await operator.get('/api/admin/audit/conversations')).status_code==401
        assert (await operator.get(f'/api/admin/audit/users/legacy/conversations/{conv}/export')).status_code==401
        await operator.post('/api/admin/login',json={'password':'000000'})
        with bank.state.database.connection() as db:
            before=[tuple(r) for r in db.execute('SELECT * FROM accounts')]
            before_tx=db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]
        base=f'/api/admin/audit/users/legacy/conversations/{conv}'
        result=(await operator.get(base)).json()
        assert result['summary']['debit_minor']==100100 and result['summary']['credit_minor']==0
        assert result['summary']['executed_count']==1
        titles=[r['title'] for r in result['timeline']]
        for label in ('支付密码验证失败','支付密码验证通过','交易短信验证通过','执行回执','已记账扣款'):
            assert label in titles
        decisions=[r for r in result['timeline'] if r['kind']=='confirmation']
        assert decisions[-1]['data']['assessment']['level']=='red'
        assert decisions[-1]['data']['assessment']['threshold_minor']==100000
        # Changing today's policy must not rewrite the recorded assessment.
        await user.patch('/api/payment-policy',json={'threshold_yuan':'2000','pin':'2468'})
        again=(await operator.get(base)).json()
        assert next(r for r in reversed(again['timeline']) if r['kind']=='confirmation')['data']['assessment']['threshold_minor']==100000
        export=await operator.get(base+'/export')
        assert export.status_code==200
        payload=export.json()
        canonical=json.dumps(payload['data'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
        assert hashlib.sha256(canonical).hexdigest()==payload['sha256']==export.headers['x-audit-sha256']
        for forbidden in ('13800123456','6222000012345678','密码是2468','code_hash','pin_hash','fingerprint','session_key','llm_api_key'):
            assert forbidden not in export.text
        assert '138****3456' in export.text
        csv=await operator.get(base+'/export?format=csv')
        assert csv.status_code==200 and '已记账扣款' in csv.text and csv.text.startswith('\ufeff')
        assert hashlib.sha256(csv.content).hexdigest()==csv.headers['x-audit-sha256']
        assert (await operator.get(base+'/export?format=pdf')).status_code==422
        assert any(r['action']=='audit_export' for r in (await operator.get('/api/admin/audit/administrators')).json()['items'])
        with bank.state.database.connection() as db:
            assert before==[tuple(r) for r in db.execute('SELECT * FROM accounts')]
            assert before_tx==db.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]

async def test_ledger_namespaces_and_recovery_never_double_count(agent):
    task=await ask(agent,[[tool('transfer_own_funds',target_account_id='savings',amount_yuan='100')],'完成'],'从日常账户转100到储蓄账户')
    operation=pending(agent)
    await confirm(agent)
    with agent.db.transaction() as db:
        conv=db.execute('SELECT conversation_id FROM tasks WHERE id=?',(task,)).fetchone()[0]
        # Ledger suffixes are real operation namespaces; wildcard-like or longer IDs are not.
        for action,amount in ((operation['id']+':stocks',500),(operation['id']+'x:stocks',90000),(operation['id'].replace('_','X')+':stocks',80000)):
            db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)',(uid('tx_'),action,'daily','测试','out',amount,'证券投资',now(),'test','仅测试命名空间'))
        child=uid('task_')
        graph=json.loads(db.execute('SELECT state_json FROM tasks WHERE id=?',(task,)).fetchone()[0])['dag']
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)',(child,conv,'completed','web',dumps({'recovery':{'parent_task_id':task},'dag':graph}),None,now()))
        add_message(db,conv,child,'assistant','text','我已经扣款999999元',{})
        result=detail(db,{'id':'legacy','username':'测试'},conv)
        assert result['summary']['debit_minor']==10500
        assert result['summary']['credit_minor']==10000
        assert result['summary']['executed_count']==1
        assert len(result['operations'][0]['ledger'])==3
        assert len([r for r in result['timeline'] if r['kind']=='step'])==len(graph['nodes'])
        assert result['tasks'][-1]['dag']['nodes'][0]['carried_from_task']==task
        assert any(r['kind']=='source' for r in result['timeline']) is False
        assert any(r['title']=='恢复任务并重新核对' for r in result['timeline'])
        db.execute("UPDATE approvals SET status='failed' WHERE id=?",(operation['id'],))
        mismatch=detail(db,{'id':'legacy','username':'测试'},conv)
        assert mismatch['summary']['debit_minor']==10500
        assert mismatch['operations'][0]['warnings']

async def test_filters_scope_deleted_and_account_security(settings):
    bank=create_app(settings,ScriptedModel());admin=admin_app(settings)
    async with bank.router.lifespan_context(bank),admin.router.lifespan_context(admin):
        first=bank.state.database;second=Database(settings.database_path.parent/'second.sqlite3');second.initialize()
        conversations=[]
        for database,label in ((first,'甲'),(second,'乙')):
            with database.transaction() as db:
                conv=database.conversation_id(db);conversations.append(conv)
                db.execute('UPDATE conversations SET title=?,updated_at=? WHERE id=?',(label+'股票','2026-10-17T12:00:00+08:00',conv))
                from banking.safety import audit
                audit(db,'transactions','opened',label+'账户保护')
        with sqlite3.connect(settings.database_path.parent/'accounts.sqlite3') as db:
            db.execute('CREATE TABLE users(id TEXT,username TEXT,phone TEXT,database_path TEXT)')
            db.executemany('INSERT INTO users VALUES(?,?,?,?)',[('a','用户甲','13800123456',str(first.path)),('b','用户乙','13900123456',str(second.path))])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin),base_url='http://127.0.0.1:8003') as client:
            await client.post('/api/admin/login',json={'password':'000000'})
            path='/api/admin/audit/conversations'
            result=(await client.get(path,params={'user_id':'a','query':'股票','from_date':'2026-10-17','to_date':'2026-10-17','status':'none'})).json()
            assert result['total']==1 and result['items'][0]['username']=='用户甲'
            assert (await client.get(path,params={'query':'no match'})).json()['total']==0
            assert (await client.get(path,params={'from_date':'2026-10-18','to_date':'2026-10-17'})).status_code==422
            assert (await client.get(path,params={'status':'not_a_status'})).status_code==422
            assert (await client.get(path,params={'user_id':'missing'})).status_code==404
            assert (await client.get(path,params={'limit':1,'offset':1})).json()['total']==2
            wrong=f'/api/admin/audit/users/b/conversations/{conversations[0]}'
            assert (await client.get(wrong)).status_code==404
            assert (await client.get(wrong+'/export')).status_code==404
            good=f'/api/admin/audit/users/a/conversations/{conversations[0]}'
            assert not any(r['kind']=='security' for r in (await client.get(good)).json()['timeline'])
            security=(await client.get('/api/admin/audit/users/a/security')).json()['items']
            assert [r['detail'] for r in security]==['甲账户保护']
            with first.transaction() as db:db.execute('UPDATE conversations SET deleted_at=? WHERE id=?',(now(),conversations[0]))
            assert (await client.get(good)).status_code==404

def test_export_redaction_and_spreadsheet_formulas():
    value=safe({'pin':'2468','code_hash':'secret','cookie':'session','fields':[{'label':'支付密码','value':'2468'}],
                'request':'手机号13800123456，密码0000，验证码：123456','card_number':'6222000012345678'})
    encoded=json.dumps(value,ensure_ascii=False)
    assert '2468' not in encoded and '123456' not in encoded and '0000' not in encoded
    assert '6222000012345678' not in encoded and 'code_hash' not in encoded
    out=csv_file([['=HYPERLINK("bad")',' @SUM(1,2)',100]],['a','b','c'])
    assert "'=HYPERLINK" in out and "' @SUM" in out
