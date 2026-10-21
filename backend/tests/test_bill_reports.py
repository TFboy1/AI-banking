from datetime import datetime
import pytest
from banking.bank import Bank, BankError
from banking.bill_reports import report
from banking.db import TZ, Database
from conftest import ask, tool, state

AT=datetime(2026,10,18,10,0,tzinfo=TZ)

def row(db,key,amount=1000,category='餐饮',direction='out',at='2026-10-12T10:00:00+08:00',merchant='饭店',merchant_key=None):
    db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)',(key,key,'daily',merchant,direction,amount,category,at,merchant_key,'账单测试备注'))

@pytest.fixture
def clean(database):
    with database.transaction() as db:db.execute('DELETE FROM transactions')
    return database


def test_year_totals_twelve_months_merchants_and_investment_separation(clean):
    with clean.transaction() as db:
        row(db,'january',10000,at='2026-01-12T12:00:00+08:00')
        row(db,'october',25000,merchant='超市',category='购物')
        row(db,'salary',100000,category='工资',direction='in')
        for key,cat,direction in [('buy','证券投资','out'),('sell','证券卖出','in'),('fund-buy','理财','out'),('fund-sale','理财','in'),('own-out','内部转账','out'),('own-in','内部转账','in')]:
            row(db,key,900000,category=cat,direction=direction)
        row(db,'future',900000,at='2026-10-19T09:00:00+08:00')
        row(db,'next-year',900000,at='2027-01-01T09:00:00+08:00')
        before=db.total_changes
        annual=report(db,{'year':2026},AT)
        assert db.total_changes==before
        assert annual['expense_minor']==35000 and annual['income_minor']==100000 and annual['net_minor']==65000
        assert annual['categories']=={'餐饮':10000,'购物':25000}
        flow=annual['investment_flows'];assert flow['out_minor']==flow['in_minor']==1800000 and len(flow['references'])==4
        trend=annual['bill_report']['trend'];assert len(trend)==12
        assert sum(m['expense_minor'] for m in trend)==annual['expense_minor']
        assert sum(m['income_minor'] for m in trend)==annual['income_minor']
        assert trend[1]['status']=='no_records' and trend[-1]['status']=='future'
        assert annual['bill_report']['top_merchants'][0]['name']=='超市'
        assert annual['bill_report']['top_merchants'][0]['references']==['october']
        assert all('future' not in s['references'] for s in annual['explanation']['sources'])
        month=report(db,{'month':'2026-10'},AT)
        assert trend[9]['expense_minor']==month['expense_minor']


def test_beijing_year_boundaries_recent_days_and_empty_future(clean):
    with clean.transaction() as db:
        row(db,'lastyear',10,at='2025-12-31T15:59:59Z')
        row(db,'newyear',20,at='2025-12-31T16:00:00Z')
        row(db,'lastday',30,at='2026-12-31T15:59:59Z')
        row(db,'nextyear',40,at='2026-12-31T16:00:00Z')
        result=report(db,{'year':2026},datetime(2027,1,2,tzinfo=TZ))
        assert result['expense_minor']==50
        assert result['bill_report']['trend'][0]['expense_minor']==20
        assert result['bill_report']['trend'][-1]['expense_minor']==30
        row(db,'recent-start',100,at='2026-10-11T16:00:00Z')
        row(db,'before-recent',200,at='2026-10-11T15:59:59Z')
        row(db,'today',300,at='2026-10-18T09:00:00+08:00')
        result=report(db,{'days':7},AT)
        assert result['expense_minor']==400 and result['period']['start'].startswith('2026-10-12')
        future=report(db,{'year':2028},AT)
        assert future['bill_report']['record_count']==0 and future['anomalies']==[]


def test_duplicate_rules_do_not_treat_annual_subscription_as_duplicates(clean):
    with clean.transaction() as db:
        for month in ('09','10'):
            row(db,'sub-'+month,2900,'订阅',at=f'2026-{month}-03T09:00:00+08:00',merchant='视频会员',merchant_key='video')
        assert report(db,{'year':2026},AT)['anomalies']==[]
        row(db,'sub-duplicate',2900,'订阅',at='2026-10-05T09:00:00+08:00',merchant='视频会员',merchant_key='video')
        row(db,'meal-one',5000,at='2026-10-10T09:00:00+08:00')
        row(db,'meal-two',5000,at='2026-10-10T09:10:00+08:00')
        row(db,'meal-outside',5000,at='2026-10-10T09:20:01+08:00')
        row(db,'other-shop',5000,at='2026-10-10T09:01:00+08:00',merchant='另一家饭店')
        clues=report(db,{'month':'2026-10'},AT)['anomalies']
        duplicate=next(c for c in clues if c['kind']=='duplicate_payment')
        assert duplicate['transaction_ids']==['meal-one','meal-two']
        subscription=next(c for c in clues if c['kind']=='repeated_subscription')
        assert subscription['transaction_ids']==['sub-10','sub-duplicate']
        for clue in clues:
            assert sum(t['amount_minor'] for t in clue['transactions'])==clue['amount_minor']
            assert [t['id'] for t in clue['transactions']]==clue['transaction_ids']


def test_transfer_window_slides_and_ignores_internal_and_investment(clean):
    with clean.transaction() as db:
        for index,minute in enumerate((0,9,11,12)):
            row(db,f't{index}',1000,'转账',at=f'2026-10-10T09:{minute:02}:00+08:00',merchant=f'收款人{index}')
        for key,cat in [('internal','内部转账'),('stock','证券投资')]:
            row(db,key,999999,cat,at='2026-10-10T09:11:30+08:00')
        clues=report(db,{'days':30},AT)['anomalies']
        assert len(clues)==1 and clues[0]['kind']=='rapid_transfers'
        assert clues[0]['transaction_ids']==['t1','t2','t3'] and clues[0]['amount_minor']==3000


def test_historical_spike_requires_prior_samples_and_exposes_basis(clean):
    with clean.transaction() as db:
        for day in range(1,6):row(db,f'baseline{day}',10000,at=f'2026-09-{day:02}T09:00:00+08:00')
        row(db,'spike',30000,at='2026-10-10T09:00:00+08:00')
        row(db,'future',1000000,at='2026-11-10T09:00:00+08:00')
        row(db,'other-category',10000,'交通')
        result=report(db,{'month':'2026-10'},AT)
        spike=next(c for c in result['anomalies'] if c['kind']=='historical_spike')
        assert spike['baseline_count']==5 and spike['median_minor_twice']==20000
        assert {t['id'] for t in spike['baseline_transactions']}=={f'baseline{d}' for d in range(1,6)}
        assert '历史不足' in result['bill_report']['historical_coverage']
        assert any(c['status']=='unknown' for c in result['explanation']['checks'])
        assert 'future' not in str(result)


def test_historical_minimum_amount_count_and_no_lookahead(clean):
    with clean.transaction() as db:
        for day in range(1,5):row(db,f'b{day}',5000,at=f'2026-09-{day:02}T09:00:00+08:00')
        row(db,'four-samples-only',40000,at='2026-10-01T09:00:00+08:00')
        row(db,'below-200',15000,at='2026-10-02T09:00:00+08:00')
        # These later observations cannot retroactively establish a baseline for October 1.
        for day in range(3,8):row(db,f'later{day}',1000,at=f'2026-10-{day:02}T09:00:00+08:00')
        result=report(db,{'month':'2026-10'},AT)
        assert not any(c['kind']=='historical_spike' for c in result['anomalies'])


@pytest.mark.parametrize('args',[{'month':'2026-13'},{'month':'2026-1'},{'month':'9999-12'},{'year':2026,'month':'2026-01'},{'year':2026,'days':30},{'days':0},{'days':366},{'year':2101},{'year':'2026'}])
def test_invalid_periods_fail_in_read_only_tool(clean,args):
    with pytest.raises(BankError):Bank(clean).query('analyze_bills',args)


def test_year_reports_are_isolated_between_wallets(clean,tmp_path):
    other=Database(tmp_path/'another.sqlite3');other.initialize()
    with clean.transaction() as db:row(db,'only-here',123456,merchant='仅本账户')
    assert Bank(clean).query('analyze_bills',{'year':2026})['bill_report']['top_merchants'][0]['name']=='仅本账户'
    assert all(m['name']!='仅本账户' for m in Bank(other).query('analyze_bills',{'year':2026})['bill_report']['top_merchants'])


async def test_chat_year_and_recent_anomalies_are_automatic_queries(agent):
    for request,args in [('帮我看今年的年度账单',{'year':2026}),('最近一个星期有没有重复扣款',{'days':7})]:
        task=await ask(agent,[[tool('analyze_bills',**args)],'报告已展示，请核对流水。'],request)
        assert state(agent,task)['status']=='completed'
        with agent.db.connection() as db:
            assert not db.execute('SELECT 1 FROM approvals WHERE task_id=?',(task,)).fetchone()
            assert db.execute("SELECT 1 FROM messages WHERE task_id=? AND kind='data'",(task,)).fetchone()
