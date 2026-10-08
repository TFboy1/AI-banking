from datetime import date

from banking.cashflow import challenge_salary_delay, challenge_unexpected_expense, project_cashflow
from banking.planning import PlanningService


def test_commitment_release_does_not_double_count_rent():
    result = project_cashflow(
        620000,
        [
            {"kind": "rent", "remaining_minor": 320000, "status": "active"},
            {"kind": "living", "remaining_minor": 300000, "status": "active"},
        ],
        [
            {
                "key": "rent",
                "kind": "expense",
                "date": "2026-10-02",
                "amount_minor": 320000,
                "release_commitment_minor": 320000,
                "release_commitment_kind": "rent",
                "label": "房租",
            }
        ],
        "2026-09-30",
    )
    assert result["feasible"]
    assert result["ending_balance_minor"] == 300000
    assert result["ending_protected_minor"] == 300000
    assert result["ending_available_minor"] == 0


def test_projection_exposes_chart_origin_and_first_gap():
    result = project_cashflow(
        10000,
        [{"kind": "living", "remaining_minor": 2000, "status": "active"}],
        [
            {"key": "salary", "kind": "income", "date": "2026-10-01", "amount_minor": 2000, "label": "工资"},
            {"key": "expense", "kind": "expense", "date": "2026-10-02", "amount_minor": 15000, "label": "支出"},
        ],
        "2026-09-30",
    )
    assert result["opening_available_minor"] == 8000
    assert [item["available_minor"] for item in result["timeline"]] == [10000, -5000]
    assert result["first_failure"] == {"date": "2026-10-02", "event": "支出", "shortfall_minor": 5000}
    already_short = project_cashflow(
        1000, [{"kind": "living", "remaining_minor": 2000, "status": "active"}], [], "2026-09-30"
    )
    assert not already_short["feasible"]
    assert already_short["first_failure"] == {
        "date": "2026-09-30", "event": "预演起点", "shortfall_minor": 1000
    }


def test_salary_delay_finds_one_day_boundary_and_repair_condition():
    commitments = [
        {"kind": "rent", "remaining_minor": 320000, "status": "active"},
        {"kind": "living", "remaining_minor": 300000, "status": "active"},
    ]
    events = [
        {"key": "salary", "kind": "income", "source": "salary", "date": "2026-10-01", "amount_minor": 1800000, "label": "工资"},
        {"key": "save", "kind": "action", "timing": "after_income", "date": "2026-10-01", "amount_minor": 200000, "label": "转存"},
        {"key": "rent", "kind": "expense", "date": "2026-10-02", "amount_minor": 320000, "release_commitment_minor": 320000, "release_commitment_kind": "rent", "label": "房租"},
    ]
    challenge = challenge_salary_delay(620000, commitments, events, "2026-09-30")
    assert challenge["found"] and challenge["minimum_days"] == 1


def test_unexpected_expense_boundary_is_exact_to_one_cent():
    commitments = [
        {"kind": "rent", "remaining_minor": 320000, "status": "active"},
        {"kind": "living", "remaining_minor": 300000, "status": "active"},
        {"kind": "subscription", "remaining_minor": 5600, "status": "active"},
    ]
    events = [{"key": "computer", "kind": "action", "date": "2026-09-30", "amount_minor": 600000, "label": "电脑"}]
    challenge = challenge_unexpected_expense(1286050, commitments, events, "2026-09-30", "2026-09-30")
    assert challenge["maximum_minor"] == 60450
    assert challenge["first_failure_minor"] == 60451


def test_salary_event_is_idempotent(database):
    service = PlanningService(database)
    payload = {"kind": "salary", "amount_minor": 1800000, "account_id": "daily", "source_key": "salary", "period": "2026-09"}
    with database.transaction() as db:
        before = db.execute("SELECT balance_minor FROM accounts WHERE id='daily'").fetchone()[0]
        first = service.trigger_salary(db, payload)
    with database.transaction() as db:
        second = service.trigger_salary(db, payload)
        after = db.execute("SELECT balance_minor FROM accounts WHERE id='daily'").fetchone()[0]
    assert not first["duplicate"] and second["duplicate"]
    assert after - before == 1800000


def test_demo_scenario_has_fixed_business_date_and_full_protection(database):
    service = PlanningService(database)
    with database.transaction() as db:
        result = service.seed_demo_scenario(db)
        available, protected = service.available_minor(db)
        assert service.business_date(db) == date(2026, 9, 30)
    assert result["daily_balance_minor"] == 625600
    assert protected == 625600 and available == 0


def test_multimonth_savings_is_split_by_salary_cycles(database):
    service = PlanningService(database)
    with database.transaction() as db:
        conversation_id = database.conversation_id(db)
        result = service.preview(
            db,
            conversation_id,
            {
                "kind": "savings",
                "title": "三个月储蓄目标",
                "amount_minor": 1000000,
                "living_reserve_minor": 0,
                "as_of": "2026-09-19",
                "salary_amount_minor": 1800000,
                "salary_day": 1,
                "action_date": "2026-09-19",
                "deadline": "2026-12-31",
                "account_id": "daily",
            },
        )
    amounts = [item["amount_minor"] for item in result["installments"]]
    assert len(amounts) == 3
    assert amounts == [333333, 333333, 333334]
    assert sum(amounts) == 1000000


def test_income_inference_needs_two_matching_cycles(database):
    service = PlanningService(database)
    with database.transaction() as db:
        assert service.infer_assumptions(db)["salary"]["amount_minor"] == 1800000
        db.execute(
            "UPDATE transactions SET amount_minor=1700000 WHERE merchant_key='salary' AND substr(occurred_at,1,7)=(SELECT MIN(substr(occurred_at,1,7)) FROM transactions WHERE merchant_key='salary')"
        )
        assumptions = service.infer_assumptions(db)
    assert assumptions["salary"] is None
