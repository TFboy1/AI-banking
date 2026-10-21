"""Persistent per-user transaction protection and per-platform circuit breakers."""
import asyncio
import json
import secrets
import time
import re

from .bank import BankError
from .db import now

SCHEMA = """
CREATE TABLE IF NOT EXISTS safety_circuits(scope TEXT PRIMARY KEY,failures INTEGER NOT NULL DEFAULT 0,
 opened_at REAL NOT NULL DEFAULT 0,blocked_until REAL NOT NULL DEFAULT 0,requires_reauth INTEGER NOT NULL DEFAULT 0,
 generation INTEGER NOT NULL DEFAULT 0,probe_token TEXT NOT NULL DEFAULT '',probe_until REAL NOT NULL DEFAULT 0,reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS safety_counters(kind TEXT PRIMARY KEY,failures INTEGER NOT NULL,last_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS safety_events(id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT NOT NULL,kind TEXT NOT NULL,
 detail TEXT NOT NULL,reference TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS safety_logins(session_key TEXT PRIMARY KEY,verified_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS safety_recovery(session_key TEXT PRIMARY KEY,code_hash TEXT NOT NULL,expires_at REAL NOT NULL,
 sent_at REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,generation INTEGER NOT NULL);
"""
USER = 'transactions'
LABELS = {'transactions': '交易', 'securities': '证券平台', 'food': '外卖平台', 'travel': '旅行平台'}


def audit(db, scope, kind, detail, reference=None):
    reference = reference if isinstance(reference, str) and re.fullmatch(r'(?:op_|task_)[0-9a-f]{32}', reference) else None
    db.execute('INSERT INTO safety_events(scope,kind,detail,reference,created_at) VALUES (?,?,?,?,?)', (scope, kind, detail, reference, now()))


def circuit(db, scope):
    db.execute('INSERT OR IGNORE INTO safety_circuits(scope) VALUES (?)', (scope,))
    return db.execute('SELECT * FROM safety_circuits WHERE scope=?', (scope,)).fetchone()


def expire_user(db):
    row = circuit(db, USER)
    if row['blocked_until'] and row['blocked_until'] <= time.time() and not row['requires_reauth']:
        db.execute("UPDATE safety_circuits SET failures=0,opened_at=0,blocked_until=0,reason='',generation=generation+1 WHERE scope=?", (USER,))
        db.execute("DELETE FROM safety_counters WHERE kind IN ('pin','sms','face')")
        db.execute('UPDATE app_identity SET failures=0,blocked_until=0 WHERE id=1')
        audit(db, USER, 'recovered', '冷却时间结束，交易保护解除。')
    return circuit(db, USER)


def check_user(db):
    from .account_lock import check
    check(db)
    row = expire_user(db)
    if row['requires_reauth']:
        raise BankError('为保护账户，交易已暂停。请重新使用手机号短信登录，再到设置的“安全状态”完成短信验证并恢复交易。查询和取消操作仍可使用。')
    if row['blocked_until'] > time.time():
        remaining = max(1, int(row['blocked_until'] - time.time() + 0.999))
        raise BankError(f'为保护账户，交易已暂时暂停（安全锁定），剩余{remaining}秒。你仍可查看账户、流水和安全记录。')


def check_business(db, name):
    check_user(db)
    scope = 'securities' if name in {'purchase_stock_basket', 'sell_stock_basket'} else 'food' if name == 'place_food_order' else 'travel' if name in {'place_travel_order','change_travel_order'} else None
    if scope and circuit(db, scope)['blocked_until']:
        raise BankError(f'{LABELS[scope]}处于故障保护中，请等待冷却后重新查询并报价，确认服务恢复后再交易；尚未扣款。')


def failure(db, kind, reference=None):
    row = db.execute('SELECT * FROM safety_counters WHERE kind=?', (kind,)).fetchone()
    count = row['failures'] if row and (kind != 'suspicious' or time.time() - row['last_at'] <= 60) else 0
    count += 1
    db.execute('INSERT OR REPLACE INTO safety_counters VALUES (?,?,?)', (kind, count, time.time()))
    labels = {'pin': '支付密码校验失败', 'sms': '短信验证码校验失败', 'face':'人脸核验失败','suspicious': '拒绝可疑操作请求'}
    audit(db, USER, kind + '_failure', labels[kind] + f'（连续{count}次）', reference)
    if count == (3 if kind == 'suspicious' else 5):
        old = circuit(db, USER)
        reauth = kind == 'suspicious' or old['requires_reauth']
        reason = '短时间多次请求不存在或不属于当前会话的确认，需重新认证。' if reauth else labels[kind] + '连续5次，暂停交易60秒。'
        db.execute('UPDATE safety_circuits SET failures=?,opened_at=?,blocked_until=?,requires_reauth=?,generation=generation+1,reason=? WHERE scope=?',
                   (count, time.time(), time.time() + 60, int(reauth), reason, USER))
        db.execute('DELETE FROM safety_recovery')
        audit(db, USER, 'opened', reason, reference)
        from .account_lock import trip
        trip(db,kind)
    return count


def success(db, kind):
    db.execute('DELETE FROM safety_counters WHERE kind=?', (kind,))


def suspicious(database, reference=None):
    with database.transaction() as db:
        failure(db, 'suspicious', reference)


def record_sms_login(db, session_key):
    # Multi-user SMS login runs before the tenant app's first startup migration.
    db.execute('CREATE TABLE IF NOT EXISTS safety_logins(session_key TEXT PRIMARY KEY,verified_at REAL NOT NULL)')
    db.execute('INSERT OR REPLACE INTO safety_logins VALUES (?,?)', (session_key, time.time()))


def admit_platform(database, scope):
    with database.transaction() as db:
        row = circuit(db, scope)
        if row['blocked_until'] > time.time():
            raise BankError(f"{LABELS[scope]}连续故障，服务已暂时暂停，剩余{max(1, int(row['blocked_until']-time.time()+.999))}秒。其他业务仍可使用。")
        token = ''
        if row['blocked_until']:
            if row['probe_until'] > time.time():
                raise BankError(f'{LABELS[scope]}正在检查恢复情况，请稍后重试。')
            token = secrets.token_hex(16)
            db.execute('UPDATE safety_circuits SET probe_token=?,probe_until=?,generation=generation+1 WHERE scope=?', (token, time.time()+135, scope))
            audit(db, scope, 'probe', '冷却结束，允许一次恢复探测。')
            row = circuit(db, scope)
        return row['generation'], token


def finish_platform(database, scope, ticket, failed):
    with database.transaction() as db:
        row = circuit(db, scope)
        if row['generation'] != ticket[0] or row['probe_token'] != ticket[1]:
            return
        if failed:
            count = row['failures'] + 1
            audit(db, scope, 'service_failure', '平台请求连接失败、超时或返回服务异常。')
            if count >= 3 or ticket[1]:
                db.execute("UPDATE safety_circuits SET failures=?,opened_at=?,blocked_until=?,generation=generation+1,probe_token='',probe_until=0,reason=? WHERE scope=?",
                           (count, time.time(), time.time()+60, '平台连续故障，暂停调用60秒。', scope))
                audit(db, scope, 'opened', '平台故障保护已开启，60秒后允许一次探测。')
            else:
                db.execute('UPDATE safety_circuits SET failures=? WHERE scope=?', (count, scope))
        else:
            db.execute("UPDATE safety_circuits SET failures=0,opened_at=0,blocked_until=0,probe_token='',probe_until=0,reason='',generation=generation+? WHERE scope=?", (int(bool(ticket[1])), scope))
            if ticket[1]:
                audit(db, scope, 'recovered', '恢复探测成功，平台服务已恢复。')


async def platform_request(database, scope, operation):
    import httpx
    ticket = admit_platform(database, scope)
    try:
        response = await operation()
        if response.status_code >= 500 or response.status_code == 429:
            finish_platform(database, scope, ticket, True)
            raise BankError(f'{LABELS[scope]}暂时服务异常，请稍后重试；尚未扣款。')
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('Invalid platform response shape')
    except (httpx.HTTPError, TimeoutError, ValueError):
        finish_platform(database, scope, ticket, True)
        raise BankError(f'{LABELS[scope]}暂时无法连接或返回无效数据，请稍后重试；尚未扣款。') from None
    except asyncio.CancelledError:
        # User cancellation is not an upstream failure. Release a probe lease.
        if ticket[1]:
            with database.transaction() as db:
                db.execute("UPDATE safety_circuits SET probe_token='',probe_until=0,generation=generation+1 WHERE scope=? AND generation=?", (scope, ticket[0]))
        raise
    finish_platform(database, scope, ticket, False)
    if not response.is_success:
        raise BankError(str(payload.get('detail', f'{LABELS[scope]}请求未完成。')))
    return payload


def snapshot(db, session_key):
    from .account_lock import status
    expire_user(db)
    scopes = [dict(circuit(db, scope)) for scope in (USER, 'securities', 'food', 'travel')]
    login = db.execute('SELECT verified_at FROM safety_logins WHERE session_key=?', (session_key,)).fetchone()
    return {'guard':status(db),'server_now': time.time(), 'scopes': [{**r, 'label': LABELS[r['scope']], 'probe_token': None} for r in scopes],
            'can_recover': bool(login and login[0] > scopes[0]['opened_at']),
            'events': [dict(r) for r in db.execute('SELECT scope,kind,detail,reference,created_at FROM safety_events ORDER BY id DESC LIMIT 50')]}
