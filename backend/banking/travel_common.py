"""Typed contract for the independent, entirely fictional travel platform."""
import secrets
from datetime import date
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .db import uid
from .securities_common import secret

BANK_SCHEMA = """
CREATE TABLE IF NOT EXISTS travel_quotes(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,payload TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS travel_orders(id TEXT PRIMARY KEY,approval_id TEXT UNIQUE NOT NULL,quote_id TEXT UNIQUE NOT NULL,account_id TEXT NOT NULL,amount_minor INTEGER NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS travel_order_state(order_id TEXT PRIMARY KEY,version INTEGER NOT NULL,status TEXT NOT NULL,payload TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS travel_quote_claims(quote_id TEXT PRIMARY KEY,action_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS travel_changes(id TEXT PRIMARY KEY,action_id TEXT UNIQUE NOT NULL,order_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS travel_change_quotes(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,payload TEXT NOT NULL,expires REAL NOT NULL);
"""
READ_TOOLS = {'get_travel_context','search_travel_options','quote_travel_order','get_travel_orders','quote_travel_change'}
EXTERNAL_TOOLS = READ_TOOLS

class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

class Empty(Body):
    pass

class Trip(Body):
    origin: str = Field(min_length=2,max_length=20,description='用户明确指定的出发城市，不可默认')
    destination: str = Field(min_length=2,max_length=20)
    departure_date: date
    return_date: date = Field(description='返程日期，也是酒店退房日期')
    area: str = Field(min_length=1,max_length=30,description='用户指定住宿区域；无偏好时需用户明确说不限')
    passengers: int = Field(default=1,strict=True,ge=1,le=6)
    rooms: int = Field(default=1,strict=True,ge=1,le=3)

class Quote(Body):
    search_id: str = Field(min_length=1,max_length=100)
    outbound_id: str = Field(min_length=1,max_length=100,description='搜索返回的去程机票或动车编号，二选一')
    inbound_id: str = Field(min_length=1,max_length=100,description='搜索返回的返程机票或动车编号，二选一')
    hotel_id: str = Field(min_length=1,max_length=100)

class Purchase(Body):
    quote_id: str = Field(min_length=1,max_length=100)
    account_id: str = Field(default='daily',min_length=1,max_length=100)

class ChangeQuote(Body):
    order_id: str = Field(min_length=1,max_length=100)
    new_quote_id: str = Field(min_length=1,max_length=100)
    change_parts: list[Literal['outbound','inbound','hotels']] = Field(min_length=1,max_length=3,description='用户明确要求修改的部分：去程、返程、酒店；没要求的部分保持原订单')

class Cancel(Body):
    order_id: str = Field(min_length=1,max_length=100)
    order_version: int = Field(strict=True,ge=1,description='get_travel_orders返回的当前版本，订单改变后必须重新核对')

class Owner(Body):
    owner: str = Field(min_length=1,max_length=100)

def owner(db):
    row=db.execute("SELECT value FROM meta WHERE key='travel_owner'").fetchone()
    if row:return row[0]
    value=uid('traveler_')
    db.execute("INSERT INTO meta VALUES ('travel_owner',?)",(value,))
    return value

def require_bridge(request,key_path):
    supplied=request.headers.get('x-travel-key','')
    if not supplied or not secrets.compare_digest(supplied.encode(),secret(key_path).encode()):
        raise HTTPException(403,'无效的旅行平台授权。')
