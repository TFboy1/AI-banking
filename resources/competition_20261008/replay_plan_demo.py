"""临时数据库回放，不连接模型、不改日常沙箱。"""
import tempfile
from pathlib import Path
from banking.db import Database
from banking.planning import PlanningService

with tempfile.TemporaryDirectory(prefix="bankmate_replay_") as tmp:
    database = Database(Path(tmp) / "bank.sqlite3")
    database.initialize()
    service = PlanningService(database)
    with database.transaction() as db:
        service.seed_demo_scenario(db)
        plan = service.preview(db, database.conversation_id(db), {
            "kind": "purchase", "title": "购买电脑方案",
            "action_label": "购买电脑", "amount_minor": 600000,
            "living_reserve_minor": 300000, "account_id": "daily",
            "salary_amount_minor": 1800000, "salary_day": 1,
            "rent_amount_minor": 320000, "rent_day": 2,
            "deadline": "2026-10-31",
        })
        variants = {v["key"]: v for v in plan["variants"]}
        assert not variants["now"]["result"]["feasible"]
        assert variants["now"]["result"]["first_failure"]["shortfall_minor"] == 600000
        assert variants["after_salary"]["result"]["feasible"]
        assert variants["after_salary"]["result"]["ending_available_minor"] == 1200000
        challenge = service.challenge(db, plan["plan_id"], plan["version"], "salary_delay")["challenge"]
        assert challenge["minimum_days"] == 1
        assert challenge["repair"]["date"] == "2026-10-02"
        assert challenge["repair"]["result"]["feasible"]
        print("回放通过：立即购买缺口 6000 元；工资后购买可行；延期 1 天失效；10 月 2 日修复通过。")
