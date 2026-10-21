import asyncio
import json

import httpx
import pytest
from banking.handoff import requested
from banking.main import create_app
from conftest import ScriptedModel, ask, balance, pending, state, tool
from test_interruption import BlockingModel
from test_security import register
from banking import human_service as human


@pytest.mark.parametrize('text', ['转人工', '帮我转人工客服', '请转接人工', '我想找人工客服', '人工客服'])
def test_handoff_intent(text):
    assert requested(text)


def test_quoted_or_negated_handoff_is_not_an_instruction():
    assert not requested('不要转人工')
    assert not requested('联系人备注是转人工')
    assert not requested('有人说“转人工”是什么意思')


async def test_handoff_queues_real_service_without_model(agent):
    task = await ask(agent, [], '转人工')
    assert state(agent, task)['status'] == 'completed'
    assert agent.model.requests == []
    with agent.db.connection() as db:
        messages = db.execute("SELECT * FROM messages WHERE task_id=? AND role='assistant' ORDER BY rowid", (task,)).fetchall()
    assert len(messages)==1 and '等待接入' in messages[0]['text']
    assert '暂不支持人工' not in messages[0]['text']


async def test_handoff_interrupts_running_model_and_invalidates_pending_trade(agent):
    model = BlockingModel()
    agent.model = model
    running = await agent.submit('查询余额')
    await model.entered.wait()
    handoff = await agent.submit('转人工', conversation_id=state(agent, running)['conversation_id'])
    await asyncio.wait_for(agent.queue.join(), 2)
    assert model.cancelled.is_set() and state(agent, running)['status'] == 'failed'
    assert state(agent, handoff)['status'] == 'completed'
    with agent.db.transaction() as db:
        service=human.current(db,state(agent,running)['conversation_id'])
        human.close(db,service['id'],'用户')
    agent.model = ScriptedModel()
    task = await ask(agent, [[tool('transfer_own_funds', target_account_id='savings', amount_yuan='100')]], '从日常账户转100元到储蓄账户')
    approval = pending(agent)
    before = balance(agent)
    handoff = await ask(agent, [], '转人工')
    assert state(agent, task)['status'] == 'failed'
    assert (await agent.confirm(approval['id'], 'confirm'))['status'] == 'failed'
    assert balance(agent) == before
    with agent.db.connection() as db:
        assert db.execute("SELECT count(*) FROM messages WHERE task_id=? AND role='assistant'", (handoff,)).fetchone()[0] == 1
    with agent.db.transaction() as db:
        service=human.current(db,state(agent,task)['conversation_id'])
        human.close(db,service['id'],'用户')
    resumed = await ask(agent, [[tool('transfer_own_funds', target_account_id='savings', amount_yuan='100')]], '继续')
    assert state(agent, resumed)['status'] == 'waiting_confirmation'
    assert balance(agent) == before
    assert json.loads(state(agent, resumed)['state_json'])['recovery']['parent_task_id'] == task


async def test_half_star_feedback_is_authenticated_scoped_and_persisted(settings):
    settings.app_auth_enabled = True
    app = create_app(settings, ScriptedModel())
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
        assert (await client.post('/api/handoffs/missing/rating', json={'conversation_id': 'missing', 'half_stars': 7})).status_code == 401
        await register(client)
        conv = (await client.get('/api/bootstrap')).json()['conversation_id']
        async def handoff(conversation):
            response = await client.post('/api/messages', json={'conversation_id': conversation, 'text': '转人工'})
            assert response.status_code == 202
            await asyncio.wait_for(app.state.agent.queue.join(), 2)
            assert (await client.post('/api/handoff/end',json={'conversation_id':conversation})).status_code==200
            messages = (await client.get('/api/bootstrap', params={'conversation_id': conversation})).json()['messages']
            return next(m for m in reversed(messages) if m['data'].get('handoff_rating'))
        first = await handoff(conv)
        other = (await client.post('/api/conversations', json={'title': '另一个对话'})).json()['conversation_id']
        second = await handoff(other)
        path = f"/api/handoffs/{first['id']}/rating"
        assert (await client.post(path, json={'conversation_id': other, 'half_stars': 7})).status_code == 400
        for bad in (0, 11, 2.5, True):
            assert (await client.post(path, json={'conversation_id': conv, 'half_stars': bad})).status_code == 422
        for units in (1, 7, 10):
            response = await client.post(path, json={'conversation_id': conv, 'half_stars': units})
            assert response.status_code == 200
            assert response.json()['data']['handoff_rating']['half_stars'] == units
        messages = (await client.get('/api/bootstrap', params={'conversation_id': conv})).json()['messages']
        assert next(m for m in messages if m['id'] == first['id'])['data']['handoff_rating']['half_stars'] == 10
        messages = (await client.get('/api/bootstrap', params={'conversation_id': other})).json()['messages']
        assert next(m for m in messages if m['id'] == second['id'])['data']['handoff_rating']['half_stars'] is None
