"""Server-side A-share demo session gate, independent of quote refresh times."""
from datetime import date,datetime,time,timedelta
from .db import TZ

# SSE/SZSE 2026 holiday notice (2025-12-22):
# https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml
# Unknown calendar years fail closed until their published calendar is added.
HOLIDAYS={2026:[('01-01','01-03'),('02-15','02-23'),('04-04','04-06'),
                ('05-01','05-05'),('06-19','06-21'),('09-25','09-27'),('10-01','10-07')]}
HOURS='交易日09:30–11:30、13:00–15:00（北京时间）'

def clock():return datetime.now(TZ)

def trading_day(day):
    return day.year in HOLIDAYS and day.weekday()<5 and not any(
        date.fromisoformat(f'{day.year}-{start}')<=day<=date.fromisoformat(f'{day.year}-{end}')
        for start,end in HOLIDAYS[day.year])

def status(at=None):
    at=at or clock()
    at=at.replace(tzinfo=TZ) if at.tzinfo is None else at.astimezone(TZ)
    day=at.date();current=at.time()
    opened=trading_day(day) and (time(9,30)<=current<time(11,30) or time(13)<=current<time(15))
    reason=('正在交易时段' if opened else f'未载入{day.year}年交易日历，暂停股票交易' if day.year not in HOLIDAYS else
            '周末休市' if day.weekday()>=5 else '节假日休市' if not trading_day(day) else
            '尚未开盘' if current<time(9,30) else '午间休市' if current<time(13) else '已收盘')
    change=None
    for offset in range(370):
        candidate=day+timedelta(days=offset)
        if trading_day(candidate):
            change=next((datetime.combine(candidate,t,TZ) for t in (time(9,30),time(11,30),time(13),time(15)) if datetime.combine(candidate,t,TZ)>at),None)
            if change:break
    return {'open':opened,'reason':reason,'hours':HOURS,'server_time':at.isoformat(),
            'next_change':change.isoformat() if change else None,'calendar_years':sorted(HOLIDAYS)}

def require_open():
    from .bank import BankError
    current=status()
    if not current['open']:
        raise BankError(f"{current['reason']}，暂不能买入或卖出股票。仅限{HOURS}；查询行情和持仓不受影响，当前未执行交易。")
