import asyncio
import hashlib
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from fastapi import Depends, HTTPException, Query

from .db import TZ, event

SCHEMA = """
CREATE TABLE IF NOT EXISTS product_nav_history (
 product_id TEXT NOT NULL,price_date TEXT NOT NULL,nav TEXT NOT NULL,previous_nav TEXT NOT NULL,
 PRIMARY KEY(product_id,price_date));
"""
DAILY_BPS = {1: 3, 2: 25, 3: 80, 4: 180, 5: 300}
PRECISION = Decimal("0.0001")


def today():
    return datetime.now(TZ).date()


def rate(product_id, day, risk):
    bound = DAILY_BPS.get(risk, 300)
    seed = int(hashlib.sha256(f"nav-v1:{product_id}:{day.isoformat()}".encode()).hexdigest()[:16], 16)
    return Decimal(seed % (2 * bound + 1) - bound) / 10000


def price(value):
    return max(PRECISION, value.quantize(PRECISION, rounding=ROUND_HALF_UP))


def sync_prices(db, day=None):
    """Persist one deterministic simulated close per calendar day; never reroll on refresh."""
    day = day or today()
    if not db.execute("SELECT 1 FROM products p WHERE NOT EXISTS (SELECT 1 FROM product_nav_history h WHERE h.product_id=p.id AND h.price_date>=?) LIMIT 1", (day.isoformat(),)).fetchone():
        return False
    owned = not db.in_transaction
    if owned:
        db.execute("BEGIN IMMEDIATE")
    changed = False
    prices_changed = False
    try:
        for product in db.execute("SELECT id,nav,risk_level FROM products").fetchall():
            last = db.execute("SELECT * FROM product_nav_history WHERE product_id=? ORDER BY price_date DESC LIMIT 1", (product["id"],)).fetchone()
            if not last:
                # Backfill synthetic history ending at the existing NAV: rollout does not revalue holdings.
                current = Decimal(product["nav"])
                for offset in range(90):
                    cursor = day - timedelta(days=offset)
                    previous = price(current / (1 + rate(product["id"], cursor, product["risk_level"])))
                    db.execute("INSERT INTO product_nav_history VALUES (?,?,?,?)", (product["id"], cursor.isoformat(), str(current), str(previous)))
                    current = previous
                changed = True
                continue
            cursor = date.fromisoformat(last["price_date"]) + timedelta(days=1)
            current = Decimal(last["nav"])
            while cursor <= day:
                updated = price(current * (1 + rate(product["id"], cursor, product["risk_level"])))
                db.execute("INSERT INTO product_nav_history VALUES (?,?,?,?)", (product["id"], cursor.isoformat(), str(updated), str(current)))
                current = updated
                cursor += timedelta(days=1)
                changed = True
                prices_changed = True
            db.execute("UPDATE products SET nav=? WHERE id=?", (str(current), product["id"]))
        if changed:
            event(db, "*", "bank.updated" if prices_changed else "market.history_initialized", {"source": "simulated_daily_nav", "date": day.isoformat()})
        if owned:
            db.commit()
        return changed
    except BaseException:
        if owned:
            db.rollback()
        raise


def daily_item(row):
    nav, previous = Decimal(row["nav"]), Decimal(row["previous_nav"])
    return {"date": row["price_date"], "nav": str(nav), "previous_nav": str(previous), "change": str(nav - previous), "change_percent": str(((nav / previous - 1) * 100).quantize(Decimal("0.0001"))), "simulated": True}


async def market_loop(database, notify):
    while True:
        with database.transaction() as db:
            changed = sync_prices(db)
        if changed:
            await notify()
        await asyncio.sleep(60)


def register_market(app, database, session):
    @app.get("/api/investments/products/{product_id}/history", dependencies=[Depends(session)])
    def history(product_id: str, days: int = Query(default=90, ge=1, le=366)):
        with database.transaction() as db:
            sync_prices(db)
            product = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
            if not product:
                raise HTTPException(404, "理财产品不存在。")
            rows = db.execute("SELECT * FROM product_nav_history WHERE product_id=? ORDER BY price_date DESC LIMIT ?", (product_id, days)).fetchall()
        return {"product": dict(product), "history": [daily_item(row) for row in reversed(rows)], "daily_limit_percent": str(Decimal(DAILY_BPS.get(product["risk_level"], 300)) / 100), "simulated": True, "note": "按自然日生成的模拟净值，不是真实行情；初始90日为合成历史。风险等级越高，模拟波动范围越大，不保证收益。"}
