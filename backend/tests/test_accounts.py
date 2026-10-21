import httpx
from banking.db import Database
from banking.main import create_app
from banking.security import encode_pin, token_hash


async def sms_login(client, phone, username=None):
    await client.post("/api/auth/open", json={})
    purpose = "register" if username else "login"
    sms = await client.post("/api/auth/sms", json={"phone": phone, "purpose": purpose})
    assert sms.status_code == 200, sms.text
    response = await client.post("/api/auth/login", json={"phone": phone, "purpose": purpose, "username": username or "", "code": sms.json()["demo_code"]})
    assert response.status_code == 200, response.text
    return response.json()


async def test_multiuser_blank_data_and_persistence(settings):
    settings.app_auth_enabled = True
    settings.multi_user_accounts = True
    legacy = Database(settings.database_path)
    legacy.initialize()
    with legacy.transaction() as db:
        db.execute("INSERT INTO app_identity(id,username,phone,pin_hash,contacts_done) VALUES (1,?,?,?,1)", ("演示文档", "11111111111", encode_pin("0000")))
    with legacy.connection() as db:
        before = [dict(row) for row in db.execute("SELECT * FROM accounts")]
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as demo, httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as new:
        state = await sms_login(demo, "11111111111")
        assert state["username"] == "演示文档" and state["ready"]
        first = (await demo.get("/api/bootstrap")).json()
        assert first["bank"]["accounts"] == before
        assert len((await demo.get("/api/contacts")).json()) == 8
        await sms_login(new, "13800123456", "新用户")
        assert (await new.get("/api/bank")).status_code == 401
        assert (await new.get("/api/contacts")).json() == []
        await new.post("/api/auth/contacts-done", json={})
        assert (await new.post("/api/auth/pin", json={"pin": "1234", "repeat_pin": "1234"})).json()["ready"]
        blank = (await new.get("/api/bootstrap")).json()
        for field in ("accounts", "cards", "holdings", "subscriptions", "transactions"):
            assert blank["bank"][field] == []
        assert blank["bank"]["risk_level"] is None
        assert (await new.get(f"/api/bootstrap?conversation_id={first['conversation_id']}")).status_code == 404
        # Birthday plans and notices use the same tenant boundary as the wallet.
        assert (await new.get('/api/birthday-plans')).json()['items']==[]
        assert (await new.get('/api/birthday-plans/notices')).json()['items']==[]
        assert (await new.post('/api/birthday-plans/foreign-plan/seen',json={})).status_code in (400,404)
        contact = (await new.post("/api/contacts", json={"name": "测试联系人", "phone": "13900123456", "bank": "测试银行", "card_number": "6222000012345678"})).json()
        assert contact["id"]
        assert len((await demo.get("/api/contacts")).json()) == 8
        assert (await new.post("/api/auth/lock-setting", json={"enabled": False, "pin": "0000"})).status_code == 400
        assert (await new.post("/api/auth/lock-setting", json={"enabled": False, "pin": "1234"})).status_code == 200
        cookies = dict(new.cookies)
        assert (await demo.get("/api/auth/status")).json()["lock_enabled"]
    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted), httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://127.0.0.1:8000", cookies=cookies) as new:
        state = (await new.post("/api/auth/open", json={})).json()
        assert state["ready"] and state["username"] == "新用户" and not state["lock_enabled"]
        assert len((await new.get("/api/contacts")).json()) == 1
        bank = (await new.get("/api/bank")).json()
        assert bank["accounts"] == [] and bank["subscriptions"] == []


async def test_switch_account_revokes_old_session_and_blocks_cross_confirmation(settings):
    settings.app_auth_enabled = True
    settings.multi_user_accounts = True
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        await sms_login(client, "13800123456", "用户一")
        await client.post("/api/auth/contacts-done", json={})
        await client.post("/api/auth/pin", json={"pin": "1234", "repeat_pin": "1234"})
        first = (await client.get("/api/bootstrap")).json()
        user_one = app.state.directory.user_for(token_hash(client.cookies["bank_session"]))
        tenant = await app.state.directory.application(user_one)
        with tenant.state.database.transaction() as db:
            approval = tenant.state.agent.create_direct_approval(db, first["conversation_id"], "save_risk_profile", {"horizon": "short", "loss_tolerance": "none", "experience": "none"})
        await client.post("/api/auth/logout", json={})
        with app.state.directory.connection() as db:
            db.execute("DELETE FROM sms")
        await sms_login(client, "13900123456", "用户二")
        await client.post("/api/auth/contacts-done", json={})
        await client.post("/api/auth/pin", json={"pin": "4321", "repeat_pin": "4321"})
        second = (await client.get("/api/bootstrap")).json()
        assert second["conversation_id"] != first["conversation_id"]
        result = await client.post(f"/api/approvals/{approval['data']['approval_id']}/decision", json={"decision": "confirm", "conversation_id": second["conversation_id"], "pin": "4321"})
        assert result.status_code == 400
        with tenant.state.database.connection() as db:
            assert db.execute("SELECT status FROM approvals WHERE id=?", (approval["data"]["approval_id"],)).fetchone()[0] == "pending"
        assert not tenant.state.auth_status(token_hash(client.cookies["bank_session"]))["ready"]
