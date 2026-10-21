import asyncio
import json

import httpx
import pytest
from banking.main import create_app
from conftest import ScriptedModel, ask, balance, pending, state, tool
from test_security import register


class BlockingModel:
    def __init__(self, first=None, late=None):
        self.first=first
        self.late=late
        self.entered=asyncio.Event()
        self.cancelled=asyncio.Event()
        self.calls=0

    async def complete(self,messages):
        self.calls+=1
        if self.first and self.calls==1:
            return {'role':'assistant','content':'','tool_calls':self.first}
        self.entered.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            if self.late:
                return {'role':'assistant','content':'','tool_calls':self.late}
            raise


async def test_legacy_schedule_receipt_without_title_or_action_id_can_be_stopped(agent):
    from banking.db import dumps
    task=await ask(agent,[[tool('transfer',recipient='zhang_san',amount_yuan='100')]],'给张三转100元，从日常账户出')
    approval=pending(agent)
    before=agent.bank.snapshot()
    with agent.db.transaction() as db:
        db.execute("UPDATE approvals SET status='executed',receipt_json=? WHERE id=?",(dumps({'schedule':{'id':'legacy_schedule'}}),approval['id']))
    result=await agent.interrupt(task,state(agent,task)['conversation_id'])
    report=result['interruption']
    assert report['debit_minor']==report['credit_minor']==0
    assert report['completed_operations'][0]['title']
    assert '尚未执行交易' in report['completed_operations'][0]['settlement']
    assert result['message']['data']['result']['display']['rows'][0][2]=='—'
    assert agent.bank.snapshot()==before


async def test_stop_model_no_money_and_worker_accepts_next_task(agent):
    model=BlockingModel();agent.model=model
    before=agent.bank.snapshot()
    task=await agent.submit('查询账户')
    await asyncio.wait_for(model.entered.wait(),1)
    result=await agent.interrupt(task,state(agent,task)['conversation_id'])
    await asyncio.wait_for(agent.queue.join(),1)
    assert result['task']['status']=='failed' and result['interruption']['stage']=='正在理解需求或规划下一步'
    assert result['interruption']['debit_minor']==result['interruption']['credit_minor']==0
    assert result['interruption']['completed_operations']==[]
    assert agent.bank.snapshot()==before and model.cancelled.is_set()
    assert not agent.worker_task.done()
    agent.model=ScriptedModel([[tool('get_accounts')],'已查询。'])
    next_task=await agent.submit('查询账户')
    await asyncio.wait_for(agent.queue.join(),1)
    assert state(agent,next_task)['status']=='completed'


async def test_stop_queued_task_does_not_start_it(agent):
    model=BlockingModel();agent.model=model
    first=await agent.submit('第一个查询')
    await model.entered.wait()
    with agent.db.transaction() as db:
        conversation=agent.db.create_conversation(db,'另一个会话')
    second=await agent.submit('另一个查询',conversation_id=conversation)
    result=await agent.interrupt(second,conversation)
    assert result['interruption']['stage']=='等待开始'
    await agent.interrupt(first,state(agent,first)['conversation_id'])
    await asyncio.wait_for(agent.queue.join(),1)
    assert model.calls==1 and state(agent,second)['status']=='failed'


async def test_stop_before_confirmation_cancels_payment_and_dependents(agent):
    task=await ask(agent,[[tool('get_accounts'),tool('transfer_own_funds',account_id='daily',target_account_id='savings',amount_yuan='100'),tool('list_cards')]],'从日常账户转100元到储蓄账户')
    approval=pending(agent)
    before=agent.bank.snapshot()
    result=await agent.interrupt(task,state(agent,task)['conversation_id'])
    assert '等待你确认' in result['interruption']['stage']
    assert (await agent.confirm(approval['id'],'confirm'))['status']=='failed'
    assert agent.bank.snapshot()==before
    nodes=json.loads(state(agent,task)['state_json'])['dag']['nodes']
    assert [n['status'] for n in nodes]==['succeeded','failed','cancelled']
    assert not result['interruption']['completed_operations']
    assert '未完成的交易按失败处理' in result['message']['data']['result']['display']['note']
    assert '未执行扣款或入账' in result['task']['error']


async def test_stop_after_committed_transfer_reports_both_sides_no_refund(agent):
    model=BlockingModel(first=[tool('transfer_own_funds',account_id='daily',target_account_id='savings',amount_yuan='100')]);agent.model=model
    daily,savings=balance(agent),balance(agent,'savings')
    task=await agent.submit('从日常账户转100元到储蓄账户')
    await asyncio.wait_for(agent.queue.join(),1)
    approval=pending(agent)
    receipt=(await agent.confirm(approval['id'],'confirm'))['receipt']
    await asyncio.wait_for(model.entered.wait(),1)
    result=await agent.interrupt(task,state(agent,task)['conversation_id'])
    await asyncio.wait_for(agent.queue.join(),1)
    report=result['interruption']
    assert report['debit_minor']==report['credit_minor']==10000
    operation=report['completed_operations'][0]
    assert operation['receipt']['action_id']==receipt['action_id'] and len(operation['ledger'])==2
    assert '储蓄账户已到账' in operation['settlement']
    assert balance(agent)==daily-10000 and balance(agent,'savings')==savings+10000
    assert result['task']['dag']['nodes'][0]['status']=='succeeded'
    assert (await agent.confirm(approval['id'],'confirm'))['status']=='executed'
    assert balance(agent)==daily-10000


async def test_late_model_reply_cannot_restart_cancelled_task(agent):
    model=BlockingModel(late=[tool('transfer',recipient='zhang_san',amount_yuan='100')]);agent.model=model
    task=await agent.submit('转账100元')
    await model.entered.wait()
    await agent.interrupt(task,state(agent,task)['conversation_id'])
    await asyncio.wait_for(agent.queue.join(),1)
    assert state(agent,task)['status']=='failed'
    with agent.db.connection() as db:
        assert db.execute('SELECT count(*) FROM approvals WHERE task_id=?',(task,)).fetchone()[0]==0


async def test_external_auto_result_is_uncertain_not_claimed_success(agent):
    entered=asyncio.Event()
    async def query(*args):
        entered.set();await asyncio.Future()
    agent.securities.query=query
    agent.model=ScriptedModel([[tool('add_securities_watchlist',symbol='000001')]])
    task=await agent.submit('帮我把平安银行加入自选')
    await entered.wait()
    result=await agent.interrupt(task,state(agent,task)['conversation_id'])
    await asyncio.wait_for(agent.queue.join(),1)
    assert result['interruption']['external_result_uncertain']
    assert result['task']['dag']['nodes'][0]['status']=='uncertain'
    assert result['interruption']['debit_minor']==0


async def test_completed_task_and_repeated_stop_preserve_result_once(agent):
    task=await ask(agent,[[tool('get_accounts')],'已查询。'],'查询账户')
    conv=state(agent,task)['conversation_id']
    first=await agent.interrupt(task,conv)
    second=await agent.interrupt(task,conv)
    assert first==second and first['task']['status']=='completed' and not first['interruption']['interrupted']
    with agent.db.connection() as db:
        assert db.execute("SELECT count(*) FROM messages WHERE task_id=? AND json_extract(data_json,'$.tool')='interrupt_task'",(task,)).fetchone()[0]==1


async def test_stop_api_requires_login_and_correct_conversation(settings):
    settings.app_auth_enabled=True
    model=BlockingModel();app=create_app(settings,model)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        assert (await c.post('/api/tasks/missing/interrupt',json={'conversation_id':'missing'})).status_code==401
        await register(c)
        conv=(await c.get('/api/bootstrap')).json()['conversation_id']
        task=(await c.post('/api/messages',json={'conversation_id':conv,'text':'查询余额'})).json()['task_id']
        await model.entered.wait()
        other=(await c.post('/api/conversations',json={'title':'其他会话'})).json()['conversation_id']
        assert (await c.post(f'/api/tasks/{task}/interrupt',json={'conversation_id':other})).status_code==400
        result=await c.post(f'/api/tasks/{task}/interrupt',json={'conversation_id':conv})
        assert result.json()['task']['status']=='failed'
        await asyncio.wait_for(app.state.agent.queue.join(),1)
        assert (await c.get(f'/api/tasks/{task}/plan')).json()['interruption']==result.json()['interruption']


@pytest.mark.parametrize('side', ['buy', 'sell'])
async def test_stock_settlement_report_uses_actual_stock_ledger(agent, side):
    import time
    from banking.db import dumps
    from banking.securities_common import owner

    with agent.db.transaction() as db:
        conv = agent.db.create_conversation(db)
        item = {'symbol': '000001', 'name': '平安银行', 'shares': 100,
                'price_minor': 1150, 'amount_minor': 115000, 'quote_date': '2026-10-16'}
        quote = {'quote_id': 'report_quote', 'side': side, 'owner': owner(db),
                 'items': [item], 'amount_minor': 115000, 'pricing_mode': '隔离测试报价'}
        db.execute('INSERT INTO securities_quotes VALUES (?,?,?,?)', ('report_quote', conv, dumps(quote), time.time() + 300))
        db.execute("INSERT OR REPLACE INTO risk_profile VALUES (1,'{}',4)")
        if side == 'sell':
            db.execute("INSERT INTO securities_positions VALUES ('000001','平安银行',100,100000)")
        message = agent.create_direct_approval(db, conv, 'purchase_stock_basket' if side == 'buy' else 'sell_stock_basket',
                                               {'quote_id': 'report_quote', 'account_id': 'daily'})
    approval_id = message['data']['approval_id']
    before = balance(agent)
    assert (await agent.confirm(approval_id, 'confirm'))['status'] == 'executed'
    # An unrelated lookalike operation must never be included in this report.
    with agent.db.transaction() as db:
        task = db.execute('SELECT task_id FROM approvals WHERE id=?', (approval_id,)).fetchone()[0]
        agent.bank._ledger(db, approval_id + 'other:stocks', 'daily', 'unrelated', 'out', 999999, '证券投资', '隔离测试')
    result = await agent.interrupt(task, conv)
    report = result['interruption']
    assert report['debit_minor'] == (115000 if side == 'buy' else 0)
    assert report['credit_minor'] == (115000 if side == 'sell' else 0)
    assert balance(agent) == before + (-115000 if side == 'buy' else 115000)
    assert len(report['completed_operations'][0]['ledger']) == 1
    assert '无转账扣款或入账流水' not in report['completed_operations'][0]['settlement']
    assert (await agent.interrupt(task, conv)) == result
    # Old reports were persisted with zero amounts: repair the saved display
    # and task together, preserving the original interruption time and funds.
    from banking.interruption import repair_saved_reports
    with agent.db.transaction() as db:
        saved = json.loads(db.execute('SELECT state_json FROM tasks WHERE id=?', (task,)).fetchone()[0])
        saved['interruption'].pop('ledger_version')
        saved['interruption'].update(debit_minor=0, credit_minor=0)
        db.execute('UPDATE tasks SET state_json=? WHERE id=?', (dumps(saved), task))
        repair_saved_reports(db, 'other-conversation')
        assert 'ledger_version' not in json.loads(db.execute('SELECT state_json FROM tasks WHERE id=?', (task,)).fetchone()[0])['interruption']
        repair_saved_reports(db, conv)
        repaired = json.loads(db.execute('SELECT state_json FROM tasks WHERE id=?', (task,)).fetchone()[0])['interruption']
        assert repaired == report
        message = json.loads(db.execute("SELECT data_json FROM messages WHERE task_id=? AND json_extract(data_json,'$.tool')='interrupt_task'", (task,)).fetchone()[0])
        assert message['result']['interruption'] == report
        assert any(m['value'] == '¥1,150.00' for m in message['result']['display']['metrics'])
        assert db.execute("SELECT count(*) FROM messages WHERE task_id=? AND json_extract(data_json,'$.tool')='interrupt_task'", (task,)).fetchone()[0] == 1
    assert balance(agent) == before + (-115000 if side == 'buy' else 115000)
