"""确定性的未来现金流投影与压力测试。所有金额均为人民币分。"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta


def _event_order(item: dict) -> tuple:
    # 无具体时刻时采用保守顺序；显式依赖到账的步骤排在收入之后。
    order = {"expense": 0, "action": 1, "income": 2, "after_income": 3}
    return item["date"], order.get(item.get("timing", item["kind"]), 1), item.get("key", "")


def project_cashflow(balance_minor: int, commitments: list[dict], events: list[dict], as_of: str) -> dict:
    balance = balance_minor
    protected_by_kind = {}
    for commitment in commitments:
        if commitment.get("status", "active") == "active":
            kind = commitment.get("kind") or commitment.get("id") or "other"
            protected_by_kind[kind] = protected_by_kind.get(kind, 0) + commitment["remaining_minor"]
    protected = sum(protected_by_kind.values())
    opening_available = balance - protected
    lowest_available = opening_available
    lowest_date = as_of
    first_failure = (
        {"date": as_of, "event": "预演起点", "shortfall_minor": -opening_available}
        if opening_available < 0
        else None
    )
    timeline = []
    for item in sorted(deepcopy(events), key=_event_order):
        amount = item["amount_minor"]
        if item["kind"] == "income":
            balance += amount
        else:
            balance -= amount
            release_kind = item.get("release_commitment_kind")
            release = min(
                protected_by_kind.get(release_kind, 0) if release_kind else 0,
                item.get("release_commitment_minor", 0),
            )
            if release_kind:
                protected_by_kind[release_kind] = protected_by_kind.get(release_kind, 0) - release
            protected -= release
            consume = min(protected, item.get("consume_budget_minor", 0))
            protected -= consume
        available = balance - protected
        timeline.append(
            {
                **item,
                "balance_minor": balance,
                "protected_minor": protected,
                "available_minor": available,
            }
        )
        if available < lowest_available:
            lowest_available, lowest_date = available, item["date"]
        if available < 0 and first_failure is None:
            first_failure = {
                "date": item["date"],
                "event": item["label"],
                "shortfall_minor": -available,
            }
    return {
        "feasible": first_failure is None,
        "opening_available_minor": opening_available,
        "ending_balance_minor": balance,
        "ending_protected_minor": protected,
        "ending_available_minor": balance - protected,
        "lowest_available_minor": lowest_available,
        "lowest_date": lowest_date,
        "first_failure": first_failure,
        "timeline": timeline,
    }


def challenge_salary_delay(
    balance_minor: int,
    commitments: list[dict],
    events: list[dict],
    as_of: str,
    max_days: int = 30,
) -> dict:
    salary = next((e for e in events if e.get("source") == "salary" and e["kind"] == "income"), None)
    if not salary:
        return {"dimension": "salary_delay", "found": False, "reason": "方案中没有预计工资事件。"}
    original = date.fromisoformat(salary["date"])
    previous = None
    for days in range(max_days + 1):
        shifted = deepcopy(events)
        target = next(e for e in shifted if e.get("key") == salary.get("key"))
        target["date"] = (original + timedelta(days=days)).isoformat()
        result = project_cashflow(balance_minor, commitments, shifted, as_of)
        if not result["feasible"]:
            return {
                "dimension": "salary_delay",
                "found": True,
                "minimum_days": days,
                "previous_days_passed": days - 1 if days else None,
                "failure": result["first_failure"],
                "scope": f"工资延期 0～{max_days} 天",
                "previous_result": previous,
            }
        previous = {"lowest_available_minor": result["lowest_available_minor"], "lowest_date": result["lowest_date"]}
    return {
        "dimension": "salary_delay",
        "found": False,
        "scope": f"工资延期 0～{max_days} 天",
        "reason": f"工资延期 {max_days} 天以内方案仍可行。",
    }


def challenge_unexpected_expense(
    balance_minor: int,
    commitments: list[dict],
    events: list[dict],
    as_of: str,
    expense_date: str,
) -> dict:
    baseline = project_cashflow(balance_minor, commitments, events, as_of)
    # 预算外支出会永久减少之后每个时点的可支配余额，边界就是该日及之后的最小余量。
    later = [baseline["ending_available_minor"] if not baseline["timeline"] else None]
    if expense_date <= as_of:
        later.append(balance_minor - sum(c["remaining_minor"] for c in commitments))
    later += [row["available_minor"] for row in baseline["timeline"] if row["date"] >= expense_date]
    capacity = max(0, min(v for v in later if v is not None))
    return {
        "dimension": "unexpected_expense",
        "found": True,
        "date": expense_date,
        "maximum_minor": capacity,
        "first_failure_minor": capacity + 1,
        "shortfall_minor": 1,
        "scope": "指定日期的一笔预算外支出，精确到 0.01 元",
    }


def earliest_repair_date(
    balance_minor: int,
    commitments: list[dict],
    events: list[dict],
    as_of: str,
    action_key: str,
    deadline: str,
) -> dict | None:
    action = next((e for e in events if e.get("key") == action_key), None)
    if not action:
        return None
    start = date.fromisoformat(action["date"])
    end = date.fromisoformat(deadline)
    for offset in range((end - start).days + 1):
        candidate = deepcopy(events)
        moved = next(e for e in candidate if e.get("key") == action_key)
        moved["date"] = (start + timedelta(days=offset)).isoformat()
        moved["timing"] = "after_income"
        result = project_cashflow(balance_minor, commitments, candidate, as_of)
        if result["feasible"]:
            return {"date": moved["date"], "delay_days": offset, "result": result}
    return None
