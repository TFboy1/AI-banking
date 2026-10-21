"""One valuation method for both securities and banking displays."""
import asyncio
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from .db import TZ
from .market_clock import checkpoint, day_price


def start_read(service, operation, *args):
    # The page has a short deadline, but the provider's bounded request must be
    # allowed to finish populating its cache rather than killed on every poll.
    if not hasattr(service, 'valuation_fetches'):
        service.valuation_fetches = {}
    key = (operation, *args)
    task = service.valuation_fetches.get(key)
    if task is None or task.done():
        task = asyncio.create_task(service.get(operation, *args))
        service.valuation_fetches[key] = task
        def finished(value):
            if service.valuation_fetches.get(key) is value:
                service.valuation_fetches.pop(key, None)
            if not value.cancelled():
                value.exception()
        task.add_done_callback(finished)
    return task


async def cached_read(service, operation, *args, timeout=6):
    return await asyncio.wait_for(asyncio.shield(start_read(service, operation, *args)), timeout=timeout)


async def read_history(service, symbol, adjust='', *, refresh=False):
    history = service.peek('history', symbol, adjust) if hasattr(service, 'peek') else None
    if history and history.get('rows'):
        if history.get('stale'):
            task = start_read(service, 'history', symbol, adjust)
            if refresh:
                try:
                    return await asyncio.wait_for(asyncio.shield(task), timeout=6)
                except (TimeoutError, ValueError, OSError):
                    pass
        return history
    return await cached_read(service, 'history', symbol, adjust)


async def daily_quotes(service, catalog):
    async def quote(stock):
        result = {**stock, 'price': None, 'open': None, 'close': None, 'quote_date': None}
        try:
            history = await read_history(service, stock['symbol'])
            if not history.get('rows'):
                return result
            rows = history['rows']
            last = rows[-1]
            if not 0 <= (datetime.now(TZ).date()-date.fromisoformat(last['date'])).days <= 10:
                return result
            price, kind = day_price(last)
            opening = kind == '开盘价'
            previous = rows[-2]['close'] if len(rows) > 1 else None
            change = (price / previous-1)*100 if price and previous else None
            return {**result, 'price': price, 'open': last.get('open'), 'close': None if opening else last.get('close'), 'quote_date': last['date'], 'kind': kind, 'change_percent': change, 'source': history.get('source'), 'stale': bool(history.get('stale'))}
        except (TimeoutError, ValueError, OSError, TypeError, KeyError):
            return result
    rows = list(await asyncio.gather(*(quote(s) for s in catalog)))
    return {'rows': rows, 'status': 'ok' if any(r['price'] for r in rows) else 'unavailable', 'stale': any(r.get('stale') for r in rows), 'error': '', 'fetched_at': checkpoint().isoformat(), 'source': 'AKShare · 开盘/收盘行情', 'pending': bool(getattr(service, 'valuation_fetches', {})), 'simulated': False}


async def value_positions(service, positions, *, cutoff_date=None):
    if not positions:
        return []

    async def estimate(position):
        result = {**position, 'market_value_minor': None, 'profit_minor': None, 'profit_percent': None, 'price_minor': None, 'quote_date': None, 'source': None, 'price_kind': None}
        try:
            history = await read_history(service, position['symbol'], refresh=True)
            if history.get('status') not in {'ok', 'stale'} or not history.get('rows'):
                return result
            candidates=[r for r in history['rows'] if r['date']<cutoff_date and r.get('close_confirmed') is not False] if cutoff_date else history['rows']
            if not candidates:return result
            last = max(candidates,key=lambda r:r['date'])
            target=date.fromisoformat(cutoff_date) if cutoff_date else datetime.now(TZ).date()
            if not 0 <= (target-date.fromisoformat(last['date'])).days <= (31 if cutoff_date else 10):
                return result
            amount, kind = (last.get('close'),'收盘价') if cutoff_date else day_price(last)
            price = Decimal(str(amount or 0))
            source, as_of = history.get('source'), last['date']
            stale = bool(history.get('stale'))
            if not price.is_finite() or price <= 0:
                return result
            minor = int((price*100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
            market_value = minor*position['shares']
            profit = market_value-position['cost_minor']
            return {**result, 'market_value_minor': market_value, 'profit_minor': profit, 'profit_percent': round(profit/position['cost_minor']*100, 2) if position['cost_minor'] else None, 'price_minor': minor, 'quote_date': as_of, 'source': source, 'price_kind': kind, 'valuation_stale': stale}
        except (TimeoutError, ValueError, ArithmeticError, TypeError, KeyError):
            return result
    return list(await asyncio.gather(*(estimate(p) for p in positions)))


def totals(positions):
    complete = all(p['market_value_minor'] is not None for p in positions)
    cost = sum(p['cost_minor'] for p in positions)
    value = sum(p['market_value_minor'] for p in positions) if complete else None
    profit = value-cost if complete else None
    return {'cost_minor': cost, 'market_value_minor': value, 'profit_minor': profit, 'profit_percent': round(profit/cost*100, 2) if cost and complete else 0 if complete else None, 'complete': complete, 'valuation': '盘中按开盘价、收盘后按收盘价估值；工作日09:30、15:00更新，休市或源不可用时保留最近价格。非实时成交价。'}
