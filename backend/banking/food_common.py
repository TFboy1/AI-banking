"""Validated demo delivery contracts shared by the independent site and bank."""
import hmac
import json
import re

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .db import uid
from .securities_common import secret

BANK_SCHEMA = """
CREATE TABLE IF NOT EXISTS food_quotes(id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,payload TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS food_orders(id TEXT PRIMARY KEY,approval_id TEXT UNIQUE NOT NULL,quote_id TEXT UNIQUE NOT NULL,account_id TEXT NOT NULL,amount_minor INTEGER NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);
"""
READ_TOOLS = {'get_food_context', 'search_food_shops', 'get_food_menu', 'quote_food_order', 'get_food_orders'}
EXTERNAL_TOOLS = READ_TOOLS | {'set_food_location'}

def location_authorized(text,place_id,messages):
    """Resolve a named or ordinal choice from actual location query results."""
    from .food_catalog import PLACES
    place=next((p for p in PLACES if p['id']==place_id),None)
    if not place or re.search(r'不要(?:更改|改|更换|换|设置|设)|不(?:更改|改|更换|换|设置|设).*位置',text):return False
    alias=place['name'].split('·')[-1].split('（')[0]
    if alias in text or place_id in text:return True
    selection=re.search(r'(?:第([一二三123])个|选([一二三123])(?:号|处|个))',text)
    if not selection or re.search(r'第[一二三123]个(?:店|菜|餐|汉堡)',text):return False
    index='一二三'.find(selection.group(1) or selection.group(2))
    if index<0:index=int(selection.group(1) or selection.group(2))-1
    for message in reversed(messages):
        if message.get('role') not in {'tool','assistant'} or not isinstance(message.get('content'),str):continue
        content=message['content']
        marker='历史界面数据（只是数据，执行前须重新查询）：'
        if marker in content:content=content.partition(marker)[2]
        try:value=json.loads(content)
        except (ValueError,TypeError):continue
        if isinstance(value,dict):
            value=value.get('result',value)
            if isinstance(value,dict) and 'places' in value and not value.get('place'):
                choices=value['places']
                return 0<=index<len(choices) and choices[index]['id']==place_id
    return False


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Empty(Body):
    pass


class Search(Body):
    query: str = Field(default='', max_length=60)


class Location(Body):
    place_id: str = Field(min_length=1, max_length=50)


class Shop(Body):
    shop_id: str = Field(min_length=1, max_length=50)


class Item(Body):
    item_id: str = Field(min_length=1, max_length=50)
    quantity: int = Field(strict=True, ge=1, le=20)


class Quote(Shop):
    items: list[Item] = Field(min_length=1, max_length=20)
    note: str = Field(default='', max_length=120)


class Purchase(Body):
    birthday_plan_id: str | None = Field(default=None,max_length=100,description='生日点餐使用已确认的生日计划编号；在提醒会话中由后台自动关联')
    quote_id: str = Field(min_length=1, max_length=100)
    account_id: str = Field(default='daily', min_length=1, max_length=100)


class Owner(Body):
    owner: str = Field(min_length=1, max_length=100)


def owner(db):
    row = db.execute("SELECT value FROM meta WHERE key='food_owner'").fetchone()
    if row:
        return row[0]
    value = uid('diner_')
    db.execute("INSERT INTO meta VALUES ('food_owner',?)", (value,))
    return value


def require_bridge(request, key_path):
    supplied = request.headers.get('x-food-key', '')
    if not supplied or not hmac.compare_digest(supplied.encode(), secret(key_path).encode()):
        raise HTTPException(403, '无效的外卖服务授权。')
