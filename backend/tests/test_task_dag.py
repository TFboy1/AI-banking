import json

import httpx
import pytest
from banking import task_dag
from banking.main import create_app
from conftest import ScriptedModel, ask, confirm, state, tool


def test_fan_out_join_and_scheduler_enforces_dependencies():
    first, second, change = tool('get_accounts'), tool('list_cards'), tool('create_account_category',name='旅行')
    task = {'pending_calls':[change,first,second]}
    task_dag.add_calls(task, [first,second,change])
    nodes = task['dag']['nodes']
    assert nodes[0]['depends_on']==nodes[1]['depends_on']==[]
    assert set(nodes[2]['depends_on'])=={first['id'],second['id']}
    # Even if a caller rearranges the queue, a dependent change cannot run first.
    assert task_dag.select(task)==first
    task_dag.mark(task,'succeeded')
    assert task_dag.select(task)==second
    task_dag.mark(task,'succeeded')
    assert task_dag.select(task)==change


def test_cycle_missing_dependency_and_failure_blocking():
    a,b = tool('get_accounts'),tool('create_account_category',name='旅行')
    task={'pending_calls':[a,b]}
    task_dag.add_calls(task,task['pending_calls'])
    task['dag']['nodes'][0]['depends_on']=[b['id']]
    with pytest.raises(ValueError,match='循环'):
        task_dag.select(task)
    task['dag']['nodes'][0]['depends_on']=['missing']
    with pytest.raises(ValueError,match='不存在'):
        task_dag.select(task)
    task['dag']['nodes'][0]['depends_on']=[]
    task_dag.select(task)
    task_dag.close(task,'failed','查询失败')
    assert [n['status'] for n in task['dag']['nodes']]==['failed','blocked']
    with pytest.raises(ValueError,match='依赖'):
        task_dag.select(task)


async def test_dag_persist_confirmation_join_receipt_and_no_replay(agent):
    calls=[tool('get_accounts'),tool('list_cards'),tool('create_account_category',name='旅行预算'),tool('get_accounts')]
    identifier=await ask(agent,[calls,'已完成。'],'查询账户和卡片，创建旅行预算账户，再查询账户')
    saved=json.loads(state(agent,identifier)['state_json'])
    assert [n['status'] for n in saved['dag']['nodes']]==['succeeded','succeeded','waiting_confirmation','pending']
    assert not any(a['name']=='旅行预算' for a in agent.bank.snapshot()['accounts'])
    await confirm(agent)
    saved=json.loads(state(agent,identifier)['state_json'])
    assert all(n['status']=='succeeded' for n in saved['dag']['nodes'])
    assert saved['dag']['nodes'][2]['receipt_id']
    assert saved['dag']['nodes'][3]['depends_on']==[calls[2]['id']]
    assert len([a for a in agent.bank.snapshot()['accounts'] if a['name']=='旅行预算'])==1


async def test_cancellation_preserves_completed_queries(agent):
    identifier=await ask(agent,[[tool('get_accounts'),tool('create_account_category',name='取消测试'),tool('list_cards')]],'创建取消测试账户')
    await confirm(agent,'cancel')
    saved=json.loads(state(agent,identifier)['state_json'])
    assert [n['status'] for n in saved['dag']['nodes']]==['succeeded','cancelled','cancelled']


async def test_plan_api_is_authenticated_and_refreshable(settings):
    from test_security import register
    settings.app_auth_enabled=True
    app=create_app(settings,ScriptedModel([[tool('get_accounts'),tool('list_cards')],'已查询。']))
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        assert (await client.get('/api/tasks/missing/plan')).status_code==401
        await register(client)
        identifier=await app.state.agent.submit('查询账户和卡片')
        await app.state.agent.queue.join()
        plan=(await client.get(f'/api/tasks/{identifier}/plan')).json()
        assert len(plan['dag']['nodes'])==2 and all(n['status']=='succeeded' for n in plan['dag']['nodes'])
        bootstrap=(await client.get('/api/bootstrap')).json()
        assert bootstrap['latest_task_plan']==plan
        assert (await client.get('/api/tasks/missing/plan')).status_code==404
