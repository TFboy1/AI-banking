from datetime import timedelta
from decimal import Decimal
from itertools import pairwise

import httpx
from banking import market
from banking.main import create_app


def test_daily_history_stability_risk_bounds_and_gap_fill(database):
    day = market.today()
    with database.transaction() as db:
        original = {r['id']: r['nav'] for r in db.execute('SELECT id,nav FROM products')}
        db.execute('DELETE FROM product_nav_history')
        assert market.sync_prices(db, day)
        first = [tuple(r) for r in db.execute('SELECT * FROM product_nav_history ORDER BY product_id,price_date')]
        assert not market.sync_prices(db, day)
        assert first == [tuple(r) for r in db.execute('SELECT * FROM product_nav_history ORDER BY product_id,price_date')]
        assert original == {r['id']: r['nav'] for r in db.execute('SELECT id,nav FROM products')}
        for product in db.execute('SELECT * FROM products').fetchall():
            rows = db.execute('SELECT * FROM product_nav_history WHERE product_id=? ORDER BY price_date', (product['id'],)).fetchall()
            assert len(rows) == 90 and rows[-1]['price_date'] == day.isoformat()
            for previous, current in pairwise(rows):
                assert Decimal(previous['nav']) == Decimal(current['previous_nav'])
            changes = [Decimal(market.daily_item(r)['change_percent']) for r in rows]
            assert max(map(abs, changes)) <= Decimal(market.DAILY_BPS[product['risk_level']]) / 100 + Decimal('.01')
            assert any(v > 0 for v in changes) and any(v < 0 for v in changes)
        assert market.sync_prices(db, day + timedelta(days=3))
        assert not market.sync_prices(db, day + timedelta(days=3))
        for product_id in original:
            assert db.execute('SELECT COUNT(*) FROM product_nav_history WHERE product_id=?', (product_id,)).fetchone()[0] == 93


async def test_history_endpoint_and_day_change_rejects_old_trade(settings, monkeypatch):
    app = create_app(settings)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8000') as c:
        assert (await c.get('/api/investments/products/cash/history')).status_code == 401
        boot = (await c.get('/api/bootstrap')).json()
        first = (await c.get('/api/investments/products/cash/history')).json()
        assert first['simulated'] and len(first['history']) == 90
        assert (await c.get('/api/investments/products/cash/history')).json() == first
        assert len((await c.get('/api/investments/products/cash/history?days=7')).json()['history']) == 7
        assert (await c.get('/api/investments/products/cash/history?days=999999')).status_code == 422
        assert (await c.get('/api/investments/products/unknown/history')).status_code == 404
        with app.state.database.transaction() as db:
            db.execute("INSERT OR REPLACE INTO risk_profile VALUES (1,'{}',5)")
            msg = app.state.agent.create_direct_approval(db, boot['conversation_id'], 'purchase_product', {'product_id': 'cash', 'amount_yuan': '100', 'account_id': 'daily'})
        future = market.today() + timedelta(days=1)
        monkeypatch.setattr(market, 'today', lambda: future)
        monkeypatch.setattr(market, 'rate', lambda *args: Decimal('.01'))
        snapshot = (await c.get('/api/bank')).json()
        assert snapshot['accounts'] == boot['bank']['accounts']
        detail = (await c.get('/api/investments/products/cash/history')).json()
        assert detail['history'][-1]['date'] == future.isoformat()
        assert Decimal(detail['product']['nav']) == Decimal(first['product']['nav']) * Decimal('1.01')
        for h in snapshot['holdings']:
            assert h['value_minor'] == int((Decimal(h['units']) * Decimal(h['nav']) * 100).quantize(Decimal(1)))
        approval = msg['data']['approval_id']
        result = await c.post(f'/api/approvals/{approval}/decision', json={'decision':'confirm', 'conversation_id':boot['conversation_id'], 'account_id':'daily'})
        assert result.json()['status'] == 'failed' and '净值已更新' in result.json()['error']
        assert (await c.get('/api/bank')).json()['accounts'] == snapshot['accounts']
