import asyncio
import json

import pytest
from banking.agent import Agent
from banking.db import Database
from banking.llm import ModelError
from banking.recovery import check_fresh_quote, is_resume_request
from banking.bank import BankError
from conftest import ScriptedModel, ask, balance, confirm, pending, state, tool
from test_interruption import BlockingModel


@pytest.mark.parametrize('text', ['继续', '接着做吧', '继续刚才的操作', '请恢复上次的任务', '帮我继续执行之前的任务'])
def test_resume_phrases(text):
    assert is_resume_request(text)


def test_new_business_request_is_not_a_resume_command():
    assert not is_resume_request('继续给张三转100元')
    assert not is_resume_request('不要继续')
    with pytest.raises(BankError, match='重新查询并报价'):
        check_fresh_quote({'recovery': {'parent_task_id': 'old'}}, 'purchase_stock_basket', {'quote_id': 'old'})


async def test_resume_waiting_trade_requires_new_approval_and_only_executes_once(agent):
    first = await ask(agent, [[tool('transfer_own_funds', account_id='daily', target_account_id='savings', amount_yuan='100')]], '从日常账户转100元到储蓄账户')
    old = pending(agent)
    before = balance(agent)
    await agent.interrupt(first, state(agent, first)['conversation_id'])
    resumed = await ask(agent, [[tool('get_accounts')], [tool('transfer_own_funds', target_account_id='savings', amount_yuan='100')], '已完成划转。'], '继续')
    assert resumed != first and state(agent, resumed)['status'] == 'waiting_confirmation'
    new = pending(agent)
    assert new['id'] != old['id'] and balance(agent) == before
    assert (await agent.confirm(old['id'], 'confirm'))['status'] == 'failed'
    saved = json.loads(state(agent, resumed)['state_json'])
    assert saved['recovery']['parent_task_id'] == first
    assert saved['request_text'] == '从日常账户转100元到储蓄账户'
    with agent.db.connection() as db:
        card = json.loads(db.execute("SELECT data_json FROM messages WHERE task_id=? AND json_extract(data_json,'$.tool')='resume_task'", (resumed,)).fetchone()[0])
        assert set(card['result']['display']) >= {'title', 'columns', 'rows', 'metrics', 'note'}
    await confirm(agent)
    assert balance(agent) == before - 10000
    completed = await ask(agent, [], '继续')
    assert state(agent, completed)['status'] == 'completed'
    assert balance(agent) == before - 10000


async def test_partial_execution_replans_remaining_work_without_duplicate_money(agent):
    first_call = tool('transfer_own_funds', account_id='daily', target_account_id='savings', amount_yuan='100')
    second_call = tool('transfer_own_funds', account_id='daily', target_account_id='savings', amount_yuan='200')
    first = await ask(agent, [[first_call, second_call]], '从日常账户先转100元到储蓄账户，再转200元')
    before = balance(agent)
    await confirm(agent)
    old = pending(agent)
    await agent.interrupt(first, state(agent, first)['conversation_id'])
    # Deliberately make the model request the completed operation with a new
    # source and a differently formatted amount; the backend must reuse receipt.
    resumed = await ask(agent, [[tool('get_accounts')], [tool('transfer_own_funds', account_id='savings', target_account_id='savings', amount_yuan='100.00'),
                                tool('transfer_own_funds', account_id='daily', target_account_id='savings', amount_yuan='200')], '已完成剩余划转。'], '接着做')
    assert state(agent, resumed)['status'] == 'waiting_confirmation'
    assert balance(agent) == before - 10000
    assert json.loads(pending(agent)['args_json'])['amount_yuan'] == '200'
    saved = json.loads(state(agent, resumed)['state_json'])
    assert saved['dag']['nodes'][0]['status'] == 'succeeded'
    assert any('already_completed' in m.get('content', '') for m in saved['messages'])
    await confirm(agent)
    assert balance(agent) == before - 30000
    assert (await agent.confirm(old['id'], 'confirm'))['status'] == 'failed'
    with agent.db.connection() as db:
        assert db.execute("SELECT count(*) FROM approvals WHERE status='executed'").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM transactions WHERE category='内部转账'").fetchone()[0] == 4
    report = (await agent.interrupt(resumed, state(agent, resumed)['conversation_id']))['interruption']
    assert report['debit_minor'] == report['credit_minor'] == 30000
    assert len(report['completed_operations']) == 2


async def test_restart_preserves_disk_checkpoint_and_waits_for_user(agent, settings):
    model = BlockingModel()
    agent.model = model
    first = await agent.submit('查询我的账户余额')
    await model.entered.wait()
    await agent.stop()
    disk = Database(agent.db.path)
    disk.initialize()
    resumed_agent = Agent(disk, settings, ScriptedModel())
    await resumed_agent.start()
    try:
        assert state(resumed_agent, first)['status'] == 'failed'
        assert resumed_agent.model.requests == []
        assert json.loads(state(resumed_agent, first)['state_json'])['restart_checkpoint']
        task = await ask(resumed_agent, [[tool('get_accounts')], '已查询最新余额。'], '恢复上次任务')
        assert state(resumed_agent, task)['status'] == 'completed'
        assert json.loads(state(resumed_agent, task)['state_json'])['recovery']['parent_task_id'] == first
    finally:
        await resumed_agent.stop()


async def test_crash_checkpoint_does_not_autorun_queued_or_waiting_payments(agent):
    task = await ask(agent, [[tool('transfer_own_funds', target_account_id='savings', amount_yuan='100')]], '从日常账户转100元到储蓄账户')
    old = pending(agent)
    before = balance(agent)
    await agent.stop()
    assert (await agent.confirm(old['id'], 'confirm'))['status'] == 'failed'
    assert balance(agent) == before
    await agent.start()
    assert state(agent, task)['status'] == 'failed' and balance(agent) == before


async def test_restart_after_money_commit_reuses_saved_receipt(agent, settings):
    model = BlockingModel(first=[tool('transfer_own_funds', target_account_id='savings', amount_yuan='100')])
    agent.model = model
    before = balance(agent)
    first = await agent.submit('从日常账户转100元到储蓄账户，然后查询银行卡')
    await asyncio.wait_for(agent.queue.join(), 2)
    await agent.confirm(pending(agent)['id'], 'confirm')
    await asyncio.wait_for(model.entered.wait(), 1)
    await agent.stop()
    disk = Database(agent.db.path)
    disk.initialize()
    fresh = Agent(disk, settings, ScriptedModel())
    await fresh.start()
    try:
        assert balance(fresh) == before - 10000
        resumed = await ask(fresh, [[tool('transfer_own_funds', target_account_id='savings', amount_yuan='100'), tool('list_cards')], '转账已经完成，银行卡已查询。'], '继续')
        assert state(fresh, resumed)['status'] == 'completed'
        assert balance(fresh) == before - 10000
        with disk.connection() as db:
            assert db.execute("SELECT count(*) FROM approvals WHERE status='executed'").fetchone()[0] == 1
        assert json.loads(state(fresh, resumed)['state_json'])['recovery']['parent_task_id'] == first
    finally:
        await fresh.stop()


async def test_two_explicit_identical_steps_keep_remaining_count_on_resume(agent):
    calls = [tool('transfer_own_funds', target_account_id='savings', amount_yuan='100') for _ in range(2)]
    first = await ask(agent, [calls], '从日常账户分两笔分别转100元到储蓄账户')
    before = balance(agent)
    await confirm(agent)
    await agent.interrupt(first, state(agent, first)['conversation_id'])
    repeats = [tool('transfer_own_funds', target_account_id='savings', amount_yuan='100') for _ in range(2)]
    resumed = await ask(agent, [repeats, '两笔划转已经完成。'], '继续')
    assert state(agent, resumed)['status'] == 'waiting_confirmation', json.loads(state(agent, resumed)['state_json']).get('recovery_allowances')
    await confirm(agent)
    assert state(agent, resumed)['status'] == 'completed'
    assert balance(agent) == before - 20000
    with agent.db.connection() as db:
        assert db.execute("SELECT count(*) FROM approvals WHERE status='executed'").fetchone()[0] == 2


async def test_model_failure_can_resume_and_other_conversation_cannot(agent):
    first = await ask(agent, [ModelError('测试模型连接失败')], '查询银行卡')
    assert state(agent, first)['status'] == 'failed'
    with agent.db.transaction() as db:
        other = agent.db.create_conversation(db)
    agent.model.responses = []
    task = await agent.submit('继续', conversation_id=other)
    await asyncio.wait_for(agent.queue.join(), 1)
    assert state(agent, task)['status'] == 'completed'
    assert 'recovery' not in json.loads(state(agent, task)['state_json'])
    # Return explicitly to the original conversation.
    agent.model.responses = [[tool('list_cards')], '已查询。']
    resumed = await agent.submit('继续', conversation_id=state(agent, first)['conversation_id'])
    await asyncio.wait_for(agent.queue.join(), 2)
    assert state(agent, resumed)['status'] == 'completed'
    assert json.loads(state(agent, resumed)['state_json'])['recovery']['parent_task_id'] == first


async def test_queued_task_is_saved_at_shutdown_not_automatically_replayed(agent, settings):
    model = BlockingModel()
    agent.model = model
    first = await agent.submit('查询账户')
    await model.entered.wait()
    with agent.db.transaction() as db:
        other = agent.db.create_conversation(db)
    second = await agent.submit('查询银行卡', conversation_id=other)
    assert state(agent, second)['status'] == 'queued'
    await agent.stop()
    disk = Database(agent.db.path)
    disk.initialize()
    fresh = Agent(disk, settings, ScriptedModel())
    await fresh.start()
    try:
        assert state(fresh, first)['status'] == state(fresh, second)['status'] == 'failed'
        assert fresh.model.requests == []
        fresh.model.responses = [[tool('list_cards')], '已查询银行卡。']
        resumed = await fresh.submit('继续', conversation_id=other)
        await asyncio.wait_for(fresh.queue.join(), 2)
        assert state(fresh, resumed)['status'] == 'completed'
    finally:
        await fresh.stop()


async def test_resume_red_payment_requires_new_pin_and_new_sms(settings):
    import httpx
    from banking.main import create_app
    from test_security import register

    settings.app_auth_enabled = True
    model = ScriptedModel()
    app = create_app(settings, model)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
        await register(client)
        conv = (await client.get('/api/bootstrap')).json()['conversation_id']
        async def start(text):
            model.responses = [[tool('get_accounts')], [tool('transfer_own_funds', target_account_id='savings', amount_yuan='1001')], '已完成。']
            response = (await client.post('/api/messages', json={'conversation_id': conv, 'text': text})).json()
            await asyncio.wait_for(app.state.agent.queue.join(), 3)
            return response['task_id'], pending(app.state.agent)['id']
        old_task, old = await start('从日常账户转1001元到储蓄账户')
        sms = (await client.post(f'/api/approvals/{old}/sms', json={'conversation_id': conv, 'account_id': 'daily'})).json()['demo_code']
        await client.post(f'/api/tasks/{old_task}/interrupt', json={'conversation_id': conv})
        task, new = await start('继续')
        before = balance(app.state.agent)
        path = f'/api/approvals/{new}/decision'
        body = {'conversation_id': conv, 'decision': 'confirm', 'account_id': 'daily'}
        assert (await client.post(path, json=body)).status_code == 428
        assert (await client.post(path, json={**body, 'pin': '2468', 'sms_code': sms})).status_code == 400
        assert balance(app.state.agent) == before
        new_sms = (await client.post(f'/api/approvals/{new}/sms', json={'conversation_id': conv, 'account_id': 'daily'})).json()['demo_code']
        assert (await client.post(path, json={**body, 'pin': '2468', 'sms_code': new_sms})).json()['status'] == 'executed'
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        assert balance(app.state.agent) == before - 100100
        assert state(app.state.agent, task)['status'] == 'completed'


async def test_resumed_stock_purchase_requotes_expired_price_before_confirmation(agent):
    import time
    from banking.db import dumps
    from banking.securities_common import owner
    with agent.db.transaction() as db:
        conv = agent.db.conversation_id(db)
        investor = owner(db)
        db.execute("INSERT OR REPLACE INTO risk_profile VALUES (1,'{}',4)")
        def quote(identifier, price):
            return {'quote_id': identifier, 'owner': investor, 'side': 'buy', 'amount_minor': price * 100,
                    'items': [{'symbol': '000001', 'name': '平安银行', 'shares': 100, 'price_minor': price,
                               'amount_minor': price * 100, 'quote_date': '2026-10-16'}], 'pricing_mode': '隔离测试报价'}
        old_quote = quote('old_quote', 1150)
        db.execute('INSERT INTO securities_quotes VALUES (?,?,?,?)', ('old_quote', conv, dumps(old_quote), time.time() + 300))
    first = await ask(agent, [[tool('purchase_stock_basket', quote_id='old_quote')]], '从日常账户买100股平安银行股票')
    await agent.interrupt(first, conv)
    with agent.db.transaction() as db:
        db.execute("UPDATE securities_quotes SET expires=0 WHERE id='old_quote'")
    before = balance(agent)
    stale = await ask(agent, [[tool('purchase_stock_basket', quote_id='old_quote')]], '继续')
    assert state(agent, stale)['status'] == 'failed'
    assert '重新查询并报价' in state(agent, stale)['error']
    assert balance(agent) == before
    async def fresh_quote(name, args, conversation):
        assert name == 'quote_stock_purchase' and args['orders'][0]['shares'] == 100
        value = quote('fresh_quote', 1200)
        with agent.db.transaction() as db:
            db.execute('INSERT INTO securities_quotes VALUES (?,?,?,?)', ('fresh_quote', conversation, dumps(value), time.time() + 300))
        return {**value, 'display': {'title': '最新股票报价', 'columns': [], 'rows': []}}
    agent.securities.query = fresh_quote
    resumed = await ask(agent, [[tool('quote_stock_purchase', orders=[{'symbol': '000001', 'shares': 100}])],
                                [tool('purchase_stock_basket', quote_id='fresh_quote')], '已完成买入。'], '继续')
    assert state(agent, resumed)['status'] == 'waiting_confirmation'
    approval = pending(agent)
    assert json.loads(approval['summary_json'])['amount_minor'] == 120000
    assert balance(agent) == before
    await confirm(agent)
    assert balance(agent) == before - 120000
    assert agent.bank.snapshot()['securities_positions'][0]['shares'] == 100
