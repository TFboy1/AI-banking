import httpx
from banking.main import create_app
from test_accounts import sms_login


async def test_username_is_saved_for_only_the_authenticated_account(settings):
    settings.app_auth_enabled = True
    settings.multi_user_accounts = True
    app = create_app(settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as first, httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as second:
        assert (await first.patch("/api/auth/profile", json={"username": "越权"})).status_code == 401
        for client, phone, name in ((first, "13800123456", "用户一"), (second, "13900123456", "用户二")):
            await sms_login(client, phone, name)
            await client.post("/api/auth/contacts-done", json={})
            await client.post("/api/auth/pin", json={"pin": "1234", "repeat_pin": "1234"})
        before = (await first.get("/api/bank")).json()
        for name in ("", "a" * 41):
            assert (await first.patch("/api/auth/profile", json={"username": name})).status_code == 422
        assert (await first.patch("/api/auth/profile", json={"username": "   "})).status_code == 400
        assert (await first.patch("/api/auth/profile", json={"username": "侵入", "user_id": "other"})).status_code == 422
        renamed = (await first.patch("/api/auth/profile", json={"username": "  新用户名  "})).json()
        assert renamed["username"] == "新用户名" and renamed["phone_masked"] == "138****3456"
        assert (await second.get("/api/auth/status")).json()["username"] == "用户二"
        assert (await first.get("/api/bank")).json() == before
        with app.state.directory.connection() as db:
            assert db.execute("SELECT username FROM users WHERE phone='13800123456'").fetchone()[0] == "新用户名"
        cookies = dict(first.cookies)
    restarted = create_app(settings)
    async with restarted.router.lifespan_context(restarted), httpx.AsyncClient(transport=httpx.ASGITransport(app=restarted), base_url="http://127.0.0.1:8000", cookies=cookies) as first:
        await first.post("/api/auth/open", json={})
        assert (await first.patch("/api/auth/profile", json={"username": "锁定时修改"})).status_code == 401
        state = (await first.post("/api/auth/unlock", json={"pin": "1234"})).json()
        assert state["ready"] and state["username"] == "新用户名"
