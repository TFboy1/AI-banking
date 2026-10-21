import json

import httpx
import pytest
from banking.main import create_app


@pytest.mark.parametrize("name,args,source", [
    ("transfer", {"recipient": "zhang_san", "amount_yuan": "100"}, "savings"),
    ("purchase_product", {"product_id": "cash", "amount_yuan": "100"}, "savings"),
    ("simulate_expense", {"merchant": "测试店", "amount_yuan": "100"}, "savings"),
    ("save_to_goal", {"total_yuan": "100"}, "daily"),
])
async def test_shared_payment_flow_requires_account_and_charges_selected(settings, name, args, source):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        boot = (await client.get("/api/bootstrap")).json()
        before = {a["id"]: a["balance_minor"] for a in boot["bank"]["accounts"]}
        with app.state.database.transaction() as db:
            db.execute("INSERT INTO risk_profile VALUES (1, '{}', 4)")
        with app.state.database.transaction() as db:
            message = app.state.agent.create_direct_approval(db, boot["conversation_id"], name, args)
        approval = message["data"]["approval_id"]
        context = await client.get(f"/api/approvals/{approval}/payment?conversation_id={boot['conversation_id']}")
        assert context.status_code == 200 and context.json()["account_required"]
        assert context.json()["account_id"] == ""
        missing = await client.post(f"/api/approvals/{approval}/decision", json={"decision": "confirm", "conversation_id": boot["conversation_id"]})
        assert missing.status_code == 400
        assert {a["id"]: a["balance_minor"] for a in (await client.get("/api/bank")).json()["accounts"]} == before
        result = await client.post(f"/api/approvals/{approval}/decision", json={"decision": "confirm", "conversation_id": boot["conversation_id"], "account_id": source})
        assert result.json()["status"] == "executed"
        after = {a["id"]: a["balance_minor"] for a in (await client.get("/api/bank")).json()["accounts"]}
        assert after[source] == before[source] - 10000
        if name != "save_to_goal":
            other = "daily" if source == "savings" else "savings"
            assert after[other] == before[other]
        with app.state.database.connection() as db:
            stored = db.execute("SELECT args_json,summary_json FROM approvals WHERE id=?", (approval,)).fetchone()
            assert json.loads(stored["args_json"])["account_id"] == source
            assert json.loads(stored["summary_json"])["payment_account_id"] == source
        again = await client.post(f"/api/approvals/{approval}/decision", json={"decision": "confirm", "conversation_id": boot["conversation_id"], "account_id": source})
        assert again.json()["status"] == "executed"
        assert {a["id"]: a["balance_minor"] for a in (await client.get("/api/bank")).json()["accounts"]} == after


async def test_payment_context_is_scoped_and_rechecks_selected_account(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        boot = (await client.get("/api/bootstrap")).json()
        with app.state.database.transaction() as db:
            message = app.state.agent.create_direct_approval(db, boot["conversation_id"], "transfer", {"recipient": "zhang_san", "amount_yuan": "100"})
            other = app.state.database.create_conversation(db)
            db.execute("UPDATE accounts SET balance_minor=0 WHERE id='savings'")
        approval = message["data"]["approval_id"]
        assert (await client.get(f"/api/approvals/{approval}/payment?conversation_id={other}")).status_code == 400
        context = (await client.get(f"/api/approvals/{approval}/payment?conversation_id={boot['conversation_id']}")).json()
        assert not next(a for a in context["accounts"] if a["id"] == "savings")["eligible"]
        result = await client.post(f"/api/approvals/{approval}/decision", json={"decision": "confirm", "conversation_id": boot["conversation_id"], "account_id": "savings"})
        assert result.json()["status"] == "failed"
        assert next(a for a in (await client.get("/api/bank")).json()["accounts"] if a["id"] == "savings")["balance_minor"] == 0
