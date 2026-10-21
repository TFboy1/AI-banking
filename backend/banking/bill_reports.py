"""Read-only, evidence-backed bills. All amounts are integer minor units."""
from collections import defaultdict, deque
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from .db import TZ

INVESTMENT = {'理财', '证券投资', '证券卖出'}
EXCLUDED = INVESTMENT | {'内部转账'}
RULE_VERSION = 'bills_v1'


def money(amount):
    return '¥' + f'{Decimal(amount) / 100:,.2f}'


def instant(value):
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return (at.replace(tzinfo=TZ) if at.tzinfo is None else at.astimezone(TZ))


def period(args, at):
    from .bank import BankError
    if sum(args.get(k) is not None for k in ('month', 'year', 'days')) > 1:
        raise BankError('月份、年份和最近天数只能选择一种。')
    if args.get('year') is not None:
        year = args['year']
        start = datetime(year, 1, 1, tzinfo=TZ)
        end = start.replace(year=year + 1)
        return 'year', str(year), start, end
    if args.get('days') is not None:
        end = at.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return 'recent', f"最近{args['days']}天（含今日）", end - timedelta(days=args['days']), end
    month = args.get('month') or at.strftime('%Y-%m')
    try:
        start = datetime.strptime(month, '%Y-%m').replace(tzinfo=TZ)
        if start.strftime('%Y-%m') != month or not 1900 <= start.year <= 2100:
            raise ValueError()
    except ValueError:
        raise BankError('月份格式应为 YYYY-MM。') from None
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return 'month', month, start, end


def evidence(row):
    return {k: row[k] for k in ('id', 'occurred_at', 'counterparty', 'direction', 'amount_minor', 'category', 'description')}


def detect(rows, historical):
    spending = [r for r in rows if r['direction'] == 'out' and r['category'] not in EXCLUDED]
    clues = []
    def add(kind, reason, items, **extra):
        clues.append({'kind': kind, 'reason': reason, 'transaction_ids': [i['id'] for i in items],
                      'merchant': items[0]['counterparty'] if len({i['counterparty'] for i in items}) == 1 else '多个收款人',
                      'amount_minor': sum(i['amount_minor'] for i in items),
                      'transactions': [evidence(i) for i in items], **extra})

    subscriptions = defaultdict(list)
    duplicates = defaultdict(list)
    for r in spending:
        if r['amount_minor'] >= 300000:
            add('large_expense', '单笔支出达到演示关注阈值3000元，请核对用途', [r])
        merchant = r['merchant_key'] or r['counterparty'].strip()
        if not merchant:
            continue
        if r['category'] == '订阅':
            subscriptions[(merchant, r['amount_minor'], instant(r['occurred_at']).strftime('%Y-%m'))].append(r)
        else:
            duplicates[(merchant, r['category'], r['amount_minor'])].append(r)
    for items in subscriptions.values():
        if len(items) > 1:
            add('repeated_subscription', '同一订阅在同一个月出现多笔同金额扣费，请核对是否重复', items)
    for items in duplicates.values():
        i = 0
        while i < len(items):
            j = i + 1
            while j < len(items) and instant(items[j]['occurred_at']) - instant(items[i]['occurred_at']) <= timedelta(minutes=10):
                j += 1
            if j - i >= 2:
                add('duplicate_payment', '同一对象、同类别、同金额在10分钟内出现重复支出，可能是正常分次支付，请核对', items[i:j])
            i = j
    transfers = [r for r in spending if r['category'] == '转账']
    i = 0
    while i < len(transfers):
        j = i + 1
        while j < len(transfers) and instant(transfers[j]['occurred_at']) - instant(transfers[i]['occurred_at']) <= timedelta(minutes=10):
            j += 1
        if j - i >= 3:
            add('rapid_transfers', '10分钟内出现至少3笔对外转账，请核对收款人和用途', transfers[i:j])
            i = j
        else:
            i += 1

    # Only earlier transactions inform the benchmark; no future or current-row leakage.
    previous = defaultdict(lambda: deque(maxlen=20))
    target_ids = {r['id'] for r in spending}
    eligible = compared = 0
    for row in historical:
        if row['direction'] != 'out' or row['category'] in EXCLUDED | {'转账'}:
            continue
        prior = previous[row['category']]
        current = instant(row['occurred_at'])
        while prior and current - instant(prior[0]['occurred_at']) > timedelta(days=90):
            prior.popleft()
        if row['id'] in target_ids:
            eligible += 1
            baseline = [r for r in prior if instant(r['occurred_at']) < current]
            if len(baseline) >= 5:
                compared += 1
                values = sorted(r['amount_minor'] for r in baseline)
                n = len(values)
                median_twice = values[n // 2] * 2 if n % 2 else values[n // 2 - 1] + values[n // 2]
                if row['amount_minor'] >= 20000 and row['amount_minor'] * 2 >= median_twice * 3:
                    median_yuan = (Decimal(median_twice) / 200).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
                    add('historical_spike', f"本笔不低于200元，且达到此前90天最近{n}笔同类支出中位数（¥{median_yuan:,.2f}）的3倍，请核对", [row],
                        baseline_transactions=[evidence(r) for r in baseline], baseline_count=n, median_minor_twice=median_twice)
        prior.append(row)
    return clues, {'eligible_count': eligible, 'compared_count': compared, 'insufficient_count': eligible - compared,
                   'rule': '只参考本笔之前90天最近20笔同类非转账日常支出，至少5笔才比较；金额至少200元且不低于历史中位数3倍时提示。'}


def report(db, args, at=None):
    at = at or datetime.now(TZ)
    kind, label, start, end = period(args, at)
    # SQL normalizes offsets, so UTC timestamps at Beijing month/year boundaries count correctly.
    historical = [dict(r) for r in db.execute('''SELECT * FROM transactions
        WHERE julianday(occurred_at)>=julianday(?) AND julianday(occurred_at)<julianday(?)
          AND julianday(occurred_at)<=julianday(?) ORDER BY julianday(occurred_at),id''',
        ((start - timedelta(days=90)).isoformat(), end.isoformat(), at.isoformat()))]
    rows = [r for r in historical if start <= instant(r['occurred_at']) < end]
    spending = [r for r in rows if r['direction'] == 'out' and r['category'] not in EXCLUDED]
    incoming = [r for r in rows if r['direction'] == 'in' and r['category'] not in EXCLUDED]
    categories = defaultdict(int)
    merchants = defaultdict(lambda: {'amount_minor': 0, 'count': 0, 'references': []})
    for r in spending:
        categories[r['category']] += r['amount_minor']
        item = merchants[r['counterparty'] or '未注明对象']
        item['amount_minor'] += r['amount_minor']; item['count'] += 1; item['references'].append(r['id'])
    total = sum(categories.values()); income = sum(r['amount_minor'] for r in incoming)
    investment = [r for r in rows if r['category'] in INVESTMENT]
    investment_out = sum(r['amount_minor'] for r in investment if r['direction'] == 'out')
    investment_in = sum(r['amount_minor'] for r in investment if r['direction'] == 'in')
    clues, coverage = detect(rows, historical)
    trend = []
    if kind == 'year':
        for month in range(1, 13):
            key = f'{start.year}-{month:02}'
            selected = [r for r in rows if instant(r['occurred_at']).strftime('%Y-%m') == key]
            inc = sum(r['amount_minor'] for r in selected if r['direction'] == 'in' and r['category'] not in EXCLUDED)
            exp = sum(r['amount_minor'] for r in selected if r['direction'] == 'out' and r['category'] not in EXCLUDED)
            trend.append({'month': key, 'income_minor': inc, 'expense_minor': exp, 'net_minor': inc-exp,
                          'record_count': len(selected), 'status': 'future' if key > at.strftime('%Y-%m') else 'recorded' if selected else 'no_records'})
    charts=[]
    if kind!='year':
        days=sorted({instant(r['occurred_at']).date().isoformat() for r in spending+incoming})
        if days:
            charts.append({'title':'每日收支趋势','type':'line','labels':days,'series':[
                {'name':'收入','values':[sum(r['amount_minor'] for r in incoming if instant(r['occurred_at']).date().isoformat()==d) for d in days]},
                {'name':'支出','values':[sum(r['amount_minor'] for r in spending if instant(r['occurred_at']).date().isoformat()==d) for d in days]}]})
    if categories:
        ordered=sorted(categories,key=lambda c:(-categories[c],c))
        charts.append({'title':'消费分类占比','type':'donut','labels':ordered,'series':[{'name':'支出','values':[categories[c] for c in ordered]}]})
    note = '仅统计已有模拟流水；空月份表示没有记录，不代表实际零收支。当期只统计到查询时刻。本人账户互转和理财、证券本金进出不计入日常收支，投资回笼可能包含收益，另列资金流。异常是待核对线索，不代表认定欺诈。'
    sources = [{'label': f'{label}模拟交易流水，共{len(rows)}笔', 'references': [r['id'] for r in rows]}]
    baseline_ids = sorted({r['id'] for c in clues for r in c.get('baseline_transactions', [])})
    if baseline_ids:
        sources.append({'label': '历史水平比较所引用的此前同类支出', 'references': baseline_ids})
    title = f'{label} ' + ('年度账单' if kind == 'year' else '月度账单' if kind == 'month' else '收支与异常核对')
    coverage_text = f"{coverage['eligible_count']}笔可比较消费中，{coverage['compared_count']}笔有足够历史，{coverage['insufficient_count']}笔历史不足，未判断是否高于平常。没有线索不代表交易一定安全。"
    return {'month': label if kind == 'month' else None, 'year': start.year if kind == 'year' else None,
            'period': {'kind': kind, 'label': label, 'start': start.isoformat(), 'end_exclusive': end.isoformat(), 'as_of': min(at, end).isoformat(), 'timezone': 'Asia/Shanghai'},
            'income_minor': income, 'expense_minor': total, 'net_minor': income-total, 'categories': dict(categories),
            'investment_flows': {'out_minor': investment_out, 'in_minor': investment_in, 'net_out_minor': investment_out-investment_in, 'references': [r['id'] for r in investment]},
            'anomalies': clues, 'bill_report': {'charts':charts,'trend': trend, 'top_merchants': [{'name': name, **value} for name, value in sorted(merchants.items(), key=lambda p: (-p[1]['amount_minor'], p[0]))[:5]],
                'historical_coverage': coverage_text, 'record_count': len(rows), 'rule_version': RULE_VERSION, 'anomaly_count': len(clues)},
            'explanation': {'as_of': at.isoformat(), 'sources': sources,
                'checks': [{'label': '收支口径', 'status': 'pass', 'detail': note},
                           {'label': '历史数据充分性', 'status': 'unknown' if coverage['insufficient_count'] else 'pass', 'detail': coverage_text},
                           {'label': '异常线索', 'status': 'pass', 'detail': f'按所示规则发现{len(clues)}条核对线索，不会自动冻结、取消或退款。'}],
                'conclusion': f'{label}已有流水支出{money(total)}，收入{money(income)}。'},
            'display': {'title': title, 'columns': ['支出类别', '金额'], 'rows': [[k, money(v)] for k, v in sorted(categories.items(), key=lambda p: (-p[1], p[0]))],
                'metrics': [{'label': '日常收入', 'value': money(income)}, {'label': '日常支出', 'value': money(total)},
                            {'label': '日常净收支', 'value': money(income-total)}, {'label': '投资资金流出', 'value': money(investment_out)}, {'label': '投资资金回笼', 'value': money(investment_in)}], 'note': note},
            'alerts': [{'text': f"{c['merchant']}：{c['reason']}（{money(c['amount_minor'])}）", 'references': c['transaction_ids'],
                        'transactions': c['transactions'], 'baseline_transactions': c.get('baseline_transactions', [])} for c in clues]}
