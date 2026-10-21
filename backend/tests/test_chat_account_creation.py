import asyncio
import json

import httpx
import pytest
from banking.bank import BankError
from banking.main import create_app
from conftest import ScriptedModel, ask, confirm, pending, state, tool
from test_security import register


async def test_chat_create_persists_zero_balance_emits_update_and_is_idempotent(agent):
    before=agent.bank.snapshot()
    task=await ask(agent,[[tool('get_accounts')],[tool('create_account_category',name='旅行预算')],'旅行预算账户已创建。'],'帮我创建一个叫旅行预算的账户')
    assert state(agent,task)['status']=='waiting_confirmation'
    assert not any(a['name']=='旅行预算' for a in agent.bank.snapshot()['accounts'])
    approval=pending(agent)
    result=await agent.confirm(approval['id'],'confirm')
    await asyncio.wait_for(agent.queue.join(),3)
    assert result['status']=='executed'
    snapshot=agent.bank.snapshot()
    account=next(a for a in snapshot['accounts'] if a['name']=='旅行预算')
    assert account['balance_minor']==0 and account['kind']=='category'
    assert snapshot['total_assets_minor']==before['total_assets_minor']
    assert (await agent.confirm(approval['id'],'confirm'))['status']=='executed'
    with agent.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts WHERE name='旅行预算'").fetchone()[0]==1
        receipt=json.loads(db.execute('SELECT receipt_json FROM approvals WHERE id=?',(approval['id'],)).fetchone()[0])
        assert receipt['account']['id']==account['id']
        events=[json.loads(r[0]) for r in db.execute("SELECT data_json FROM events WHERE type='bank.updated'")]
        assert any(e.get('operation')=='account.created' for e in events)


@pytest.mark.parametrize('reply',['已为你创建旅行账户。','旅行账户创建好了。','已创建账户：储蓄计划。'])
async def test_model_claim_without_creation_receipt_is_not_success(agent,reply):
    task=await ask(agent,[reply],'帮我新建一个旅行账户')
    with agent.db.connection() as db:
        text=db.execute('SELECT text FROM messages WHERE task_id=? ORDER BY rowid DESC LIMIT 1',(task,)).fetchone()[0]
        assert '尚未创建' in text and '成功回执' in text
        assert not db.execute("SELECT 1 FROM accounts WHERE name='旅行'").fetchone()


async def test_cancel_duplicate_reserved_names_and_execution_recheck(agent):
    await ask(agent,[[tool('create_account_category',name='预算账户')]],'新建预算账户')
    assert (await confirm(agent,'cancel'))['status']=='cancelled'
    assert not any(a['name']=='预算账户' for a in agent.bank.snapshot()['accounts'])
    with pytest.raises(BankError,match='同名'):
        agent.bank.prepare('create_account_category',{'name':'日常账户'})
    with pytest.raises(BankError,match='系统持仓汇总'):
        agent.bank.prepare('create_account_category',{'name':'投资账户'})
    await ask(agent,[[tool('create_account_category',name='预算账户')]],'新建预算账户')
    with agent.db.transaction() as db:
        db.execute("INSERT INTO accounts VALUES ('race','预算账户','category',0)")
    assert (await confirm(agent))['status']=='failed'
    with agent.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM accounts WHERE name='预算账户'").fetchone()[0]==1


async def test_created_account_visible_in_bank_api_after_chat_confirmation(settings):
    settings.app_auth_enabled=True
    model=ScriptedModel([[tool('create_account_category',name='学习基金')],'学习基金账户已创建。'])
    app=create_app(settings,model)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as c:
        await register(c)
        conv=(await c.get('/api/bootstrap')).json()['conversation_id']
        await c.post('/api/messages',json={'conversation_id':conv,'text':'帮我创建学习基金账户'})
        await asyncio.wait_for(app.state.agent.queue.join(),3)
        approval=pending(app.state.agent)
        result=await c.post(f"/api/approvals/{approval['id']}/decision",json={'conversation_id':conv,'decision':'confirm'})
        assert result.json()['status']=='executed'
        await asyncio.wait_for(app.state.agent.queue.join(),3)
        accounts=(await c.get('/api/bank')).json()['accounts']
        assert next(a for a in accounts if a['name']=='学习基金')['balance_minor']==0
