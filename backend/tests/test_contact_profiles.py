import json

import httpx
import pytest
from banking.agent import model_safe_text
from banking.bank import BankError
from banking.main import create_app
from conftest import ask, pending, tool


async def test_optional_contact_fields_profile_and_restart(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        await client.get("/api/bootstrap")
        response = await client.post("/api/contacts", json={"name": "小林", "relationship": "朋友", "birthday": "2000-02-29", "gender": "女", "special_attention": True})
        assert response.status_code == 201
        person = response.json()
        assert person["accounts"] == [] and person["phones"] == []
        assert person["relationship"] == "朋友" and person["special_attention"]
        app.state.database.initialize()
        loaded = (await client.get(f"/api/contacts/{person['id']}")).json()
        assert loaded["phones"] == [] and loaded["accounts"] == [] and loaded["birthday"] == "2000-02-29"
        with app.state.database.connection() as db:
            assert model_safe_text(db, "普通查询") == "普通查询"
        updated = await client.patch(f"/api/contacts/{person['id']}/profile", json={"note": " 家人小林 ", "relationship": "家人", "birthday": None, "special_attention": False})
        assert updated.json()["relationship"] == "家人" and updated.json()["birthday"] is None
        assert updated.json()["note"] == "家人小林"
        assert (await client.get(f"/api/contacts/{person['id']}" )).json()["name"] == "小林"
        assert (await client.patch(f"/api/contacts/{person['id']}/profile", json={"note": ""})).json()["note"] == ""
        assert updated.json()["gender"] == "女" and not updated.json()["special_attention"]
        assert (await client.patch(f"/api/contacts/{person['id']}/profile", json={"birthday": "2001-02-29"})).status_code == 422
        assert (await client.patch(f"/api/contacts/{person['id']}/profile", json={"birthday": "2999-01-01"})).status_code == 422
        assert (await client.post("/api/contacts", json={"name": "小吴", "bank": "演示银行"})).status_code == 400
        assert (await client.post(f"/api/contacts/{person['id']}/details", json={"phone": "13800123456", "bank": "演示银行", "card_number": "6222000098765432"})).status_code == 200
        assert len((await client.get(f"/api/contacts/{person['id']}")).json()["accounts"]) == 1


async def test_ai_add_contact_restores_phone_and_waits_for_confirmation(agent):
    await ask(agent, [[tool("create_contact", name="小林", relationship="朋友", birthday="2000-03-04", gender="女", special_attention=True)]], "帮我添加朋友小林，手机号13800123456，生日2000年3月4日，女，特别关注")
    approval = pending(agent)
    assert json.loads(approval["args_json"])["phone"] == "13800123456"
    assert agent.bank.query("list_contacts", {"keyword": "小林"})["contacts"] == []
    agent.model.responses = ["联系人已添加。"]
    assert (await agent.confirm(approval["id"], "confirm"))["status"] == "executed"
    await agent.queue.join()
    person = agent.bank.query("list_contacts", {"keyword": "小林"})["contacts"][0]
    assert person["relationship"] == "朋友" and person["special_attention"]
    assert person["phone"].endswith("3456") and person["accounts"] == []
    assert (await agent.confirm(approval["id"], "confirm"))["status"] == "executed"
    assert len(agent.bank.query("list_contacts", {"keyword": "小林"})["contacts"]) == 1


async def test_ai_duplicate_cannot_guess_new_person_and_confirm_rechecks(agent):
    await ask(agent, [[tool("create_contact", name="张三", new_person=True)]], "添加联系人张三，他是我的朋友")
    with agent.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0
    await ask(agent, [[tool("create_contact", name="张三", new_person=True, relationship="朋友")]], "不是同一个人，另建联系人张三")
    approval = pending(agent)
    agent.model.responses = ["已添加。"]
    await agent.confirm(approval["id"], "confirm")
    await agent.queue.join()
    assert len(agent.bank.query("list_contacts", {"keyword": "张三"})["contacts"]) == 2
    agent.bank.prepare("create_contact", {"name": "小王"})
    with agent.db.transaction() as db:
        from banking.contacts import create_person

        create_person(db, {"name": "小王"})
        with pytest.raises(BankError, match="同名"):
            agent.bank.execute(db, "create_contact", {"name": "小王"}, "operation")
