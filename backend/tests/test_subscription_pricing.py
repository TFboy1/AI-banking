import asyncio
import json

import pytest
from banking.bank import CAPABILITIES, TOOLS, BankError, tool_schemas
from banking.capabilities import Capability, CapabilityRegistry
from conftest import ask, confirm, state, tool
from subscription_consent import ask, subscription_facts, consent_reads


@pytest.mark.parametrize("months,expected", [(1,2500), (6,15000), (12,30000), (18,45000), (24,60000), (1200,3000000)])
def test_quote_calculates_from_database_in_minor_units(agent, months, expected):
    result = subscription_facts(agent.bank,"quote_subscriptions", {"services": ["office"], "months": months, "inquire_packages": True})
    assert result["outcome"]["facts"]["total_minor"] == expected
    assert result["outcome"]["status"] == "verified"
    assert result["evidence"]["references"] == [{"table": "subscriptions", "id": "office"}]
    assert "无法确认是否有套餐" in result["answer"]
    assert len(result["display"]["rows"]) == 1


@pytest.mark.parametrize("months", [0, -1, 1.5, "24", True, 1201])
def test_invalid_structured_period_is_rejected(agent, months):
    with pytest.raises(BankError):
        subscription_facts(agent.bank,"quote_subscriptions", {"services": ["office"], "months": months})


@pytest.mark.parametrize("services", [[], ["未知服务"], ["会员"], ["office", "未知服务"]])
def test_missing_or_ambiguous_service_is_never_guessed(agent, services):
    result = subscription_facts(agent.bank,"quote_subscriptions", {"services": services})
    assert result["outcome"]["status"] == "needs_clarification"
    assert "total_minor" not in result["outcome"]["facts"]
    assert "¥" not in result["answer"]


def test_multi_service_quote_deduplicates_and_does_not_mutate(agent):
    before = agent.bank.snapshot()
    result = subscription_facts(agent.bank,"quote_subscriptions", {"services": ["office", "轻办公会员", "cloud"], "months": 24})
    assert result["outcome"]["facts"]["total_minor"] == 88800
    assert len(result["outcome"]["facts"]["quotes"]) == 2
    assert agent.bank.snapshot() == before


def test_registry_keeps_existing_permissions_and_schemas():
    assert {item["function"]["name"] for item in tool_schemas()} == set(TOOLS)
    for name, (_, writes, _) in TOOLS.items():
        assert CAPABILITIES[name].requires_confirmation == writes
    assert CAPABILITIES["transfer"].domain == "transfers"
    assert CAPABILITIES["find_recipients"].domain == "contacts"
    assert CAPABILITIES["preview_cashflow"].effect == "preview"
    registry = CapabilityRegistry()
    registry.register(CAPABILITIES["transfer"])
    with pytest.raises(ValueError):
        registry.register(CAPABILITIES["transfer"])
    with pytest.raises(ValueError):
        registry.register(Capability("invalid", TOOLS["get_accounts"][0], "", "accounts", "execute"))


async def test_reported_cancel_list_and_resubscribe_conversation(agent):
    await ask(agent, [[tool("cancel_subscription", subscription_id="office")]], "帮我取消轻办公会员的订阅")
    await confirm(agent)
    with agent.db.connection() as db:
        approval = json.loads(db.execute("SELECT data_json FROM messages WHERE kind='approval' ORDER BY rowid DESC").fetchone()[0])
    assert "执行成功" in approval["explanation"]["conclusion"]
    await ask(agent, [[tool("list_subscriptions")]], "我有哪些订阅")
    task_id = await ask(agent, [[tool("list_subscriptions")], [tool("quote_subscriptions", services=["office"])], "报价已查询。"], "我想要重新订阅，需要多少钱")
    assert state(agent, task_id)["status"] == "completed"
    with agent.db.connection() as db:
        rows = db.execute("SELECT kind,text,data_json FROM messages WHERE task_id=? AND role='assistant' ORDER BY rowid", (task_id,)).fetchall()
    assert "轻办公会员" in rows[-1]["text"] and "¥25.00" in rows[-1]["text"]
    assert "¥154.00" not in rows[-1]["text"]
    assert json.loads(rows[-2]["data_json"])["tool"] == "quote_subscriptions"


async def test_model_receives_only_current_conversation_context(agent):
    await ask(agent, [[tool("quote_subscriptions", services=["office"])], "报价已查询。"], "轻办公会员每月多少钱")
    with agent.db.transaction() as db:
        new_conversation = agent.db.create_conversation(db)
    agent.model.responses = [[tool("quote_subscriptions", services=[])], "请明确订阅服务。"]
    task_id = await agent.submit("我想重新订阅，需要多少钱", conversation_id=new_conversation)
    await asyncio.wait_for(agent.queue.join(), 3)
    await consent_reads(agent,task_id)
    history = agent.model.requests[-1]
    assert all("轻办公会员" not in message["content"] for message in history)
    assert state(agent, task_id)["status"] == "completed"


async def test_quote_does_not_swallow_combined_transfer_or_note(agent):
    task_id = await ask(agent, [[tool("quote_subscriptions", services=["office"], months=24), tool("transfer", recipient="zhang_san", amount_yuan="200", note="晚餐AA")]], "轻办公会员订阅两年多少钱，再从日常账户给张三转200元备注晚餐AA")
    assert state(agent, task_id)["status"] == "waiting_confirmation"
    with agent.db.connection() as db:
        approval = db.execute("SELECT tool_name,args_json FROM approvals WHERE task_id=? AND status='pending'", (task_id,)).fetchone()
    assert approval["tool_name"] == "transfer"
    assert json.loads(approval["args_json"])["note"] == "晚餐AA"


async def test_structured_quote_accepts_model_period_without_phrase_parser(agent):
    task_id = await ask(agent, [[tool("quote_subscriptions", services=["office"], months=18, inquire_packages=True)], "按当前月费估算18个月450元，暂无优惠报价。"], "轻办公会员再用十八个月，长期买能便宜些吗")
    assert state(agent, task_id)["status"] == "completed"
    # Generic results remain in the tool context; a combined/unclassified request lets the model summarize.


async def test_backend_rejects_model_guess_without_user_source(agent):
    task_id = await ask(agent, [[tool("quote_subscriptions", services=["office"])], "报价已查询。"], "我想重新订阅，需要多少钱")
    with agent.db.connection() as db:
        answer = db.execute("SELECT text FROM messages WHERE task_id=? ORDER BY rowid DESC", (task_id,)).fetchone()[0]
        quote = json.loads(db.execute("SELECT data_json FROM messages WHERE task_id=? AND kind='data'", (task_id,)).fetchone()[0])["result"]
    assert quote["outcome"]["status"] == "needs_clarification"
    assert "¥25.00" not in answer


async def test_two_period_comparisons_keep_both_quote_calls(agent):
    task_id = await ask(agent, [[tool("quote_subscriptions", services=["office"], months=12), tool("quote_subscriptions", services=["office"], months=24)], "报价已查询。"], "轻办公会员订阅一年和两年分别多少钱")
    with agent.db.connection() as db:
        results = [json.loads(row[0])["result"]["outcome"]["facts"]["total_minor"] for row in db.execute("SELECT data_json FROM messages WHERE task_id=? AND kind='data' ORDER BY rowid", (task_id,))]
    assert results == [30000, 60000]
    assert state(agent, task_id)["status"] == "completed"


async def test_ambiguous_historical_subject_requires_clarification(agent):
    await ask(agent, [[tool("quote_subscriptions", services=["office", "cloud"])], "报价已查询。"], "轻办公会员和云端存储多少钱")
    task_id = await ask(agent, [[tool("quote_subscriptions", services=["office"])], "报价已查询。"], "重新订阅要多少钱")
    with agent.db.connection() as db:
        quote = json.loads(db.execute("SELECT data_json FROM messages WHERE task_id=? AND kind='data'", (task_id,)).fetchone()[0])["result"]
    assert quote["outcome"]["status"] == "needs_clarification"
