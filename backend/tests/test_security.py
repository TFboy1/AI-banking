from face_test_support import bind_test_face, grant
import time

import httpx
import pytest
from banking.bank import BankError
from banking.main import create_app


async def register(client):
    await client.post("/api/auth/open", json={})
    sms = (await client.post("/api/auth/sms", json={"phone": "13800123456", "purpose": "register"})).json()
    response = await client.post("/api/auth/login", json={"phone": "13800123456", "purpose": "register", "username": "测试用户", "code": sms["demo_code"]})
    assert response.status_code == 200
    await client.post("/api/auth/contacts-done", json={})
    return await client.post("/api/auth/pin", json={"pin": "2468", "repeat_pin": "2468"})


async def test_registration_guide_and_reopen_lock(settings):
    settings.app_auth_enabled = True
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        assert (await client.get("/api/bootstrap")).status_code == 401
        state = (await client.post("/api/auth/open", json={})).json()
        assert state["lock_enabled"] and not state["registered"]
        sms = (await client.post("/api/auth/sms", json={"phone": "13800123456", "purpose": "register"})).json()
        assert sms["simulated"] and len(sms["demo_code"]) == 6
        login = {"phone": "13800123456", "purpose": "register", "username": "测试用户", "code": sms["demo_code"]}
        assert (await client.post("/api/auth/login", json=login)).status_code == 200
        assert (await client.post("/api/auth/login", json=login)).status_code == 400
        assert (await client.get("/api/bank")).status_code == 401
        assert (await client.post("/api/contacts", json={"name": "测试联系人", "phone": "13900123456", "bank": "测试银行", "card_number": "6222000012345678"})).status_code == 201
        await client.post("/api/auth/contacts-done", json={})
        assert (await client.post("/api/auth/pin", json={"pin": "2468", "repeat_pin": "2469"})).status_code == 400
        state = (await client.post("/api/auth/pin", json={"pin": "2468", "repeat_pin": "2468"})).json()
        assert state["ready"]
        assert (await client.get("/api/bootstrap")).status_code == 200
        with app.state.database.connection() as db:
            stored = db.execute("SELECT pin_hash FROM app_identity").fetchone()[0]
            assert stored != "2468" and ":" in stored
        assert not (await client.post("/api/auth/open", json={})).json()["ready"]
        assert (await client.get("/api/bank")).status_code == 401
        assert (await client.post("/api/auth/unlock", json={"pin": "0000"})).status_code == 400
        assert (await client.post("/api/auth/unlock", json={"pin": "2468"})).json()["ready"]


async def test_money_confirmation_requires_pin_even_when_app_lock_disabled(settings):
    settings.app_auth_enabled = True
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        await register(client)
        assert not (await client.post("/api/auth/lock-setting", json={"enabled": False, "pin": "2468"})).json()["lock_enabled"]
        boot = (await client.get("/api/bootstrap")).json()
        with app.state.database.transaction() as db:
            message = app.state.agent.create_direct_approval(db, boot["conversation_id"], "transfer", {"recipient": "zhang_san", "amount_yuan": "20", "account_id": "daily"})
        approval = message["data"]["approval_id"]
        args = {"decision": "confirm", "conversation_id": boot["conversation_id"]}
        path = f"/api/approvals/{approval}/decision"
        assert (await client.post(path, json=args)).status_code == 428
        assert (await client.post(path, json={**args, "pin": "0000"})).status_code == 400
        assert (await client.get("/api/bank")).json()["total_assets_minor"] == boot["bank"]["total_assets_minor"]
        with app.state.database.transaction() as db, pytest.raises(BankError, match="四位密码"):
            app.state.agent.confirm_in_transaction(db, approval, "confirm")
        assert (await client.post(path, json={**args, "pin": "2468"})).json()["status"] == "executed"
        after = (await client.get("/api/bank")).json()
        assert after["total_assets_minor"] == boot["bank"]["total_assets_minor"] - 2000
        assert (await client.post(path, json={**args, "pin": "2468"})).json()["status"] == "executed"
        assert (await client.get("/api/bank")).json()["total_assets_minor"] == after["total_assets_minor"]


async def test_sms_login_and_pin_failure_lockout(settings):
    settings.app_auth_enabled = True
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        await register(client)
        for _ in range(5):
            assert (await client.post("/api/auth/unlock", json={"pin": "0000"})).status_code == 400
        assert (await client.post("/api/auth/unlock", json={"pin": "2468"})).status_code == 400
        with app.state.database.transaction() as db:
            assert db.execute("SELECT failures FROM app_identity").fetchone()[0] == 5
            db.execute("UPDATE app_identity SET blocked_until=0")
            db.execute("UPDATE safety_circuits SET blocked_until=? WHERE scope='transactions'", (time.time()-1,))
        assert (await client.post("/api/auth/unlock", json={"pin": "2468"})).status_code == 200
        await client.post("/api/auth/logout", json={})
        assert (await client.get("/api/investments")).status_code == 401
        assert (await client.post("/api/auth/sms", json={"phone": "13900000000", "purpose": "login"})).status_code == 400
        sms = (await client.post("/api/auth/sms", json={"phone": "13800123456", "purpose": "login"})).json()
        assert (await client.post("/api/auth/sms", json={"phone": "13800123456", "purpose": "login"})).status_code == 400
        result = await client.post("/api/auth/login", json={"phone": "13800123456", "purpose": "login", "code": sms["demo_code"]})
        assert result.json()["ready"]
        assert (await client.post("/api/auth/pin", json={"pin": "1234", "repeat_pin": "1234"})).status_code == 400
        change={"pin":"1234","repeat_pin":"1234","current_pin":"2468"}
        bind_test_face(app)
        assert (await client.post('/api/auth/pin',json=change)).status_code==400
        sms=(await client.post('/api/auth/pin/sms',json=change)).json()['demo_code']
        assert (await client.post('/api/auth/pin',json={**change,'sms_code':sms})).status_code==400
        bind_test_face(app)
        # Binding changes revoke old SMS; this fixture refreshes that credential.
        with app.state.database.transaction() as db: db.execute('UPDATE pin_change_sms SET sent=0')
        sms=(await client.post('/api/auth/pin/sms',json=change)).json()['demo_code']
        assert (await client.post('/api/auth/pin',json={**change,'sms_code':sms,'face_token':grant(app,client,purpose='pin_change')})).status_code==200
        assert (await client.post("/api/auth/unlock", json={"pin": "2468"})).status_code == 400
        assert (await client.post("/api/auth/unlock", json={"pin": "1234"})).status_code == 200


async def test_sms_wrong_attempts_and_expiry(settings):
    settings.app_auth_enabled = True
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        await client.post("/api/auth/open", json={})
        sms = (await client.post("/api/auth/sms", json={"phone": "13800123456", "purpose": "register"})).json()
        wrong = "000000" if sms["demo_code"] != "000000" else "111111"
        login = {"phone": "13800123456", "purpose": "register", "username": "用户", "code": wrong}
        for _ in range(5):
            assert (await client.post("/api/auth/login", json=login)).status_code == 400
        assert (await client.post("/api/auth/login", json={**login, "code": sms["demo_code"]})).status_code == 400
        with app.state.database.transaction() as db:
            db.execute("UPDATE sms_challenges SET attempts=0,expires_at=?", (time.time() - 1,))
        assert (await client.post("/api/auth/login", json={**login, "code": sms["demo_code"]})).status_code == 400
