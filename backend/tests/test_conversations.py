import asyncio
from contextlib import asynccontextmanager

import httpx
from banking.bank import Bank
from banking.db import Database, add_message
from banking.main import create_app
from conftest import ScriptedModel, tool


@asynccontextmanager
async def running_client(settings, model=None):
    app = create_app(settings, model or ScriptedModel())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client,
    ):
        yield app, client


async def test_conversations_keep_independent_context_and_persist_selection(settings):
    model = ScriptedModel(["记住了。", "这是新的对话。"])
    async with running_client(settings, model) as (app, client):
        initial = (await client.get("/api/bootstrap")).json()
        first = initial["conversation_id"]
        await client.post("/api/messages", json={"text": "我的暗号是青松七号", "conversation_id": first})
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        second = (await client.post("/api/conversations", json={})).json()["conversation_id"]
        assert second != first
        fresh = (await client.get("/api/bootstrap")).json()
        assert fresh["conversation_id"] == second and len(fresh["messages"]) == 1
        assert fresh["bank"] == initial["bank"]
        await client.post("/api/messages", json={"text": "你好", "conversation_id": second})
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        assert all("青松七号" not in item["content"] for item in model.requests[-1])
        listing = (await client.get("/api/conversations")).json()
        assert next(item for item in listing if item["id"] == first)["title"] == "我的暗号是青松七号"
        assert (
            await client.patch(f"/api/conversations/{second}", json={"title": "理财计划"})
        ).status_code == 200
        restored = (await client.get("/api/bootstrap", params={"conversation_id": first})).json()
        assert any(item["text"] == "我的暗号是青松七号" for item in restored["messages"])
        assert (await client.get("/api/bootstrap")).json()["conversation_id"] == first
        assert next(c for c in restored["conversations"] if c["id"] == second)["title"] == "理财计划"
        assert (await client.patch(f"/api/conversations/{first}", json={"title": " "})).status_code == 400
    # 服务重启后保留标题与全部会话，数据库不会重新播种。
    async with running_client(settings) as (_, client):
        restored = (await client.get("/api/bootstrap")).json()
        assert {c["id"] for c in restored["conversations"]} == {first, second}
        assert restored["bank"] == initial["bank"]


async def test_explicit_conversation_prevents_stale_tab_message_and_inspection_mixup(settings):
    async with running_client(settings, ScriptedModel(["已收到第一段的消息。"])) as (app, client):
        first = (await client.get("/api/bootstrap")).json()["conversation_id"]
        second = (await client.post("/api/conversations", json={})).json()["conversation_id"]
        await client.post("/api/messages", json={"text": "留在第一段", "conversation_id": first})
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        result = (
            await client.post(
                "/api/sandbox/inspect", json={"tool": "analyze_bills", "conversation_id": first}
            )
        ).json()
        assert result["conversation_id"] == first
        current = (await client.get("/api/bootstrap")).json()
        assert current["conversation_id"] == second and len(current["messages"]) == 1
        assert (
            await client.post("/api/messages", json={"text": "你好", "conversation_id": "missing"})
        ).status_code == 404


async def test_confirmation_stays_with_original_conversation_and_shares_bank(settings):
    model = ScriptedModel([[tool("transfer", recipient="张三", amount_yuan="200")], "已完成。"])
    async with running_client(settings, model) as (app, client):
        before = (await client.get("/api/bootstrap")).json()
        first = before["conversation_id"]
        await client.post("/api/messages", json={"text": "给张三转200元", "conversation_id": first})
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        pending = (await client.get("/api/bootstrap")).json()["messages"][-1]["data"]["approval_id"]
        second = (await client.post("/api/conversations", json={})).json()["conversation_id"]
        wrong = await client.post(
            f"/api/approvals/{pending}/decision", json={"decision": "confirm", "conversation_id": second}
        )
        assert wrong.status_code == 400
        assert (await client.get("/api/bank")).json()["total_assets_minor"] == before["bank"][
            "total_assets_minor"
        ]
        confirmed = await client.post(
            f"/api/approvals/{pending}/decision", json={"decision": "confirm", "conversation_id": first}
        )
        assert confirmed.json()["status"] == "executed"
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        after = (await client.get("/api/bootstrap")).json()
        assert after["conversation_id"] == second and len(after["messages"]) == 1
        assert after["bank"]["total_assets_minor"] == before["bank"]["total_assets_minor"] - 20000
        assert (await client.delete(f"/api/conversations/{first}")).status_code == 200
        with app.state.database.connection() as db:
            assert (
                db.execute("SELECT status FROM approvals WHERE id=?", (pending,)).fetchone()[0] == "executed"
            )
            assert (
                db.execute("SELECT COUNT(*) FROM transactions WHERE action_id IS NOT NULL").fetchone()[0] == 1
            )
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM events WHERE type='bank.updated' AND conversation_id='*'"
                ).fetchone()[0]
                == 1
            )


async def test_deleting_last_conversation_cancels_pending_but_keeps_money(settings):
    async with running_client(
        settings, ScriptedModel([[tool("transfer", recipient="张三", amount_yuan="200")]])
    ) as (app, client):
        initial = (await client.get("/api/bootstrap")).json()
        first = initial["conversation_id"]
        await client.post("/api/messages", json={"text": "给张三转200元"})
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        pending = (await client.get("/api/bootstrap")).json()["messages"][-1]["data"]["approval_id"]
        removed = (await client.delete(f"/api/conversations/{first}")).json()
        assert removed["conversation_id"] != first
        fresh = (await client.get("/api/bootstrap")).json()
        assert len(fresh["conversations"]) == 1 and len(fresh["messages"]) == 1
        assert fresh["bank"] == initial["bank"]
        assert (await client.get("/api/bootstrap", params={"conversation_id": first})).status_code == 404
        assert (await app.state.agent.confirm(pending, "confirm"))["status"] == "cancelled"


async def test_reset_and_delete_cannot_interrupt_running_task_in_other_conversation(settings):
    class WaitingModel:
        async def complete(self, messages):
            await asyncio.Event().wait()

    async with running_client(settings, WaitingModel()) as (_, client):
        first = (await client.get("/api/bootstrap")).json()["conversation_id"]
        await client.post("/api/messages", json={"text": "你好", "conversation_id": first})
        await client.post("/api/conversations", json={})
        assert (await client.delete(f"/api/conversations/{first}")).status_code == 409
        assert (await client.post("/api/sandbox/reset", json={"confirmation": "RESET"})).status_code == 409


def test_legacy_database_migration_preserves_history_receipts_and_balances(database):
    before = Bank(database).snapshot()
    with database.transaction() as db:
        original = database.conversation_id(db)
        add_message(db, original, None, "user", "text", "以前的账单查询", {})
        db.execute("ALTER TABLE conversations RENAME TO conversations_new")
        db.execute("CREATE TABLE conversations (id TEXT PRIMARY KEY,created_at TEXT NOT NULL)")
        db.execute("INSERT INTO conversations SELECT id,created_at FROM conversations_new")
        db.execute("DROP TABLE conversations_new")
        db.execute("DROP TABLE sessions")
        db.execute("CREATE TABLE sessions (token_hash TEXT PRIMARY KEY,created_at TEXT NOT NULL)")
        db.execute("INSERT INTO sessions VALUES ('legacy-token','2026-01-01')")
    upgraded = Database(database.path)
    upgraded.initialize()
    upgraded.initialize()
    with upgraded.connection() as db:
        assert upgraded.conversations(db)[0]["title"] == "以前的账单查询"
        assert upgraded.conversation_id(db) == original
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert Bank(upgraded).snapshot() == before
