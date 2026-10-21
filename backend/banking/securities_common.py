import hashlib
import hmac
import re
import secrets
from datetime import date
from decimal import Decimal
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .db import uid

BANK_SCHEMA = """
CREATE TABLE IF NOT EXISTS securities_quotes (id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,payload TEXT NOT NULL,expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS securities_positions (symbol TEXT PRIMARY KEY,name TEXT NOT NULL,shares INTEGER NOT NULL,cost_minor INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS securities_orders (id TEXT PRIMARY KEY,approval_id TEXT NOT NULL UNIQUE,quote_id TEXT NOT NULL UNIQUE,account_id TEXT NOT NULL,amount_minor INTEGER NOT NULL,items_json TEXT NOT NULL,created_at TEXT NOT NULL);
"""
READ_TOOLS = {"get_securities_watchlist", "quote_stock_purchase", "quote_stock_sale", "get_securities_positions"}

EXTERNAL_TOOLS = READ_TOOLS | {"search_securities_stocks", "get_stock_price", "add_securities_watchlist"}


class StockSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=50)


class StockSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[0-9]{6}$")


class SecuritiesEmpty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StockOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[0-9]{6}$")
    shares: int = Field(strict=True, ge=100, le=100000, multiple_of=100)


class QuoteStocks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    orders: list[StockOrder] = Field(min_length=1, max_length=20)


class StockPurchaseOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[0-9]{6}$")
    shares: int | None = Field(default=None, strict=True, ge=100, le=100000, multiple_of=100)
    amount_yuan: str | None = Field(default=None, pattern=r"^[0-9]{1,7}(\.[0-9]{1,2})?$")

    @model_validator(mode="after")
    def exactly_one_quantity(self):
        if (self.shares is None) == (self.amount_yuan is None):
            raise ValueError("股数和预算金额必须且只能填写一个。")
        if self.amount_yuan is not None and Decimal(self.amount_yuan) <= 0:
            raise ValueError("预算金额必须大于零。")
        return self


class QuoteStockPurchase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    orders: list[StockPurchaseOrder] = Field(min_length=1, max_length=20)


class ValuationPosition(StockSymbol):
    name: str = Field(min_length=1, max_length=80)
    shares: int = Field(strict=True, ge=1)
    cost_minor: int = Field(strict=True, ge=0)


class ValuationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    positions: list[ValuationPosition] = Field(max_length=1000)

class HistoricalValuationRequest(ValuationRequest):
    cutoff_date:date


class PurchaseStocks(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: str = Field(min_length=1, max_length=100)
    account_id: str = Field(default="daily", min_length=1, max_length=100)


class SellStocks(PurchaseStocks):
    account_id: str = Field(min_length=1, max_length=100)


def secret(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(secrets.token_hex(32))
    except FileExistsError:
        pass
    return path.read_text(encoding="utf-8").strip()


def require_bridge(request: Request, path: Path):
    incoming = request.headers.get("x-securities-key", "")
    if not incoming or not hmac.compare_digest(incoming.encode(), secret(path).encode()):
        raise HTTPException(403, "无效的证券服务授权。")


def owner(db):
    current = db.execute("SELECT value FROM meta WHERE key='securities_owner'").fetchone()
    if current:
        return current[0]
    identifier = uid("investor_")
    db.execute("INSERT INTO meta VALUES ('securities_owner',?)", (identifier,))
    return identifier


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def quantity_authorized(text, arguments):
    def numeric(raw):
        if raw.isdigit():
            return int(raw)
        digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        total, value = 0, 0
        for char in raw:
            if char in digits:
                value = digits[char]
            else:
                total += (value or 1) * {"十": 10, "百": 100, "千": 1000}[char]
                value = 0
        return total + value
    quantities = {numeric(raw) * (100 if unit == "手" else 1) for raw, unit in re.findall(r"([0-9]+|[零一二两三四五六七八九十百千]+)\s*(股|手)", text)}
    return bool(quantities) and bool(arguments.get("orders")) and all(item.get("shares") in quantities for item in arguments["orders"])


def purchase_quantity_authorized(text, arguments):
    # A single explicit budget cannot silently authorize several stocks.
    from collections import Counter
    budgets = Counter(Decimal(raw.replace(',', '')) for raw in re.findall(r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*(?:块(?:钱)?|元|人民币)", text))
    for item in arguments.get("orders", []):
        amount = item.get("amount_yuan")
        if amount is None:
            if not quantity_authorized(text, {"orders": [item]}):
                return False
        else:
            value = Decimal(amount)
            if budgets[value] <= 0:
                return False
            budgets[value] -= 1
    return bool(arguments.get("orders"))
