"""用真实模型和临时银行数据库检查对话工具链，不执行资金变更。"""

import asyncio
import json
import tempfile
from pathlib import Path

from banking.agent import Agent
from banking.config import ROOT, Settings
from banking.db import Database, add_message


async def verify(settings):
    database = Database(settings.database_path)
    database.initialize()
    agent = Agent(database, settings)
    initial = agent.bank.snapshot()
    await agent.start()
    try:
        first = database.conversation_id()
        # 复现原会话包含连接失败和模型推诿回复的场景。
        with database.transaction() as db:
            add_message(
                db, first, None, "assistant", "error", "无法连接模型服务，请检查服务地址与网络连接。", {}
            )
            add_message(
                db, first, None, "assistant", "text", "当前无法查询您这个月的消费记录，请提供具体月份。", {}
            )

        async def ask(text, conversation_id, expected_tool):
            task_id = await agent.submit(text, conversation_id=conversation_id)
            # 阻塞等待任务结束，不轮询模型状态。
            await asyncio.wait_for(agent.queue.join(), timeout=180)
            with database.connection() as db:
                task = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                rows = db.execute("SELECT * FROM messages WHERE task_id=?", (task_id,)).fetchall()
                results = [json.loads(row["data_json"]) for row in rows if row["kind"] == "data"]
                approvals = [json.loads(row["data_json"]) for row in rows if row["kind"] == "approval"]
                assert task["status"] in ("completed", "waiting_confirmation"), task["error"]
                if expected_tool == "transfer":
                    assert approvals and task["status"] == "waiting_confirmation"
                else:
                    result = next((item["result"] for item in results if item["tool"] == expected_tool), None)
                    assert result, f"没有调用 {expected_tool}"
                    if expected_tool == "analyze_bills":
                        assert (
                            result["expense_minor"] == agent.bank.query("analyze_bills", {})["expense_minor"]
                        )
            print(f"通过：{text} → {expected_tool}（{task['status']}）", flush=True)

        await ask("这个月的钱花在哪了", first, "analyze_bills")
        with database.transaction() as db:
            second = database.create_conversation(db)
        await ask("查询我的账户余额", second, "get_accounts")
        await ask("从日常账户给张三转 200 元，备注晚餐 AA", second, "transfer")
        assert agent.bank.snapshot() == initial, "检查不得执行资金或业务状态变更"
        print("真实模型检查完成，临时银行数据未发生资金变更。", flush=True)
    finally:
        await agent.stop()


def main():
    settings = Settings(feishu_enabled=False)
    if not settings.model_ready:
        raise SystemExit("请先在 .env 配置真实模型。")
    data_root = (ROOT / "data").resolve()
    data_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="banking-model-check-", dir=data_root) as folder:
        temporary = Path(folder).resolve()
        assert temporary.parent == data_root
        settings.database_path = temporary / "check.sqlite3"
        asyncio.run(verify(settings))


if __name__ == "__main__":
    main()
