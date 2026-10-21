import asyncio,json
import pytest
from banking.bank import BankError
from banking.partial_completion import choose,refresh
from conftest import ask,confirm,balance,pending,state,tool

def report(agent,task):
    with agent.db.connection() as db:
        row=db.execute("SELECT data_json FROM messages WHERE task_id=? AND json_extract(data_json,'$.tool')='partial_completion'",(task,)).fetchone()
        return json.loads(row[0])['result']['partial_completion'] if row else None

async def partial(agent):
    task=await ask(agent,[[tool('transfer_own_funds',account_id='daily',target_account_id='savings',amount_yuan='100'),tool('transfer_own_funds',account_id='daily',target_account_id='savings',amount_yuan='99999999')]],'先转100元到储蓄，再转99999999元')
    await agent.confirm(pending(agent)['id'],'confirm',account_id='daily')
    await asyncio.wait_for(agent.queue.join(),5)
    assert state(agent,task)['status']=='failed'
    return task,state(agent,task)['conversation_id']

async def test_partial_failure_shows_actual_ledger_and_unfinished(agent):
    before=balance(agent);task,conv=await partial(agent);value=report(agent,task)
    assert value['debit_minor']==value['credit_minor']==10000
    assert len(value['completed'])==1 and value['unfinished']
    assert not value['completed'][0]['can_cancel']
    assert balance(agent)==before-10000
    with agent.db.transaction() as db:
        refresh(agent,db,conv);refresh(agent,db,conv)
        assert db.execute("SELECT count(*) FROM messages WHERE task_id=? AND json_extract(data_json,'$.tool')='partial_completion'",(task,)).fetchone()[0]==1

async def test_keep_is_money_free_and_prevents_resume(agent):
    task,conv=await partial(agent);value=report(agent,task);before=agent.bank.snapshot()
    with agent.db.transaction() as db:choose(agent,db,task,conv,'keep',value['version'])
    assert report(agent,task)['decision']=='kept' and agent.bank.snapshot()==before
    next_task=await ask(agent,[],'继续')
    assert state(agent,next_task)['status']=='completed'
    assert agent.bank.snapshot()==before

async def test_replan_targets_selected_checkpoint_after_other_conversation_turn(agent):
    task,conv=await partial(agent)
    await ask(agent,[[tool('get_accounts')],'余额已查询'],'查余额')
    value=report(agent,task)
    with agent.db.transaction() as db:
        result,queued=choose(agent,db,task,conv,'replan',value['version'])
        saved=json.loads(db.execute('SELECT state_json FROM tasks WHERE id=?',(queued,)).fetchone()[0])
    assert saved['recovery']['parent_task_id']==task
    assert saved['executed_count']==1 and result['task_id']==queued
    assert report(agent,task)['decision']=='replanning'
    # No worker is queued by choose itself; endpoint dispatch occurs after commit.
    assert state(agent,queued)['status']=='queued'

async def test_stale_choice_cross_conversation_and_unrelated_order_rejected(agent):
    task,conv=await partial(agent);value=report(agent,task)
    with agent.db.transaction() as db:other=agent.db.create_conversation(db)
    for conversation,action,version,order in [(other,'keep',1,None),(conv,'keep',999,None),(conv,'cancel_order',value['version'],'foreign')]:
        with pytest.raises(BankError),agent.db.transaction() as db:choose(agent,db,task,conversation,action,version,order)
    assert report(agent,task)['decision']=='pending'

async def test_failure_without_any_success_does_not_offer_refund(agent):
    task=await ask(agent,[[tool('unknown_tool')]],'查询测试')
    assert state(agent,task)['status']=='failed' and report(agent,task) is None

async def test_concurrent_keep_rejects_second_and_keeps_balances(agent):
    task,conv=await partial(agent);value=report(agent,task);before=agent.bank.snapshot()
    with agent.db.transaction() as db:choose(agent,db,task,conv,'keep',value['version'])
    with pytest.raises(BankError),agent.db.transaction() as db:choose(agent,db,task,conv,'keep',value['version'])
    assert agent.bank.snapshot()==before

def test_replan_cannot_change_or_cancel_preserved_order(database):
    from banking.partial_completion import protect_preserved_orders
    with database.connection() as db:
        with pytest.raises(BankError,match='保持不变'):
            protect_preserved_orders(db,{'partial_preserve_orders':['own']},'cancel_travel_order',{'order_id':'own'})
        protect_preserved_orders(db,{'partial_preserve_orders':['own']},'cancel_travel_order',{'order_id':'different'})

async def test_partial_record_survives_restart(agent,settings):
    from banking.agent import Agent
    from banking.db import Database
    from conftest import ScriptedModel
    task,conv=await partial(agent);before=agent.bank.snapshot()
    await agent.stop()
    restarted=Agent(Database(agent.db.path),settings,ScriptedModel())
    await restarted.start()
    try:
        value=report(restarted,task)
        assert value['decision']=='pending' and value['debit_minor']==10000
        with restarted.db.transaction() as db:choose(restarted,db,task,conv,'keep',value['version'])
        assert restarted.bank.snapshot()==before
    finally:await restarted.stop()

async def test_read_consent_is_not_a_compensatable_mutation(agent):
    task=await ask(agent,[[tool('list_subscriptions'),tool('unknown_tool')]],'查看订阅')
    with agent.db.connection() as db:
        assert not db.execute("SELECT 1 FROM approvals WHERE task_id=? AND status='pending'",(task,)).fetchone()
    assert report(agent,task) is None

async def travel_partial(app,bank,conv,quote):
    agent=app.state.agent
    agent.model.responses=[[tool('place_travel_order',quote_id=quote['quote_id'])],[tool('unknown_tool')]]
    task=await agent.submit('请预订已经选好的方案，之后继续处理下一步',conversation_id=conv)
    await asyncio.wait_for(agent.queue.join(),5)
    approval=pending(agent);path='/api/approvals/'+approval['id']
    sms=(await bank.post(path+'/sms',json={'conversation_id':conv,'account_id':'daily'})).json()
    result=await bank.post(path+'/decision',json={'conversation_id':conv,'decision':'confirm','pin':'2468','account_id':'daily','sms_code':sms['demo_code']})
    assert result.status_code==200,result.text
    await asyncio.wait_for(agent.queue.join(),5)
    assert state(agent,task)['status']=='failed'
    return task,report(agent,task)

async def test_travel_refund_requires_confirmation_then_updates_original_card(settings,tmp_path):
    from test_travel import setup,client,connected,quote_for
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web);quote=await quote_for(app,conv)
        task,value=await travel_partial(app,bank,conv,quote);before=app.state.agent.bank.snapshot()
        item=value['completed'][0];assert item['can_cancel']
        path=f'/api/tasks/{task}/partial-resolution'
        response=await bank.post(path,json={'conversation_id':conv,'action':'cancel_order','version':value['version'],'order_id':item['order_id']})
        assert response.status_code==200,response.text
        assert app.state.agent.bank.snapshot()==before
        approval=response.json()['message']['data']['approval_id']
        context='/api/approvals/'+approval
        assert (await bank.post(context+'/decision',json={'conversation_id':conv,'decision':'confirm'})).status_code==428
        sms=(await bank.post(context+'/sms',json={'conversation_id':conv})).json()
        paid=await bank.post(context+'/decision',json={'conversation_id':conv,'decision':'confirm','pin':'2468','sms_code':sms['demo_code']})
        assert paid.status_code==200,paid.text
        assert paid.json()['receipt']['refund_minor']==quote['amount_minor']
        after=app.state.agent.bank.snapshot()
        assert (await bank.post(context+'/decision',json={'conversation_id':conv,'decision':'confirm','pin':'2468'})).status_code==200
        assert app.state.agent.bank.snapshot()==after
        updated=report(app.state.agent,task)
        assert updated['completed'][0]['order_status']=='cancelled'
        assert updated['completed'][0]['refunded_minor']==quote['amount_minor'] and not updated['can_replan']

async def test_declining_refund_restores_cancel_option(settings,tmp_path):
    from test_travel import setup,client,connected,quote_for
    app,site=setup(settings,tmp_path)
    async with app.router.lifespan_context(app),client(app,8000) as bank,client(site,8004) as web:
        conv=await connected(bank,web);quote=await quote_for(app,conv)
        task,value=await travel_partial(app,bank,conv,quote);item=value['completed'][0]
        request={'conversation_id':conv,'action':'cancel_order','version':value['version'],'order_id':item['order_id']}
        response=await bank.post(f'/api/tasks/{task}/partial-resolution',json=request)
        assert response.status_code==200,response.text
        assert (await bank.post(f'/api/tasks/{task}/partial-resolution',json=request)).status_code==400
        approval=response.json()['message']['data']['approval_id'];before=app.state.agent.bank.snapshot()
        assert (await bank.post('/api/approvals/'+approval+'/decision',json={'conversation_id':conv,'decision':'cancel'})).status_code==200
        assert report(app.state.agent,task)['completed'][0]['can_cancel']
        assert app.state.agent.bank.snapshot()==before
