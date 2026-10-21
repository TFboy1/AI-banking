import asyncio
import json
import sqlite3
from datetime import datetime

from banking.db import TZ
from banking.market_clock import checkpoint, day_price, next_checkpoint
from banking.stock_valuation import daily_quotes, value_positions
from banking.stocks import CATALOG, StockService


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=TZ)


def test_open_close_boundaries_and_weekend():
    assert checkpoint(instant('2026-10-16T09:29:59')) == instant('2026-10-15T15:00:00')
    assert checkpoint(instant('2026-10-16T09:30:00')) == instant('2026-10-16T09:30:00')
    assert checkpoint(instant('2026-10-16T14:59:59')) == instant('2026-10-16T09:30:00')
    assert checkpoint(instant('2026-10-16T15:00:00')) == instant('2026-10-16T15:00:00')
    assert next_checkpoint(instant('2026-10-16T15:00:00')) == instant('2026-10-19T09:30:00')
    assert checkpoint(instant('2026-10-18T12:00:00')) == instant('2026-10-16T15:00:00')
    row = {'date':'2026-10-16','open':11.41,'close':11.54}
    assert day_price(row, instant('2026-10-16T12:00:00')) == (11.41,'开盘价')
    assert day_price(row, instant('2026-10-16T15:00:00')) == (11.54,'收盘价')


async def test_only_fetch_once_per_checkpoint_and_all_twenty_prices(tmp_path, monkeypatch):
    import banking.market_clock as clock
    from banking import stocks
    now = instant('2026-10-16T10:00:00')
    monkeypatch.setattr(clock, 'checkpoint', lambda *args: checkpoint(now))
    monkeypatch.setattr(stocks, 'checkpoint', lambda: checkpoint(now))
    monkeypatch.setattr(stocks.time, 'time', lambda: now.timestamp())
    service = StockService(tmp_path/'cache.sqlite3')
    calls = []
    async def worker(operation, symbol='', adjust=''):
        calls.append((operation,symbol))
        return {'rows':[{'date':'2026-10-15','open':11.4,'close':11.5}, {'date':'2026-10-16','open':11.41,'close':11.54}], 'source':'isolated-history-api'}
    service.worker = worker
    first = await daily_quotes(service, CATALOG)
    assert len(first['rows']) == 20 and all(r['price']==11.41 and r['close'] is None for r in first['rows'])
    assert len(calls) == 20 and all(c[0]=='history' for c in calls)
    await daily_quotes(service, CATALOG)
    await value_positions(service,[{'symbol':'000001','shares':100,'cost_minor':115000}])
    assert len(calls) == 20
    now = instant('2026-10-16T15:00:15')
    # Updating once at close replaces opening values without another page click.
    await asyncio.gather(*(service.get('history', s['symbol']) for s in CATALOG))
    closed = await daily_quotes(service, CATALOG)
    assert all(r['price']==11.54 and r['close']==11.54 for r in closed['rows'])
    valued = (await value_positions(service,[{'symbol':'000001','shares':100,'cost_minor':115000}]))[0]
    assert valued['market_value_minor'] == 115400 and valued['profit_minor'] == 400
    assert valued['profit_percent'] == .35 and len(calls) == 40


async def test_cached_history_is_used_even_when_refresh_fails(tmp_path, monkeypatch):
    import banking.market_clock as clock
    from banking import stocks
    monkeypatch.setattr(clock, 'checkpoint', lambda *args: checkpoint(instant('2026-10-16T15:01:00')))
    monkeypatch.setattr(stocks, 'checkpoint', lambda: checkpoint(instant('2026-10-19T09:30:00')))
    service = StockService(tmp_path/'cache.sqlite3')
    with sqlite3.connect(service.path) as db:
        db.execute('INSERT INTO stock_cache VALUES (?,?,?)',('history:000001:',json.dumps({'rows':[{'date':'2026-10-16','open':11.41,'close':11.54}], 'source':'cached-real-source'}),instant('2026-10-16T15:01:00').timestamp()))
    started = asyncio.Event()
    async def unavailable(*args):
        started.set()
        raise OSError('isolated outage')
    service.worker = unavailable
    value = await asyncio.wait_for(value_positions(service,[{'symbol':'000001','shares':100,'cost_minor':115000}]),1)
    assert value[0]['market_value_minor']==115400 and value[0]['valuation_stale']
    await started.wait()
    await asyncio.gather(*list(service.valuation_fetches.values()))
