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

from .agent import Agent, BusyError
from .bank import BankError
from .config import ROOT, Settings
from .db import TZ, Database, add_message, dumps, event, message_dict, now
from .planning import PlanningService


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatBody(Body):
    text: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=100)


class DecisionBody(Body):
    decision: Literal["confirm", "cancel"]
    conversation_id: str | None = Field(default=None, max_length=100)


class ConversationBody(Body):
    title: str = Field(default="新会话", min_length=1, max_length=60)


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


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_app(settings: Settings | None = None, model=None):
    settings = settings or Settings()
    database = Database(settings.database_path)
    agent = Agent(database, settings, model)
    planning = PlanningService(database)

    @asynccontextmanager
    async def lifespan(app):
        database.initialize()
        await agent.start()
        reminder_task = asyncio.create_task(planning.run_reminder_loop(agent.changed))
        sender = None
        if settings.feishu_ready:
            from .feishu import OutboxSender

            sender = asyncio.create_task(OutboxSender(database, settings, agent.changed).run())
        try:
            yield
        finally:
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

    def bridge(request: Request):
        expected = "Bearer " + settings.feishu_bridge_token
        supplied = request.headers.get("Authorization", "")
        if not settings.feishu_ready or not secrets.compare_digest(supplied, expected):
            raise HTTPException(403, "飞书桥接未启用或凭证不正确。")

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
    def bootstrap(request: Request, response: Response, conversation_id: str | None = None):
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
            "bank": agent.bank.snapshot(),
            "config": {
                "model_configured": settings.model_ready,
                "model_name": settings.llm_model,
                "feishu_enabled": settings.feishu_ready,
                "feishu_bound": binding,
            },
        }

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
        with database.transaction() as db:
            result = planning.trigger_salary(db, body.model_dump())
        await agent.notify()
        return result

    @app.post("/api/sandbox/scenario")
    async def demo_scenario(body: DemoScenarioBody, session_key: str = Depends(session)):
        with database.transaction() as db:
            result = planning.seed_demo_scenario(db)
            event(db, "*", "bank.updated", {})
        await agent.notify()
        return result

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

    @app.post("/api/approvals/{approval_id}/decision")
    async def decide(approval_id: str, body: DecisionBody, session_key: str = Depends(session)):
        with database.connection() as db:
            conversation_id = resolve_conversation(db, session_key, body.conversation_id)
        return await agent.confirm(approval_id, body.decision, conversation_id)

    @app.get("/api/bank", dependencies=[Depends(session)])
    def bank_snapshot():
        return agent.bank.snapshot()

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
                "accounts",
                "contacts",
                "cards",
                "transactions",
                "products",
                "holdings",
                "risk_profile",
                "subscriptions",
                "approvals",
                "messages",
                "tasks",
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
            new_conversation = database.seed(db)
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

    @app.post("/api/internal/feishu", dependencies=[Depends(bridge)])
    async def feishu_event(body: FeishuBody):
        task_id = None
        with database.transaction() as db:
            duplicate = db.execute(
                "SELECT result_json FROM feishu_inbox WHERE event_id=?", (body.event_id,)
            ).fetchone()
            if duplicate:
                return {**json.loads(duplicate[0]), "duplicate": True}
            binding = db.execute("SELECT * FROM feishu_binding WHERE id=1").fetchone()
            bound = bool(
                binding
                and binding["tenant_key"] == body.tenant_key
                and binding["open_id"] == body.open_id
                and binding["chat_id"] == body.chat_id
            )
            text = body.text.strip()
            if body.kind == "message" and text.startswith(("绑定 ", "/绑定 ")):
                code_hash = hash_token(text.split(maxsplit=1)[1].strip().upper())
                code = db.execute(
                    "SELECT expires_at FROM binding_codes WHERE code_hash=?", (code_hash,)
                ).fetchone()
                if code and datetime.fromisoformat(code[0]) > datetime.now(TZ):
                    db.execute("DELETE FROM binding_codes")
                    db.execute(
                        "INSERT OR REPLACE INTO feishu_binding VALUES (1,?,?,?)",
                        (body.tenant_key, body.open_id, body.chat_id),
                    )
                    result = {"reply": "绑定成功。你现在可以在这里操作 Web 中的同一个模拟银行账户。"}
                    event(db, "*", "binding.updated", {"bound": True})
                else:
                    result = {"reply": "绑定码无效或已过期，请在 Web 端重新生成。"}
            elif not bound:
                result = {"reply": "请先在本机 Web 设置中生成绑定码，再在本私聊发送“绑定 绑定码”。"}
            else:
                try:
                    if body.kind == "card":
                        if not body.approval_id or not body.decision:
                            raise BankError("确认卡片参数不完整。")
                        result, task_id = agent.confirm_in_transaction(db, body.approval_id, body.decision)
                        result["reply"] = {
                            "executed": "操作已完成",
                            "cancelled": "已取消",
                            "expired": "卡片已过期",
                            "failed": "操作未完成",
                        }.get(result["status"], "已处理")
                    elif text:
                        task_id = agent.submit_in_transaction(db, text, "feishu")
                        result = {"task_id": task_id, "reply": "收到，正在处理你的需求。"}
                    else:
                        result = {"reply": "目前支持文本消息，请直接描述你的需求。"}
                except BankError as exc:
                    result = {"reply": str(exc)}
            db.execute("INSERT INTO feishu_inbox VALUES (?,?)", (body.event_id, dumps(result)))
        if task_id:
            agent.queue.put_nowait(task_id)
        await agent.notify()
        return result

    frontend_dist = ROOT / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="web")
    return app


app = create_app()
