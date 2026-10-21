"""Independent food login; bank cookies never authenticate this website."""
import secrets
import time

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .db import uid
from .securities_common import token_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS food_users(id TEXT PRIMARY KEY,phone TEXT UNIQUE NOT NULL,label TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS food_sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS food_sms(challenge TEXT PRIMARY KEY,phone TEXT NOT NULL,code_hash TEXT NOT NULL,expires REAL NOT NULL,sent REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS food_auth_limits(client TEXT PRIMARY KEY,window INTEGER NOT NULL,attempts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS food_bindings(bank_owner TEXT PRIMARY KEY,food_user TEXT NOT NULL UNIQUE,bank_label TEXT NOT NULL);
"""


class PhoneBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    phone: str = Field(pattern=r'^1[0-9]{10}$')


class LoginBody(PhoneBody):
    code: str = Field(pattern=r'^[0-9]{6}$')


def cookie(response, request, name, value, age):
    response.set_cookie(name, value, httponly=True, samesite='strict', secure=request.url.scheme == 'https', max_age=age)


def register_food_auth(app, connection):
    with connection() as db:
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO food_users VALUES ('food_demo','11111111111','演示账户')")

    def identity(request):
        with connection() as db:
            row = db.execute('SELECT u.*,b.bank_owner,b.bank_label FROM food_sessions s JOIN food_users u ON u.id=s.user_id LEFT JOIN food_bindings b ON b.food_user=u.id WHERE s.token_hash=? AND s.expires>?', (token_hash(request.cookies.get('food_auth', '')), time.time())).fetchone()
        if not row:
            raise HTTPException(401, '请先使用手机号短信登录外卖账户。')
        expected = request.headers.get('x-food-account')
        if expected and expected != row['id']:
            raise HTTPException(409, '登录账户已切换，请刷新页面。')
        return dict(row)

    @app.post('/api/food/auth/sms')
    def sms(body: PhoneBody, request: Request, response: Response):
        client = request.client.host if request.client else 'unknown'
        window = int(time.time() // 60)
        with connection() as db:
            db.execute('INSERT INTO food_auth_limits VALUES (?,?,1) ON CONFLICT(client) DO UPDATE SET attempts=CASE WHEN window=excluded.window THEN attempts+1 ELSE 1 END,window=excluded.window', (client, window))
            count = db.execute('SELECT attempts FROM food_auth_limits WHERE client=?', (client,)).fetchone()[0]
        if count > 10:
            raise HTTPException(429, '短信请求过于频繁，请稍后再试。')
        challenge = request.cookies.get('food_login', '') or secrets.token_urlsafe(32)
        key = token_hash(challenge)
        with connection() as db:
            previous = db.execute('SELECT MAX(sent) FROM food_sms WHERE phone=? OR challenge=?', (body.phone, key)).fetchone()[0]
            if previous and time.time() - previous < 60:
                raise HTTPException(429, '验证码已发送，请60秒后再试。')
            code = f'{secrets.randbelow(1000000):06d}'
            db.execute('INSERT OR REPLACE INTO food_sms VALUES (?,?,?,?,?,0)', (key, body.phone, token_hash(code), time.time() + 300, time.time()))
        cookie(response, request, 'food_login', challenge, 600)
        return {'demo_code': code, 'phone_masked': body.phone[:3] + '****' + body.phone[-4:], 'expires_in': 300, 'simulated': True}

    @app.post('/api/food/auth/login')
    def login(body: LoginBody, request: Request, response: Response):
        error = None
        token = secrets.token_urlsafe(32)
        key = token_hash(request.cookies.get('food_login', ''))
        with connection() as db:
            row = db.execute('SELECT * FROM food_sms WHERE challenge=?', (key,)).fetchone()
            if not row or row['phone'] != body.phone or row['expires'] <= time.time() or row['attempts'] >= 5:
                error = '验证码无效或已过期，请重新获取。'
            elif not secrets.compare_digest(row['code_hash'], token_hash(body.code)):
                db.execute('UPDATE food_sms SET attempts=attempts+1 WHERE challenge=?', (key,))
                error = '验证码错误。'
            else:
                db.execute('DELETE FROM food_sms WHERE challenge=?', (key,))
                db.execute('INSERT OR IGNORE INTO food_users VALUES (?,?,?)', (uid('fooduser_'), body.phone, '外卖用户' + body.phone[-4:]))
                user = db.execute('SELECT id FROM food_users WHERE phone=?', (body.phone,)).fetchone()[0]
                db.execute('DELETE FROM food_sessions WHERE token_hash=?', (token_hash(request.cookies.get('food_auth', '')),))
                db.execute('INSERT INTO food_sessions VALUES (?,?,?)', (token_hash(token), user, time.time() + 30 * 86400))
        if error:
            raise HTTPException(400, error)
        cookie(response, request, 'food_auth', token, 30 * 86400)
        response.delete_cookie('food_login')
        return {'authenticated': True}

    @app.post('/api/food/auth/logout')
    def logout(request: Request, response: Response):
        with connection() as db:
            db.execute('DELETE FROM food_sessions WHERE token_hash=?', (token_hash(request.cookies.get('food_auth', '')),))
        response.delete_cookie('food_auth')
        response.delete_cookie('food_login')
        return {'authenticated': False}

    return identity
