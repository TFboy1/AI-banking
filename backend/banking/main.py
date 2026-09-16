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

    @asynccontextmanager
    async def lifespan(app):
        database.initialize()
        await agent.start()
        sender = None
        if settings.feishu_ready:
            from .feishu import OutboxSender

            sender = asyncio.create_task(OutboxSender(database, settings, agent.changed).run())
        try:
            yield
        finally:
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

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            allowed = {
                "http://127.0.0.1:5173",
                "http://localhost:5173",
                "http://127.0.0.1:8000",
                "http://localhost:8000",
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
            ):
                db.execute(f"DELETE FROM {table}")
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
