"""Third protection episode locks the app; only a trusted local operator can review."""
import json

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from .bank import BankError
from .db import dumps, event, now, uid

SCHEMA = """
CREATE TABLE IF NOT EXISTS account_guard(id INTEGER PRIMARY KEY CHECK(id=1),credential_trips INTEGER NOT NULL DEFAULT 0,
 suspicious_trips INTEGER NOT NULL DEFAULT 0,locked INTEGER NOT NULL DEFAULT 0,locked_at TEXT,reason TEXT NOT NULL DEFAULT '');
INSERT OR IGNORE INTO account_guard(id) VALUES(1);
CREATE TABLE IF NOT EXISTS manual_unlock_requests(id TEXT PRIMARY KEY,status TEXT NOT NULL,note TEXT NOT NULL,
 created_at TEXT NOT NULL,reviewed_at TEXT,operator TEXT,review_note TEXT);
"""
ALLOWED = {'/api/health', '/api/auth/open', '/api/auth/status', '/api/auth/sms', '/api/auth/login', '/api/auth/logout',
           '/api/safety/guard', '/api/safety/unlock-request', '/api/handoff', '/api/handoff/messages', '/api/handoff/end', '/api/internal/feishu'}


def locked(db):
    row = db.execute('SELECT locked FROM account_guard WHERE id=1').fetchone()
    return bool(row and row[0])


def check(db):
    if locked(db):
        raise BankError('系统已安全锁定，仅可申请人工解锁。重新登录、短信恢复或等待冷却均不能解除。')


def trip(db, kind):
    if locked(db):
        return
    field = 'suspicious_trips' if kind == 'suspicious' else 'credential_trips'
    db.execute(f'UPDATE account_guard SET {field}={field}+1 WHERE id=1')
    row = db.execute('SELECT * FROM account_guard WHERE id=1').fetchone()
    if row[field] < 3:
        return
    reason = ('可疑请求保护' if kind == 'suspicious' else '密码、验证码或人脸保护') + '累计触发3次，系统已安全锁定，需人工审核解锁。'
    db.execute('UPDATE account_guard SET locked=1,locked_at=?,reason=? WHERE id=1', (now(), reason))
    db.execute('DELETE FROM safety_recovery')
    db.execute('DELETE FROM payment_sms')
    # Fence every running or queued job, retaining request state and receipts.
    for task in db.execute("SELECT * FROM tasks WHERE status IN ('running','queued','waiting_confirmation')").fetchall():
        state = json.loads(task['state_json'])
        state['manual_lock_checkpoint'] = {'saved_at': now(), 'previous_status': task['status']}
        db.execute("UPDATE tasks SET status='failed',error=?,state_json=? WHERE id=?", (reason, dumps(state), task['id']))
        for approval in db.execute("SELECT * FROM approvals WHERE task_id=? AND status='pending'", (task['id'],)).fetchall():
            from .db import update_message
            data = json.loads(db.execute('SELECT data_json FROM messages WHERE id=?',(approval['message_id'],)).fetchone()[0])
            data.update(status='failed',error='系统安全锁定，该未完成交易已停止，未扣款。')
            db.execute("UPDATE approvals SET status='failed' WHERE id=?",(approval['id'],))
            update_message(db,approval['message_id'],data)
    from .safety import audit
    audit(db,'transactions','system_locked',reason)
    event(db,'*','system.locked',{'system_locked': True})


def status(db):
    row = dict(db.execute('SELECT * FROM account_guard WHERE id=1').fetchone())
    request = db.execute('SELECT id,status,created_at,reviewed_at,review_note FROM manual_unlock_requests ORDER BY rowid DESC LIMIT 1').fetchone()
    return {'system_locked': bool(row['locked']), 'reason': row['reason'], 'locked_at': row['locked_at'],
            'credential_trips': row['credential_trips'], 'suspicious_trips': row['suspicious_trips'],
            'request': dict(request) if request else None}


class UnlockRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    note: str = Field(min_length=1,max_length=500)


def register(app,database,session):
    @app.get('/api/safety/guard')
    def guard(session_key: str = Depends(session)):
        with database.connection() as db:
            return status(db)

    @app.post('/api/safety/unlock-request')
    def request(body: UnlockRequest,session_key: str = Depends(session)):
        with database.transaction() as db:
            if not locked(db):
                raise BankError('当前系统未锁定，无需申请人工解锁。')
            if not body.note.strip():
                raise BankError('请填写申请说明。')
            if not db.execute("SELECT 1 FROM manual_unlock_requests WHERE status='pending'").fetchone():
                db.execute('INSERT INTO manual_unlock_requests VALUES (?,?,?,?,NULL,NULL,NULL)',(uid('review_'),'pending',body.note.strip(),now()))
                from .safety import audit
                audit(db,'transactions','manual_requested','已提交人工解锁申请，等待管理员审核。')
            from . import human_service as human
            row=db.execute('SELECT active_conversation_id FROM sessions WHERE token_hash=?',(session_key,)).fetchone()
            conversation=row[0] if row and row[0] else None
            if not conversation or not db.execute('SELECT 1 FROM conversations WHERE id=? AND deleted_at IS NULL',(conversation,)).fetchone():
                conversation=database.conversation_id(db)
            previous=human.current(db,conversation)
            service=human.open_session(db,conversation)
            if not previous or previous['status']=='closed':
                from .db import add_message
                add_message(db,conversation,None,'user','text',body.note.strip(),{'human_session_id':service['id']})
                add_message(db,conversation,None,'assistant','text','人工解锁申请已送达客服工作台，可以在这里继续与客服交流。',{'human_session_id':service['id']})
            db.execute('UPDATE sessions SET active_conversation_id=? WHERE token_hash=?',(conversation,session_key))
            return status(db)


def review(database,request_id,approve,operator,note):
    """Local operator API, deliberately not exposed as an HTTP endpoint."""
    if not operator.strip() or not note.strip():
        raise BankError('人工审核需填写审核人和审核说明。')
    with database.transaction() as db:
        request = db.execute('SELECT * FROM manual_unlock_requests WHERE id=?',(request_id,)).fetchone()
        if not request or request['status'] != 'pending' or not locked(db):
            raise BankError('申请不存在、已审核或账户不再锁定。')
        db.execute('UPDATE manual_unlock_requests SET status=?,reviewed_at=?,operator=?,review_note=? WHERE id=?',
                   ('approved' if approve else 'rejected',now(),operator.strip(),note.strip(),request_id))
        if approve:
            db.execute("UPDATE account_guard SET locked=0,locked_at=NULL,reason='',credential_trips=0,suspicious_trips=0 WHERE id=1")
            db.execute("UPDATE safety_circuits SET failures=0,opened_at=0,blocked_until=0,requires_reauth=0,reason='',generation=generation+1 WHERE scope='transactions'")
            db.execute('DELETE FROM safety_counters')
            db.execute('DELETE FROM safety_recovery')
            db.execute('DELETE FROM payment_sms')
            db.execute('UPDATE app_identity SET failures=0,blocked_until=0 WHERE id=1')
            db.execute('UPDATE app_sessions SET unlocked=0')
        from .safety import audit
        audit(db,'transactions','manual_approved' if approve else 'manual_rejected','人工审核通过并解锁。' if approve else '人工审核未通过，保持锁定。')
        event(db,'*','system.locked',{'system_locked': not approve})
        return status(db)
