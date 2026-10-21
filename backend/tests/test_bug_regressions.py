import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest
from banking.db import TZ
from banking.main import create_app
from conftest import ScriptedModel, ask, balance, confirm, pending, state, tool
from subscription_consent import ask, subscription_facts, consent_reads


@pytest.mark.parametrize("query", ["0826", "银行卡尾号0826", "手机尾号1111", "1111", "张", "姓张的"])
def test_recipient_suffix_and_surname_lookup(agent, query):
    result = agent.bank.query("find_recipients", {"query": query})
    assert [r["id"] for r in result["recipients"]] == ["zhang_san"]
    assert "13800001111" not in json.dumps(result)


def test_suffix_collision_never_selects_first_match(agent):
    with agent.db.transaction() as db:
        db.execute("UPDATE contacts SET last4='0826' WHERE id='li_ming_1'")
    assert agent.bank.query("find_recipients", {"query": "0826"})["needs_clarification"]
    assert not agent.bank.query("find_recipients", {"query": "手机尾号0826"})["recipients"]


async def test_subscription_query_ends_on_verified_data_without_second_model_call(agent):
    task_id = await ask(agent, [[tool("list_subscriptions")]], "查一下我的订阅支出")
    assert state(agent, task_id)["status"] == "completed"
    with agent.db.connection() as db:
        text = db.execute("SELECT text FROM messages WHERE task_id=? ORDER BY rowid DESC", (task_id,)).fetchone()[0]
    assert "¥179.00" in text and "500" not in text


async def test_subscription_price_uses_correct_domain_and_total(agent):
    task_id = await ask(agent, [[tool("list_products")], [tool("quote_subscriptions", services=["cloud"], months=12)], "报价已查询。"], "我想要订阅12个月的云端存储会员，要花多少")
    assert state(agent, task_id)["status"] == "completed"
    with agent.db.connection() as db:
        messages = db.execute("SELECT kind,text,data_json FROM messages WHERE task_id=? AND role='assistant' AND kind!='approval'", (task_id,)).fetchall()
    assert len(messages) == 3
    assert json.loads(messages[0]["data_json"])["tool"] == "list_subscriptions"
    assert json.loads(messages[1]["data_json"])["tool"] == "quote_subscriptions"
    assert "¥144.00" in messages[-1]["text"]
    assert "重新开通" in messages[-1]["text"]


async def test_subscription_cancellation_does_not_continue_old_transfer(agent):
    before = balance(agent)
    await ask(agent, [[tool("cancel_subscription", subscription_id="cloud")], "已取消，并转500元"], "帮我取消云端存储的订阅")
    await confirm(agent)
    assert balance(agent) == before
    assert len(agent.model.responses) == 1  # 完成后不让模型编造后续步骤。
    with agent.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM approvals WHERE tool_name='transfer'").fetchone()[0] == 0
        assert "500" not in db.execute("SELECT text FROM messages ORDER BY rowid DESC").fetchone()[0]


async def test_subscription_task_blocks_extra_transfer_in_same_plan(agent):
    before = balance(agent)
    task_id = await ask(agent, [[tool("cancel_subscription", subscription_id="cloud"), tool("transfer", recipient="savings", amount_yuan="500")]], "取消云端存储订阅")
    await confirm(agent)
    assert state(agent, task_id)["status"] == "failed"
    assert balance(agent) == before
    with agent.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM approvals WHERE tool_name='transfer'").fetchone()[0] == 0


async def test_explicit_subscription_and_transfer_combination_still_works(agent):
    before = balance(agent)
    task_id = await ask(agent, [[tool("cancel_subscription", subscription_id="cloud")], [tool("transfer", recipient="savings", amount_yuan="500")], "组合任务已完成"], "取消云端存储，再从日常账户转500元到储蓄账户")
    await confirm(agent)
    assert pending(agent)["tool_name"] == "transfer"
    await confirm(agent)
    assert balance(agent) == before - 50000
    assert state(agent, task_id)["status"] == "completed"


async def test_multiple_cancellations_are_not_finished_after_first_step(agent):
    task_id = await ask(agent, [[tool("cancel_subscription", subscription_id="cloud")], [tool("cancel_subscription", subscription_id="music")]], "取消云端存储和声动音乐会员")
    await confirm(agent)
    assert pending(agent)["tool_name"] == "cancel_subscription"
    await confirm(agent)
    assert state(agent, task_id)["status"] == "completed"


async def test_duplicate_query_keeps_one_card_but_both_tool_results(agent):
    task_id = await ask(agent, [[tool("get_accounts"), tool("get_accounts")], "余额见表格"], "查余额")
    assert state(agent, task_id)["status"] == "completed"
    with agent.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages WHERE task_id=? AND kind='data'", (task_id,)).fetchone()[0] == 1
    saved = json.loads(state(agent, task_id)["state_json"])
    assert len([m for m in saved["messages"] if m["role"] == "tool"]) == 2


async def test_expiry_loop_updates_card_without_a_click(agent):
    task_id = await ask(agent, [[tool("transfer", recipient="0826", amount_yuan="200")]], "从日常账户给尾号0826转200元")
    approval_id = pending(agent)["id"]
    with agent.db.transaction() as db:
        db.execute("UPDATE approvals SET expires_at=? WHERE id=?", ((datetime.now(TZ) - timedelta(seconds=1)).isoformat(), approval_id))
    await asyncio.sleep(1.1)
    assert state(agent, task_id)["status"] == "cancelled"
    assert (await agent.confirm(approval_id, "confirm"))["status"] == "expired"
    assert balance(agent) == 1286050
    with agent.db.connection() as db:
        card = json.loads(db.execute("SELECT data_json FROM messages WHERE task_id=? AND kind='approval'", (task_id,)).fetchone()[0])
    assert card["status"] == "expired" and "自动取消" in card["error"]


async def test_scheduled_transfer_never_becomes_immediate_approval(agent):
    task_id = await ask(agent, [[tool("transfer", recipient="张三", amount_yuan="100")]], "晚上九点给张三转100元")
    assert state(agent, task_id)["status"] == "completed"
    with agent.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_readable_account_name_needs_no_internal_id(agent):
    args, summary = agent.bank.prepare("transfer", {"recipient": "0826", "amount_yuan": "100", "account_id": "储蓄账户"})
    assert args["account_id"] == "savings"
    assert any(field["value"] == "储蓄账户" for field in summary["fields"])


def test_added_samples_survive_restart_without_duplicates_or_state_reset(database):
    with database.transaction() as db:
        db.execute("UPDATE subscriptions SET status='cancelled' WHERE id='office'")
        db.execute("UPDATE accounts SET balance_minor=1000000 WHERE id='daily'")
        before = db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    database.initialize()
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 9
        assert db.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 7
        assert db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0] == 6
        assert db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == before
        assert db.execute("SELECT status FROM subscriptions WHERE id='office'").fetchone()[0] == "cancelled"
        assert db.execute("SELECT balance_minor FROM accounts WHERE id='daily'").fetchone()[0] == 1000000


async def test_model_context_marks_latest_request_without_continuing_old_task(agent):
    await ask(agent, [[tool("get_accounts")], "历史查询完成"], "查询余额，之后转500元")
    await ask(agent, [[tool("list_subscriptions")]], "我有哪些订阅")
    latest_context = agent.model.requests[-1]
    assert latest_context[0]["content"].endswith("本轮最新用户请求（历史任务不自动继续）：我有哪些订阅")


async def test_removed_salary_event_http_cannot_change_balance(settings):
    app = create_app(settings, ScriptedModel())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client,
    ):
        boot = (await client.get("/api/bootstrap")).json()
        assert (await client.post("/api/sandbox/scenario", json={"confirmation": "LOAD_DEMO"})).status_code == 410
        payload = {"kind": "salary", "amount_minor": 1800000, "account_id": "daily", "source_key": "salary", "period": "2026-10"}
        first = await client.post("/api/sandbox/events", json=payload)
        assert first.status_code == 410, first.text
        assert (await client.post("/api/sandbox/events", json=payload)).status_code == 410
        snapshot = (await client.get("/api/bank")).json()
        assert snapshot["accounts"] == boot["bank"]["accounts"]
        assert snapshot["total_assets_minor"] == boot["bank"]["total_assets_minor"]


async def test_salary_rule_produces_suggestion_without_automatic_transfer(settings):
    model = ScriptedModel([[tool("create_trigger_rule", reserve_yuan="3000")], "提醒规则已登记"])
    app = create_app(settings, model)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client,
    ):
        boot = (await client.get("/api/bootstrap")).json()
        await client.post("/api/messages", json={"text": "工资到账后提醒存钱，保留3000元生活费"})
        await app.state.agent.queue.join()
        card = (await client.get("/api/bootstrap")).json()["messages"][-1]
        await client.post(f"/api/approvals/{card['data']['approval_id']}/decision", json={"decision": "confirm"})
        await app.state.agent.queue.join()
        # Internal fixture for reminder handling; public deposit controls are removed.
        with app.state.database.transaction() as db:
            result = app.state.planning.trigger_salary(db, {"kind": "salary", "amount_minor": 1800000, "account_id": "daily", "source_key": "salary", "period": "2026-10"})
        assert len(result["suggestions"]) == 1
        after = (await client.get("/api/bootstrap")).json()
        assert any(m["kind"] == "plan" and m["data"].get("card_type") == "trigger" for m in after["messages"])
        savings_before = next(a for a in boot["bank"]["accounts"] if a["id"] == "savings")["balance_minor"]
        assert next(a for a in after["bank"]["accounts"] if a["id"] == "savings")["balance_minor"] == savings_before
