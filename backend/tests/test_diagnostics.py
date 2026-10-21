import json

from banking.bank import Bank
from banking.db import dumps, now
from conftest import ask, balance, tool


def test_purchase_diagnosis_lists_independent_blockers_without_changes(database):
    bank = Bank(database)
    with database.transaction() as db:
        db.execute("UPDATE accounts SET balance_minor=50000 WHERE id='daily'")
        db.execute("UPDATE cards SET status='frozen',daily_limit_minor=50000 WHERE id='card_daily'")
        created = now()
        db.execute(
            "INSERT INTO commitments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "commit_test",
                "plan_test",
                database.conversation_id(db),
                "daily",
                "living",
                "生活费",
                40000,
                40000,
                "2026-12-31",
                "active",
                created,
                created,
            ),
        )
    result = bank.query("diagnose_purchase", {"product_id": "growth", "amount_yuan": "1000"})
    diagnosis = result["diagnosis"]
    blocked = {item["key"] for item in diagnosis["checks"] if item["status"] == "block"}
    assert diagnosis["status"] == "blocked"
    assert {"risk", "reserved_funds", "balance", "card_status", "daily_limit"} <= blocked
    assert next(item for item in diagnosis["checks"] if item["key"] == "minimum")["status"] == "pass"
    assert "commit_test" in result["explanation"]["sources"][0]["references"]
    with database.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
        assert db.execute("SELECT balance_minor FROM accounts WHERE id='daily'").fetchone()[0] == 50000


def test_transfer_diagnosis_distinguishes_missing_and_ambiguous_data(database):
    bank = Bank(database)
    incomplete = bank.query("diagnose_transfer", {"recipient": "李明"})["diagnosis"]
    assert incomplete["status"] == "unknown"
    assert next(item for item in incomplete["checks"] if item["key"] == "recipient")["status"] == "unknown"
    assert next(item for item in incomplete["checks"] if item["key"] == "balance")["status"] == "unknown"
    assert all(step["prompt"] for step in incomplete["next_steps"])
    contacts = bank.query("find_recipients", {"query": "李明"})
    assert "13800002222" not in dumps(contacts)
    assert "13900003333" not in dumps(contacts)


async def test_ready_diagnosis_does_not_bypass_confirmation_recheck(agent):
    before = balance(agent)
    diagnosis = agent.bank.query("diagnose_transfer", {"recipient": "张三", "amount_yuan": "200"})
    assert diagnosis["diagnosis"]["status"] == "ready"
    with agent.db.transaction() as db:
        message = agent.create_direct_approval(db, agent.db.conversation_id(db), "transfer", {"recipient": "张三", "amount_yuan": "200"})
    with agent.db.transaction() as db:
        db.execute("UPDATE cards SET status='frozen' WHERE id='card_daily'")
    result = await agent.confirm(message["data"]["approval_id"], "confirm", account_id="daily")
    assert result["status"] == "failed" and "已冻结" in result["error"]
    assert balance(agent) == before


async def test_bill_evidence_is_persisted_and_model_context_masks_phone(agent):
    await ask(
        agent,
        [[tool("find_recipients", query="zhang_san"), tool("analyze_bills")], "已查询，请查看卡片。"],
        "查看 13800001111 对应联系人，以及这个月的消费",
    )
    assert "13800001111" not in dumps(agent.model.requests)
    with agent.db.connection() as db:
        row = db.execute(
            "SELECT data_json FROM messages WHERE kind='data' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        data = json.loads(row[0])
        explanation = data["result"]["explanation"]
        month = data["result"]["month"]
        expected = {
            item[0]
            for item in db.execute("SELECT id FROM transactions WHERE substr(occurred_at,1,7)=?", (month,))
        }
    assert set(explanation["sources"][0]["references"]) == expected
    assert "已有流水支出" in explanation["conclusion"]


async def test_agent_persists_read_only_diagnosis_card(agent):
    await ask(
        agent,
        [[tool("diagnose_purchase", product_id="growth", amount_yuan="1000")], "检查结果在诊断卡中。"],
        "为什么这款理财买不了？",
    )
    with agent.db.connection() as db:
        row = db.execute("SELECT data_json FROM messages WHERE kind='diagnosis' ORDER BY rowid DESC LIMIT 1").fetchone()
        assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    assert row
    data = json.loads(row[0])
    assert data["result"]["diagnosis"]["status"] == "blocked"
    assert data["result"]["explanation"]["checks"]
