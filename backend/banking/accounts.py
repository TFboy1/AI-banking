"""Account directory and isolated banking applications, one SQLite database per user."""
import asyncio
import secrets
import sqlite3
import time
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .bank import BankError
from .config import ROOT
from .db import Database, now, uid
from .security import LoginBody, SMSBody, UsernameBody, token_hash

HUB_PATHS = {"/api/auth/open", "/api/auth/status", "/api/auth/sms", "/api/auth/login", "/api/auth/profile", "/api/health", "/internal/securities/portfolio"}


class AccountDirectory:
    def __init__(self, settings, factory, model=None):
        self.settings = settings
        self.factory = factory
        self.model = model
        self.path = settings.database_path.parent / "accounts.sqlite3"
        self.apps = {}
        self.stack = AsyncExitStack()
        self.lock = asyncio.Lock()
        self.feishu_lock = asyncio.Lock()

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        Database(self.settings.database_path).initialize()
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,phone TEXT UNIQUE NOT NULL,username TEXT NOT NULL,database_path TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS login_sessions(token_hash TEXT PRIMARY KEY,user_id TEXT);
                CREATE TABLE IF NOT EXISTS sms(token_hash TEXT PRIMARY KEY,phone TEXT NOT NULL,purpose TEXT NOT NULL,code_hash TEXT NOT NULL,expires REAL NOT NULL,sent REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0);
            """)
            with Database(self.settings.database_path).connection() as legacy:
                person = legacy.execute("SELECT * FROM app_identity WHERE id=1").fetchone()
            if person:
                db.execute("INSERT OR IGNORE INTO users VALUES ('legacy',?,?,?)", (person["phone"], person["username"], str(self.settings.database_path.resolve())))
                # Existing browser sessions belong to the preserved legacy account.
                with Database(self.settings.database_path).connection() as legacy:
                    for row in legacy.execute("SELECT token_hash FROM sessions"):
                        db.execute("INSERT OR IGNORE INTO login_sessions VALUES (?,'legacy')", (row[0],))
        # Independent sites can request offline users' orders. Upgrade schemas
        # and payment rules without seeding or resetting their business data.
        from .travel_common import BANK_SCHEMA as TRAVEL_SCHEMA
        from .payment_security import SCHEMA as PAYMENT_SCHEMA
        from .birthdays import SCHEMA as BIRTHDAY_SCHEMA
        with self.connection() as db:
            paths=[Path(r[0]) for r in db.execute('SELECT database_path FROM users')]
        for path in paths:
            if path.exists():
                with Database(path).connection() as db:db.executescript(TRAVEL_SCHEMA+PAYMENT_SCHEMA+BIRTHDAY_SCHEMA)

    def user_for(self, key):
        with self.connection() as db:
            row = db.execute("SELECT u.* FROM users u JOIN login_sessions s ON s.user_id=u.id WHERE s.token_hash=?", (key,)).fetchone()
        return dict(row) if row else None

    async def application(self, user):
        async with self.lock:
            if user["id"] not in self.apps:
                settings = self.settings.model_copy(update={
                    "database_path": Path(user["database_path"]), "multi_user_accounts": False,
                    "scheduled_worker_enabled":False,
                    "daily_report_worker_enabled":False,
                    "birthday_worker_enabled":False,
                    "seed_demo_data": user["id"] == "legacy",
                    "feishu_enabled": self.settings.feishu_enabled,
                })
                app = self.factory(settings, self.model)
                await self.stack.enter_async_context(app.router.lifespan_context(app))
                self.apps[user["id"]] = app
            return self.apps[user["id"]]

    def ensure_session(self, request, response):
        token = request.cookies.get("bank_session", "")
        key = token_hash(token)
        with self.connection() as db:
            if not token or not db.execute("SELECT 1 FROM login_sessions WHERE token_hash=?", (key,)).fetchone():
                token = secrets.token_urlsafe(32)
                key = token_hash(token)
                db.execute("INSERT INTO login_sessions VALUES (?,NULL)", (key,))
        response.set_cookie("bank_session", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=30 * 86400)
        return key

    async def status(self, key):
        user = self.user_for(key)
        if user:
            app = await self.application(user)
            return app.state.auth_status(key)
        return {"registered": False, "authenticated": False, "has_pin": False, "contacts_done": False,
                "lock_enabled": True, "ready": False, "username": "", "phone_masked": ""}


class UserRouter:
    def __init__(self, app, directory):
        self.app = app
        self.directory = directory

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/api/"):
            if scope['path']=='/api/internal/feishu':
                from .feishu_routing import dispatch
                return await dispatch(self.directory,scope,receive,send)
            request = Request(scope)
            origin = request.headers.get("origin")
            allowed = {"http://127.0.0.1:8000", "http://localhost:8000", "http://127.0.0.1:5173", "http://localhost:5173"}
            if self.directory.settings.feishu_public_url:
                from .feishu_payment import public_origin
                allowed.add(public_origin(self.directory.settings))
            if origin and origin not in allowed:
                return await JSONResponse({"detail": "不接受来自其他站点的请求。"}, status_code=403)(scope, receive, send)
            if scope["path"] not in HUB_PATHS:
                user = self.directory.user_for(token_hash(request.cookies.get("bank_session", "")))
                if not user:
                    return await JSONResponse({"detail": "请先注册或登录你的账号。"}, status_code=401)(scope, receive, send)
                application = await self.directory.application(user)
                return await application(scope, receive, send)
        await self.app(scope, receive, send)


def create_multiuser_app(settings, factory, model=None):
    directory = AccountDirectory(settings, factory, model)

    @asynccontextmanager
    async def lifespan(app):
        directory.initialize()
        if settings.feishu_ready:
            with directory.connection() as db:users=[dict(r) for r in db.execute('SELECT * FROM users')]
            for user in users:await directory.application(user)
        from .scheduled_transfers import directory_loop
        worker=asyncio.create_task(directory_loop(directory,settings)) if settings.scheduled_worker_enabled else None
        from .daily_reports import directory_loop as daily_report_loop
        reports=asyncio.create_task(daily_report_loop(directory,settings)) if settings.daily_report_worker_enabled else None
        from .birthdays import directory_loop as birthday_loop
        birthdays=asyncio.create_task(birthday_loop(directory,settings)) if settings.birthday_worker_enabled else None
        try:
            yield
        finally:
            if birthdays:
                birthdays.cancel()
                await asyncio.gather(birthdays,return_exceptions=True)
            if reports:
                reports.cancel()
                await asyncio.gather(reports,return_exceptions=True)
            if worker:
                worker.cancel()
                await asyncio.gather(worker,return_exceptions=True)
            await directory.stack.aclose()

    app = FastAPI(title="有数 · 多用户模拟银行", lifespan=lifespan)
    app.state.directory = directory
    app.add_middleware(UserRouter, directory=directory)

    @app.exception_handler(BankError)
    async def error_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "model_configured": settings.model_ready, "feishu_enabled": settings.feishu_ready}

    @app.get("/api/auth/status")
    async def status(request: Request, response: Response):
        return await directory.status(directory.ensure_session(request, response))

    @app.patch("/api/auth/profile")
    async def rename_user(body: UsernameBody, request: Request):
        key = token_hash(request.cookies.get("bank_session", ""))
        user = directory.user_for(key)
        if not user or not (await directory.status(key))["ready"]:
            raise HTTPException(401, "请先登录并解锁应用。")
        username = body.username.strip()
        if not username:
            raise BankError("用户名不能为空。")
        # One transaction updates the account directory and this user's identity.
        with directory.connection() as db:
            db.execute("ATTACH DATABASE ? AS identity_db", (user["database_path"],))
            db.execute("UPDATE users SET username=? WHERE id=?", (username, user["id"]))
            db.execute("UPDATE identity_db.app_identity SET username=? WHERE id=1", (username,))
        return await directory.status(key)

    @app.post("/api/auth/open")
    async def open_app(request: Request, response: Response):
        key = directory.ensure_session(request, response)
        user = directory.user_for(key)
        if user:
            application = await directory.application(user)
            with application.state.database.transaction() as db:
                db.execute("UPDATE app_sessions SET unlocked=0 WHERE token_hash=?", (key,))
        return await directory.status(key)

    @app.post("/api/auth/sms")
    def sms(body: SMSBody, request: Request, response: Response):
        key = directory.ensure_session(request, response)
        with directory.connection() as db:
            user = db.execute("SELECT * FROM users WHERE phone=?", (body.phone,)).fetchone()
            if body.purpose == "register" and user:
                raise BankError("该手机号已注册，请使用短信登录。")
            if body.purpose == "login" and not user:
                raise BankError("该手机号尚未注册，请先注册。")
            previous = db.execute("SELECT sent FROM sms WHERE token_hash=?", (key,)).fetchone()
            if previous and time.time() - previous[0] < 60:
                raise BankError("验证码已发送，请 60 秒后再试。")
            code = f"{secrets.randbelow(1000000):06d}"
            db.execute("INSERT OR REPLACE INTO sms VALUES (?,?,?,?,?,?,0)", (key, body.phone, body.purpose, token_hash(code), time.time() + 300, time.time()))
        return {"demo_code": code, "phone_masked": body.phone[:3] + "****" + body.phone[-4:], "expires_in": 300, "simulated": True}

    @app.post("/api/auth/login")
    async def login(body: LoginBody, request: Request):
        key = token_hash(request.cookies.get("bank_session", ""))
        error = None
        async with directory.lock:
            with directory.connection() as db:
                challenge = db.execute("SELECT * FROM sms WHERE token_hash=?", (key,)).fetchone()
                user = db.execute("SELECT * FROM users WHERE phone=?", (body.phone,)).fetchone()
                if not challenge or challenge["phone"] != body.phone or challenge["purpose"] != body.purpose or challenge["expires"] <= time.time() or challenge["attempts"] >= 5:
                    error = "验证码无效或已过期，请重新获取。"
                elif not secrets.compare_digest(challenge["code_hash"], token_hash(body.code)):
                    db.execute("UPDATE sms SET attempts=attempts+1 WHERE token_hash=?", (key,))
                    error = "验证码错误。"
                elif body.purpose == "register":
                    if user or not body.username.strip():
                        error = "该手机号已注册或用户名为空。"
                    else:
                        user_id = uid("user_")
                        path = settings.database_path.parent / "users" / (user_id + ".sqlite3")
                        database = Database(path, seed_demo=False)
                        database.initialize()
                        with database.transaction() as tenant:
                            tenant.execute("INSERT INTO app_identity(id,username,phone) VALUES (1,?,?)", (body.username.strip(), body.phone))
                        db.execute("INSERT INTO users VALUES (?,?,?,?)", (user_id, body.phone, body.username.strip(), str(path.resolve())))
                        user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
                elif not user:
                    error = "手机号未注册。"
                if not error:
                    db.execute("DELETE FROM sms WHERE token_hash=?", (key,))
                    old = directory.user_for(key)
                    if old:
                        with Database(Path(old["database_path"])).transaction() as tenant:
                            tenant.execute("UPDATE app_sessions SET authenticated=0,unlocked=0 WHERE token_hash=?", (key,))
                    db.execute("UPDATE login_sessions SET user_id=? WHERE token_hash=?", (user["id"], key))
                    with Database(Path(user["database_path"])).transaction() as tenant:
                        tenant.execute("INSERT OR IGNORE INTO sessions(token_hash,created_at) VALUES (?,?)", (key, now()))
                        tenant.execute("INSERT OR REPLACE INTO app_sessions VALUES (?,1,1)", (key,))
                        from .safety import record_sms_login
                        record_sms_login(tenant, key)
        if error:
            raise BankError(error)
        return await directory.status(key)

    @app.post('/internal/securities/portfolio')
    async def securities_portfolio(request: Request):
        from .securities_bridge import PortfolioRequest, portfolio
        from .securities_common import require_bridge

        require_bridge(request, settings.securities_bridge_key_path)
        body = PortfolioRequest.model_validate(await request.json())
        with directory.connection() as db:
            users = [dict(r) for r in db.execute('SELECT * FROM users')]
        for user in users:
            with Database(Path(user['database_path'])).connection() as db:
                row = db.execute("SELECT value FROM meta WHERE key='securities_owner'").fetchone()
                if row and row[0] == body.owner:
                    return portfolio(db)
        raise HTTPException(404, '未找到关联的有数账户。')

    @app.post('/internal/connections/redeem')
    async def redeem_connection(request: Request):
        from .connections import Redeem, authorize, matches, redeem
        body = Redeem.model_validate(await request.json())
        authorize(request, settings, body.platform)
        with directory.connection() as db:
            users = [dict(r) for r in db.execute('SELECT * FROM users')]
        candidates = []
        for user in users:
            with Database(Path(user['database_path'])).connection() as db:
                if matches(db, body.code):
                    candidates.append(user)
        if len(candidates) != 1:
            raise HTTPException(400, '通用同步码无效或有冲突，请在有数重新生成。')
        with Database(Path(candidates[0]['database_path'])).transaction() as db:
            return redeem(db, body)

    @app.post('/internal/travel/orders')
    async def travel_orders(request: Request):
        from .travel_bridge import orders
        from .travel_common import Owner, require_bridge
        require_bridge(request, settings.travel_bridge_key_path)
        body = Owner.model_validate(await request.json())
        with directory.connection() as db:
            users = [dict(r) for r in db.execute('SELECT * FROM users')]
        for user in users:
            with Database(Path(user['database_path'])).connection() as db:
                row = db.execute("SELECT value FROM meta WHERE key='travel_owner'").fetchone()
                if row and row[0] == body.owner:
                    return orders(db)
        raise HTTPException(404, '未找到关联的有数账户。')

    @app.get('/travel.html', include_in_schema=False)
    def independent_travel_page():
        raise HTTPException(404, '旅行演示站独立运行在8004端口。')

    @app.post('/internal/food/orders')
    async def food_orders(request: Request):
        from .food_bridge import orders
        from .food_common import Owner, require_bridge
        require_bridge(request, settings.food_bridge_key_path)
        body = Owner.model_validate(await request.json())
        with directory.connection() as db:
            users = [dict(r) for r in db.execute('SELECT * FROM users')]
        for user in users:
            with Database(Path(user['database_path'])).connection() as db:
                row = db.execute("SELECT value FROM meta WHERE key='food_owner'").fetchone()
                if row and row[0] == body.owner:
                    return orders(db)
        raise HTTPException(404, '未找到关联的有数账户。')

    @app.get('/food.html', include_in_schema=False)
    def independent_food_page():
        raise HTTPException(404, '外卖演示站独立运行在8002端口。')

    @app.get("/securities.html", include_in_schema=False)
    def retired_securities_page():
        raise HTTPException(404, "证券站已独立运行。")

    if (ROOT / "frontend/dist").is_dir():
        app.mount("/", StaticFiles(directory=ROOT / "frontend/dist", html=True), name="web")
    return app
