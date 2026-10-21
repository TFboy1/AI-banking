import asyncio
import json

import pytest
from banking.cards import add_card_record
from banking.funding import chosen_source
from conftest import ask, pending, state, tool


ACCOUNTS = [
    {'id': 'daily', 'name': '日常账户'},
    {'id': 'travel', 'name': '旅行账户'},
    {'id': 'abc1', 'name': '中国农业银行 · 尾号1234'},
]


@pytest.mark.parametrize('text,expected', [
    ('从中国农业银行的卡里往旅行账户转1000吧', 'abc1'),
    ('从农业银行转1000到旅行账户', 'abc1'),
    ('用农行卡给张三转100元', 'abc1'),
    ('从日常账户转到中国农业银行', 'daily'),
    ('往中国农业银行转1000', None),
    ('查看中国农业银行余额', None),
    ('从中国农业银行尾号9999的卡往旅行账户转1000', None),
])
def test_explicit_bank_source(text, expected):
    assert chosen_source(text, ACCOUNTS) == expected


def test_multiple_cards_require_tail_and_destination_does_not_disambiguate():
    accounts = ACCOUNTS + [{'id': 'abc2', 'name': '中国农业银行 · 尾号5678'}]
    assert chosen_source('从中国农业银行的卡里往旅行账户转1000吧', accounts) is None
    assert chosen_source('从农行尾号5678的卡往旅行账户转1000', accounts) == 'abc2'
    assert chosen_source('从农行转1000给尾号5678的卡', accounts) is None
    assert chosen_source('农行', accounts, followup=True) is None
    assert chosen_source('农行', ACCOUNTS, followup=True) == 'abc1'


async def test_chat_bank_source_overrides_model_default_and_moves_only_selected_card(agent):
    with agent.db.transaction() as db:
        card = add_card_record(db, {'bank_name': '中国农业银行', 'card_number': '6222000000011234'})
        db.execute("INSERT INTO accounts VALUES ('travel','旅行账户','checking',0)")
    before = {a['id']: a['balance_minor'] for a in agent.bank.snapshot()['accounts']}
    task = await ask(agent, [[tool('transfer_own_funds', account_id='daily', target_account_id='travel', amount_yuan='1000')], '已完成划转。'],
                     '从中国农业银行的卡里往旅行账户转1000吧')
    assert state(agent, task)['status'] == 'waiting_confirmation'
    approval = pending(agent)
    assert json.loads(approval['args_json'])['account_id'] == card['account_id']
    assert json.loads(approval['summary_json'])['payment_account_id'] == card['account_id']
    assert {a['id']: a['balance_minor'] for a in agent.bank.snapshot()['accounts']} == before
    assert (await agent.confirm(approval['id'], 'confirm'))['status'] == 'executed'
    await agent.queue.join()
    after = {a['id']: a['balance_minor'] for a in agent.bank.snapshot()['accounts']}
    assert after[card['account_id']] == before[card['account_id']] - 100000
    assert after['travel'] == 100000
    assert after['daily'] == before['daily']
    assert sum(after.values()) == sum(before.values())


async def test_payment_dialog_receives_preselection_and_still_requires_pin(settings):
    import httpx
    from banking.main import create_app
    from conftest import ScriptedModel
    from test_security import register

    settings.app_auth_enabled = True
    model = ScriptedModel()
    app = create_app(settings, model)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
        await register(client)
        conv = (await client.get('/api/bootstrap')).json()['conversation_id']
        with app.state.database.transaction() as db:
            card = add_card_record(db, {'bank_name': '中国农业银行', 'card_number': '6222000000011234'})
            db.execute("INSERT INTO accounts VALUES ('travel','旅行账户','checking',0)")
        model.responses = [[tool('transfer_own_funds', account_id='daily', target_account_id='travel', amount_yuan='1000')]]
        await client.post('/api/messages', json={'conversation_id': conv, 'text': '从中国农业银行的卡里往旅行账户转1000吧'})
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        message = next(m for m in reversed((await client.get('/api/bootstrap')).json()['messages']) if m['kind'] == 'approval')
        approval = message['data']['approval_id']
        context = (await client.get(f'/api/approvals/{approval}/payment?conversation_id={conv}')).json()
        assert context['account_id'] == card['account_id']
        assert context['account_required'] and not context['sms_required']
        before = (await client.get('/api/bank')).json()
        response = await client.post(f'/api/approvals/{approval}/decision', json={'conversation_id': conv, 'decision': 'confirm'})
        assert response.status_code == 428
        assert (await client.get('/api/bank')).json() == before
