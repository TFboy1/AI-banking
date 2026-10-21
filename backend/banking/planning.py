"""资金计划、承诺与条件触发服务。"""

from __future__ import annotations

import json
from calendar import monthrange
from datetime import date, datetime, timedelta
from statistics import median

from .bank import BankError, yuan
from .cashflow import (
    challenge_salary_delay,
    challenge_unexpected_expense,
    earliest_repair_date,
    project_cashflow,
)
from .db import TZ, add_message, dumps, event, now, uid


def _next_month(value: date) -> date:
    return (value.replace(day=28) + timedelta(days=4)).replace(day=1)


def _day_in_month(value: date, day: int) -> date:
    return value.replace(day=min(day, monthrange(value.year, value.month)[1]))


class PlanningService:
    def __init__(self, database):
        self.database = database

    async def run_reminder_loop(self, changed):
        """日期提醒只产生待复核事件，不伪造工资到账。"""
        import asyncio

        while True:
            today = datetime.now(TZ).date()
            with self.database.transaction() as db:
                for rule in db.execute(
                    "SELECT * FROM trigger_rules WHERE status='active'",
                ).fetchall():
                    created_date = datetime.fromisoformat(rule["created_at"]).date()
                    due = _day_in_month(created_date.replace(day=1), rule["expected_day"])
                    if due < created_date:
                        due = _day_in_month(_next_month(due), rule["expected_day"])
                    if rule["last_period"]:
                        last = date.fromisoformat(rule["last_period"] + "-01")
                        due = _day_in_month(_next_month(last), rule["expected_day"])
                    if today < due:
                        continue
                    period = due.strftime("%Y-%m")
                    if db.execute(
                        "SELECT 1 FROM trigger_runs WHERE rule_id=? AND period=? AND kind='due_reminder'",
                        (rule["id"], period),
                    ).fetchone():
                        continue
                    actual = db.execute(
                        "SELECT 1 FROM bank_events WHERE kind='salary' AND period=? AND json_extract(payload_json,'$.source_key')=?",
                        (period, rule["source_key"]),
                    ).fetchone()
                    if actual:
                        continue
                    run_id, notification_id = uid("run_"), uid("notice_")
                    reminder = {
                        "notification_id": notification_id,
                        "rule_id": rule["id"],
                        "period": period,
                        "review_only": True,
                    }
                    db.execute(
                        "INSERT INTO trigger_runs VALUES (?,?,?,?,?,?,?)",
                        (run_id, rule["id"], None, period, "due_reminder", dumps(reminder), now()),
                    )
                    db.execute(
                        "INSERT INTO notifications VALUES (?,?,?,?,?,?,?)",
                        (notification_id, rule["conversation_id"], "reminder", "预计发薪日已到，请核对是否到账", dumps(reminder), None, now()),
                    )
                    add_message(
                        db,
                        rule["conversation_id"],
                        None,
                        "assistant",
                        "text",
                        "预计发薪日已到，请先核对工资是否实际到账；本次提醒不会增加账户余额。",
                        {"notification_id": notification_id},
                    )
                    event(db, "*", "notification", {"conversation_id": rule["conversation_id"], **reminder})
            # 一分钟一次只检查本地 SQLite；sleep 可被服务关闭立即取消，避免与 SSE 条件锁竞争。
            await asyncio.sleep(60)

    @staticmethod
    def business_date(db) -> date:
        row = db.execute("SELECT value FROM meta WHERE key='business_date'").fetchone()
        return date.fromisoformat(row[0]) if row else datetime.now(TZ).date()

    @staticmethod
    def active_commitments(db, account_id="daily"):
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM commitments WHERE account_id=? AND status='active' AND remaining_minor>0",
                (account_id,),
            )
        ]

    def available_minor(self, db, account_id="daily"):
        balance = db.execute("SELECT balance_minor FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not balance:
            raise BankError("找不到计划使用的账户。")
        protected = sum(c["remaining_minor"] for c in self.active_commitments(db, account_id))
        return balance[0] - protected, protected

    def infer_assumptions(self, db):
        as_of = self.business_date(db)
        complete_start = as_of.replace(day=1)
        earliest = (_next_month(complete_start) - timedelta(days=95)).isoformat()
        rows = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM transactions WHERE occurred_at>=? AND occurred_at<? ORDER BY occurred_at",
                (earliest, complete_start.isoformat()),
            )
        ]

        def recurring(key, direction):
            values = [r for r in rows if r["merchant_key"] == key and r["direction"] == direction]
            months = {r["occurred_at"][:7] for r in values}
            if len(months) < 2:
                return None
            patterns = {}
            for row in values:
                pattern = (row["amount_minor"], int(row["occurred_at"][8:10]))
                patterns.setdefault(pattern, []).append(row)
            chosen = max(patterns.values(), key=len)
            if len({r["occurred_at"][:7] for r in chosen}) < 2:
                return None
            return {
                "amount_minor": int(median(r["amount_minor"] for r in chosen)),
                "day": int(median(int(r["occurred_at"][8:10]) for r in chosen)),
                "evidence_ids": [r["id"] for r in chosen[-3:]],
                "months": sorted({r["occurred_at"][:7] for r in chosen}),
            }

        salary = recurring("salary", "in")
        rent = recurring("rent", "out")
        subscriptions = [
            dict(row)
            for row in db.execute("SELECT * FROM subscriptions WHERE status='active' ORDER BY next_charge_date")
        ]
        return {"as_of": as_of.isoformat(), "salary": salary, "rent": rent, "subscriptions": subscriptions}

    def _events(self, assumptions, payload, action_date):
        as_of = date.fromisoformat(assumptions["as_of"])
        deadline = date.fromisoformat(payload["deadline"])
        events = []
        cursor = as_of.replace(day=1)
        salary = assumptions.get("salary")
        realized_salary_periods = set(assumptions.get("realized_salary_periods", []))
        rent = assumptions.get("rent")
        while cursor <= deadline:
            if salary and cursor.strftime("%Y-%m") not in realized_salary_periods:
                event_date = _day_in_month(cursor, salary["day"])
                if event_date >= as_of:
                    events.append(
                        {
                            "key": f"salary:{event_date:%Y-%m}",
                            "kind": "income",
                            "source": "salary",
                            "date": event_date.isoformat(),
                            "amount_minor": salary["amount_minor"],
                            "label": "预计工资到账",
                        }
                    )
            if rent:
                event_date = _day_in_month(cursor, rent["day"])
                if event_date >= as_of:
                    events.append(
                        {
                            "key": f"rent:{event_date:%Y-%m}",
                            "kind": "expense",
                            "source": "rent",
                            "date": event_date.isoformat(),
                            "amount_minor": rent["amount_minor"],
                            "release_commitment_minor": rent["amount_minor"],
                            "release_commitment_kind": "rent",
                            "label": "预计房租",
                        }
                    )
            cursor = _next_month(cursor)
        for item in assumptions["subscriptions"]:
            event_date = date.fromisoformat(item["next_charge_date"])
            while event_date <= deadline:
                if event_date >= as_of:
                    events.append(
                        {
                            "key": f"subscription:{item['id']}:{event_date:%Y-%m}",
                            "kind": "expense",
                            "source": "subscription",
                            "date": event_date.isoformat(),
                            "amount_minor": item["amount_minor"],
                            "release_commitment_minor": item["amount_minor"],
                            "release_commitment_kind": "subscription",
                            "label": item["name"],
                        }
                    )
                event_date = _day_in_month(_next_month(event_date), event_date.day)
        if payload["kind"] == "savings" and payload.get("installments"):
            for index, installment in enumerate(payload["installments"]):
                events.append(
                    {
                        "key": f"primary_action:{index}",
                        "kind": "action",
                        "timing": "after_income",
                        "date": installment["date"],
                        "amount_minor": installment["amount_minor"],
                        "label": f"第 {index + 1} 期目标转存",
                    }
                )
        elif payload["kind"] in ("purchase", "savings"):
            events.append(
                {
                    "key": "primary_action",
                    "kind": "action",
                    "date": action_date,
                    "amount_minor": payload["amount_minor"],
                    "label": payload.get("action_label") or ("模拟消费" if payload["kind"] == "purchase" else "目标转存"),
                }
            )
        return events

    def preview(self, db, conversation_id, payload):
        assumptions = self.infer_assumptions(db)
        as_of = date.fromisoformat(payload.get("as_of") or assumptions["as_of"])
        assumptions["as_of"] = as_of.isoformat()
        if payload.get("salary_amount_minor") is not None:
            assumptions["salary"] = {
                "amount_minor": payload["salary_amount_minor"],
                "day": payload.get("salary_day", 1),
                "evidence_ids": [],
                "months": [],
                "source": "user",
            }
        elif payload.get("salary_day") and assumptions["salary"]:
            assumptions["salary"]["day"] = payload["salary_day"]
        if payload.get("rent_amount_minor") is not None:
            assumptions["rent"] = {
                "amount_minor": payload["rent_amount_minor"],
                "day": payload.get("rent_day", 2),
                "evidence_ids": [],
                "months": [],
                "source": "user",
            }
        elif payload.get("rent_day") and assumptions["rent"]:
            assumptions["rent"]["day"] = payload["rent_day"]
        assumptions["realized_salary_periods"] = [
            row[0]
            for row in db.execute(
                "SELECT DISTINCT period FROM bank_events WHERE kind='salary' AND json_extract(payload_json,'$.source_key')='salary'"
            )
        ]
        deadline = date.fromisoformat(payload.get("deadline") or _next_month(as_of).replace(day=monthrange(_next_month(as_of).year, _next_month(as_of).month)[1]).isoformat())
        if deadline < as_of or deadline > as_of + timedelta(days=366):
            raise BankError("计划期限须在业务日期之后的十二个月内。")
        amount = payload["amount_minor"]
        if amount <= 0:
            raise BankError("计划金额必须大于 0。")
        account = db.execute("SELECT * FROM accounts WHERE id=?", (payload.get("account_id", "daily"),)).fetchone()
        if not account:
            raise BankError("找不到计划使用的账户。")
        commitments = self.active_commitments(db, account["id"])
        reserve = payload.get("living_reserve_minor", 0)
        current_living = sum(c["remaining_minor"] for c in commitments if c.get("kind") == "living")
        if reserve > current_living:
            commitments.append(
                {"remaining_minor": reserve - current_living, "status": "active", "kind": "living"}
            )
        if assumptions.get("rent") and not any(c.get("kind") == "rent" for c in commitments):
            commitments.append(
                {"remaining_minor": assumptions["rent"]["amount_minor"], "status": "active", "kind": "rent"}
            )
        if assumptions["subscriptions"] and not any(c.get("kind") == "subscription" for c in commitments):
            commitments.append(
                {
                    "remaining_minor": sum(item["amount_minor"] for item in assumptions["subscriptions"]),
                    "status": "active",
                    "kind": "subscription",
                }
            )
        normalized = {
            **payload,
            "account_id": account["id"],
            "deadline": deadline.isoformat(),
            "as_of": as_of.isoformat(),
            "action_date": payload.get("action_date") or as_of.isoformat(),
            "living_reserve_minor": reserve,
        }
        if payload["kind"] == "savings" and assumptions.get("salary"):
            dates = []
            cursor = as_of.replace(day=1)
            while cursor <= deadline:
                salary_date = _day_in_month(cursor, assumptions["salary"]["day"])
                if salary_date >= max(as_of, date.fromisoformat(normalized["action_date"])):
                    dates.append(salary_date.isoformat())
                cursor = _next_month(cursor)
            if dates:
                each, remainder = divmod(amount, len(dates))
                normalized["installments"] = [
                    {"date": item, "amount_minor": each + (remainder if index == len(dates) - 1 else 0)}
                    for index, item in enumerate(dates)
                ]
        now_events = self._events(assumptions, normalized, normalized["action_date"])
        variants = [{
            "key": "now",
            "label": "按工资周期储蓄" if normalized.get("installments") else "按计划执行",
            "action_date": normalized["installments"][0]["date"] if normalized.get("installments") else normalized["action_date"],
            "result": project_cashflow(account["balance_minor"], commitments, now_events, as_of.isoformat()),
        }]
        salary = next((e for e in now_events if e.get("source") == "salary" and e["date"] >= normalized["action_date"]), None)
        if salary and payload["kind"] == "purchase":
            after_salary = salary["date"]
            salary_events = self._events(assumptions, normalized, after_salary)
            action = next(e for e in salary_events if e["key"] == "primary_action")
            action["timing"] = "after_income"
            variants.append(
                {
                    "key": "after_salary",
                    "label": "工资到账后执行",
                    "action_date": after_salary,
                    "result": project_cashflow(account["balance_minor"], commitments, salary_events, as_of.isoformat()),
                }
            )
        plan_id = payload.get("plan_id") or uid("plan_")
        existing = db.execute("SELECT current_version FROM plans WHERE id=?", (plan_id,)).fetchone()
        version = existing[0] + 1 if existing else 1
        result = {
            "plan_id": plan_id,
            "version": version,
            "title": payload.get("title") or ("购买计划" if payload["kind"] == "purchase" else "储蓄计划"),
            "kind": payload["kind"],
            "status": "draft",
            "amount_minor": amount,
            "living_reserve_minor": reserve,
            "deadline": deadline.isoformat(),
            "assumptions": assumptions,
            "variants": variants,
            "installments": normalized.get("installments", []),
        }
        sources = [
            {
                "label": f"{account['name']}余额 {yuan(account['balance_minor'])}；业务日期 {as_of.isoformat()}",
                "references": [account["id"]],
            },
        ]
        for label, assumption in (("预计工资", assumptions.get("salary")), ("预计房租", assumptions.get("rent"))):
            if assumption:
                origin = "用户提供" if assumption.get("source") == "user" else "历史流水推断"
                sources.append(
                    {
                        "label": f"{label} {yuan(assumption['amount_minor'])}，每月 {assumption['day']} 日（{origin}）",
                        "references": assumption.get("evidence_ids", []),
                    }
                )
        if assumptions["subscriptions"]:
            sources.append(
                {
                    "label": f"{len(assumptions['subscriptions'])} 项有效代扣协议",
                    "references": [item["id"] for item in assumptions["subscriptions"]],
                }
            )
        if commitments:
            sources.append(
                {
                    "label": f"预演纳入已安排及新增保护资金 {yuan(sum(item['remaining_minor'] for item in commitments))}",
                    "references": [item["id"] for item in commitments if item.get("id")],
                }
            )
        result["explanation"] = {
            "as_of": now(),
            "sources": sources,
            "checks": [
                {
                    "label": variant["label"],
                    "status": "pass" if variant["result"]["feasible"] else "block",
                    "detail": (
                        f"最低可自由安排 {yuan(variant['result']['lowest_available_minor'])}"
                        if variant["result"]["feasible"]
                        else f"{variant['result']['first_failure']['date']} · "
                        f"{variant['result']['first_failure']['event']}，缺口 "
                        f"{yuan(variant['result']['first_failure']['shortfall_minor'])}"
                    ),
                }
                for variant in variants
            ],
            "conclusion": "这是按当前账务和上述假设得到的预演；采用计划与实际执行仍需分别确认。",
        }
        created = now()
        if existing:
            db.execute(
                "UPDATE plans SET title=?,kind=?,status='draft',current_version=?,goal_minor=?,deadline=?,updated_at=? WHERE id=?",
                (result["title"], payload["kind"], version, amount, deadline.isoformat(), created, plan_id),
            )
        else:
            db.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?)",
                (plan_id, conversation_id, payload["kind"], result["title"], "draft", version, amount, deadline.isoformat(), created, created),
            )
        db.execute(
            "INSERT INTO plan_versions VALUES (?,?,?,?,?)",
            (plan_id, version, dumps(normalized), dumps(result), created),
        )
        return result

    def get_plan(self, db, plan_id):
        plan = db.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
        if not plan:
            raise BankError("找不到这份资金计划。")
        version = db.execute(
            "SELECT * FROM plan_versions WHERE plan_id=? AND version=?", (plan_id, plan["current_version"])
        ).fetchone()
        result = json.loads(version["result_json"])
        result["status"] = plan["status"]
        result["commitments"] = self.active_commitments(db)
        result["executed_minor"] = db.execute(
            "SELECT COALESCE(SUM(amount_minor),0) FROM plan_executions WHERE plan_id=? AND status='executed'",
            (plan_id,),
        ).fetchone()[0]
        return result

    def challenge(self, db, plan_id, version, dimension, expense_date=None):
        row = db.execute(
            "SELECT * FROM plan_versions WHERE plan_id=? AND version=?", (plan_id, version)
        ).fetchone()
        if not row:
            raise BankError("计划版本不存在，可能已被更新。")
        payload, stored = json.loads(row["input_json"]), json.loads(row["result_json"])
        account = db.execute("SELECT * FROM accounts WHERE id=?", (payload["account_id"],)).fetchone()
        commitments = self.active_commitments(db, payload["account_id"])
        current_living = sum(c["remaining_minor"] for c in commitments if c.get("kind") == "living")
        if payload.get("living_reserve_minor", 0) > current_living:
            commitments.append(
                {
                    "remaining_minor": payload["living_reserve_minor"] - current_living,
                    "status": "active",
                    "kind": "living",
                }
            )
        if stored["assumptions"].get("rent") and not any(c.get("kind") == "rent" for c in commitments):
            commitments.append(
                {
                    "remaining_minor": stored["assumptions"]["rent"]["amount_minor"],
                    "status": "active",
                    "kind": "rent",
                }
            )
        if stored["assumptions"]["subscriptions"] and not any(
            c.get("kind") == "subscription" for c in commitments
        ):
            commitments.append(
                {
                    "remaining_minor": sum(
                        item["amount_minor"] for item in stored["assumptions"]["subscriptions"]
                    ),
                    "status": "active",
                    "kind": "subscription",
                }
            )
        variant = next((item for item in stored["variants"] if item["result"]["feasible"]), stored["variants"][0])
        events = self._events(stored["assumptions"], payload, variant["action_date"])
        if variant["key"] == "after_salary":
            next(e for e in events if e["key"] == "primary_action")["timing"] = "after_income"
        if dimension == "salary_delay":
            challenge = challenge_salary_delay(account["balance_minor"], commitments, events, payload["as_of"])
            if challenge.get("found"):
                salary = next(e for e in events if e.get("source") == "salary")
                delayed = list(events)
                delayed_salary = next(e for e in delayed if e["key"] == salary["key"])
                delayed_salary["date"] = (date.fromisoformat(salary["date"]) + timedelta(days=challenge["minimum_days"])).isoformat()
                challenge["repair"] = earliest_repair_date(
                    account["balance_minor"], commitments, delayed, payload["as_of"], "primary_action", payload["deadline"]
                )
        elif dimension == "unexpected_expense":
            challenge = challenge_unexpected_expense(
                account["balance_minor"], commitments, events, payload["as_of"], expense_date or variant["action_date"]
            )
        else:
            raise BankError("不支持这个挑战维度。")
        return {"plan_id": plan_id, "version": version, "title": stored["title"], "challenge": challenge}

    def list_plans(self, db):
        return [dict(row) for row in db.execute("SELECT * FROM plans ORDER BY updated_at DESC")]

    def trigger_salary(self, db, payload):
        period = payload["period"]
        event_key = payload.get("event_key") or f"salary:{payload['source_key']}:{period}"
        existing = db.execute("SELECT * FROM bank_events WHERE event_key=?", (event_key,)).fetchone()
        if existing:
            return {"duplicate": True, "event_id": existing["id"], "suggestions": []}
        account = db.execute("SELECT * FROM accounts WHERE id=?", (payload["account_id"],)).fetchone()
        if not account:
            raise BankError("工资入账账户不存在。")
        event_id = uid("evt_")
        occurred_at = payload.get("occurred_at") or now()
        db.execute("UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?", (payload["amount_minor"], account["id"]))
        db.execute(
            "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (uid("tx_"), event_id, account["id"], "工资收入", "in", payload["amount_minor"], "工资", occurred_at, payload["source_key"], "模拟工资到账事件"),
        )
        db.execute(
            "INSERT INTO bank_events VALUES (?,?,?,?,?,?,?)",
            (event_id, event_key, "salary", period, dumps(payload), occurred_at, now()),
        )
        suggestions = []
        for rule in db.execute(
            "SELECT * FROM trigger_rules WHERE status='active' AND source_key=? AND account_id=?",
            (payload["source_key"], account["id"]),
        ).fetchall():
            if rule["cadence"] == "once" and rule["last_period"]:
                continue
            available, protected = self.available_minor(db, account["id"])
            amount = max(0, available - rule["reserve_minor"])
            result = {"amount_minor": amount, "protected_minor": protected, "balance_minor": account["balance_minor"] + payload["amount_minor"]}
            run_id = uid("run_")
            db.execute(
                "INSERT OR IGNORE INTO trigger_runs VALUES (?,?,?,?,?,?,?)",
                (run_id, rule["id"], event_id, period, "salary", dumps(result), now()),
            )
            if not db.execute("SELECT changes()").fetchone()[0]:
                continue
            db.execute(
                "UPDATE trigger_rules SET last_period=?,status=CASE WHEN cadence='once' THEN 'completed' ELSE status END,updated_at=? WHERE id=?",
                (period, now(), rule["id"]),
            )
            notification_id = uid("notice_")
            notice = {
                "notification_id": notification_id,
                "rule_id": rule["id"],
                "event_id": event_id,
                "amount_minor": amount,
                "period": period,
                "plan_id": rule["plan_id"],
            }
            db.execute(
                "INSERT INTO notifications VALUES (?,?,?,?,?,?,?)",
                (notification_id, rule["conversation_id"], "trigger", "工资已到账，储蓄方案已重算", dumps(notice), None, now()),
            )
            add_message(
                db,
                rule["conversation_id"],
                None,
                "assistant",
                "plan",
                "工资到账后，我按最新余额重新计算了可转存金额。",
                {"card_type": "trigger", **notice},
            )
            event(db, "*", "notification", {"conversation_id": rule["conversation_id"], **notice})
            suggestions.append(notice)
        event(db, "*", "bank.updated", {})
        return {"duplicate": False, "event_id": event_id, "suggestions": suggestions}

    def seed_demo_scenario(self, db):
        subscription_total = db.execute("SELECT COALESCE(SUM(amount_minor),0) FROM subscriptions WHERE status='active'").fetchone()[0]
        demo_balance = 620000 + subscription_total
        db.execute("UPDATE accounts SET balance_minor=? WHERE id='daily'", (demo_balance,))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('business_date','2026-09-30')")
        db.execute("DELETE FROM commitments")
        created = now()
        for kind, label, amount, due in (
            ("rent", "10 月房租", 320000, "2026-10-02"),
            ("living", "下月生活费", 300000, "2026-10-31"),
            ("subscription", "下月订阅", subscription_total, "2026-10-31"),
        ):
            if amount == 0:
                continue
            db.execute(
                "INSERT INTO commitments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid("commit_"), "demo", self.database.conversation_id(db), "daily", kind, label, amount, amount, due, "active", created, created),
            )
        return {"business_date": "2026-09-30", "daily_balance_minor": demo_balance, "protected_minor": demo_balance}
