import asyncio

import pytest
import pytest_asyncio
from banking.agent import Agent
from banking.config import Settings
from banking.db import Database, dumps, uid

@pytest.fixture(autouse=True)
def stock_session_clock(monkeypatch):
    """Legacy business tests run during an explicit open session, not wall time.

    Session-gate tests override this clock to exercise closed boundaries.
    """
    from datetime import datetime
    from banking.db import TZ
    monkeypatch.setattr('banking.stock_trading.clock',lambda:datetime(2026,10,16,10,0,tzinfo=TZ))


def tool(tool_name, **arguments):
    return {"id": uid("call_"), "type": "function", "function": {"name": tool_name, "arguments": dumps(arguments)}}


class ScriptedModel:
    """只用于自动化测试的模型替身，产品代码不提供脚本伪装模式。"""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.requests = []

    async def complete(self, messages):
        self.requests.append(messages.copy())
        assert self.responses, "测试模型缺少预设响应"
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, str):
            return {"role": "assistant", "content": response}
        return {"role": "assistant", "content": "", "tool_calls": response}


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "bank.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None, app_auth_enabled=False, multi_user_accounts=False,daily_report_worker_enabled=False, birthday_worker_enabled=False, database_path=tmp_path / "api.sqlite3", llm_base_url="", llm_api_key="", llm_model=""
    )


@pytest_asyncio.fixture
async def agent(database, settings):
    agent = Agent(database, settings, ScriptedModel())
    await agent.start()
    try:
        yield agent
    finally:
        await agent.stop()


async def ask(agent, responses, text="测试用户请求；付款账户选择日常账户"):
    agent.model.responses = list(responses)
    task_id = await agent.submit(text)
    await asyncio.wait_for(agent.queue.join(), 3)
    return task_id


def pending(agent):
    with agent.db.connection() as db:
        row = db.execute(
            "SELECT * FROM approvals WHERE status='pending' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        assert row, "没有生成待确认操作"
        return dict(row)


def state(agent, task_id):
    with agent.db.connection() as db:
        return dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


async def confirm(agent, decision="confirm"):
    result = await agent.confirm(pending(agent)["id"], decision)
    await asyncio.wait_for(agent.queue.join(), 3)
    return result


def balance(agent, account="daily"):
    return next(a["balance_minor"] for a in agent.bank.snapshot()["accounts"] if a["id"] == account)
