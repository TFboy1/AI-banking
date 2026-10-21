import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import Query

from .db import TZ
from .market_clock import checkpoint

CATALOG = [{"symbol": code, "name": name} for code, name in [
    ("000001", "平安银行"), ("600519", "贵州茅台"), ("300750", "宁德时代"), ("002594", "比亚迪"),
    ("600036", "招商银行"), ("601318", "中国平安"), ("000858", "五粮液"), ("000333", "美的集团"),
    ("600900", "长江电力"), ("601398", "工商银行"), ("600030", "中信证券"), ("300059", "东方财富"),
    ("002415", "海康威视"), ("601012", "隆基绿能"), ("600276", "恒瑞医药"), ("688981", "中芯国际"),
    ("000651", "格力电器"), ("601899", "紫金矿业"), ("600887", "伊利股份"), ("601888", "中国中免"),
]]


class StockService:
    def __init__(self, cache_path: Path):
        self.path = cache_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS stock_cache (key TEXT PRIMARY KEY,payload TEXT NOT NULL,fetched REAL NOT NULL)")
        self.limit = asyncio.Semaphore(2)
        self.locks = {}
        self.failures = {}

    def dated_payload(self, payload, fetched, operation):
        if operation != 'history' or fetched is None:
            return payload
        at = datetime.fromtimestamp(fetched, TZ)
        return {**payload, 'rows': [{**r, 'close_confirmed': r['date'] < at.date().isoformat() or (r['date'] == at.date().isoformat() and at.hour >= 15)} for r in payload['rows']]}

    def peek(self, operation, symbol='', adjust=''):
        """Read an existing provider result without waiting for a new download."""
        with sqlite3.connect(self.path) as db:
            row = db.execute('SELECT payload,fetched FROM stock_cache WHERE key=?', (f'{operation}:{symbol}:{adjust}',)).fetchone()
        if not row:
            return None
        age = max(0, time.time()-row[1])
        stale = row[1] < checkpoint().timestamp() or bool(self.failures.get(f'{operation}:{symbol}:{adjust}'))
        return {**self.dated_payload(json.loads(row[0]), row[1], operation), 'status': 'stale' if stale else 'ok', 'stale': stale, 'age_seconds': age, 'fetched_at': datetime.fromtimestamp(row[1], TZ).isoformat(timespec='seconds')}

    async def worker(self, operation, symbol="", adjust=""):
        async with self.limit:
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "banking.stock_worker", operation, *([symbol, adjust] if operation == "history" else []), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=45)
            except (TimeoutError, asyncio.CancelledError):
                if process.returncode is None:
                    process.kill()
                await process.communicate()
                raise
            result = json.loads(stdout.decode("utf-8"))
            if not result.get("ok"):
                raise ValueError(result.get("error", "行情获取失败"))
            return result["data"]

    async def get(self, operation, symbol="", adjust=""):
        key = f"{operation}:{symbol}:{adjust}"
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            with sqlite3.connect(self.path) as db:
                row = db.execute("SELECT payload,fetched FROM stock_cache WHERE key=?", (key,)).fetchone()
            payload, fetched = (json.loads(row[0]), row[1]) if row else (None, None)
            error = ""
            if fetched is None or fetched < checkpoint().timestamp():
                failure = self.failures.get(key)
                if failure and failure[0] > time.time():
                    error = failure[1]
                else:
                    try:
                        payload = await self.worker(operation, symbol, adjust)
                        fetched = time.time()
                        with sqlite3.connect(self.path) as db:
                            db.execute("INSERT OR REPLACE INTO stock_cache VALUES (?,?,?)", (key, json.dumps(payload, ensure_ascii=False, allow_nan=False), fetched))
                        self.failures.pop(key, None)
                    except (TimeoutError, ValueError, OSError, json.JSONDecodeError):
                        error = "行情源暂时不可用或响应超时，请稍后再试。"
                        self.failures[key] = (time.time() + 60, error)
            return {**self.dated_payload(payload or {"rows": [], "source": "AKShare"}, fetched, operation), "status": "stale" if error and payload else "unavailable" if error else "ok", "stale": bool(error), "error": error, "fetched_at": datetime.fromtimestamp(fetched, TZ).isoformat(timespec="seconds") if fetched is not None else None, "update_schedule": "工作日09:30、15:00；节假日或源无新数据时保留最近行情", "simulated": False}


def register_stocks(app, cache_path):
    service = StockService(cache_path)
    app.state.stock_service = service

    @app.get("/api/stocks/catalog")
    def catalog(q: str = Query(default="", max_length=50)):
        keyword = q.strip().lower()
        return {"items": [s for s in CATALOG if not keyword or keyword in s["symbol"] or keyword in s["name"].lower()], "scope": "测试股票池：20只代表性A股"}

    @app.get("/api/stocks/quotes")
    async def quotes(q: str = Query(default="", max_length=50), limit: int = Query(default=100, ge=1, le=6000)):
        from .stock_valuation import daily_quotes
        result = await daily_quotes(service, CATALOG)
        keyword = q.strip().lower()
        selected = [r for r in result["rows"] if r["symbol"] in {s["symbol"] for s in CATALOG} and (not keyword or keyword in r["symbol"] or keyword in r["name"].lower())]
        return {**result, "rows": selected[:limit], "total": len(selected)}

    @app.get("/api/stocks/{symbol}/history")
    async def history(symbol: str, days: int = Query(default=120, ge=5, le=360), adjust: Literal["none", "qfq"] = "none"):
        import re

        from fastapi import HTTPException

        if not re.fullmatch(r"\d{6}", symbol):
            raise HTTPException(422, "请输入六位股票代码。")
        if symbol not in {s["symbol"] for s in CATALOG}:
            raise HTTPException(400, "测试版本仅支持股票池中的20只代表性股票。")
        from .stock_valuation import read_history
        try:
            result = await read_history(service, symbol, "" if adjust == "none" else adjust)
        except TimeoutError:
            result = {'rows': [], 'status': 'unavailable', 'stale': False, 'source': 'AKShare', 'error': '首次加载正在补齐历史数据，请稍后刷新。', 'fetched_at': None}
        point = checkpoint()
        rows = [{"date": r['date'], "open": r['open'], "close": None if r.get('close_confirmed') is False or (r['date'] == point.date().isoformat() and point.hour == 9) else r['close']} for r in result['rows'][-days:]]
        return {**result, "symbol": symbol, "rows": rows, "quote_date": rows[-1]['date'] if rows else None}
