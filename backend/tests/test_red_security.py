from face_test_support import bind_test_face, grant
import json
import time

import httpx
import pytest
from banking.bank import BankError
from banking.main import create_app
from banking.payment_security import risk
from test_security import register


async def create_payment(app, conversation, amount, name="transfer"):
    args = {"recipient": "zhang_san", "amount_yuan": amount} if name == "transfer" else {"product_id": "cash", "amount_yuan": amount}
    with app.state.database.transaction() as db:
        message = app.state.agent.create_direct_approval(db, conversation, name, args)
    return message["data"]["approval_id"]


async def test_user_single_threshold_boundary_and_double_validation(settings):
    settings.app_auth_enabled = True
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        await register(client)
        bind_test_face(app)
        boot = (await client.get("/api/bootstrap")).json()
        conversation = boot["conversation_id"]
        assert (await client.get("/api/payment-policy")).json()["threshold_minor"] == 100000
        assert (await client.patch("/api/payment-policy", json={"threshold_yuan": "0", "pin": "2468"})).status_code == 400
        assert (await client.patch("/api/payment-policy", json={"threshold_yuan": "50", "pin": "0000"})).status_code == 400
        assert (await client.patch("/api/payment-policy", json={"threshold_yuan": "50", "pin": "2468"})).status_code == 200
        assert (await client.get("/api/payment-policy")).json()["threshold_minor"] == 5000
        assert (await client.patch("/api/payment-policy", json={"threshold_yuan": "1000", "pin": "2468"})).status_code == 200
        app.state.database.initialize()
        assert (await client.get("/api/payment-policy")).json()["threshold_minor"] == 100000
        for amount in ["300", "200", "500"]:
            approval = await create_payment(app, conversation, amount)
            context = (await client.get(f"/api/approvals/{approval}/payment?conversation_id={conversation}")).json()
            assert context["level"] == "yellow" and not context["sms_required"]
            result = await client.post(f"/api/approvals/{approval}/decision", json={"decision": "confirm", "conversation_id": conversation, "account_id": "daily", "pin": "2468"})
            assert result.json()["status"] == "executed"
        approval = await create_payment(app, conversation, "1000.01")
        body = {"decision": "confirm", "conversation_id": conversation, "account_id": "savings", "pin": "2468"}
        path = f"/api/approvals/{approval}/decision"
        context = (await client.get(f"/api/approvals/{approval}/payment?conversation_id={conversation}")).json()
        assert context["level"] == "red" and context["transaction_minor"] == 100001 and context["basis"] == "single_transaction"
        assert "daily_transfer_minor" not in context
        before = (await client.get("/api/bank")).json()
        assert (await client.post(path, json=body)).status_code == 400
        sms = (await client.post(f"/api/approvals/{approval}/sms", json={"conversation_id": conversation, "account_id": "savings"})).json()
        assert len(sms["demo_code"]) == 6 and sms["simulated"]
        assert (await client.post(path, json={**body, "account_id": "daily", "sms_code": sms["demo_code"]})).status_code == 400
        assert (await client.get("/api/bank")).json() == before
        wrong = "000000" if sms["demo_code"] != "000000" else "111111"
        assert (await client.post(path, json={**body, "sms_code": wrong})).status_code == 400
        with app.state.database.connection() as db:
            assert db.execute("SELECT attempts FROM payment_sms WHERE approval_id=?", (approval,)).fetchone()[0] == 1
        assert (await client.post(path, json={**body, "pin": "0000", "sms_code": sms["demo_code"]})).status_code == 400
        result = await client.post(path, json={**body, "sms_code": sms["demo_code"],"face_token":grant(app,client,approval,"savings")})
        assert result.json()["status"] == "executed"
        after = (await client.get("/api/bank")).json()
        assert next(a for a in after["accounts"] if a["id"] == "savings")["balance_minor"] == next(a for a in before["accounts"] if a["id"] == "savings")["balance_minor"] - 100001
        assert (await client.post(path, json={**body, "sms_code": sms["demo_code"]})).json()["status"] == "executed"
        assert (await client.get("/api/bank")).json() == after
        with app.state.database.connection() as db:
            assert db.execute("SELECT consumed FROM payment_sms WHERE approval_id=?", (approval,)).fetchone()[0] == 1


async def test_red_sms_expiry_policy_change_and_purchase_rule(settings):
    settings.app_auth_enabled = True
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        await register(client)
        bind_test_face(app)
        conversation = (await client.get("/api/bootstrap")).json()["conversation_id"]
        with app.state.database.transaction() as db:
            db.execute("INSERT INTO risk_profile VALUES (1, '{}', 4)")
        approval = await create_payment(app, conversation, "100", "purchase_product")
        assert (await client.get(f"/api/approvals/{approval}/payment?conversation_id={conversation}")).json()["sms_required"]
        body = {"decision": "confirm", "conversation_id": conversation, "account_id": "daily", "pin": "2468"}
        sms = (await client.post(f"/api/approvals/{approval}/sms", json={"conversation_id": conversation, "account_id": "daily"})).json()
        assert (await client.post(f"/api/approvals/{approval}/sms", json={"conversation_id": conversation, "account_id": "daily"})).status_code == 400
        other_conversation = (await client.post("/api/conversations", json={})).json()["conversation_id"]
        other_approval = await create_payment(app, other_conversation, "100", "purchase_product")
        other_sms = await client.post(f"/api/approvals/{other_approval}/sms", json={"conversation_id": other_conversation, "account_id": "daily"})
        assert other_sms.status_code == 200 and other_sms.json()["demo_code"]
        assert (await client.post(f"/api/approvals/{other_approval}/decision", json={"decision": "cancel", "conversation_id": other_conversation})).json()["status"] == "cancelled"
        assert (await client.patch("/api/payment-policy", json={"threshold_yuan": "1200", "pin": "2468"})).status_code == 200
        assert (await client.post(f"/api/approvals/{approval}/decision", json={**body, "sms_code": sms["demo_code"]})).status_code == 400
        with app.state.database.transaction() as db:
            db.execute("UPDATE payment_sms SET sent_at=0 WHERE approval_id=?", (approval,))
        sms = (await client.post(f"/api/approvals/{approval}/sms", json={"conversation_id": conversation, "account_id": "daily"})).json()
        with app.state.database.transaction() as db:
            db.execute("UPDATE payment_sms SET expires_at=? WHERE approval_id=?", (time.time()-1, approval))
        assert (await client.post(f"/api/approvals/{approval}/decision", json={**body, "sms_code": sms["demo_code"]})).status_code == 400
        with app.state.database.transaction() as db, pytest.raises(BankError, match="短信验证码"):
            app.state.agent.confirm_in_transaction(db, approval, "confirm", transaction_verified=True, account_id="daily")
        assert (await client.post(f"/api/approvals/{approval}/decision", json={"decision": "cancel", "conversation_id": conversation})).json()["status"] == "cancelled"
        with app.state.database.connection() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?", (approval,)).fetchone()
            assessment = risk(db, row, json.loads(row["args_json"]))
            assert assessment['basis']=='single_transaction' and 'daily_transfer_minor' not in assessment
