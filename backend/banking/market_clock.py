"""Two daily price checkpoints, in the Shanghai timezone."""
from datetime import datetime, time, timedelta

from .db import TZ


def checkpoint(now=None):
    now = (now or datetime.now(TZ)).astimezone(TZ)
    day = now.date()
    if day.weekday() < 5 and now.time() >= time(15):
        return datetime.combine(day, time(15), TZ)
    if day.weekday() < 5 and now.time() >= time(9, 30):
        return datetime.combine(day, time(9, 30), TZ)
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return datetime.combine(day, time(15), TZ)


def next_checkpoint(now=None):
    now = (now or datetime.now(TZ)).astimezone(TZ)
    for offset in range(4):
        day = now.date()+timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        for hour, minute in ((9, 30), (15, 0)):
            candidate = datetime.combine(day, time(hour, minute), TZ)
            if candidate > now:
                return candidate


def day_price(row, now=None):
    point = checkpoint(now)
    opening = row.get('close_confirmed') is False or (row['date'] == point.date().isoformat() and point.hour == 9)
    return row.get('open') if opening else row.get('close'), '开盘价' if opening else '收盘价'
