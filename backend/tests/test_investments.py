import httpx
from banking.main import create_app


async def test_investment_account_requires_session_and_assessment(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        assert (await client.get("/api/investments")).status_code == 401
        boot = (await client.get("/api/bootstrap")).json()
        data = (await client.get("/api/investments")).json()
        assert data["risk_level"] is None
        assert len(data["questionnaire"]["questions"]) == 3
        assert not any(p["eligible"] for p in data["products"])
        response = await client.post("/api/investments/approvals", json={
            "conversation_id": boot["conversation_id"], "tool": "purchase_product",
            "arguments": {"product_id": "cash", "amount_yuan": "100", "account_id": "daily"},
        })
        assert response.status_code == 400
        assert (await client.get("/api/bank")).json()["total_assets_minor"] == boot["bank"]["total_assets_minor"]


async def test_investment_panel_risk_purchase_and_redemption(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        boot = (await client.get("/api/bootstrap")).json()
        conversation = boot["conversation_id"]

        async def prepare(tool, arguments):
            return await client.post("/api/investments/approvals", json={
                "conversation_id": conversation, "tool": tool, "arguments": arguments,
            })

        async def confirm(message):
            return await client.post(f"/api/approvals/{message['data']['approval_id']}/decision", json={
                "conversation_id": conversation, "decision": "confirm",
            })

        risk = await prepare("save_risk_profile", {"horizon": "short", "loss_tolerance": "none", "experience": "none"})
        assert risk.status_code == 200
        assert (await client.get("/api/investments")).json()["risk_level"] is None
        assert (await confirm(risk.json())).json()["status"] == "executed"
        assert (await client.get("/api/investments")).json()["risk_level"] == 1
        assert (await prepare("purchase_product", {"product_id": "growth", "amount_yuan": "1000"})).status_code == 400
        assert (await prepare("transfer", {"recipient": "zhang_san", "amount_yuan": "100"})).status_code == 422
        purchase = await prepare("purchase_product", {"product_id": "flex_cash", "amount_yuan": "10", "account_id": "savings"})
        assert purchase.status_code == 200
        assert not any(h["product_id"] == "flex_cash" for h in (await client.get("/api/bank")).json()["holdings"])
        assert (await confirm(purchase.json())).json()["status"] == "executed"
        assert (await confirm(purchase.json())).json()["status"] == "executed"
        bank = (await client.get("/api/bank")).json()
        assert next(a for a in bank["accounts"] if a["id"] == "savings")["balance_minor"] == 819000
        assert next(h for h in bank["holdings"] if h["product_id"] == "flex_cash")["units"] == "10.0000"
        redemption = await prepare("redeem_product", {"product_id": "flex_cash", "units": "10", "account_id": "savings"})
        assert (await confirm(redemption.json())).json()["status"] == "executed"
        assert (await client.get("/api/bank")).json()["total_assets_minor"] == boot["bank"]["total_assets_minor"]


async def test_panel_confirmation_rechecks_changed_risk(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        boot = (await client.get("/api/bootstrap")).json()
        with app.state.database.transaction() as db:
            db.execute("INSERT INTO risk_profile VALUES (1, '{}', 4)")
        response = await client.post("/api/investments/approvals", json={
            "conversation_id": boot["conversation_id"], "tool": "purchase_product",
            "arguments": {"product_id": "growth", "amount_yuan": "1000"},
        })
        assert response.status_code == 200
        with app.state.database.transaction() as db:
            db.execute("UPDATE risk_profile SET risk_level=1")
        approval = response.json()["data"]["approval_id"]
        result = await client.post(f"/api/approvals/{approval}/decision", json={"decision": "confirm", "conversation_id": boot["conversation_id"], "account_id": "daily"})
        assert result.json()["status"] == "failed"
        assert (await client.get("/api/bank")).json()["total_assets_minor"] == boot["bank"]["total_assets_minor"]
