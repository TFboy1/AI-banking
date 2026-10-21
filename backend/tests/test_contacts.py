import asyncio

import httpx
from banking.agent import model_safe_text
from banking.main import create_app
from conftest import ScriptedModel, tool


async def test_contact_directory_mask_reveal_notes_and_multiple_banks(settings):
    app = create_app(settings, ScriptedModel())
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        assert (await client.get('/api/contacts')).status_code == 401
        await client.get('/api/bootstrap')
        people = (await client.get('/api/contacts')).json()
        assert len(people) == 8
        chen = next(person for person in people if person['id'] == 'chen_yue')
        assert chen['name'] == '陈*' and chen['phone'] == '*******4444'
        assert len(chen['accounts']) == 2
        assert {a['card_number'][-4:] for a in chen['accounts']} == {'2468', '5810'}
        revealed = (await client.get('/api/contacts/chen_yue')).json()
        assert revealed['name'] == '陈悦' and revealed['phone'] == '13700004444'
        assert all(a['card_number'].isdigit() for a in revealed['accounts'])
        note = (await client.patch('/api/contacts/chen_yue/note', json={'note': ' 同事小陈 '})).json()
        assert note['note'] == '同事小陈' and note['name'] == '陈*'
        with app.state.database.connection() as db:
            assert db.execute("SELECT name FROM contacts WHERE id='chen_yue'").fetchone()[0] == '陈悦'
            assert model_safe_text(db, '给13700004444转100元') == '给手机尾号4444转100元'
        assert app.state.agent.bank.query('find_recipients', {'query': '同事小陈'})['needs_clarification']
        created = (await client.post('/api/contacts/chen_yue/conversation')).json()
        boot = (await client.get('/api/bootstrap', params=created)).json()
        assert '什么操作' in boot['messages'][0]['text']
        assert boot['messages'][0]['data']['source'] == 'contact_selected'
        assert '13700004444' not in boot['messages'][0]['text']
        assert not boot['active_task']
        app.state.database.initialize()
        assert (await client.get('/api/contacts/chen_yue')).json()['note'] == '同事小陈'


async def test_contact_creation_duplicate_identity_and_supplements(settings):
    app = create_app(settings, ScriptedModel())
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        await client.get('/api/bootstrap')
        body = {'name': '陈悦', 'phone': '13911112222', 'bank': '测试银行', 'card_number': '6222111122223333'}
        assert (await client.post('/api/contacts', json=body)).status_code == 400
        assert len((await client.post('/api/contacts/check-name', json={'name': '陈悦'})).json()) == 1
        merged = (await client.post('/api/contacts', json={**body, 'same_person_id': 'chen_yue'})).json()
        assert len(merged['accounts']) == 3 and len(merged['phones']) == 2
        assert len((await client.get('/api/contacts')).json()) == 8
        separate = (await client.post('/api/contacts', json={**body, 'card_number': '6222111122224444', 'new_person': True})).json()
        assert separate['id'] != 'chen_yue'
        assert len((await client.get('/api/contacts')).json()) == 9
        phone = await client.post('/api/contacts/chen_yue/details', json={'phone': '13922223333'})
        assert phone.status_code == 200 and len(phone.json()['phones']) == 3
        candidates = app.state.agent.bank.query('find_recipients', {'query': '手机尾号3333'})
        assert len(candidates['recipients']) >= 3 and candidates['needs_clarification']
        assert (await client.post('/api/contacts/chen_yue/details', json={'bank': '测试银行'})).status_code == 400
        assert (await client.post('/api/contacts/chen_yue/details', json={})).status_code == 400
        assert (await client.post('/api/contacts/chen_yue/details', json={'bank': '测试银行', 'card_number': body['card_number']})).status_code == 400
        assert (await client.patch('/api/contacts/chen_yue/note', json={'note': 'x'*121})).status_code == 422
        app.state.database.initialize()
        assert len((await client.get('/api/contacts')).json()) == 9
        assert len((await client.get('/api/contacts/chen_yue')).json()['phones']) == 3
        assert (await client.post('/api/sandbox/reset', json={'confirmation':'RESET'})).status_code == 200
        assert len((await client.get('/api/contacts')).json()) == 8


async def test_selected_person_with_two_cards_cannot_default_to_first(settings):
    app = create_app(settings, ScriptedModel([[tool("transfer", recipient="chen_yue", amount_yuan="200")]]))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        await client.get('/api/bootstrap')
        created = (await client.post('/api/contacts/chen_yue/conversation')).json()
        await client.post('/api/messages', json={'text': '给他转200元', **created})
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        boot = (await client.get('/api/bootstrap', params=created)).json()
        assert not boot['active_task']
        assert '指定收款银行' in boot['messages'][-1]['text']
        with app.state.database.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM approvals').fetchone()[0] == 0
        app.state.agent.model.responses = [[tool("transfer", recipient="chen_yue_2", amount_yuan="200")]]
        await client.post('/api/messages', json={'text': '从日常账户给这位的银行卡尾号5810转200元', **created})
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        boot = (await client.get('/api/bootstrap', params=created)).json()
        assert boot['active_task']['status'] == 'waiting_confirmation'
