"""One bank-issued code, independently redeemable once for each platform."""
import secrets
import time
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .securities_common import secret, token_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS connection_code(id INTEGER PRIMARY KEY CHECK(id=1),code_hash TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS connection_claims(platform TEXT PRIMARY KEY,platform_user TEXT NOT NULL);
"""


class Redeem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(pattern=r'^[0-9]{8}$')
    platform: Literal['securities', 'food', 'travel']
    platform_user: str = Field(min_length=1, max_length=100)


def generate(database):
    code = ''.join(secrets.choice('0123456789') for _ in range(8))
    expires = time.time()+300
    with database.transaction() as db:
        previous = db.execute('SELECT code_hash FROM connection_code WHERE id=1').fetchone()
        while previous and previous[0] == token_hash(code):
            code = ''.join(secrets.choice('0123456789') for _ in range(8))
        db.execute('INSERT OR REPLACE INTO connection_code VALUES (1,?,?)', (token_hash(code), expires))
        db.execute('DELETE FROM connection_claims')
    return {'code': code, 'expires': expires, 'expires_in': 300, 'platforms': ['securities', 'food', 'travel']}


def authorize(request, settings, platform):
    key, header = (settings.securities_bridge_key_path, 'x-securities-key') if platform == 'securities' else (settings.food_bridge_key_path, 'x-food-key')
    if platform == 'travel':key,header=settings.travel_bridge_key_path,'x-travel-key'
    supplied = request.headers.get(header, '')
    if not supplied or not secrets.compare_digest(supplied.encode(), secret(key).encode()):
        raise HTTPException(403, '无效的平台关联授权。')


def matches(db, code):
    exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='connection_code' AND type='table'").fetchone()
    return bool(exists and db.execute('SELECT 1 FROM connection_code WHERE code_hash=? AND expires>?', (token_hash(code), time.time())).fetchone())


def redeem(db, body):
    from .account_lock import check
    check(db)
    if not matches(db, body.code):
        raise HTTPException(400, '通用同步码错误、已过期或已被重新生成。')
    if db.execute('SELECT 1 FROM connection_claims WHERE platform=?', (body.platform,)).fetchone():
        raise HTTPException(400, '该平台已使用此同步码，请在有数重新生成。其他尚未关联的平台仍可使用当前码。')
    if body.platform == 'securities':
        from .securities_common import owner
    elif body.platform == 'travel':
        from .travel_common import owner
    else:
        from .food_common import owner
    investor = owner(db)
    person = db.execute('SELECT username FROM app_identity WHERE id=1').fetchone()
    db.execute('INSERT INTO connection_claims VALUES (?,?)', (body.platform, body.platform_user))
    return {'owner': investor, 'label': person[0] if person else '演示用户'}


def register_connections(app, database, settings, session):
    @app.post('/api/connections/pairing-code', dependencies=[Depends(session)])
    def pairing_code():
        return generate(database)

    @app.post('/internal/connections/redeem')
    def claim(body: Redeem, request: Request):
        authorize(request, settings, body.platform)
        with database.transaction() as db:
            return redeem(db, body)
