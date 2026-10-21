import asyncio
import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .account_categories import register_categories
from .account_transfers import register_account_transfers
from .agent import Agent, BusyError
from .bank import DEBIT_TOOLS, BankError, ContactProfile, CreateContact
from .cards import register_cards
from .config import ROOT, Settings
from .connections import register_connections
from .contacts import add_details, contact, create_person, person_key, update_profile
from .db import TZ, Database, add_message, dumps, event, message_dict, now
from .travel_bridge import register_bank_travel
from .food_bridge import register_bank_food
from .market import market_loop, register_market
from .payment_security import policy, risk, send_sms
from .planning import PlanningService
from .securities_bridge import register_bank_securities
from .securities_common import StockOrder
from .security import MONEY_TOOLS, register_auth, verify_pin


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatBody(Body):
    text: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=100)


class DecisionBody(Body):
    face_token: str | None = Field(default=None,max_length=150)
    execute_at: str | None = Field(default=None,max_length=64)
    decision: Literal["confirm", "cancel"]
    conversation_id: str | None = Field(default=None, max_length=100)
    pin: str | None = Field(default=None, pattern=r"^[0-9]{4}$")
    account_id: str | None = Field(default=None, min_length=1, max_length=100)
    sms_code: str | None = Field(default=None, pattern=r"^[0-9]{6}$")


class PaymentSMSBody(Body):
    execute_at: str | None = Field(default=None,max_length=64)
    conversation_id: str = Field(min_length=1, max_length=100)
    account_id: str | None = Field(default=None, min_length=1, max_length=100)


class ConversationBodyForInterrupt(Body):
    conversation_id: str = Field(min_length=1, max_length=100)


class PartialChoiceBody(ConversationBodyForInterrupt):
    action:Literal['keep','replan','cancel_order']
    version:int=Field(ge=1)
    order_id:str|None=Field(default=None,max_length=100)


class PaymentPolicyBody(Body):
    threshold_yuan: str = Field(pattern=r"^(0|[1-9]\d{0,8})(\.\d{1,2})?$")
    pin: str = Field(pattern=r"^[0-9]{4}$")


class ConversationBody(Body):
    title: str = Field(default="新会话", min_length=1, max_length=60)


class InvestmentActionBody(Body):
    conversation_id: str = Field(min_length=1, max_length=100)
    tool: Literal["save_risk_profile", "purchase_product", "redeem_product"]
    arguments: dict


class StockSellBody(StockOrder):
    conversation_id: str = Field(min_length=1, max_length=100)
    account_id: str = Field(min_length=1, max_length=100)


class ContactCreateBody(CreateContact):
    pass


class ContactDetailsBody(Body):
    phone: str | None = Field(default=None, pattern=r"^1[3-9]\d{9}$")
    bank: str | None = Field(default=None, min_length=1, max_length=60)
    card_number: str | None = Field(default=None, pattern=r"^\d{12,19}$")


class ContactNameBody(Body):
    name: str = Field(min_length=1, max_length=60)


class ContactNoteBody(Body):
    note: str = Field(max_length=120)


class ResetBody(Body):
    confirmation: Literal["RESET"]


class InspectBody(Body):
    tool: Literal["get_accounts", "analyze_bills", "list_products", "list_cards", "list_subscriptions"]
    conversation_id: str | None = Field(default=None, max_length=100)


class PlanPreviewBody(Body):
    conversation_id: str | None = Field(default=None, max_length=100)
    kind: Literal["purchase", "savings"]
    title: str = Field(default="资金安排", min_length=1, max_length=80)
    action_label: str = Field(default="", max_length=80)
    amount_minor: int = Field(gt=0, le=100_000_000)
    living_reserve_minor: int = Field(default=0, ge=0, le=100_000_000)
    action_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    deadline: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    account_id: str = Field(default="daily", max_length=100)
    salary_amount_minor: int | None = Field(default=None, gt=0, le=100_000_000)
    salary_day: int | None = Field(default=None, ge=1, le=28)
    rent_amount_minor: int | None = Field(default=None, gt=0, le=100_000_000)
    rent_day: int | None = Field(default=None, ge=1, le=28)
    plan_id: str | None = Field(default=None, max_length=100)


class PlanActionBody(Body):
    conversation_id: str | None = Field(default=None, max_length=100)
    version: int = Field(ge=1)
    action: Literal[
        "challenge_salary",
        "challenge_expense",
        "apply_repair",
        "activate",
        "execute",
        "create_rule",
        "adopt_trigger",
    ]
    variant_key: Literal["now", "after_salary"] = "now"
    expense_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    cadence: Literal["once", "monthly"] = "once"
    reserve_minor: int = Field(default=0, ge=0, le=100_000_000)
    notification_id: str | None = Field(default=None, max_length=100)


class SandboxEventBody(Body):
    kind: Literal["salary"]
    amount_minor: int = Field(gt=0, le=100_000_000)
    account_id: str = Field(default="daily", max_length=100)
    source_key: str = Field(default="salary", min_length=1, max_length=80)
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    event_key: str | None = Field(default=None, max_length=200)
    occurred_at: str | None = Field(default=None, max_length=50)


class DemoScenarioBody(Body):
    confirmation: Literal["LOAD_DEMO"]


class FeishuBody(Body):
    event_id: str = Field(min_length=1, max_length=200)
    kind: Literal["message", "card"]
    tenant_key: str = Field(min_length=1, max_length=200)
    open_id: str = Field(min_length=1, max_length=200)
    chat_id: str = Field(min_length=1, max_length=200)
    chat_type: Literal["p2p"]
    text: str = Field(default="", max_length=4000)
    approval_id: str | None = None
    decision: Literal["confirm", "cancel"] | None = None
    action: Literal['stop','resume','partial','human_end','rating','focus'] | None = None
    task_id: str | None = Field(default=None,max_length=100)
    conversation_id: str | None = Field(default=None,max_length=100)
    message_id: str | None = Field(default=None,max_length=100)
    choice: Literal['keep','replan','cancel_order'] | None = None
    version: int | None = Field(default=None,ge=1)
    order_id: str | None = Field(default=None,max_length=100)
    half_stars: int | None = Field(default=None,strict=True,ge=1,le=10)


class FeishuPreferencesBody(Body):
    notifications: bool = Field(strict=True)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _create_single_app(settings: Settings | None = None, model=None):
    settings = settings or Settings()
    database = Database(settings.database_path, seed_demo=settings.seed_demo_data)
    agent = Agent(database, settings, model)
    planning = PlanningService(database)

    @asynccontextmanager
    async def lifespan(app):
        database.initialize()
        from .face import configure as configure_face
        configure_face(database,settings.face_enabled)
        with database.transaction() as db:
            if db.execute('SELECT 1 FROM feishu_binding WHERE id=1').fetchone():
                from .feishu_actions import dedicated_conversation
                dedicated_conversation(database, db)
        await agent.start()
        from .scheduled_transfers import loop as scheduled_loop
        scheduled_task=asyncio.create_task(scheduled_loop(database,agent.notify,settings)) if settings.scheduled_worker_enabled else None
        from .daily_reports import loop as daily_report_loop
        daily_task=asyncio.create_task(daily_report_loop(database,settings,agent.notify)) if settings.daily_report_worker_enabled else None
        from .birthdays import loop as birthday_loop
        birthday_task=asyncio.create_task(birthday_loop(database,settings,agent.notify)) if settings.birthday_worker_enabled else None
        reminder_task = asyncio.create_task(planning.run_reminder_loop(agent.changed))
        price_task = asyncio.create_task(market_loop(database, agent.notify))
        sender = None
        if settings.feishu_ready:
            from .feishu import OutboxSender

            sender = asyncio.create_task(OutboxSender(database, settings, agent.changed).run())
        try:
            yield
        finally:
            if birthday_task:
                birthday_task.cancel()
                await asyncio.gather(birthday_task,return_exceptions=True)
            if daily_task:
                daily_task.cancel()
                await asyncio.gather(daily_task,return_exceptions=True)
            if scheduled_task:
                scheduled_task.cancel()
                await asyncio.gather(scheduled_task,return_exceptions=True)
            price_task.cancel()
            await asyncio.gather(price_task, return_exceptions=True)
            reminder_task.cancel()
            try:
                await reminder_task
            except asyncio.CancelledError:
                pass
            if sender:
                sender.cancel()
                try:
                    await sender
                except asyncio.CancelledError:
                    pass
            await agent.stop()

    app = FastAPI(title="有数 · AI Banking 沙箱", version="0.1.0", lifespan=lifespan)
    app.state.agent = agent
    app.state.database = database
    app.state.settings = settings
    app.state.planning = planning
    register_auth(app, database, settings)

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            allowed = {
                "http://127.0.0.1:5173",
                "http://localhost:5173",
                "http://127.0.0.1:8000",
                "http://localhost:8000",
                "https://093c-2406-da18-1f2f-d00-e27a-9a35-fc21-c012.ngrok-free.app",
            }
            if settings.feishu_public_url:
                from .feishu_payment import public_origin
                allowed.add(public_origin(settings))
            if origin and origin not in allowed:
                return JSONResponse({"detail": "不接受来自其他站点的请求。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def session(request: Request):
        token = request.cookies.get("bank_session", "")
        with database.connection() as db:
            valid = db.execute("SELECT 1 FROM sessions WHERE token_hash=?", (hash_token(token),)).fetchone()
        if not valid:
            raise HTTPException(401, "会话已失效，请刷新页面。")
        return hash_token(token)

    from .speech import register_speech
    register_speech(app, database, settings, session)

    def resolve_conversation(db, session_key, requested=None):
        if requested is not None:
            row = db.execute(
                "SELECT id FROM conversations WHERE id=? AND deleted_at IS NULL", (requested,)
            ).fetchone()
            if not row:
                raise HTTPException(404, "会话不存在或已删除。")
            return row["id"]
        row = db.execute(
            """SELECT c.id FROM sessions s JOIN conversations c ON c.id=s.active_conversation_id
            WHERE s.token_hash=? AND c.deleted_at IS NULL""",
            (session_key,),
        ).fetchone()
        return row["id"] if row else database.conversation_id(db)

    from .face import register as register_face
    register_face(app,database,settings,session,resolve_conversation)

    def bridge(request: Request):
        expected = "Bearer " + settings.feishu_bridge_token
        supplied = request.headers.get("Authorization", "")
        if not settings.feishu_ready or not secrets.compare_digest(supplied, expected):
            raise HTTPException(403, "飞书桥接未启用或凭证不正确。")

    register_categories(app, database, agent, settings, session, resolve_conversation)
    register_account_transfers(app, database, agent, session, resolve_conversation)
    register_cards(app, database, agent, settings, session, resolve_conversation)
    register_market(app, database, session)
    register_bank_securities(app, database, agent, settings, session)
    register_bank_food(app, database, agent, settings, session)
    register_bank_travel(app, database, agent, settings, session)
    register_connections(app, database, settings, session)
    from .handoff import register as register_handoff
    register_handoff(app, database, agent, session, resolve_conversation)
    from .scheduled_transfers import register as register_scheduled
    register_scheduled(app,database,agent,session,resolve_conversation)
    from .daily_reports import register as register_daily_reports
    register_daily_reports(app,database,agent,session)
    from .birthdays import register as register_birthdays
    register_birthdays(app,database,agent,session,resolve_conversation)
    from .safety_api import register as register_safety
    register_safety(app, database, agent, session)
    from .account_lock import register as register_account_lock
    register_account_lock(app, database, session)

    @app.get("/api/investments", dependencies=[Depends(session)])
    def investment_account():
        return {
            **agent.bank.query("list_products", {}),
            "questionnaire": agent.bank.query("get_risk_questionnaire", {}),
        }

    @app.post('/api/subscriptions/{subscription_id}/cancel')
    async def cancel_subscription(subscription_id: str, body: PaymentSMSBody, session_key: str = Depends(session)):
        with database.transaction() as db:
            current = resolve_conversation(db, session_key, body.conversation_id)
            message = agent.create_direct_approval(db, current, 'cancel_subscription', {'subscription_id':subscription_id})
        await agent.notify()
        return message

    @app.post('/api/securities/sell')
    async def sell_stock(body: StockSellBody, session_key: str = Depends(session)):
        with database.connection() as db:
            current = resolve_conversation(db, session_key, body.conversation_id)
        quote = await agent.securities.query('quote_stock_sale', {'orders': [{'symbol': body.symbol, 'shares': body.shares}]}, current)
        if not quote.get('quote_id'):
            raise BankError(quote.get('answer', '无法取得卖出报价。'))
        with database.transaction() as db:
            message = agent.create_direct_approval(db, current, 'sell_stock_basket', {'quote_id': quote['quote_id'], 'account_id': body.account_id})
        await agent.notify()
        return message

    @app.post("/api/investments/approvals")
    async def investment_approval(body: InvestmentActionBody, session_key: str = Depends(session)):
        with database.transaction() as db:
            conversation_id = resolve_conversation(db, session_key, body.conversation_id)
            message = agent.create_direct_approval(db, conversation_id, body.tool, body.arguments)
        await agent.notify()
        return message

    @app.exception_handler(BankError)
    async def bank_error_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409 if isinstance(exc, BusyError) else 400)

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "model_configured": settings.model_ready,
            "feishu_enabled": settings.feishu_ready,
        }

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request, response: Response, conversation_id: str | None = None):
        token = request.cookies.get("bank_session", "")
        with database.transaction() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE token_hash=?", (hash_token(token),)).fetchone():
                token = secrets.token_urlsafe(32)
                db.execute(
                    "INSERT INTO sessions(token_hash,created_at) VALUES (?,?)", (hash_token(token), now())
                )
            conversation_id = resolve_conversation(db, hash_token(token), conversation_id)
            db.execute(
                "UPDATE sessions SET active_conversation_id=? WHERE token_hash=?",
                (conversation_id, hash_token(token)),
            )
            from .interruption import repair_saved_reports
            repair_saved_reports(db, conversation_id)
            from .partial_completion import refresh as refresh_partial
            refresh_partial(agent,db,conversation_id)
            messages = [
                message_dict(row)
                for row in db.execute(
                    "SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid", (conversation_id,)
                )
            ]
            db.execute(
                "UPDATE notifications SET read_at=COALESCE(read_at,?) WHERE conversation_id=?",
                (now(), conversation_id),
            )
            active = agent.active(db, conversation_id)
            event_id = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM events WHERE conversation_id IN (?, '*')", (conversation_id,)
            ).fetchone()[0]
            binding = bool(db.execute("SELECT 1 FROM feishu_binding").fetchone())
            conversations = database.conversations(db)
            from .task_dag import latest
            latest_task_plan = latest(db, conversation_id)
            from .human_service import current as human_current
            human_session = human_current(db, conversation_id)
        response.set_cookie(
            "bank_session",
            token,
            httponly=True,
            samesite="strict",
            max_age=30 * 24 * 3600,
            secure=request.url.scheme == "https",
        )
        return {
            "conversation_id": conversation_id,
            "conversations": conversations,
            "messages": messages,
            "event_id": event_id,
            "active_task": dict(active) if active else None,
            "latest_task_plan": latest_task_plan,
            "human_session": human_session,
            "bank": (await agent.securities.snapshot()),
            "config": {
                "model_configured": settings.model_ready,
                "model_name": settings.llm_model,
                "feishu_enabled": settings.feishu_ready,
                "feishu_bound": binding,
            },
        }

    @app.get("/api/contacts", dependencies=[Depends(session)])
    def list_contacts():
        with database.connection() as db:
            return [contact(db, row["id"]) for row in db.execute("SELECT person_id AS id FROM contact_links GROUP BY person_id ORDER BY MIN(rowid)")]

    @app.post("/api/contacts/check-name", dependencies=[Depends(session)])
    def check_contact_name(body: ContactNameBody):
        with database.connection() as db:
            return [contact(db, row[0]) for row in db.execute("SELECT DISTINCT l.person_id FROM contacts c JOIN contact_links l ON l.contact_id=c.id WHERE c.name=?", (body.name.strip(),))]

    @app.post("/api/contacts", status_code=201, dependencies=[Depends(session)])
    async def create_contact(body: ContactCreateBody):
        with database.transaction() as db:
            result = create_person(db, body.model_dump())
            event(db, "*", "contacts.updated", {})
        await agent.notify()
        return result

    @app.patch("/api/contacts/{contact_id}/profile", dependencies=[Depends(session)])
    async def save_contact_profile(contact_id: str, body: ContactProfile):
        with database.transaction() as db:
            result = update_profile(db, contact_id, body.model_dump(exclude_unset=True))
            event(db, "*", "contacts.updated", {})
        await agent.notify()
        return result

    @app.post("/api/contacts/{contact_id}/details", dependencies=[Depends(session)])
    async def supplement_contact(contact_id: str, body: ContactDetailsBody):
        with database.transaction() as db:
            result = add_details(db, contact_id, body.phone, body.bank.strip() if body.bank else None, body.card_number)
            event(db, "*", "contacts.updated", {})
        await agent.notify()
        return result

    @app.get("/api/contacts/{contact_id}", dependencies=[Depends(session)])
    def contact_detail(contact_id: str):
        with database.connection() as db:
            return contact(db, contact_id, reveal=True)

    @app.patch("/api/contacts/{contact_id}/note", dependencies=[Depends(session)])
    async def save_contact_note(contact_id: str, body: ContactNoteBody):
        with database.transaction() as db:
            contact(db, contact_id)
            db.execute("UPDATE contact_details SET note=? WHERE contact_id IN (SELECT contact_id FROM contact_links WHERE person_id=?)", (body.note.strip(), person_key(db, contact_id)))
            result = contact(db, contact_id)
            event(db, "*", "contacts.updated", {})
        await agent.notify()
        return result

    @app.post("/api/contacts/{contact_id}/conversation", status_code=201)
    async def contact_conversation(contact_id: str, session_key: str = Depends(session)):
        with database.transaction() as db:
            person = contact(db, contact_id)
            label = person["note"] or person["name"]
            conversation_id = database.create_conversation(db, f"联系人 · {label}"[:60])
            db.execute("INSERT INTO conversation_contacts VALUES (?,?)", (conversation_id, person["id"]))
            db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
            add_message(db, conversation_id, None, "assistant", "text",
                        f"已选择联系人：{label}（原名：{person['name']}，手机尾号 {person['phone'][-4:]}，银行卡账户 {len(person['accounts'])} 个）。你想对这位联系人进行什么操作？", {"source": "contact_selected", "contact_id": person["id"]})
            db.execute("UPDATE sessions SET active_conversation_id=? WHERE token_hash=?", (conversation_id, session_key))
            event(db, "*", "conversations.updated", {"conversation_id": conversation_id})
        await agent.notify()
        return {"conversation_id": conversation_id}

    @app.get("/api/conversations", dependencies=[Depends(session)])
    def list_conversations():
        with database.connection() as db:
            return database.conversations(db)

    @app.get("/api/plans", dependencies=[Depends(session)])
    def list_plans():
        with database.connection() as db:
            return planning.list_plans(db)

    @app.get("/api/plans/{plan_id}", dependencies=[Depends(session)])
    def get_plan(plan_id: str):
        with database.connection() as db:
            return planning.get_plan(db, plan_id)

    @app.post("/api/plans/preview", status_code=201)
    async def preview_plan(body: PlanPreviewBody, session_key: str = Depends(session)):
        with database.transaction() as db:
            conversation_id = resolve_conversation(db, session_key, body.conversation_id)
            result = planning.preview(db, conversation_id, body.model_dump(exclude={"conversation_id"}, exclude_none=True))
            message = add_message(
                db,
                conversation_id,
                None,
                "assistant",
                "plan",
                "我按当前账务生成了两种执行时点的预演。",
                {"card_type": "preview", "plan": result},
            )
        await agent.notify()
        return message

    @app.post("/api/plans/{plan_id}/actions")
    async def plan_action(plan_id: str, body: PlanActionBody, session_key: str = Depends(session)):
        with database.transaction() as db:
            conversation_id = resolve_conversation(db, session_key, body.conversation_id)
            plan = planning.get_plan(db, plan_id)
            owner = db.execute("SELECT conversation_id FROM plans WHERE id=?", (plan_id,)).fetchone()
            if not owner:
                raise BankError("找不到这份资金计划。")
            if body.action.startswith("challenge"):
                dimension = "salary_delay" if body.action == "challenge_salary" else "unexpected_expense"
                result = planning.challenge(db, plan_id, body.version, dimension, body.expense_date)
                message = add_message(
                    db,
                    conversation_id,
                    None,
                    "assistant",
                    "plan",
                    "我找到了这份方案最先失效的边界，并重新验证了修复建议。",
                    {"card_type": "challenge", **result},
                )
            elif body.action == "apply_repair":
                repair = planning.challenge(db, plan_id, body.version, "salary_delay")["challenge"].get(
                    "repair"
                )
                if not repair:
                    raise BankError("当前挑战结果没有可直接采用的日期修复。")
                version_row = db.execute(
                    "SELECT input_json FROM plan_versions WHERE plan_id=? AND version=?",
                    (plan_id, body.version),
                ).fetchone()
                payload = json.loads(version_row["input_json"])
                payload["plan_id"] = plan_id
                payload["action_date"] = repair["date"]
                result = planning.preview(db, conversation_id, payload)
                message = add_message(
                    db,
                    conversation_id,
                    None,
                    "assistant",
                    "plan",
                    "我已把修复建议保存为新的计划版本，并重新验证了现金流。",
                    {"card_type": "preview", "plan": result},
                )
            elif body.action == "activate":
                message = agent.create_direct_approval(
                    db,
                    conversation_id,
                    "activate_plan",
                    {"plan_id": plan_id, "plan_version": body.version, "variant_key": body.variant_key},
                    "采用计划只会保存安排与预留，具体扣款仍需另行确认。",
                )
            elif body.action == "execute":
                version_row = db.execute(
                    "SELECT input_json,result_json FROM plan_versions WHERE plan_id=? AND version=?",
                    (plan_id, body.version),
                ).fetchone()
                if not version_row or plan["status"] != "active" or plan["version"] != body.version:
                    raise BankError("请先确认采用这份计划的当前版本。")
                payload = json.loads(version_row["input_json"])
                result = json.loads(version_row["result_json"])
                variant = next((item for item in result["variants"] if item["key"] == body.variant_key), None)
                if not variant:
                    raise BankError("找不到要执行的方案。")
                if body.variant_key == "after_salary":
                    period = variant["action_date"][:7]
                    arrived = db.execute(
                        "SELECT 1 FROM bank_events WHERE kind='salary' AND period=?", (period,)
                    ).fetchone()
                    if not arrived:
                        raise BankError("工资尚未实际到账；计划会保留，到账事件触发后再生成执行确认。")
                if payload["kind"] == "purchase":
                    message = agent.create_direct_approval(
                        db,
                        conversation_id,
                        "simulate_expense",
                        {
                            "merchant": payload.get("action_label") or "电子产品商店",
                            "amount_yuan": f"{payload['amount_minor'] / 100:.2f}",
                            "category": "购物",
                            "account_id": payload["account_id"],
                            "plan_id": plan_id,
                            "plan_version": body.version,
                        },
                    )
                else:
                    installment = None
                    for step in result.get("installments", []):
                        if step["date"] <= PlanningService.business_date(db).isoformat():
                            installment = step["amount_minor"]
                            break
                    if result.get("installments") and installment is None:
                        raise BankError("下一期储蓄尚未到执行日，计划会保留到期。")
                    message = agent.create_direct_approval(
                        db,
                        conversation_id,
                        "save_to_goal",
                        {
                            "total_yuan": f"{payload['amount_minor'] / 100:.2f}",
                            "installment_yuan": f"{installment / 100:.2f}" if installment else None,
                            "plan_id": plan_id,
                            "account_id": payload["account_id"],
                        },
                    )
            else:
                if body.action == "adopt_trigger":
                    notice = db.execute("SELECT * FROM notifications WHERE id=?", (body.notification_id,)).fetchone()
                    if not notice:
                        raise BankError("工资到账建议不存在或已处理。")
                    data = json.loads(notice["data_json"])
                    if notice["conversation_id"] != conversation_id:
                        raise BankError("该建议属于其他会话，请切换到来源会话后采纳。")
                    amount = data.get("amount_minor", 0)
                    if amount <= 0:
                        raise BankError("当前没有可转存金额，已安排资金需要优先保留。")
                    message = agent.create_direct_approval(
                        db,
                        conversation_id,
                        "save_to_goal",
                        {"total_yuan": f"{amount / 100:.2f}", "account_id": "daily"},
                        "工资到账后的储蓄建议待确认。",
                    )
                    db.execute("UPDATE notifications SET read_at=? WHERE id=?", (now(), body.notification_id))
                else:
                    message = agent.create_direct_approval(
                        db,
                        conversation_id,
                        "create_trigger_rule",
                        {
                            "reserve_yuan": f"{body.reserve_minor / 100:.2f}",
                            "cadence": body.cadence,
                            "source_key": "salary",
                            "account_id": "daily",
                            "plan_id": plan_id,
                        },
                        "确认后只登记提醒规则，工资到账时会重新计算，不会自动转账。",
                    )
        await agent.notify()
        return message

    @app.post("/api/sandbox/events")
    async def sandbox_event(body: SandboxEventBody, session_key: str = Depends(session)):
        raise HTTPException(410, "模拟到账功能已移除。")

    @app.post("/api/sandbox/scenario")
    async def demo_scenario(body: DemoScenarioBody, session_key: str = Depends(session)):
        raise HTTPException(410, "模拟到账演示场景已移除。")

    @app.post("/api/notifications/{notification_id}/read")
    async def read_notification(notification_id: str, session_key: str = Depends(session)):
        with database.transaction() as db:
            row = db.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
            if not row:
                raise HTTPException(404, "通知不存在。")
            resolve_conversation(db, session_key, row["conversation_id"])
            db.execute("UPDATE notifications SET read_at=? WHERE id=?", (now(), notification_id))
            event(db, "*", "conversations.updated", {"conversation_id": row["conversation_id"]})
        await agent.notify()
        return {"status": "read"}

    @app.post("/api/notifications/{notification_id}/adopt")
    async def adopt_notification(notification_id: str, session_key: str = Depends(session)):
        with database.transaction() as db:
            notice = db.execute("SELECT * FROM notifications WHERE id=?", (notification_id,)).fetchone()
            if not notice:
                raise HTTPException(404, "通知不存在。")
            conversation_id = resolve_conversation(db, session_key, notice["conversation_id"])
            payload = json.loads(notice["data_json"])
            amount = int(payload.get("amount_minor", 0))
            if amount <= 0:
                raise BankError("当前没有可转存金额，已安排资金需要优先保留。")
            message = agent.create_direct_approval(
                db,
                conversation_id,
                "save_to_goal",
                {
                    "total_yuan": f"{amount / 100:.2f}",
                    "account_id": "daily",
                    "plan_id": payload.get("plan_id"),
                },
                "工资到账后的储蓄建议待确认。",
            )
            db.execute("UPDATE notifications SET read_at=? WHERE id=?", (now(), notification_id))
            event(db, "*", "conversations.updated", {"conversation_id": conversation_id})
        await agent.notify()
        return message

    @app.post("/api/conversations", status_code=201)
    async def new_conversation(body: ConversationBody, session_key: str = Depends(session)):
        if not body.title.strip():
            raise BankError("会话名称不能为空。")
        with database.transaction() as db:
            conversation_id = database.create_conversation(db, body.title.strip())
            db.execute(
                "UPDATE sessions SET active_conversation_id=? WHERE token_hash=?",
                (conversation_id, session_key),
            )
        await agent.notify()
        return {"conversation_id": conversation_id}

    @app.patch("/api/conversations/{conversation_id}")
    async def rename_conversation(
        conversation_id: str, body: ConversationBody, session_key: str = Depends(session)
    ):
        if not body.title.strip():
            raise BankError("会话名称不能为空。")
        with database.transaction() as db:
            resolve_conversation(db, session_key, conversation_id)
            db.execute(
                "UPDATE conversations SET title=?,updated_at=? WHERE id=?",
                (body.title.strip(), now(), conversation_id),
            )
            event(db, "*", "conversations.updated", {"conversation_id": conversation_id})
        await agent.notify()
        return {"conversation_id": conversation_id, "title": body.title.strip()}

    @app.delete("/api/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: str, session_key: str = Depends(session)):
        with database.transaction() as db:
            resolve_conversation(db, session_key, conversation_id)
            active = agent.active(db, conversation_id)
            if active and active["status"] in ("running", "queued"):
                raise BusyError("这条会话仍在处理请求，请结束后再删除。")
            if active:
                agent._cancel_pending(db, active["id"], "会话已删除，尚未确认的操作已取消。")
            db.execute(
                "UPDATE trigger_rules SET status='paused',updated_at=? WHERE conversation_id=? AND status='active'",
                (now(), conversation_id),
            )
            db.execute("UPDATE conversations SET deleted_at=? WHERE id=?", (now(), conversation_id))
            remaining = database.conversations(db)
            fallback = remaining[0]["id"] if remaining else database.create_conversation(db)
            if database.conversation_id(db) == conversation_id:
                db.execute("UPDATE meta SET value=? WHERE key='conversation_id'", (fallback,))
            db.execute(
                "UPDATE sessions SET active_conversation_id=? WHERE active_conversation_id=?",
                (fallback, conversation_id),
            )
            event(db, "*", "conversations.updated", {"conversation_id": conversation_id, "deleted": True})
            selected = resolve_conversation(db, session_key)
        await agent.notify()
        return {"conversation_id": selected}

    @app.post("/api/messages", status_code=202)
    async def chat(body: ChatBody, session_key: str = Depends(session)):
        if not body.text.strip():
            raise BankError("请输入你的需求。")
        with database.connection() as db:
            conversation_id = resolve_conversation(db, session_key, body.conversation_id)
        task_id = await agent.submit(body.text.strip(), conversation_id=conversation_id)
        return {"task_id": task_id}

    @app.get("/api/payment-policy", dependencies=[Depends(session)])
    def payment_policy():
        with database.connection() as db:
            return policy(db)

    @app.patch("/api/payment-policy")
    async def set_payment_policy(body: PaymentPolicyBody, session_key: str = Depends(session)):
        from .bank import minor
        amount = minor(body.threshold_yuan)
        if amount <= 0:
            raise BankError('单笔交易门槛必须大于0元。')
        verify_pin(database, body.pin)
        with database.transaction() as db:
            before = policy(db)
            if before['threshold_minor'] != amount:
                db.execute('UPDATE payment_policy SET threshold_minor=?,version=version+1 WHERE id=1', (amount,))
                event(db, '*', 'payment.policy_updated', {'basis':'single_transaction',
                    'previous_threshold_minor':before['threshold_minor'],'threshold_minor':amount})
            result = policy(db)
        await agent.notify()
        return result

    @app.post("/api/approvals/{approval_id}/sms")
    async def payment_sms(approval_id: str, body: PaymentSMSBody, session_key: str = Depends(session)):
        with database.transaction() as db:
            current = resolve_conversation(db, session_key, body.conversation_id)
            approval = db.execute("SELECT a.*,t.conversation_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?", (approval_id,)).fetchone()
            if not approval or approval["conversation_id"] != current or approval["status"] != "pending" or datetime.fromisoformat(approval["expires_at"]) <= datetime.now(TZ):
                raise BankError("当前操作不存在、已结束或已过期。")
            args = json.loads(approval["args_json"])
            if approval["tool_name"] in DEBIT_TOOLS:
                args["account_id"] = body.account_id or args.get("account_id")
                if not args["account_id"]:
                    raise BankError("请先选择付款账户。")
            agent.bank._check_action(db, approval["tool_name"], args)
            from .scheduled_actions import timing
            args=timing(approval['tool_name'],args,body.execute_at)
            agent.bank._check_action(db, approval["tool_name"], args)
            result = send_sms(db, approval, args, session_key)
        await agent.notify()
        return result

    @app.get("/api/approvals/{approval_id}/payment")
    async def payment_context(approval_id: str, conversation_id: str | None = None, session_key: str = Depends(session)):
        with database.connection() as db:
            current = resolve_conversation(db, session_key, conversation_id)
            approval = db.execute("SELECT a.*,t.conversation_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?", (approval_id,)).fetchone()
            if not approval or approval["conversation_id"] != current:
                raise BankError("找不到当前会话的待支付操作。")
            if approval["status"] != "pending" or datetime.fromisoformat(approval["expires_at"]) <= datetime.now(TZ):
                raise BankError("该操作已结束或过期，请重新发起。")
            args = json.loads(approval["args_json"])
            summary = json.loads(approval["summary_json"])
            debit = approval["tool_name"] in DEBIT_TOOLS and summary.get('debit_minor',1)>0
            from .scheduled_actions import TOOLS as schedulable
            return {"tool": approval["tool_name"], "schedule_allowed":approval['tool_name'] in schedulable,"execute_at":args.get('execute_at',''),"title": summary["title"], "amount_minor": summary.get("amount_minor"), "fields": summary["fields"],
                    "account_required": debit, "account_id": args.get("account_id", "") if debit else "",
                    "accounts": agent.bank.payment_options(db, approval["tool_name"], args) if debit else [],
                    **(risk(db, approval, args) if approval["tool_name"] in MONEY_TOOLS else {"sms_required": False, "level": "yellow"})}

    @app.post("/api/approvals/{approval_id}/decision")
    async def decide(approval_id: str, body: DecisionBody, session_key: str = Depends(session)):
        with database.connection() as db:
            conversation_id = resolve_conversation(db, session_key, body.conversation_id)
        if settings.app_auth_enabled and body.decision == "confirm" and body.pin is None and not body.face_token:
            with database.connection() as db:
                approval = db.execute("SELECT tool_name,status FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if approval and approval["status"] == "pending" and approval["tool_name"] in MONEY_TOOLS:
                raise HTTPException(428, "请输入四位交易密码。")
        return await agent.confirm(approval_id, body.decision, conversation_id, pin=body.pin, account_id=body.account_id, sms_code=body.sms_code, session_key=session_key,execute_at=body.execute_at,face_token=body.face_token)

    from .feishu_payment import register as register_feishu_payment
    register_feishu_payment(app, database, settings, session, payment_context, payment_sms, decide, PaymentSMSBody, DecisionBody)

    @app.post('/api/tasks/{task_id}/partial-resolution')
    async def partial_resolution(task_id:str,body:PartialChoiceBody,session_key:str=Depends(session)):
        from .partial_completion import choose
        with database.transaction() as db:
            current=resolve_conversation(db,session_key,body.conversation_id)
            result,queued=choose(agent,db,task_id,current,body.action,body.version,body.order_id)
        if queued:agent.queue.put_nowait(queued)
        await agent.notify()
        return result

    @app.post("/api/tasks/{task_id}/interrupt")
    async def interrupt_task(task_id: str, body: ConversationBodyForInterrupt, session_key: str = Depends(session)):
        with database.connection() as db:
            current = resolve_conversation(db, session_key, body.conversation_id)
        return await agent.interrupt(task_id, current)

    @app.get("/api/tasks/{task_id}/plan", dependencies=[Depends(session)])
    def task_plan(task_id: str):
        from .task_dag import public_task
        with database.connection() as db:
            row = db.execute("SELECT t.* FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE t.id=? AND c.deleted_at IS NULL", (task_id,)).fetchone()
            if not row:
                raise HTTPException(404, "任务不存在。")
            return public_task(row)

    @app.get("/api/bank", dependencies=[Depends(session)])
    async def bank_snapshot(refresh: bool = False):
        return (await agent.securities.snapshot(refresh=refresh))

    @app.post("/api/sandbox/inspect")
    async def inspect_bank(body: InspectBody, session_key: str = Depends(session)):
        result = agent.bank.query(body.tool, {})
        with database.transaction() as db:
            conversation_id = resolve_conversation(db, session_key, body.conversation_id)
            message = add_message(
                db,
                conversation_id,
                None,
                "assistant",
                "data",
                result["display"]["title"],
                {"tool": body.tool, "result": result, "source": "sandbox"},
            )
        await agent.notify()
        return message

    @app.post("/api/sandbox/reset", dependencies=[Depends(session)])
    async def reset_bank(body: ResetBody):
        with database.transaction() as db:
            active = db.execute("SELECT 1 FROM tasks WHERE status IN ('running','queued') LIMIT 1").fetchone()
            if active:
                raise BusyError("请等待当前请求处理结束，再重置演示数据。")
            for table in (
                "analysis_runs",
                "analysis_datasets",
                "daily_report_runs",
                "birthday_plans",
                "scheduled_transfers",
                "accounts",
                "travel_quotes",
                "travel_order_state",
                "travel_changes",
                "travel_change_quotes",
                "travel_quote_claims",
                "travel_orders",
                "food_quotes",
                "food_orders",
                "securities_quotes",
                "securities_positions",
                "securities_orders",
                "contacts",
                "cards",
                "linked_card_accounts",
                "transactions",
                "products",
                "product_nav_history",
                "holdings",
                "risk_profile",
                "subscriptions",
                "approvals",
                "messages",
                "tasks",
                "conversation_contacts",
                "contact_details",
                "contact_profiles",
                "contact_links",
                "contact_phones",
                "conversations",
                "feishu_outbox",
                "binding_codes",
                "plans",
                "plan_versions",
                "commitments",
                "plan_executions",
                "trigger_rules",
                "bank_events",
                "trigger_runs",
                "notifications",
            ):
                db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM meta WHERE key='business_date'")
            empty_user = bool(db.execute("SELECT 1 FROM meta WHERE key='empty_user'").fetchone())
            new_conversation = database.seed_empty(db) if empty_user else database.seed(db)
            database.seed_contact_details(db)
            from .report_history import reset_history
            reset_history(db)
            db.execute("UPDATE sessions SET active_conversation_id=?", (new_conversation,))
            event(db, "*", "reset", {"conversation_id": new_conversation})
        await agent.notify()
        return {"conversation_id": new_conversation}

    @app.get("/api/events")
    async def events(
        request: Request,
        after: int = 0,
        conversation_id: str | None = None,
        session_key: str = Depends(session),
    ):
        with database.connection() as db:
            conversation_id = resolve_conversation(db, session_key, conversation_id)
        try:
            cursor = max(0, int(request.headers.get("last-event-id", after)))
        except ValueError:
            cursor = 0

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                from .account_lock import locked
                with database.connection() as db:
                    is_locked = locked(db)
                if is_locked:
                    yield 'event: system.locked\ndata: {"system_locked":true}\n\n'
                    return
                if settings.app_auth_enabled and not app.state.auth_status(session_key)["ready"]:
                    return
                # 条件变量阻塞等待；持久化序号支持连接恢复，不反复轮询任务。
                async with agent.changed:
                    with database.connection() as db:
                        rows = db.execute(
                            "SELECT * FROM events WHERE conversation_id IN (?, '*') AND id>? ORDER BY id LIMIT 100",
                            (conversation_id, cursor),
                        ).fetchall()
                    if not rows:
                        try:
                            await asyncio.wait_for(agent.changed.wait(), timeout=15)
                        except TimeoutError:
                            pass
                if not rows:
                    yield ": keepalive\n\n"
                    continue
                for row in rows:
                    with database.connection() as db:
                        if locked(db):
                            yield 'event: system.locked\ndata: {"system_locked":true}\n\n'
                            return
                    if settings.app_auth_enabled and not app.state.auth_status(session_key)["ready"]:
                        return
                    cursor = row["id"]
                    yield f"id: {cursor}\nevent: {row['type']}\ndata: {row['data_json']}\n\n"
                    if row["type"] == "reset":
                        return

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.post("/api/feishu/binding-code", dependencies=[Depends(session)])
    def binding_code():
        if not settings.feishu_ready:
            raise BankError("飞书还未配置。请先按接入说明配置应用凭证及桥接进程。")
        code = secrets.token_hex(6).upper()
        with database.transaction() as db:
            db.execute("DELETE FROM binding_codes")
            db.execute(
                "INSERT INTO binding_codes VALUES (?,?)",
                (hash_token(code), (datetime.now(TZ) + timedelta(minutes=10)).isoformat()),
            )
        return {"code": code, "expires_in_seconds": 600, "instruction": f"在飞书机器人私聊发送：绑定 {code}"}

    @app.get('/api/feishu/preferences',dependencies=[Depends(session)])
    def feishu_preferences():
        with database.connection() as db:
            row=db.execute("SELECT value FROM meta WHERE key='feishu_notifications'").fetchone()
            return {'notifications':not row or row[0]!='0','bound':bool(db.execute('SELECT 1 FROM feishu_binding').fetchone())}

    @app.post('/api/feishu/preferences',dependencies=[Depends(session)])
    def set_feishu_preferences(body:FeishuPreferencesBody):
        with database.transaction() as db:
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('feishu_notifications',?)",('1' if body.notifications else '0',))
            if not body.notifications:
                db.execute("UPDATE feishu_outbox SET status='failed',error='用户已关闭主动通知' WHERE status='pending' AND json_extract(payload_json,'$.data._feishu_notify')=1")
        return {'notifications':body.notifications}

    @app.post('/api/feishu/unbind',dependencies=[Depends(session)])
    def feishu_unbind():
        with database.transaction() as db:
            db.execute('DELETE FROM feishu_binding');db.execute('DELETE FROM binding_codes')
            db.execute("DELETE FROM meta WHERE key='feishu_conversation'")
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('feishu_binding_nonce',?)",(secrets.token_hex(16),))
            db.execute("UPDATE feishu_outbox SET status='failed',error='用户已解除飞书关联' WHERE status='pending'")
            event(db,'*','binding.updated',{'bound':False})
        return {'bound':False}

    @app.post("/api/internal/feishu", dependencies=[Depends(bridge)])
    async def feishu_event(body: FeishuBody):
        from .feishu_actions import handle
        return await handle(agent,database,body)

    @app.get('/travel.html', include_in_schema=False)
    def independent_travel_page():
        raise HTTPException(404, '旅行演示站独立运行在8004端口。')

    @app.get('/food.html', include_in_schema=False)
    def independent_food_page():
        raise HTTPException(404, '外卖演示站独立运行在8002端口。')

    @app.get("/securities.html", include_in_schema=False)
    def retired_securities_page():
        raise HTTPException(404, "证券站已独立运行。")

    frontend_dist = ROOT / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="web")
    return app


def create_app(settings: Settings | None = None, model=None):
    settings = settings or Settings()
    if settings.app_auth_enabled and settings.multi_user_accounts:
        from .accounts import create_multiuser_app

        return create_multiuser_app(settings, _create_single_app, model)
    return _create_single_app(settings, model)


app = create_app()
