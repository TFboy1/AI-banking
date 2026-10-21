from datetime import datetime

import httpx
import pytest
from banking.bank import BankError, validate
from banking.db import TZ
from banking.main import create_app
from banking.securities_common import QuoteStockPurchase, purchase_quantity_authorized
from banking.stock_site import create_stock_app
from conftest import ScriptedModel, ask, tool
from pydantic import ValidationError
from test_security import register
from test_stock_accounts import stock_login


def test_budget_validation_and_explicit_authorization():
    args = {"orders": [{"symbol": "600519", "amount_yuan": "1000"}]}
    assert purchase_quantity_authorized("我想买1000块的贵州茅台", args)
    assert purchase_quantity_authorized("买1,000.00元贵州茅台", args)
    assert not purchase_quantity_authorized("买贵州茅台", args)
    assert not purchase_quantity_authorized("买100元贵州茅台", args)
    assert not purchase_quantity_authorized("总共1000元买这两只", {"orders": args["orders"] * 2})
    assert validate("quote_stock_purchase", args)["orders"][0]["amount_yuan"] == "1000"
    for order in ({"shares": 100, "amount_yuan": "1000"}, {}, {"amount_yuan": "0"}, {"amount_yuan": "-1"}, {"amount_yuan": "1.001"}, {"shares": 101}):
        with pytest.raises(ValidationError):
            QuoteStockPurchase.model_validate({"orders": [{"symbol": "600519", **order}]})


async def test_budget_quote_and_chat(settings, tmp_path):
    settings.app_auth_enabled = True
    settings.securities_bridge_key_path = tmp_path / "key"
    app = create_app(settings, ScriptedModel())
    stocks = create_stock_app(tmp_path / "market.sqlite3", broker_path=tmp_path / "broker.sqlite3", key_path=settings.securities_bridge_key_path)
    stocks.state.bank_transport = httpx.ASGITransport(app=app)
    app.state.agent.securities.transport = httpx.ASGITransport(app=stocks)
    price = 10
    async def history(*args):
            return {"status": "ok", "rows": [{"date": datetime.now(TZ).date().isoformat(), "open": price, "close": price}], "source": "isolated-test"}
    stocks.state.stock_service.get = history
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as bank, httpx.AsyncClient(transport=httpx.ASGITransport(app=stocks), base_url="http://127.0.0.1:8001") as market:
        await register(bank)
        await stock_login(market, "11111111111")
        code = (await bank.post("/api/securities/pairing-code", json={})).json()["code"]
        assert (await market.post("/api/securities/connect", json={"symbols": ["000001", "600519"], "code": code})).status_code == 200
        conv = (await bank.get("/api/bootstrap")).json()["conversation_id"]
        bridge = app.state.agent.securities
        before = (await bank.get("/api/bank")).json()
        quote = await bridge.query("quote_stock_purchase", {"orders": [{"symbol": "000001", "amount_yuan": "1250.50"}]}, conv)
        item = quote["items"][0]
        assert item["shares"] == 100 and item["amount_minor"] == 100000
        assert item["budget_minor"] == 125050 and item["remaining_minor"] == 25050
        assert "250.50" in quote["answer"]
        exact = await bridge.query("quote_stock_purchase", {"orders": [{"symbol": "000001", "amount_yuan": "2000"}]}, conv)
        assert exact["items"][0]["shares"] == 200 and exact["items"][0]["remaining_minor"] == 0
        with app.state.database.transaction() as db:
            db.execute("INSERT OR REPLACE INTO risk_profile VALUES (1, '{}', 4)")
            approval = app.state.agent.create_direct_approval(db, conv, "purchase_stock_basket", {"quote_id": quote["quote_id"]})
        aid = approval["data"]["approval_id"]
        context = (await bank.get(f"/api/approvals/{aid}/payment?conversation_id={conv}")).json()
        assert context["level"] == "red" and context["sms_required"]
        sms = (await bank.post(f"/api/approvals/{aid}/sms", json={"conversation_id": conv, "account_id": "daily"})).json()
        body = {"conversation_id": conv, "decision": "confirm", "account_id": "daily", "pin": "2468", "sms_code": sms["demo_code"]}
        assert (await bank.post(f"/api/approvals/{aid}/decision", json=body)).json()["status"] == "executed"
        after = (await bank.get("/api/bank")).json()
        balances = lambda value: next(a["balance_minor"] for a in value["accounts"] if a["id"] == "daily")
        assert balances(before) - balances(after) == 100000
        price = 1500
        with app.state.database.connection() as db:
            count = db.execute("SELECT count(*) FROM securities_quotes").fetchone()[0]
        with pytest.raises(BankError, match="至少需要.*150000.00"):
            await bridge.query("quote_stock_purchase", {"orders": [{"symbol": "600519", "amount_yuan": "1000"}]}, conv)
        await ask(app.state.agent, [[tool("quote_stock_purchase", orders=[{"symbol": "600519", "amount_yuan": "1000"}])], "预算不足一手，尚未扣款。"], "我想买1000块的贵州茅台")
        assert (await bank.get("/api/bank")).json() == after
        with app.state.database.connection() as db:
            assert db.execute("SELECT count(*) FROM securities_quotes").fetchone()[0] == count
        price = 10
        await ask(app.state.agent, [[tool("quote_stock_purchase", orders=[{"symbol": "000001", "amount_yuan": "1250.50"}])], "已报价，实际1000元，余款250.50元不扣。"], "买1250.50元平安银行")
        with app.state.database.connection() as db:
            assert db.execute("SELECT count(*) FROM securities_quotes").fetchone()[0] == count + 1
