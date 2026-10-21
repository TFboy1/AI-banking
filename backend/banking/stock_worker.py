"""Bounded-process AKShare adapter. Only JSON on stdout; no simulated market prices."""
import contextlib
import io
import json
import math
import re
import sys
from datetime import datetime, timedelta
from itertools import pairwise

from requests import RequestException

from .db import TZ


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def run(operation, symbol="", adjust=""):
    import akshare as ak

    if operation == "quotes":
        frame = ak.stock_zh_a_spot_em()
        rows = []
        for item in frame.to_dict("records"):
            code = str(item.get("代码", "")).zfill(6)
            if not re.fullmatch(r"\d{6}", code):
                continue
            rows.append({"symbol": code, "name": str(item.get("名称", code)), "price": number(item.get("最新价")), "change_percent": number(item.get("涨跌幅")), "change": number(item.get("涨跌额")), "open": number(item.get("今开")), "high": number(item.get("最高")), "low": number(item.get("最低")), "previous_close": number(item.get("昨收")), "volume": number(item.get("成交量")) * 100 if number(item.get("成交量")) is not None else None, "amount": number(item.get("成交额")), "turnover_percent": number(item.get("换手率"))})
        if not rows:
            raise ValueError("上游没有返回行情")
        return {"rows": rows, "source": "AKShare · 东方财富", "kind": "quote_snapshot", "volume_unit": "股", "currency": "CNY"}
    if operation != "history" or not re.fullmatch(r"\d{6}", symbol) or adjust not in {"", "qfq"}:
        raise ValueError("不支持的行情请求")
    start = (datetime.now(TZ) - timedelta(days=550)).strftime("%Y%m%d")
    end = datetime.now(TZ).strftime("%Y%m%d")
    try:
        frame = ak.stock_zh_a_hist(symbol=symbol, period="daily", start_date=start, end_date=end, adjust=adjust, timeout=7)
        if frame.empty:
            raise ValueError("空行情")
        rows = [{"date": str(r["日期"])[:10], "open": number(r["开盘"]), "close": number(r["收盘"]), "high": number(r["最高"]), "low": number(r["最低"]), "volume": number(r["成交量"]) * 100 if number(r["成交量"]) is not None else None, "amount": number(r.get("成交额")), "change_percent": number(r.get("涨跌幅"))} for r in frame.to_dict("records")]
        source = "AKShare · 东方财富"
    except (ValueError, KeyError, TypeError, IndexError, OSError, RuntimeError, RequestException):
        # A separate documented AKShare source; never synthesize missing prices.
        frame = ak.stock_zh_a_hist_tx(symbol=symbol, start_date=start, end_date=end, adjust=adjust, timeout=7)
        rows = [{"date": str(r["date"])[:10], "open": number(r["open"]), "close": number(r["close"]), "high": number(r["high"]), "low": number(r["low"]), "volume": number(r.get("volume")), "amount": number(r.get("amount")), "change_percent": None} for r in frame.to_dict("records")]
        # AKShare 1.19.1 treats sz000* as index volume, but these six-digit codes are stocks.
        if symbol.startswith("000"):
            for row in rows:
                implied = row["amount"] / row["volume"] if row["volume"] and row["amount"] else None
                if implied and row["high"] and row["low"] and implied > row["high"] * 20 and row["low"] / 2 <= implied / 100 <= row["high"] * 2:
                    row["volume"] *= 100
        source = "AKShare · 腾讯证券"
    rows = sorted([r for r in rows if all(r[k] is not None and r[k] > 0 for k in ("open", "close", "high", "low")) and r["low"] <= min(r["open"], r["close"]) <= max(r["open"], r["close"]) <= r["high"]], key=lambda r: r["date"])
    rows = list({r["date"]: r for r in rows}.values())
    if not rows:
        raise ValueError("没有可用的历史行情")
    for previous, current in pairwise(rows):
        if current["change_percent"] is None:
            current["change_percent"] = (current["close"] / previous["close"] - 1) * 100
    return {"symbol": symbol, "rows": rows, "source": source, "kind": "daily_history", "adjust": adjust or "none", "volume_unit": "股", "currency": "CNY", "quote_date": rows[-1]["date"]}


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            payload = run(*sys.argv[1:])
        print(json.dumps({"ok": True, "data": payload}, ensure_ascii=True, allow_nan=False))
    except (ValueError, KeyError, TypeError, IndexError, OSError, RuntimeError, ImportError, RequestException) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: 上游行情暂时不可用"}, ensure_ascii=True))
