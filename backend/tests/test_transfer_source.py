import asyncio
import json

from conftest import ask, pending, state, tool


async def test_missing_source_creates_card_then_payment_selects_account(agent):
    import pytest
    from banking.bank import BankError

    before = agent.bank.snapshot()
    task = await ask(agent, [[tool('transfer', recipient='zhang_san', amount_yuan='200', account_id='daily', note='晚餐AA')]], '给张三转200元，备注晚餐AA')
    assert state(agent, task)['status'] == 'waiting_confirmation'
    approval = pending(agent)
    args = json.loads(approval['args_json'])
    assert args['account_id'] == '' and args['note'] == '晚餐AA'
    assert agent.bank.snapshot() == before
    with pytest.raises(BankError, match='选择付款账户'):
        await agent.confirm(approval['id'], 'confirm')
    agent.model.responses = ['转账已完成。']
    assert (await agent.confirm(approval['id'], 'confirm', account_id='savings'))['status'] == 'executed'
    await agent.queue.join()
    after = agent.bank.snapshot()
    amounts = {a['id']: a['balance_minor'] for a in before['accounts']}
    assert next(a['balance_minor'] for a in after['accounts'] if a['id'] == 'daily') == amounts['daily']
    assert next(a['balance_minor'] for a in after['accounts'] if a['id'] == 'savings') == amounts['savings'] - 20000
    assert (await agent.confirm(approval['id'], 'confirm', account_id='daily'))['status'] == 'executed'
    assert agent.bank.snapshot() == after


async def test_explicit_source_is_used_and_target_account_is_not_a_source(agent):
    task = await ask(agent, [[tool('transfer', recipient='zhang_san', amount_yuan='100', account_id='daily')]], '从储蓄账户给张三转100元')
    assert state(agent, task)['status'] == 'waiting_confirmation'
    assert json.loads(pending(agent)['args_json'])['account_id'] == 'savings'
    await agent.confirm(pending(agent)['id'], 'cancel')
    task = await ask(agent, [[tool('transfer', recipient='savings', amount_yuan='500')]], '转500元到储蓄账户')
    assert state(agent, task)['status'] == 'waiting_confirmation'
    assert json.loads(pending(agent)['args_json'])['account_id'] == ''


async def test_source_question_from_model_accepts_choice(agent):
    await ask(agent, ['请问从哪个账户转出？日常账户还是储蓄账户？'], '给张三转200元')
    task = await ask(agent, [[tool('transfer', recipient='zhang_san', amount_yuan='200')]], '储蓄账户')
    assert state(agent, task)['status'] == 'waiting_confirmation'
    assert json.loads(pending(agent)['args_json'])['account_id'] == 'savings'


async def test_source_draft_does_not_resume_after_unrelated_message(agent):
    await ask(agent, [[tool('transfer', recipient='zhang_san', amount_yuan='200')]], '给张三转200元')
    await ask(agent, ['好的，先不转。'], '算了不转了')
    task = await ask(agent, ['你想查询储蓄账户的什么信息？'], '储蓄账户')
    assert state(agent, task)['status'] == 'completed'
    with agent.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0] == 0


async def test_choice_does_not_leak_into_new_conversation(agent):
    await ask(agent, [[tool('transfer', recipient='zhang_san', amount_yuan='200')]], '给张三转200元')
    with agent.db.transaction() as db:
        other = agent.db.create_conversation(db)
    agent.model.responses = ['请提供收款人和金额。']
    task = await agent.submit('储蓄账户', conversation_id=other)
    await asyncio.wait_for(agent.queue.join(), 3)
    assert state(agent, task)['status'] == 'completed'
    with agent.db.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE t.conversation_id=?', (other,)).fetchone()[0] == 0
        assert json.loads(pending(agent)['args_json'])['account_id'] == ''
