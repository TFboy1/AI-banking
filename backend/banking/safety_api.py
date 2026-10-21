import secrets
import time

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from .bank import BankError
from .security import token_hash
from .safety import USER, audit, circuit, failure, snapshot


class RecoveryCode(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(pattern=r'^[0-9]{6}$')


def eligible(db, session_key):
    from .account_lock import check
    check(db)
    row = circuit(db, USER)
    login = db.execute('SELECT verified_at FROM safety_logins WHERE session_key=?', (session_key,)).fetchone()
    if not row['requires_reauth']:
        raise BankError('当前无需短信恢复，短暂锁定请等待冷却结束。')
    if not login or login[0] <= row['opened_at']:
        raise BankError('请先退出并重新使用手机号短信登录，再验证恢复交易。')
    return row


def register(app, database, agent, session):
    @app.get('/api/safety/status')
    def status(session_key: str = Depends(session)):
        with database.transaction() as db:
            return snapshot(db, session_key)

    @app.post('/api/safety/recovery-sms')
    def send(session_key: str = Depends(session)):
        with database.transaction() as db:
            row = eligible(db, session_key)
            previous = db.execute('SELECT sent_at FROM safety_recovery WHERE session_key=?', (session_key,)).fetchone()
            if previous and time.time()-previous[0] < 60:
                raise BankError('恢复验证码已发送，请60秒后再试。')
            code = f'{secrets.randbelow(1000000):06d}'
            db.execute('INSERT OR REPLACE INTO safety_recovery VALUES (?,?,?,?,0,?)', (session_key, token_hash(code), time.time()+300, time.time(), row['generation']))
            audit(db, USER, 'recovery_sms_sent', '重新登录后发送交易恢复验证码。')
        return {'demo_code': code, 'expires_in': 300, 'simulated': True}

    @app.post('/api/safety/recover')
    async def recover(body: RecoveryCode, session_key: str = Depends(session)):
        error = None
        with database.transaction() as db:
            row = eligible(db, session_key)
            challenge = db.execute('SELECT * FROM safety_recovery WHERE session_key=?', (session_key,)).fetchone()
            if not challenge or challenge['generation'] != row['generation'] or challenge['expires_at'] <= time.time() or challenge['attempts'] >= 5:
                error = '恢复验证码无效或已过期，请重新获取。'
            elif not secrets.compare_digest(challenge['code_hash'], token_hash(body.code)):
                db.execute('UPDATE safety_recovery SET attempts=attempts+1 WHERE session_key=?', (session_key,))
                failure(db, 'sms')
                error = '恢复验证码错误。'
            else:
                db.execute("UPDATE safety_circuits SET failures=0,opened_at=0,blocked_until=0,requires_reauth=0,reason='',generation=generation+1 WHERE scope=?", (USER,))
                db.execute('DELETE FROM safety_counters')
                db.execute('DELETE FROM safety_recovery')
                db.execute('DELETE FROM payment_sms')
                db.execute('UPDATE app_identity SET failures=0,blocked_until=0 WHERE id=1')
                audit(db, USER, 'recovered', '已重新短信登录并验证恢复，交易保护解除；原交易仍需重新确认。')
        if error:
            raise BankError(error)
        await agent.notify()
        with database.transaction() as db:
            return snapshot(db, session_key)
