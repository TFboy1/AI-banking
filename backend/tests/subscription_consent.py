"""Explicit consent helpers for existing business tests; permission tests do not use these."""
import asyncio,json
from conftest import ask as submit
from banking.bank import validate

def subscription_facts(bank,name,args):
    # Unit tests of domain arithmetic, separate from HTTP/Agent permission tests.
    parsed=validate(name,args)
    with bank.database.connection() as db:return getattr(bank,'_'+name)(db,parsed)

async def consent_reads(agent,key):
    for _ in range(12):
        with agent.db.connection() as db:
            pending=db.execute("SELECT * FROM approvals WHERE task_id=? AND status='pending' ORDER BY rowid LIMIT 1",(key,)).fetchone()
        if not pending or pending['tool_name'] not in {'list_subscriptions','quote_subscriptions'}:return key
        await agent.confirm(pending['id'],'confirm');await asyncio.wait_for(agent.queue.join(),3)
    raise AssertionError('Too many repeated subscription queries')

async def ask(agent,responses,text='测试用户请求；付款账户选择日常账户'):
    return await consent_reads(agent,await submit(agent,responses,text))
