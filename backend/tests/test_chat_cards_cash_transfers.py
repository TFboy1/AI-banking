import json

import httpx
from banking.main import create_app
from conftest import ScriptedModel, ask, confirm, pending, state, tool
from test_security import register


async def test_chat_card_confirmation_idempotency_and_duplicate(agent):
    number='6222000000018899'
    before=agent.bank.snapshot()['total_assets_minor']
    task=await ask(agent,[[tool('add_bank_card',bank_name='测试银行',card_number=number)],'银行卡已添加。'],f'帮我添加测试银行的银行卡，卡号{number}')
    approval=pending(agent)
    summary=json.loads(approval['summary_json'])
    assert number not in json.dumps(summary) and '8899' in json.dumps(summary)
    assert state(agent,task)['status']=='waiting_confirmation'
    assert agent.bank.snapshot()['total_assets_minor']==before
    result=await confirm(agent)
    assert result['status']=='executed' and result['receipt']['card']['balance_minor']==500000
    assert agent.bank.snapshot()['total_assets_minor']==before+500000
    assert (await agent.confirm(approval['id'],'confirm'))['status']=='executed'
    assert agent.bank.snapshot()['total_assets_minor']==before+500000
    duplicate=await ask(agent,[[tool('add_bank_card',bank_name='测试银行',card_number=number)]],f'再添加测试银行银行卡{number}')
    assert state(agent,duplicate)['status']=='failed'
    assert agent.bank.snapshot()['total_assets_minor']==before+500000


async def test_chat_card_cancel_and_fabricated_number_blocked(agent):
    before=agent.bank.snapshot()['total_assets_minor']
    number='6222000000019988'
    await ask(agent,[[tool('add_bank_card',bank_name='取消银行',card_number=number)]],f'添加取消银行银行卡{number}')
    await confirm(agent,'cancel')
    assert agent.bank.snapshot()['total_assets_minor']==before
    task=await ask(agent,[[tool('add_bank_card',bank_name='取消银行',card_number='6222000000019999')]],f'添加取消银行银行卡{number}')
    assert state(agent,task)['status']=='failed' and '不能编造' in state(agent,task)['error']
    missing=await ask(agent,[[tool('add_bank_card',bank_name='取消银行',card_number=number)]],'帮我添加取消银行银行卡')
    assert state(agent,missing)['status']=='failed'
    assert agent.bank.snapshot()['total_assets_minor']==before


async def test_chat_card_claim_without_receipt_is_blocked(agent):
    task=await ask(agent,['已成功添加银行卡。'],'帮我添加银行卡')
    with agent.db.connection() as db:
        reply=db.execute("SELECT text FROM messages WHERE task_id=? AND role='assistant' ORDER BY rowid DESC LIMIT 1",(task,)).fetchone()[0]
    assert '没有添加银行卡的成功回执' in reply


async def test_card_bank_name_followup_uses_user_supplied_number(agent):
    number='6222000000097766'
    await ask(agent,['请告诉我这张卡的银行名称。'],f'帮我添加银行卡，卡号{number}')
    identifier=await ask(agent,[[tool('add_bank_card',bank_name='测试银行',card_number=number)],'银行卡已添加。'],'测试银行')
    assert state(agent,identifier)['status']=='waiting_confirmation'
    await confirm(agent)
    assert any(card['last4']=='7766' for card in agent.bank.snapshot()['cards'])


async def test_cash_to_cash_security_totals_and_target_recheck(settings):
    settings.app_auth_enabled=True
    app=create_app(settings,ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client)
        conv=(await client.get('/api/bootstrap')).json()['conversation_id']
        category=(await client.post('/api/account-categories',json={'name':'旅行预算'})).json()['id']
        with app.state.database.transaction() as db:
            db.execute("UPDATE accounts SET balance_minor=200000 WHERE id='daily'")
            db.execute("UPDATE accounts SET balance_minor=100000 WHERE id='savings'")
        initial=(await client.get('/api/bank')).json()
        for source,target,amount,red in [('daily','savings','1000',False),('savings',category,'1000.01',True),(category,'daily','0.01',False)]:
            before=(await client.get('/api/bank')).json()
            message=(await client.post('/api/account-transfers',json={'conversation_id':conv,'account_id':source,'target_account_id':target,'amount_yuan':amount})).json()
            approval=message['data']['approval_id']
            context=(await client.get(f'/api/approvals/{approval}/payment?conversation_id={conv}')).json()
            assert context['sms_required']==red
            assert not next(a for a in context['accounts'] if a['id']==target)['eligible']
            path=f'/api/approvals/{approval}/decision'
            args={'conversation_id':conv,'decision':'confirm','account_id':source}
            assert (await client.post(path,json=args)).status_code==428
            args['pin']='2468'
            if red:
                assert (await client.post(path,json=args)).status_code==400
                args['sms_code']=(await client.post(f'/api/approvals/{approval}/sms',json={'conversation_id':conv,'account_id':source})).json()['demo_code']
            result=(await client.post(path,json=args)).json()
            assert result['status']=='executed'
            after=(await client.get('/api/bank')).json()
            balances=lambda value:{a['id']:a['balance_minor'] for a in value['accounts']}
            moved=round(float(amount)*100)
            assert balances(before)[source]-balances(after)[source]==moved
            assert balances(after)[target]-balances(before)[target]==moved
            assert after['total_assets_minor']==initial['total_assets_minor']
            assert after['account_balance_minor']==initial['account_balance_minor']
            assert (await client.post(path,json=args)).json()['status']=='executed'
            with app.state.database.connection() as db:
                rows=db.execute('SELECT direction,category,amount_minor FROM transactions WHERE action_id IN (?,?)',(approval+':out',approval+':in')).fetchall()
                assert len(rows)==2 and all(r['category']=='内部转账' and r['amount_minor']==moved for r in rows)
        for source,target in [('daily','daily'),('daily','investment_summary'),('missing','daily')]:
            assert (await client.post('/api/account-transfers',json={'conversation_id':conv,'account_id':source,'target_account_id':target,'amount_yuan':'1'})).status_code==400


async def test_ai_cash_transfer_and_dag(agent):
    before={a['id']:a['balance_minor'] for a in agent.bank.snapshot()['accounts']}
    task=await ask(agent,[[tool('get_accounts')],[tool('transfer_own_funds',account_id='daily',target_account_id='savings',amount_yuan='100')],'已完成划转。'],'从日常账户转100元到储蓄账户')
    assert state(agent,task)['status']=='waiting_confirmation'
    assert {a['id']:a['balance_minor'] for a in agent.bank.snapshot()['accounts']}==before
    await confirm(agent)
    after={a['id']:a['balance_minor'] for a in agent.bank.snapshot()['accounts']}
    assert after['daily']==before['daily']-10000 and after['savings']==before['savings']+10000
    assert all(n['status']=='succeeded' for n in json.loads(state(agent,task)['state_json'])['dag']['nodes'])
