"""Short-lived, account-authenticated payment links for Feishu approvals.

The link is not login or transaction authorization. Existing authenticated
sessions, PIN/SMS checks, circuits and the agent's idempotent commit are reused.
"""
import base64
import hashlib
import hmac
import json
from datetime import datetime
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from .db import TZ, dumps
from .security import MONEY_TOOLS


def public_origin(settings):
    value = (settings.feishu_public_url or settings.backend_url).rstrip('/')
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'} or
            (parsed.scheme != 'https' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'})):
        raise ValueError('飞书支付地址须为 HTTPS 站点根地址；本机测试可使用 HTTP 回环地址。')
    return value


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def scope(db, approval_id):
    row = db.execute('''SELECT a.*,t.channel,t.conversation_id,c.deleted_at FROM approvals a
        JOIN tasks t ON t.id=a.task_id JOIN conversations c ON c.id=t.conversation_id WHERE a.id=?''', (approval_id,)).fetchone()
    binding = db.execute('SELECT tenant_key,open_id,chat_id FROM feishu_binding WHERE id=1').fetchone()
    identity = db.execute('SELECT phone,pin_hash FROM app_identity WHERE id=1').fetchone()
    if not row or row['channel'] != 'feishu' or row['tool_name'] not in MONEY_TOOLS or not binding or not identity or row['deleted_at']:
        raise HTTPException(404, '找不到本账号的飞书交易。')
    nonce=db.execute("SELECT value FROM meta WHERE key='feishu_binding_nonce'").fetchone()
    return row, digest([list(binding),nonce[0] if nonce else '']), digest(list(identity))


def payment_link(db, approval_id, settings):
    if not settings.app_auth_enabled or not settings.feishu_ready:
        return None
    try:
        row, binding, identity = scope(db, approval_id)
    except HTTPException:
        return None
    payload = {'id': row['id'], 'binding': binding, 'identity': identity,
               'args': digest([row['tool_name'], row['args_json'], row['summary_json']]),
               'expires': row['expires_at']}
    raw = base64.urlsafe_b64encode(dumps(payload).encode()).decode().rstrip('=')
    signature = hmac.new(settings.feishu_bridge_token.encode(), ('feishu-pay-v1:' + raw).encode(), hashlib.sha256).hexdigest()
    return public_origin(settings) + '/feishu-pay.html#' + raw + '.' + signature


def authorize(db, token, settings):
    try:
        if not settings.app_auth_enabled or not settings.feishu_ready or len(token) > 3000:
            raise ValueError()
        raw, signature = token.split('.')
        expected = hmac.new(settings.feishu_bridge_token.encode(), ('feishu-pay-v1:' + raw).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
        row, binding, identity = scope(db, data['id'])
        if data['binding'] != binding or data['identity'] != identity or data['expires'] != row['expires_at']:
            raise ValueError()
        if datetime.fromisoformat(data['expires']) <= datetime.now(TZ):
            raise HTTPException(410, '支付链接已过期，请返回飞书重新发起并核对交易结果。')
        if row['status'] == 'pending' and data['args'] != digest([row['tool_name'], row['args_json'], row['summary_json']]):
            raise HTTPException(409, '交易内容已变化，请返回飞书使用最新确认卡。')
        return row
    except HTTPException:
        raise
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise HTTPException(403, '支付链接无效或绑定已变化，请返回飞书重新打开。') from None


class SMSRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account_id: str | None = Field(default=None, min_length=1, max_length=100)


class DecisionRequest(SMSRequest):
    face_token: str | None = Field(default=None,max_length=150)
    decision: Literal['confirm', 'cancel']
    pin: str | None = Field(default=None, pattern=r'^[0-9]{4}$')
    sms_code: str | None = Field(default=None, pattern=r'^[0-9]{6}$')


def register(app, database, settings, session, context_handler, sms_handler, decision_handler, sms_body, decision_body):

    def approval_for(request):
        with database.connection() as db:
            return authorize(db, request.headers.get('X-Feishu-Payment', ''), settings)

    @app.get('/api/feishu-pay/context')
    async def context(request: Request, session_key: str = Depends(session)):
        row = approval_for(request)
        if row['status'] != 'pending':
            return {'status': row['status'], 'receipt': json.loads(row['receipt_json'] or 'null')}
        value = await context_handler(row['id'], row['conversation_id'], session_key)
        # This page authorizes the already specified operation only. Scheduling
        # remains in the full application and cannot be injected into requests.
        return {**value, 'status': 'pending', 'schedule_allowed': False}

    @app.post('/api/feishu-pay/sms')
    async def sms(request: Request, body: SMSRequest, session_key: str = Depends(session)):
        row = approval_for(request)
        return await sms_handler(row['id'], sms_body(conversation_id=row['conversation_id'], **body.model_dump()), session_key)

    @app.post('/api/feishu-pay/decision')
    async def decision(request: Request, body: DecisionRequest, session_key: str = Depends(session)):
        row = approval_for(request)
        return await decision_handler(row['id'], decision_body(conversation_id=row['conversation_id'], **body.model_dump()), session_key)
