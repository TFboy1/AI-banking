"""Local demo identity, simulated SMS, and server-enforced PIN verification."""
import hashlib
import secrets
import time

from fastapi import HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .bank import BankError
from .db import now

MONEY_TOOLS = {"schedule_transfer","add_bank_card", "change_travel_order","cancel_travel_order", "place_travel_order", "place_food_order","transfer_own_funds", "sell_stock_basket", "purchase_stock_basket", "transfer", "purchase_product", "redeem_product", "simulate_expense", "save_to_goal"}
from .pin_change import SCHEMA as PIN_CHANGE_SCHEMA

SCHEMA = PIN_CHANGE_SCHEMA + """
CREATE TABLE IF NOT EXISTS app_identity (
 id INTEGER PRIMARY KEY CHECK(id=1),username TEXT NOT NULL,phone TEXT NOT NULL,
 pin_hash TEXT,lock_enabled INTEGER NOT NULL DEFAULT 1,contacts_done INTEGER NOT NULL DEFAULT 0,
 failures INTEGER NOT NULL DEFAULT 0,blocked_until REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS app_sessions (
 token_hash TEXT PRIMARY KEY,authenticated INTEGER NOT NULL DEFAULT 0,unlocked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sms_challenges (
 token_hash TEXT PRIMARY KEY,phone TEXT NOT NULL,purpose TEXT NOT NULL,code_hash TEXT NOT NULL,
 expires_at REAL NOT NULL,sent_at REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0);
"""


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def encode_pin(pin):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 120_000).hex()
    return salt + ":" + digest


def pin_matches(pin, encoded):
    if not encoded or not isinstance(pin, str) or len(pin) != 4 or not pin.isascii() or not pin.isdigit():
        return False
    salt, digest = encoded.split(":")
    actual = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 120_000).hex()
    return secrets.compare_digest(actual, digest)


def verify_pin(database, pin, reference=None):
    from .safety import check_user, failure, success
    error = None
    with database.transaction() as db:
        check_user(db)
        person = db.execute("SELECT * FROM app_identity WHERE id=1").fetchone()
        if not person or not person["pin_hash"]:
            error = "请先完成注册并设置四位交易密码。"
        elif person["blocked_until"] > time.time():
            error = "密码连续错误，已暂时锁定，请稍后再试。"
        elif not pin_matches(pin, person["pin_hash"]):
            failures = person["failures"] + 1
            db.execute("UPDATE app_identity SET failures=?,blocked_until=? WHERE id=1", (failures, time.time() + 60 if failures >= 5 else 0))
            failure(db, 'pin', reference)
            error = "密码错误。" if failures < 5 else "密码连续错误，已锁定 60 秒。"
        else:
            db.execute("UPDATE app_identity SET failures=0,blocked_until=0 WHERE id=1")
            success(db, 'pin')
            if reference:
                from .db import event
                event(db,'*','payment.pin_verified',{'approval_id':reference})
    if error:
        raise BankError(error)


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SMSBody(Body):
    phone: str = Field(pattern=r"^1\d{10}$")
    purpose: str = Field(pattern=r"^(register|login)$")


class LoginBody(SMSBody):
    code: str = Field(pattern=r"^\d{6}$")
    username: str = Field(default="", max_length=40)


class PINBody(Body):
    pin: str = Field(pattern=r"^[0-9]{4}$")


class SetPINBody(PINBody):
    face_token: str | None = Field(default=None,max_length=150)
    sms_code: str | None = None
    repeat_pin: str = Field(pattern=r"^[0-9]{4}$")
    current_pin: str | None = None


class LockBody(Body):
    enabled: bool
    pin: str = Field(pattern=r"^[0-9]{4}$")


class UsernameBody(Body):
    username: str = Field(min_length=1, max_length=40)


def register_auth(app, database, settings):
    def ensure_session(request, response):
        token = request.cookies.get("bank_session", "")
        key = token_hash(token)
        with database.transaction() as db:
            if not token or not db.execute("SELECT 1 FROM sessions WHERE token_hash=?", (key,)).fetchone():
                token = secrets.token_urlsafe(32)
                key = token_hash(token)
                db.execute("INSERT INTO sessions(token_hash,created_at) VALUES (?,?)", (key, now()))
            db.execute("INSERT OR IGNORE INTO app_sessions(token_hash) VALUES (?)", (key,))
        response.set_cookie("bank_session", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=30 * 86400)
        return key

    def identity_status(key):
        with database.connection() as db:
            person = db.execute("SELECT * FROM app_identity WHERE id=1").fetchone()
            session = db.execute("SELECT * FROM app_sessions WHERE token_hash=?", (key,)).fetchone()
        authenticated = bool(session and session["authenticated"])
        unlocked = bool(session and session["unlocked"])
        has_pin = bool(person and person["pin_hash"])
        contacts_done = bool(person and person["contacts_done"])
        enabled = bool(person["lock_enabled"]) if person else True
        ready = authenticated and has_pin and contacts_done and (unlocked or not enabled)
        from .account_lock import locked
        with database.connection() as db:
            system_locked = locked(db)
            face_done=bool(db.execute('SELECT done FROM face_onboarding WHERE id=1').fetchone()[0])
            from .face import enabled as face_active
            face_enabled=face_active(db)
        ready = ready and not system_locked
        return {"system_locked": system_locked, "registered": bool(person), "authenticated": authenticated, "has_pin": has_pin,
                "contacts_done": contacts_done, "lock_enabled": enabled, "ready": ready,"face_onboarding_done":face_done,"face_enabled":face_enabled,
                "username": person["username"] if authenticated and person else "",
                "phone_masked": person["phone"][:3] + "****" + person["phone"][-4:] if authenticated and person else ""}

    def authenticated(request):
        key = token_hash(request.cookies.get("bank_session", ""))
        if not identity_status(key)["authenticated"]:
            raise HTTPException(401, "请先完成短信认证。")
        return key

    app.state.auth_status = identity_status

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        path = request.url.path
        from .account_lock import locked, ALLOWED
        with database.connection() as db:
            system_locked = locked(db)
        if path.startswith('/api/') and system_locked and path not in ALLOWED:
            return JSONResponse({'detail':'系统已安全锁定，仅可申请人工解锁。','system_locked':True},status_code=423,headers={'X-System-Locked':'1'})
        support = system_locked and path in {'/api/handoff','/api/handoff/messages','/api/handoff/end'}
        if support and settings.app_auth_enabled and not identity_status(token_hash(request.cookies.get('bank_session','')))['authenticated']:
            return JSONResponse({'detail':'请先使用手机号登录后联系人工。'},status_code=401)
        if settings.app_auth_enabled and not support and path.startswith("/api/") and not path.startswith("/api/auth/") and path not in {"/api/health", "/api/internal/feishu", '/api/safety/guard', '/api/safety/unlock-request'}:
            state = identity_status(token_hash(request.cookies.get("bank_session", "")))
            onboarding_contact = path.startswith("/api/contacts") and state["authenticated"] and not state["contacts_done"]
            if not state["ready"] and not onboarding_contact:
                return JSONResponse({"detail": "请先登录、完成引导并解锁应用。"}, status_code=401)
        response = await call_next(request)
        with database.connection() as db:
            if locked(db): response.headers['X-System-Locked']='1'
        return response

    @app.get("/api/auth/status")
    def status(request: Request, response: Response):
        return identity_status(ensure_session(request, response))

    @app.post("/api/auth/open")
    def open_app(request: Request, response: Response):
        key = ensure_session(request, response)
        with database.transaction() as db:
            db.execute("UPDATE app_sessions SET unlocked=0 WHERE token_hash=?", (key,))
        return identity_status(key)

    @app.post("/api/auth/sms")
    def send_sms(body: SMSBody, request: Request, response: Response):
        key = ensure_session(request, response)
        with database.transaction() as db:
            person = db.execute("SELECT * FROM app_identity WHERE id=1").fetchone()
            if body.purpose == "register" and person:
                raise BankError("本模拟应用已有注册账号，请使用手机号短信登录。")
            if body.purpose == "login" and (not person or person["phone"] != body.phone):
                raise BankError("该手机号尚未注册，请先注册应用。")
            previous = db.execute("SELECT sent_at FROM sms_challenges WHERE token_hash=?", (key,)).fetchone()
            if previous and time.time() - previous[0] < 60:
                raise BankError("验证码已发送，请 60 秒后再试。")
            code = f"{secrets.randbelow(1000000):06d}"
            db.execute("INSERT OR REPLACE INTO sms_challenges VALUES (?,?,?,?,?,?,0)", (key, body.phone, body.purpose, token_hash(code), time.time() + 300, time.time()))
        return {"demo_code": code, "phone_masked": body.phone[:3] + "****" + body.phone[-4:], "expires_in": 300, "simulated": True}

    @app.post("/api/auth/login")
    def login(body: LoginBody, request: Request):
        key = token_hash(request.cookies.get("bank_session", ""))
        error = None
        with database.transaction() as db:
            challenge = db.execute("SELECT * FROM sms_challenges WHERE token_hash=?", (key,)).fetchone()
            if not challenge or challenge["phone"] != body.phone or challenge["purpose"] != body.purpose or challenge["expires_at"] <= time.time() or challenge["attempts"] >= 5:
                error = "验证码无效或已过期，请重新获取。"
            elif not secrets.compare_digest(challenge["code_hash"], token_hash(body.code)):
                db.execute("UPDATE sms_challenges SET attempts=attempts+1 WHERE token_hash=?", (key,))
                error = "验证码错误。"
            else:
                person = db.execute("SELECT * FROM app_identity WHERE id=1").fetchone()
                if body.purpose == "register":
                    if person or not body.username.strip():
                        error = "账号已注册或用户名为空，请检查信息。"
                    else:
                        db.execute("INSERT INTO app_identity(id,username,phone) VALUES (1,?,?)", (body.username.strip(), body.phone))
                elif not person or person["phone"] != body.phone:
                    error = "登录手机号不匹配。"
                if not error:
                    db.execute("DELETE FROM sms_challenges WHERE token_hash=?", (key,))
                    db.execute("UPDATE app_sessions SET authenticated=1,unlocked=1 WHERE token_hash=?", (key,))
                    from .safety import record_sms_login
                    record_sms_login(db, key)
        if error:
            raise BankError(error)
        return identity_status(key)

    @app.post("/api/auth/contacts-done")
    def contacts_done(request: Request):
        key = authenticated(request)
        with database.transaction() as db:
            db.execute("UPDATE app_identity SET contacts_done=1 WHERE id=1")
        return identity_status(key)

    @app.post('/api/auth/pin/sms')
    def pin_sms(body: SetPINBody, request: Request):
        key=authenticated(request)
        from .pin_change import send
        return send(database,key,body)

    @app.post('/api/auth/face-onboarding')
    def face_onboarding(request:Request):
        key=authenticated(request)
        if not identity_status(key)['ready']:raise HTTPException(401,'请先完成注册并设置支付密码。')
        with database.transaction() as db:db.execute('UPDATE face_onboarding SET done=1 WHERE id=1')
        return identity_status(key)

    @app.post("/api/auth/pin")
    def set_pin(body: SetPINBody, request: Request):
        key = authenticated(request)
        from .pin_change import apply
        apply(database,key,body)
        return identity_status(key)

    @app.post("/api/auth/unlock")
    def unlock(body: PINBody, request: Request, response: Response):
        key = ensure_session(request, response)
        verify_pin(database, body.pin)
        with database.transaction() as db:
            db.execute("UPDATE app_sessions SET authenticated=1,unlocked=1 WHERE token_hash=?", (key,))
        return identity_status(key)

    @app.post("/api/auth/lock-setting")
    def lock_setting(body: LockBody, request: Request):
        key = authenticated(request)
        verify_pin(database, body.pin)
        with database.transaction() as db:
            db.execute("UPDATE app_identity SET lock_enabled=? WHERE id=1", (int(body.enabled),))
        return identity_status(key)

    @app.post("/api/auth/logout")
    def logout(request: Request):
        key = token_hash(request.cookies.get("bank_session", ""))
        with database.transaction() as db:
            db.execute("UPDATE app_sessions SET authenticated=0,unlocked=0 WHERE token_hash=?", (key,))
        return identity_status(key)
