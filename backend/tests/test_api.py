import asyncio

import httpx
from banking.main import create_app
from conftest import ScriptedModel, tool


async def test_session_origin_and_tampered_decision_are_rejected(settings):
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client,
    ):
        assert (await client.get("/api/bank")).status_code == 401
        assert (await client.get("/api/bootstrap")).status_code == 200
        assert (await client.get("/api/bank")).status_code == 200
        assert (
            await client.post(
                "/api/messages", json={"text": "转账"}, headers={"Origin": "https://untrusted.example"}
            )
        ).status_code == 403
        assert (
            await client.post(
                "/api/approvals/unknown/decision", json={"decision": "confirm", "amount_yuan": "999"}
            )
        ).status_code == 422
        assert (await client.post("/api/internal/feishu", json={})).status_code == 403


async def test_missing_model_is_reported_without_fake_success(settings):
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client,
    ):
        boot = (await client.get("/api/bootstrap")).json()
        assert not boot["config"]["model_configured"]
        assert (await client.post("/api/messages", json={"text": "给张三转200元"})).status_code == 202
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        after = (await client.get("/api/bootstrap")).json()
        assert after["messages"][-1]["kind"] == "error"
        assert "尚未连接大模型" in after["messages"][-1]["text"]
        assert after["bank"]["total_assets_minor"] == boot["bank"]["total_assets_minor"]


async def test_reset_clears_pending_operation_and_reseeds_bank(settings):
    app = create_app(settings, ScriptedModel([[tool("transfer", recipient="张三", amount_yuan="200")]]))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client,
    ):
        initial = (await client.get("/api/bootstrap")).json()
        await client.post("/api/messages", json={"text": "给张三转200元"})
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        before_reset = (await client.get("/api/bootstrap")).json()
        approval_id = before_reset["messages"][-1]["data"]["approval_id"]
        assert (await client.post("/api/sandbox/reset", json={"confirmation": "RESET"})).status_code == 200
        after_reset = (await client.get("/api/bootstrap")).json()
        assert len(after_reset["messages"]) == 1
        assert after_reset["conversation_id"] != initial["conversation_id"]
        assert after_reset["bank"]["total_assets_minor"] == initial["bank"]["total_assets_minor"]
        assert (
            await client.post(f"/api/approvals/{approval_id}/decision", json={"decision": "confirm"})
        ).status_code == 400


async def test_feishu_binding_deduplication_and_cross_channel_confirmation(settings):
    app = create_app(
        settings, ScriptedModel([[tool("transfer", recipient="张三", amount_yuan="200")], "已完成"])
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client,
    ):
        # 生命周期启动时飞书关闭，不启动外发进程。这里只测试入站适配接口。
        settings.feishu_enabled = True
        settings.feishu_app_id = "test_app"
        settings.feishu_app_secret = "test_secret"
        settings.feishu_bridge_token = "a" * 40
        await client.get("/api/bootstrap")
        headers = {"Authorization": "Bearer " + settings.feishu_bridge_token}
        message = {
            "kind": "message",
            "event_id": "unbound",
            "tenant_key": "tenant",
            "open_id": "owner",
            "chat_id": "private_chat",
            "chat_type": "p2p",
            "text": "给张三转200元",
        }
        result = (await client.post("/api/internal/feishu", json=message, headers=headers)).json()
        assert "先在本机" in result["reply"]
        binding = (await client.post("/api/feishu/binding-code", json={})).json()
        message.update(event_id="bind", text="绑定 " + binding["code"])
        assert (
            "绑定成功"
            in (await client.post("/api/internal/feishu", json=message, headers=headers)).json()["reply"]
        )
        message.update(event_id="payment", text="给张三转200元")
        first = (await client.post("/api/internal/feishu", json=message, headers=headers)).json()
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        duplicate = (await client.post("/api/internal/feishu", json=message, headers=headers)).json()
        assert first["task_id"] == duplicate["task_id"] and duplicate["duplicate"]
        boot = (await client.get("/api/bootstrap")).json()
        approval_id = boot["messages"][-1]["data"]["approval_id"]
        forged = {
            **message,
            "event_id": "forged",
            "kind": "card",
            "open_id": "stranger",
            "approval_id": approval_id,
            "decision": "confirm",
        }
        assert (
            "先在本机"
            in (await client.post("/api/internal/feishu", json=forged, headers=headers)).json()["reply"]
        )
        # 飞书发起，Web 确认，共用同一个执行事务。
        confirmed = (
            await client.post(f"/api/approvals/{approval_id}/decision", json={"decision": "confirm"})
        ).json()
        assert confirmed["status"] == "executed"
        await asyncio.wait_for(app.state.agent.queue.join(), 3)
        card = {**forged, "event_id": "owner-confirm", "open_id": "owner"}
        assert (await client.post("/api/internal/feishu", json=card, headers=headers)).json()[
            "status"
        ] == "executed"
        final = (await client.get("/api/bank")).json()
        assert next(a["balance_minor"] for a in final["accounts"] if a["id"] == "daily") == 1266050


async def test_only_readonly_inspection_is_exposed_without_model(settings):
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client,
    ):
        await client.get("/api/bootstrap")
        result = await client.post("/api/sandbox/inspect", json={"tool": "analyze_bills"})
        assert result.status_code == 200 and result.json()["kind"] == "data"
        assert (await client.post("/api/sandbox/inspect", json={"tool": "transfer"})).status_code == 422
