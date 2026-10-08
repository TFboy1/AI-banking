import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .db import TZ, Database, dumps, now, uid


class BankError(ValueError):
    pass


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Money = Annotated[str, Field(pattern=r"^(0|[1-9]\d{0,8})(\.\d{1,2})?$")]
Identifier = Annotated[str, Field(min_length=1, max_length=100)]


class Empty(Input):
    pass


class RecipientQuery(Input):
    query: str = Field(min_length=1, max_length=100)


class BillsQuery(Input):
    month: str | None = Field(default=None, description="YYYY-MM，省略为本月")


class Transfer(Input):
    recipient: Identifier = Field(description="姓名、手机号、联系人 ID 或本人目标账户 ID；重名须澄清")
    amount_yuan: Money = Field(description="人民币元，必须是字符串，例如 200.00")
    account_id: Identifier = "daily"
    note: str = Field(default="", max_length=120)
    plan_id: str | None = Field(default=None, max_length=100)


class TransferDiagnosis(Input):
    recipient: Identifier | None = None
    amount_yuan: Money | None = None
    account_id: Identifier = "daily"


class PurchaseDiagnosis(Input):
    product_id: Identifier | None = None
    amount_yuan: Money | None = None
    account_id: Identifier = "daily"


class RiskAnswers(Input):
    horizon: Literal["short", "medium", "long"]
    loss_tolerance: Literal["none", "low", "moderate", "high"]
    experience: Literal["none", "some", "experienced"]


class Purchase(Input):
    product_id: Identifier
    amount_yuan: Money
    account_id: Identifier = "daily"


class Redemption(Input):
    product_id: Identifier
    units: str = Field(pattern=r"^(0|[1-9]\d{0,9})(\.\d{1,4})?$")
    account_id: Identifier = "daily"


class CardStatus(Input):
    card_id: Identifier = "card_daily"
    status: Literal["active", "frozen"]


class CardLimit(Input):
    daily_limit_yuan: Money
    card_id: Identifier = "card_daily"


class CancelSubscription(Input):
    subscription_id: Identifier


class CashflowPreview(Input):
    kind: Literal["purchase", "savings"]
    amount_yuan: Money
    title: str = Field(default="资金安排", min_length=1, max_length=80)
    action_label: str = Field(default="", max_length=80)
    action_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    deadline: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    living_reserve_yuan: str = Field(default="0", pattern=r"^(0|[1-9]\d{0,8})(\.\d{1,2})?$")
    account_id: Identifier = "daily"
    plan_id: str | None = Field(default=None, max_length=100)
    as_of: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    salary_amount_yuan: str | None = Field(default=None, pattern=r"^(0|[1-9]\d{0,8})(\.\d{1,2})?$")
    salary_day: int | None = Field(default=None, ge=1, le=28)
    rent_amount_yuan: str | None = Field(default=None, pattern=r"^(0|[1-9]\d{0,8})(\.\d{1,2})?$")
    rent_day: int | None = Field(default=None, ge=1, le=28)


class SimulatedExpense(Input):
    merchant: str = Field(min_length=1, max_length=80)
    amount_yuan: Money
    category: str = Field(default="购物", min_length=1, max_length=30)
    account_id: Identifier = "daily"
    plan_id: str | None = Field(default=None, max_length=100)
    plan_version: int | None = Field(default=None, ge=1)
    budget_commitment_id: str | None = Field(default=None, max_length=100)


class ReserveFunds(Input):
    label: str = Field(min_length=1, max_length=80)
    kind: Literal["rent", "living", "subscription", "other"]
    amount_yuan: Money
    due_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    account_id: Identifier = "daily"
    plan_id: str | None = Field(default=None, max_length=100)


class TriggerRule(Input):
    reserve_yuan: str = Field(default="0", pattern=r"^(0|[1-9]\d{0,8})(\.\d{1,2})?$")
    cadence: Literal["once", "monthly"] = "once"
    expected_day: int = Field(default=1, ge=1, le=28)
    source_key: str = Field(default="salary", min_length=1, max_length=80)
    account_id: Identifier = "daily"
    plan_id: str | None = Field(default=None, max_length=100)


class SavingsGoal(Input):
    total_yuan: Money = Field(description="该目标累计要转入储蓄账户的总金额")
    plan_id: str | None = Field(default=None, max_length=100, description="修改已有目标时传入计划 ID")
    account_id: Identifier = "daily"
    installment_yuan: Money | None = None


class ActivatePlan(Input):
    plan_id: Identifier
    plan_version: int = Field(ge=1)
    variant_key: Literal["now", "after_salary"] = "now"


class AdjustCommitment(Input):
    commitment_id: Identifier
    remaining_yuan: str = Field(pattern=r"^(0|[1-9]\d{0,8})(\.\d{1,2})?$")


TOOLS = {
    "get_accounts": (Empty, False, "查询本人账户、可用余额和当前风险等级。"),
    "find_recipients": (
        RecipientQuery,
        False,
        "按姓名或手机号查找联系人及本人账户，多个结果时先请用户选择。",
    ),
    "analyze_bills": (BillsQuery, False, "按月查询消费分类、收入支出和可解释的异常线索；金额由数据库计算。"),
    "get_risk_questionnaire": (Empty, False, "获取三项风险问题、选项和当前回答；不得替用户编造回答。"),
    "list_products": (Empty, False, "查询模拟产品、风险匹配和当前持仓。所有净值均为演示数据。"),
    "list_cards": (Empty, False, "查询银行卡状态和日支付限额。"),
    "list_subscriptions": (Empty, False, "查看周期扣费识别依据、代扣协议状态和预计下次扣费。"),
    "list_plans": (Empty, False, "查询跨会话共享的资金计划、已完成目标金额和当前资金承诺。"),
    "preview_cashflow": (CashflowPreview, False, "预演购买或储蓄目标对未来现金流的影响，返回可核对的方案卡。"),
    "diagnose_transfer": (
        TransferDiagnosis,
        False,
        "用户询问为什么不能转账时，逐项检查收款人、余额、已安排资金、卡片和当日限额；只读，不发起转账。",
    ),
    "diagnose_purchase": (
        PurchaseDiagnosis,
        False,
        "用户询问为什么无法申购时，逐项检查产品、风险测评、起购金额、余额和卡片；只读，不发起申购。",
    ),
    "transfer": (Transfer, True, "发起转账确认。不会立即执行，只有用户点击确认后才会转账。"),
    "save_risk_profile": (RiskAnswers, True, "保存用户实际回答的风险问卷，需要用户确认，由后端计算等级。"),
    "purchase_product": (Purchase, True, "发起模拟申购确认，需已有风险测评且匹配产品风险。"),
    "redeem_product": (Redemption, True, "发起按份额赎回确认；先查实际持仓。"),
    "set_card_status": (CardStatus, True, "发起冻结或解冻银行卡确认。"),
    "set_card_limit": (CardLimit, True, "调整银行卡单日支付限额，需要确认，演示上限为 50000 元。"),
    "cancel_subscription": (
        CancelSubscription,
        True,
        "取消指定订阅的自动代扣，需要确认；不产生退款或即时入账。",
    ),
    "simulate_expense": (SimulatedExpense, True, "发起一笔模拟消费确认，确认后生成扣款流水与回执。"),
    "reserve_funds": (ReserveFunds, True, "为房租、生活费或其他用途预留资金，需要用户确认。"),
    "create_trigger_rule": (TriggerRule, True, "登记工资到账后的储蓄提醒规则，需要用户确认，绝不自动转账。"),
    "save_to_goal": (SavingsGoal, True, "创建或修改累计储蓄目标，只对尚未完成的差额生成转存确认。"),
    "activate_plan": (ActivatePlan, True, "采用经过预演的指定计划版本，并使其中的资金预留生效。"),
    "adjust_commitment": (AdjustCommitment, True, "减少或解除一项已确认资金承诺，需要用户再次确认。"),
}


def tool_schemas():
    return [
        {
            "type": "function",
            "function": {"name": name, "description": description, "parameters": model.model_json_schema()},
        }
        for name, (model, _, description) in TOOLS.items()
    ]


def validate(name, arguments):
    if name not in TOOLS:
        raise BankError("该工具不在允许使用的银行工具列表中。")
    try:
        return TOOLS[name][0].model_validate(arguments).model_dump()
    except ValidationError as exc:
        errors = [f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in exc.errors()]
        raise BankError("工具参数不正确：" + "；".join(errors)) from None


def minor(value: str) -> int:
    amount = int(Decimal(value) * 100)
    if amount <= 0:
        raise BankError("金额必须大于 0。")
    return amount


def yuan(value: int) -> str:
    return f"¥{Decimal(value) / 100:,.2f}"


def table(title, columns, rows, *, note="", metrics=None):
    return {"title": title, "columns": columns, "rows": rows, "note": note, "metrics": metrics or []}


def check(key, label, status, detail, references=(), error=None):
    return {
        "key": key,
        "label": label,
        "status": status,
        "detail": detail,
        "references": list(references),
        "error": error,
    }


def public_checks(checks):
    return [{key: value for key, value in item.items() if key != "error"} for item in checks]


def check_status(checks):
    if any(item["status"] == "block" for item in checks):
        return "blocked"
    if any(item["status"] == "unknown" for item in checks):
        return "unknown"
    return "ready"


def require_checks(checks):
    for item in checks:
        if item["status"] != "pass":
            raise BankError(item["error"] or item["detail"])


def row_required(db, table_name, identifier, label):
    # 表名只由下方固定业务代码传入，模型参数仅作为绑定值。
    result = db.execute(f"SELECT * FROM {table_name} WHERE id=?", (identifier,)).fetchone()
    if not result:
        raise BankError(f"找不到{label}，请先查询可用选项。")
    return dict(result)


def risk_level(answers):
    # 保守的演示规则，最终等级取三项能力的最低值。
    return min(
        {"short": 1, "medium": 2, "long": 4}[answers["horizon"]],
        {"none": 1, "low": 2, "moderate": 3, "high": 4}[answers["loss_tolerance"]],
        {"none": 2, "some": 3, "experienced": 4}[answers["experience"]],
    )


RISK_LABELS = {
    "horizon": {"short": "一年以内", "medium": "一至三年", "long": "三年以上"},
    "loss_tolerance": {
        "none": "不接受亏损",
        "low": "可接受小幅亏损",
        "moderate": "可接受中等亏损",
        "high": "可接受较大亏损",
    },
    "experience": {"none": "没有投资经验", "some": "有一些投资经验", "experienced": "投资经验较丰富"},
}


class Bank:
    def __init__(self, database: Database):
        self.database = database

    def snapshot(self):
        with self.database.connection() as db:
            accounts = [dict(row) for row in db.execute("SELECT * FROM accounts")]
            cards = [dict(row) for row in db.execute("SELECT * FROM cards")]
            holdings = self._holdings(db)
            risk = db.execute("SELECT risk_level FROM risk_profile WHERE id=1").fetchone()
            protected = sum(
                row[0]
                for row in db.execute(
                    "SELECT remaining_minor FROM commitments WHERE status='active' AND remaining_minor>0"
                )
            )
            business_row = db.execute("SELECT value FROM meta WHERE key='business_date'").fetchone()
            business = datetime.fromisoformat(business_row[0]).date() if business_row else datetime.now(TZ).date()
            next_salary = (business.replace(day=28) + timedelta(days=4)).replace(day=1)
            daily = next((a for a in accounts if a["id"] == "daily"), None)
            return {
                "accounts": accounts,
                "cards": cards,
                "holdings": holdings,
                "risk_level": risk[0] if risk else None,
                "total_assets_minor": sum(a["balance_minor"] for a in accounts)
                + sum(h["value_minor"] for h in holdings),
                "transactions": [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM transactions ORDER BY occurred_at DESC,rowid DESC LIMIT 12"
                    )
                ],
                "subscriptions": [dict(row) for row in db.execute("SELECT * FROM subscriptions")],
                "current_month": datetime.now(TZ).strftime("%Y-%m"),
                "protected_minor": protected,
                "free_minor": max(0, (daily["balance_minor"] if daily else 0) - protected),
                "business_date": business.isoformat(),
                "next_salary_period": next_salary.strftime("%Y-%m"),
            }

    def _holdings(self, db):
        result = []
        for row in db.execute(
            "SELECT h.*,p.name,p.nav,p.risk_level FROM holdings h JOIN products p ON p.id=h.product_id"
        ):
            item = dict(row)
            item["value_minor"] = int(
                (Decimal(item["units"]) * Decimal(item["nav"]) * 100).to_integral_value(
                    rounding=ROUND_HALF_UP
                )
            )
            if Decimal(item["units"]) > 0:
                result.append(item)
        return result

    def query(self, name, arguments, conversation_id=None):
        args = validate(name, arguments)
        if TOOLS[name][1]:
            raise BankError("变更工具必须经过操作确认。")
        if name == "preview_cashflow":
            args["_conversation_id"] = conversation_id
            with self.database.transaction() as db:
                return getattr(self, "_" + name)(db, args)
        with self.database.connection() as db:
            return getattr(self, "_" + name)(db, args)

    def _get_accounts(self, db, args):
        accounts = [dict(row) for row in db.execute("SELECT * FROM accounts")]
        risk = db.execute("SELECT risk_level FROM risk_profile WHERE id=1").fetchone()
        return {
            "accounts": accounts,
            "risk_level": risk[0] if risk else None,
            "display": table(
                "账户与可用余额",
                ["账户", "可用余额"],
                [[a["name"], yuan(a["balance_minor"])] for a in accounts],
            ),
        }

    def _recipients(self, db, query):
        query = query.strip().lower()
        result = []
        for row in db.execute("SELECT * FROM accounts"):
            item = dict(row)
            if query in item["name"].lower() or query == item["id"]:
                result.append(
                    {"id": item["id"], "name": item["name"], "kind": "own_account", "detail": "本人账户"}
                )
        for row in db.execute("SELECT * FROM contacts"):
            item = dict(row)
            if query in item["name"].lower() or query in item["phone"] or query == item["id"]:
                masked_phone = item["phone"][:3] + "****" + item["phone"][-4:]
                result.append(
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "kind": "contact",
                        "phone_masked": masked_phone,
                        "detail": f"{item['bank']} · 尾号 {item['last4']} · 手机 {masked_phone}",
                    }
                )
        return result

    def _find_recipients(self, db, args):
        items = self._recipients(db, args["query"])
        return {
            "recipients": items,
            "needs_clarification": len(items) > 1,
            "display": table(
                "找到的收款人",
                ["姓名 / 账户", "识别信息"],
                [[i["name"], i["detail"]] for i in items],
                note="有多个匹配结果，请指定手机号或具体账户。" if len(items) > 1 else "",
            ),
        }

    def _analyze_bills(self, db, args):
        month = args["month"] or datetime.now(TZ).strftime("%Y-%m")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise BankError("月份格式应为 YYYY-MM。")
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM transactions WHERE substr(occurred_at,1,7)=? ORDER BY occurred_at", (month,)
            )
        ]
        # 本人账户互转和投资本金进出不计入消费；完整流水仍可在模拟面板查看。
        spending = [r for r in rows if r["direction"] == "out" and r["category"] not in ("内部转账", "理财")]
        income = sum(
            r["amount_minor"]
            for r in rows
            if r["direction"] == "in" and r["category"] not in ("内部转账", "理财")
        )
        categories = defaultdict(int)
        repeated = defaultdict(list)
        anomalies = []
        for row in spending:
            categories[row["category"]] += row["amount_minor"]
            if row["merchant_key"]:
                repeated[(row["merchant_key"], row["amount_minor"])].append(row)
            if row["amount_minor"] >= 300000:
                anomalies.append(
                    {
                        "reason": "单笔消费达到演示关注阈值 3000 元",
                        "transaction_ids": [row["id"]],
                        "merchant": row["counterparty"],
                        "amount_minor": row["amount_minor"],
                    }
                )
        for items in repeated.values():
            if len(items) > 1 and items[0]["category"] == "订阅":
                anomalies.append(
                    {
                        "reason": "同一订阅在一个月内出现多笔同金额扣费，请核对",
                        "transaction_ids": [i["id"] for i in items],
                        "merchant": items[0]["counterparty"],
                        "amount_minor": sum(i["amount_minor"] for i in items),
                    }
                )
        total = sum(categories.values())
        return {
            "month": month,
            "income_minor": income,
            "expense_minor": total,
            "categories": dict(categories),
            "anomalies": anomalies,
            "explanation": {
                "as_of": now(),
                "sources": [
                    {
                        "label": f"{month} 的模拟交易流水，共 {len(rows)} 笔",
                        "references": [row["id"] for row in rows],
                    }
                ],
                "checks": [
                    {
                        "label": "消费口径",
                        "status": "pass",
                        "detail": f"纳入 {len(spending)} 笔对外支出；本人账户互转和理财本金进出不计入消费。",
                    },
                    {
                        "label": "异常线索",
                        "status": "pass",
                        "detail": f"按演示阈值及同月订阅重复扣费检查，发现 {len(anomalies)} 条待核对线索；不等于认定欺诈。",
                    },
                ],
                "conclusion": f"本月消费 {yuan(total)}，收入 {yuan(income)}，均由上述流水汇总。",
            },
            "display": table(
                f"{month} 月度账单",
                ["消费类别", "支出"],
                [[key, yuan(value)] for key, value in sorted(categories.items(), key=lambda p: -p[1])],
                metrics=[
                    {"label": "本月收入", "value": yuan(income)},
                    {"label": "本月支出", "value": yuan(total)},
                ],
                note="统计排除本人账户互转和理财本金进出。异常提示是核对线索，不代表已认定欺诈。",
            ),
            "alerts": [
                {
                    "text": f"{a['merchant']}：{a['reason']}（{yuan(a['amount_minor'])}）",
                    "references": a["transaction_ids"],
                }
                for a in anomalies
            ],
        }

    def _get_risk_questionnaire(self, db, args):
        current = db.execute("SELECT * FROM risk_profile WHERE id=1").fetchone()
        questions = [
            {
                "key": key,
                "label": label,
                "options": [{"value": value, "label": text} for value, text in RISK_LABELS[key].items()],
            }
            for key, label in [
                ("horizon", "预计投资多久？"),
                ("loss_tolerance", "可以接受怎样的亏损？"),
                ("experience", "你的投资经验如何？"),
            ]
        ]
        return {
            "questions": questions,
            "current_answers": json.loads(current["answers_json"]) if current else None,
            "display": table(
                "先了解你的风险偏好",
                ["问题", "可选回答"],
                [[q["label"], " / ".join(o["label"] for o in q["options"])] for q in questions],
                note="请按实际情况回答三项问题。这是演示测评，等级由后端固定规则计算。",
            ),
        }

    def _list_products(self, db, args):
        risk = db.execute("SELECT risk_level FROM risk_profile WHERE id=1").fetchone()
        products = [dict(row) for row in db.execute("SELECT * FROM products")]
        for product in products:
            product["eligible"] = bool(risk and risk[0] >= product["risk_level"])
        return {
            "products": products,
            "holdings": self._holdings(db),
            "risk_level": risk[0] if risk else None,
            "display": table(
                "模拟产品与风险匹配",
                ["产品", "风险", "模拟净值", "起购", "当前匹配"],
                [
                    [
                        p["name"],
                        f"R{p['risk_level']}",
                        p["nav"],
                        yuan(p["min_purchase_minor"]),
                        "可申购" if p["eligible"] else ("需先测评" if not risk else "超出风险等级"),
                    ]
                    for p in products
                ],
                note="固定演示净值，申赎即时结算，无收益承诺。份额保留 4 位；不足一分的申购款向上取整、赎回款向下取整。",
            ),
        }

    def _list_cards(self, db, args):
        cards = [dict(row) for row in db.execute("SELECT * FROM cards")]
        return {
            "cards": cards,
            "display": table(
                "我的银行卡",
                ["卡片", "尾号", "状态", "日支付限额"],
                [
                    [
                        c["name"],
                        c["last4"],
                        "正常" if c["status"] == "active" else "已冻结",
                        yuan(c["daily_limit_minor"]),
                    ]
                    for c in cards
                ],
            ),
        }

    def _list_subscriptions(self, db, args):
        subscriptions = [dict(row) for row in db.execute("SELECT * FROM subscriptions")]
        for item in subscriptions:
            evidence = [
                dict(row)
                for row in db.execute(
                    "SELECT id,occurred_at,amount_minor FROM transactions WHERE merchant_key=? AND direction='out' ORDER BY occurred_at DESC",
                    (item["merchant_key"],),
                )
            ]
            item["evidence"] = evidence[:6]
            item["recurring_detected"] = len({e["occurred_at"][:7] for e in evidence}) >= 2
            if item["status"] != "active":
                item["next_charge_date"] = None
        active_total = sum(s["amount_minor"] for s in subscriptions if s["status"] == "active")
        return {
            "subscriptions": subscriptions,
            "active_monthly_minor": active_total,
            "display": table(
                "订阅与自动代扣",
                ["服务", "每月金额", "下次扣费", "状态"],
                [
                    [
                        s["name"],
                        yuan(s["amount_minor"]),
                        s["next_charge_date"] or "—",
                        "代扣中" if s["status"] == "active" else "已取消",
                    ]
                    for s in subscriptions
                ],
                metrics=[{"label": "有效协议每月预计支出", "value": yuan(active_total)}],
                note="依据跨月扣费记录识别周期性，并与模拟代扣协议核对。取消仅阻止未来代扣，不会立即退款。",
            ),
        }

    def _preview_cashflow(self, db, args):
        from .planning import PlanningService

        conversation_id = args.pop("_conversation_id", None) or self.database.conversation_id(db)
        amount_yuan = args.pop("amount_yuan")
        reserve_yuan = args.pop("living_reserve_yuan")
        salary_yuan = args.pop("salary_amount_yuan", None)
        rent_yuan = args.pop("rent_amount_yuan", None)
        payload = {
            **args,
            "amount_minor": minor(amount_yuan),
            "living_reserve_minor": int(Decimal(reserve_yuan) * 100),
        }
        if salary_yuan is not None:
            payload["salary_amount_minor"] = minor(salary_yuan)
        if rent_yuan is not None:
            payload["rent_amount_minor"] = minor(rent_yuan)
        result = PlanningService(self.database).preview(db, conversation_id, payload)
        best = next((v for v in result["variants"] if v["result"]["feasible"]), result["variants"][0])
        return {
            "plan": result,
            "display": table(
                result["title"],
                ["方案", "执行日期", "最低可支配", "结果"],
                [
                    [
                        variant["label"],
                        variant["action_date"],
                        yuan(variant["result"]["lowest_available_minor"]),
                        "可行" if variant["result"]["feasible"] else "存在缺口",
                    ]
                    for variant in result["variants"]
                ],
                metrics=[
                    {"label": "计划金额", "value": yuan(result["amount_minor"])},
                    {"label": "生活费预留", "value": yuan(result["living_reserve_minor"])},
                ],
                note=f"建议采用“{best['label']}”。金额、日期和风险均由后端按当前账务计算。",
            ),
        }

    def _list_plans(self, db, args):
        plans = []
        for row in db.execute("SELECT * FROM plans ORDER BY updated_at DESC"):
            item = dict(row)
            item["executed_minor"] = db.execute(
                "SELECT COALESCE(SUM(amount_minor),0) FROM plan_executions WHERE plan_id=? AND status='executed'",
                (item["id"],),
            ).fetchone()[0]
            plans.append(item)
        commitments = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM commitments WHERE status='active' AND remaining_minor>0 ORDER BY due_date"
            )
        ]
        return {
            "plans": plans,
            "commitments": commitments,
            "display": table(
                "资金计划与承诺",
                ["计划 / 承诺", "状态或期限", "金额"],
                [
                    [p["title"], p["status"], f"已完成 {yuan(p['executed_minor'])} / 目标 {yuan(p['goal_minor'] or 0)}"]
                    for p in plans
                ]
                + [[c["label"], c["due_date"] or "持续有效", yuan(c["remaining_minor"])] for c in commitments],
                note="承诺跨会话共享；减少或解除承诺需要再次确认。",
            ),
        }

    def prepare(self, name, arguments):
        args = validate(name, arguments)
        if not TOOLS[name][1]:
            raise BankError("该操作不需要确认。")
        if name == "save_to_goal" and not args.get("plan_id"):
            args["plan_id"] = uid("plan_")
        with self.database.connection() as db:
            summary = self._check_action(db, name, args)
        return args, summary

    def _check_debit(self, db, account_id, amount):
        account, checks = self._debit_assessment(db, account_id, amount)
        require_checks(checks)
        return account

    def _debit_assessment(self, db, account_id, amount):
        account_row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not account_row:
            return None, [check("account", "付款账户", "block", "找不到付款账户。", error="找不到付款账户，请先查询可用选项。")]
        account = dict(account_row)
        checks = [check("account", "付款账户", "pass", account["name"], [account_id])]
        commitments = [
            dict(row)
            for row in db.execute(
                """SELECT c.id,c.label,c.remaining_minor,COALESCE(v.title,'已删除会话') AS conversation_title
                FROM commitments c LEFT JOIN conversations v ON v.id=c.conversation_id
                WHERE c.account_id=? AND c.status='active' AND c.remaining_minor>0 ORDER BY c.due_date""",
                (account_id,),
            )
        ]
        protected = sum(row["remaining_minor"] for row in commitments)
        free = account["balance_minor"] - protected
        labels = [
            f"{row['label']} {yuan(row['remaining_minor'])}（会话：{row['conversation_title']}）"
            for row in commitments[:3]
        ]
        reserved_error = (
            f"这笔操作会动用已安排资金。当前余额 {yuan(account['balance_minor'])}，"
            f"其中 {yuan(protected)} 已为其他用途保留，可自由安排 {yuan(max(0, free))}。"
            + (" 已有安排：" + "；".join(labels) + "。" if labels else "")
        )
        checks.append(
            check(
                "reserved_funds",
                "已安排资金",
                "unknown" if amount is None else "block" if protected and free < amount else "pass",
                f"当前余额 {yuan(account['balance_minor'])}；已安排 {yuan(protected)}；可自由安排 {yuan(max(0, free))}。"
                + ("还需提供金额。" if amount is None else f"本次需 {yuan(amount)}。"),
                [row["id"] for row in commitments],
                error=reserved_error,
            )
        )
        checks.append(
            check(
                "balance",
                "账户余额",
                "unknown" if amount is None else "block" if account["balance_minor"] < amount else "pass",
                f"{account['name']}余额 {yuan(account['balance_minor'])}"
                + ("；还需提供金额。" if amount is None else f"；本次需 {yuan(amount)}。"),
                [account_id],
                error=f"{account['name']}余额不足，当前可用 {yuan(account['balance_minor'])}。",
            )
        )
        card_row = db.execute("SELECT * FROM cards WHERE account_id=?", (account_id,)).fetchone()
        if not card_row:
            checks.append(check("card_status", "关联卡状态", "pass", "该账户未关联银行卡，现有规则无卡片状态限制。"))
            checks.append(check("daily_limit", "当日支付限额", "pass", "该账户未关联银行卡，现有规则无卡片日限额。"))
            return account, checks
        card = dict(card_row)
        checks.append(
            check(
                "card_status",
                "关联卡状态",
                "pass" if card["status"] == "active" else "block",
                f"{card['name']}（尾号 {card['last4']}）{'正常' if card['status'] == 'active' else '已冻结'}。",
                [card["id"]],
                error="付款账户关联银行卡已冻结，请先解冻。",
            )
        )
        today = datetime.now(TZ).date().isoformat()
        # 与确认时一致：只统计本次演示产生的当日出账，不计入种子历史流水。
        spent_rows = [
            dict(row)
            for row in db.execute(
                """SELECT id,amount_minor FROM transactions WHERE account_id=? AND direction='out'
                AND action_id IS NOT NULL AND substr(occurred_at,1,10)=?""",
                (account_id, today),
            )
        ]
        remaining = max(0, card["daily_limit_minor"] - sum(row["amount_minor"] for row in spent_rows))
        checks.append(
            check(
                "daily_limit",
                "当日支付限额",
                "unknown" if amount is None else "block" if amount > remaining else "pass",
                f"尾号 {card['last4']} 当日剩余额度 {yuan(remaining)}"
                + ("；还需提供金额。" if amount is None else f"；本次需 {yuan(amount)}。"),
                [card["id"], *[row["id"] for row in spent_rows]],
                error=f"超过银行卡当日可支付额度，目前剩余 {yuan(remaining)}。",
            )
        )
        return account, checks

    def _transfer_assessment(self, db, recipient, amount, account_id):
        matches = self._recipients(db, recipient) if recipient else []
        if not recipient:
            recipient_check = check("recipient", "收款人", "unknown", "请提供收款人。", error="请提供收款人。")
            target = None
        elif not matches:
            recipient_check = check(
                "recipient", "收款人", "block", "没有找到已有收款人。",
                error="没有找到收款人，请提供已有联系人的姓名、手机号或本人账户名称。",
            )
            target = None
        elif len(matches) > 1:
            recipient_check = check(
                "recipient", "收款人", "unknown", "匹配到多个同名收款人，请按账户尾号或联系人编号指定。",
                [item["id"] for item in matches],
                error="存在多个同名收款人，请让用户指定手机号，不得自行选择。",
            )
            target = None
        else:
            target = matches[0]
            same_account = target["kind"] == "own_account" and target["id"] == account_id
            recipient_check = check(
                "recipient",
                "收款人",
                "block" if same_account else "pass",
                f"{target['name']} · {target['detail']}",
                [target["id"]],
                error="付款与收款账户不能相同。" if same_account else None,
            )
        account, debit_checks = self._debit_assessment(db, account_id, amount)
        amount_check = check(
            "amount", "转账金额", "unknown" if amount is None else "pass",
            "请提供拟转账金额。" if amount is None else f"本次拟转账 {yuan(amount)}。",
            error="请提供转账金额。",
        )
        return target, account, [recipient_check, amount_check, *debit_checks]

    def _purchase_assessment(self, db, product_id, amount, account_id):
        product_row = db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone() if product_id else None
        product = dict(product_row) if product_row else None
        if not product_id:
            product_check = check("product", "理财产品", "unknown", "请指定产品。", error="请指定理财产品。")
        elif not product:
            product_check = check("product", "理财产品", "block", "找不到该产品。", error="找不到理财产品，请先查询可用选项。")
        else:
            product_check = check("product", "理财产品", "pass", product["name"], [product_id])
        risk = db.execute("SELECT risk_level FROM risk_profile WHERE id=1").fetchone()
        if not product:
            risk_check = check("risk", "适当性", "unknown", "需要先确定产品风险等级。")
            minimum_check = check("minimum", "起购金额", "unknown", "需要先确定产品起购金额。")
        else:
            risk_status = "block" if not risk or risk[0] < product["risk_level"] else "pass"
            risk_check = check(
                "risk",
                "适当性",
                risk_status,
                f"产品风险 R{product['risk_level']}；"
                + ("尚未完成风险测评。" if not risk else f"当前测评 R{risk[0]}。"),
                [product_id, *(["risk_profile:1"] if risk else [])],
                error=(
                    "请先完成并确认风险问卷，再发起申购。"
                    if not risk else f"产品风险 R{product['risk_level']} 高于你的测评等级 R{risk[0]}，不能申购。"
                ),
            )
            minimum_check = check(
                "minimum",
                "起购金额",
                "unknown" if amount is None else "block" if amount < product["min_purchase_minor"] else "pass",
                f"{product['name']}起购 {yuan(product['min_purchase_minor'])}"
                + ("；还需提供申购金额。" if amount is None else f"；本次 {yuan(amount)}。"),
                [product_id],
                error=f"该产品起购金额为 {yuan(product['min_purchase_minor'])}。",
            )
        account, debit_checks = self._debit_assessment(db, account_id, amount)
        amount_check = check(
            "amount", "申购金额", "unknown" if amount is None else "pass",
            "请提供拟申购金额。" if amount is None else f"本次拟申购 {yuan(amount)}。",
            error="请提供申购金额。",
        )
        return product, account, [product_check, risk_check, amount_check, minimum_check, *debit_checks]

    @staticmethod
    def _decision_explanation(checks, conclusion="当前检查已通过；确认时会按最新账务重新检查，当前尚未执行。"):
        references = list(dict.fromkeys(ref for item in checks for ref in item["references"]))
        return {
            "as_of": now(),
            "sources": [{"label": "模拟银行当前业务记录", "references": references}],
            "checks": public_checks(checks),
            "conclusion": conclusion,
        }

    @staticmethod
    def _generic_explanation(args):
        references = [value for key, value in args.items() if key.endswith("_id") and isinstance(value, str)]
        return {
            "as_of": now(),
            "sources": [{"label": "模拟银行当前业务记录", "references": references}],
            "checks": [
                {
                    "label": "确认闸门",
                    "status": "pass",
                    "detail": "该卡片只保存待确认参数；点击确认后重新按当前业务状态校验。",
                }
            ],
            "conclusion": "当前尚未执行。只有成功回执表示变更已经完成。",
        }

    @staticmethod
    def _diagnosis_next_steps(checks):
        suggestions = {
            "recipient": ("补充收款人", "我想继续诊断上一笔转账，收款人是 "),
            "product": ("补充产品", "我想继续诊断理财申购，产品是 "),
            "amount": ("补充金额", "我想继续诊断上一项操作，金额是 "),
            "risk": ("查看风险测评", "请给我风险测评的三个问题，我会自己回答。"),
            "minimum": ("核对起购金额", "请列出产品的起购金额和风险等级。"),
            "reserved_funds": ("查看资金安排", "请列出我当前的资金计划和已安排资金。"),
            "balance": ("查看账户余额", "请查看我的账户余额。"),
            "card_status": ("查看银行卡状态", "请查看我的银行卡状态。"),
            "daily_limit": ("查看支付限额", "请查看我的银行卡当日支付限额。"),
        }
        result = []
        seen = set()
        unknown = {item["key"] for item in checks if item["status"] == "unknown"}
        for item in checks:
            if item["status"] == "pass" or item["key"] not in suggestions:
                continue
            if "amount" in unknown and item["key"] in {"reserved_funds", "balance", "daily_limit"}:
                continue
            if "product" in unknown and item["key"] in {"risk", "minimum"}:
                continue
            label, prompt = suggestions[item["key"]]
            if prompt not in seen:
                result.append({"label": label, "prompt": prompt})
                seen.add(prompt)
        return result

    def _diagnosis_result(self, title, checks):
        status = check_status(checks)
        conclusion = {
            "ready": "按当前数据可以发起申请；资金或状态变更仍须由你确认，并会再次校验。",
            "blocked": "当前存在办理阻碍；请先处理列出的未通过项目。",
            "unknown": "信息还不足以判断能否办理；请补充待确认项目。",
        }[status]
        return {
            "diagnosis": {
                "title": title,
                "status": status,
                "checks": public_checks(checks),
                "next_steps": self._diagnosis_next_steps(checks),
            },
            "explanation": self._decision_explanation(checks, conclusion),
        }

    def _diagnose_transfer(self, db, args):
        amount = minor(args["amount_yuan"]) if args["amount_yuan"] is not None else None
        _, _, checks = self._transfer_assessment(db, args["recipient"], amount, args["account_id"])
        return self._diagnosis_result("转账办理条件", checks)

    def _diagnose_purchase(self, db, args):
        amount = minor(args["amount_yuan"]) if args["amount_yuan"] is not None else None
        _, _, checks = self._purchase_assessment(db, args["product_id"], amount, args["account_id"])
        return self._diagnosis_result("理财申购条件", checks)

    def _check_action(self, db, name, args):
        fields = []
        extra = {}
        if name == "transfer":
            amount = minor(args["amount_yuan"])
            target, account, checks = self._transfer_assessment(db, args["recipient"], amount, args["account_id"])
            require_checks(checks)
            # 固定收款实体，后续确认不再根据可变名称重新匹配。
            args["recipient"] = target["id"]
            title = "确认转账"
            fields = [
                ["付款账户", account["name"]],
                ["收款人", target["name"]],
                ["识别信息", target["detail"]],
                ["转账金额", yuan(amount)],
                ["备注", args["note"] or "无"],
            ]
            extra = {"amount_minor": amount, "explanation": self._decision_explanation(checks)}
        elif name == "save_risk_profile":
            level = risk_level(args)
            title = "确认风险测评"
            fields = [
                ["投资期限", RISK_LABELS["horizon"][args["horizon"]]],
                ["亏损承受", RISK_LABELS["loss_tolerance"][args["loss_tolerance"]]],
                ["投资经验", RISK_LABELS["experience"][args["experience"]]],
                ["演示风险等级", f"R{level}"],
            ]
        elif name == "purchase_product":
            amount = minor(args["amount_yuan"])
            product, account, checks = self._purchase_assessment(db, args["product_id"], amount, args["account_id"])
            require_checks(checks)
            units, cost = self._purchase_quote(product, amount)
            title = "确认模拟申购"
            fields = [
                ["产品", product["name"]],
                ["风险", f"R{product['risk_level']}"],
                ["付款账户", account["name"]],
                ["实际扣款", yuan(cost)],
                ["固定模拟净值", product["nav"]],
                ["获得份额", str(units)],
            ]
            extra = {"amount_minor": cost, "explanation": self._decision_explanation(checks)}
        elif name == "redeem_product":
            product = row_required(db, "products", args["product_id"], "理财产品")
            units = Decimal(args["units"])
            holding = db.execute("SELECT units FROM holdings WHERE product_id=?", (product["id"],)).fetchone()
            if units <= 0 or not holding or units > Decimal(holding[0]):
                raise BankError("赎回份额必须大于 0 且不能超过实际持仓。")
            account = row_required(db, "accounts", args["account_id"], "到账账户")
            amount = self._redemption_quote(product, units)
            if amount <= 0:
                raise BankError("赎回金额不足 0.01 元，请增加赎回份额。")
            title = "确认模拟赎回"
            fields = [
                ["产品", product["name"]],
                ["赎回份额", str(units)],
                ["预计到账", yuan(amount)],
                ["到账账户", account["name"]],
            ]
            extra = {"amount_minor": amount}
        elif name == "set_card_status":
            card = row_required(db, "cards", args["card_id"], "银行卡")
            if card["status"] == args["status"]:
                raise BankError("银行卡已经处于这个状态，无需重复操作。")
            title = "确认冻结银行卡" if args["status"] == "frozen" else "确认解冻银行卡"
            fields = [
                ["银行卡", card["name"] + " · " + card["last4"]],
                ["变更后状态", "冻结" if args["status"] == "frozen" else "正常"],
            ]
        elif name == "set_card_limit":
            card = row_required(db, "cards", args["card_id"], "银行卡")
            amount = minor(args["daily_limit_yuan"])
            if amount > 5000000:
                raise BankError("演示银行卡的日支付限额最高为 50000 元。")
            title = "确认调整支付限额"
            fields = [
                ["银行卡", card["name"] + " · " + card["last4"]],
                ["原限额", yuan(card["daily_limit_minor"])],
                ["新限额", yuan(amount)],
            ]
        elif name == "cancel_subscription":
            item = row_required(db, "subscriptions", args["subscription_id"], "代扣协议")
            if item["status"] != "active":
                raise BankError("该代扣协议已取消，无需重复操作。")
            title = "确认取消自动代扣"
            fields = [
                ["订阅服务", item["name"]],
                ["每月代扣", yuan(item["amount_minor"])],
                ["生效方式", "停止未来扣费，历史扣费不退款"],
            ]
        elif name == "simulate_expense":
            amount = minor(args["amount_yuan"])
            commitment = None
            if args.get("budget_commitment_id"):
                commitment = row_required(db, "commitments", args["budget_commitment_id"], "预算承诺")
                if commitment["status"] != "active" or commitment["account_id"] != args["account_id"]:
                    raise BankError("这笔预算承诺已不可用。")
                if commitment["remaining_minor"] < amount:
                    raise BankError("这笔消费超过对应预算的剩余额度。")
                # 预算内消费会同步消耗保护额度，因此仅校验真实余额和卡片。
                db.execute("SAVEPOINT budget_check")
                db.execute(
                    "UPDATE commitments SET remaining_minor=remaining_minor-? WHERE id=?", (amount, commitment["id"])
                )
                try:
                    account = self._check_debit(db, args["account_id"], amount)
                finally:
                    db.execute("ROLLBACK TO budget_check")
                    db.execute("RELEASE budget_check")
            else:
                account = self._check_debit(db, args["account_id"], amount)
            if args.get("plan_id"):
                plan = db.execute("SELECT * FROM plans WHERE id=?", (args["plan_id"],)).fetchone()
                if not plan or plan["current_version"] != args.get("plan_version"):
                    raise BankError("计划已经更新，请重新核对最新预演结果。")
            title = "确认模拟消费"
            fields = [
                ["商户", args["merchant"]],
                ["消费类别", args["category"]],
                ["付款账户", account["name"]],
                ["消费金额", yuan(amount)],
                ["预算归属", commitment["label"] if commitment else "预算外支出"],
            ]
            extra = {"amount_minor": amount, "plan_id": args.get("plan_id")}
        elif name == "reserve_funds":
            amount = minor(args["amount_yuan"])
            account = row_required(db, "accounts", args["account_id"], "预留账户")
            available = account["balance_minor"] - db.execute(
                "SELECT COALESCE(SUM(remaining_minor),0) FROM commitments WHERE account_id=? AND status='active'",
                (account["id"],),
            ).fetchone()[0]
            if available < amount:
                raise BankError(f"可自由安排资金只有 {yuan(max(0, available))}，不足以新增这项预留。")
            title = "确认资金预留"
            fields = [
                ["用途", args["label"]],
                ["类型", {"rent": "房租", "living": "生活费", "subscription": "订阅", "other": "其他"}[args["kind"]]],
                ["预留金额", yuan(amount)],
                ["使用期限", args["due_date"]],
            ]
            extra = {"amount_minor": amount, "plan_id": args.get("plan_id")}
        elif name == "create_trigger_rule":
            reserve = int(Decimal(args["reserve_yuan"]) * 100)
            row_required(db, "accounts", args["account_id"], "工资到账账户")
            title = "确认条件触发提醒"
            fields = [
                ["条件", "检测到工资实际到账"],
                ["到账后仍需保留", yuan(reserve)],
                ["频率", "仅下一次" if args["cadence"] == "once" else "每月"],
                ["动作", "重新计算并提醒，绝不自动转账"],
            ]
            extra = {"amount_minor": reserve, "plan_id": args.get("plan_id")}
        elif name == "save_to_goal":
            target = minor(args["total_yuan"])
            completed = db.execute(
                "SELECT COALESCE(SUM(amount_minor),0) FROM plan_executions WHERE plan_id=? AND status='executed'",
                (args["plan_id"],),
            ).fetchone()[0]
            pending_amount = target - completed
            if pending_amount <= 0:
                raise BankError(
                    f"这个目标已经完成 {yuan(completed)}，新目标为 {yuan(target)}，无需追加转存。"
                )
            installment = minor(args["installment_yuan"]) if args.get("installment_yuan") else pending_amount
            pending_amount = min(pending_amount, installment)
            account = self._check_debit(db, args["account_id"], pending_amount)
            title = "确认储蓄目标差额"
            fields = [
                ["已完成", yuan(completed)],
                ["新累计目标", yuan(target)],
                ["本次待追加", yuan(pending_amount)],
                ["付款账户", account["name"]],
                ["收款账户", "储蓄账户"],
            ]
            extra = {"amount_minor": pending_amount, "plan_id": args["plan_id"], "goal_minor": target}
        elif name == "activate_plan":
            plan = db.execute("SELECT * FROM plans WHERE id=?", (args["plan_id"],)).fetchone()
            version = db.execute(
                "SELECT * FROM plan_versions WHERE plan_id=? AND version=?",
                (args["plan_id"], args["plan_version"]),
            ).fetchone()
            if not plan or not version or plan["current_version"] != args["plan_version"]:
                raise BankError("计划已经更新，请重新核对最新版本。")
            result = json.loads(version["result_json"])
            variant = next((item for item in result["variants"] if item["key"] == args["variant_key"]), None)
            if not variant:
                raise BankError("这个计划方案不存在。")
            if not variant["result"]["feasible"]:
                raise BankError("这个方案当前存在资金缺口，不能采用。请先选择可行方案或调整假设。")
            living_existing = db.execute(
                "SELECT COALESCE(SUM(remaining_minor),0) FROM commitments WHERE account_id='daily' AND kind='living' AND status='active'"
            ).fetchone()[0]
            incremental = max(0, result["living_reserve_minor"] - living_existing)
            if result["assumptions"].get("rent") and not db.execute(
                "SELECT 1 FROM commitments WHERE account_id='daily' AND kind='rent' AND status='active'"
            ).fetchone():
                incremental += result["assumptions"]["rent"]["amount_minor"]
            existing_subscriptions = {
                row[0]
                for row in db.execute(
                    "SELECT label FROM commitments WHERE account_id='daily' AND kind='subscription' AND status='active'"
                )
            }
            incremental += sum(
                item["amount_minor"]
                for item in result["assumptions"].get("subscriptions", [])
                if item["name"] not in existing_subscriptions
            )
            free = db.execute("SELECT balance_minor FROM accounts WHERE id='daily'").fetchone()[0] - db.execute(
                "SELECT COALESCE(SUM(remaining_minor),0) FROM commitments WHERE account_id='daily' AND status='active'"
            ).fetchone()[0]
            if incremental > free:
                raise BankError(f"账务在预演后发生变化，当前只能新增预留 {yuan(max(0, free))}，请重新预演。")
            title = "确认采用资金计划"
            fields = [
                ["计划", plan["title"]],
                ["方案", variant["label"]],
                ["执行日期", variant["action_date"]],
                ["计划金额", yuan(result["amount_minor"])],
                ["生活费预留", yuan(result["living_reserve_minor"])],
            ]
            extra = {"amount_minor": result["living_reserve_minor"], "plan_id": plan["id"]}
        elif name == "adjust_commitment":
            commitment = row_required(db, "commitments", args["commitment_id"], "资金承诺")
            if commitment["status"] != "active":
                raise BankError("这项资金承诺已经结束。")
            remaining = int(Decimal(args["remaining_yuan"]) * 100)
            if remaining >= commitment["remaining_minor"]:
                raise BankError("调整后的承诺必须小于当前剩余额度；增加额度请新建资金预留。")
            title = "确认调整资金承诺"
            fields = [
                ["用途", commitment["label"]],
                ["原剩余额度", yuan(commitment["remaining_minor"])],
                ["调整后额度", yuan(remaining)],
                ["释放资金", yuan(commitment["remaining_minor"] - remaining)],
            ]
            extra = {"amount_minor": commitment["remaining_minor"] - remaining}
        else:
            raise BankError("不允许执行该工具。")
        return {
            "title": title,
            "fields": [{"label": label, "value": value} for label, value in fields],
            "explanation": extra.pop("explanation", self._generic_explanation(args)),
            **extra,
        }

    @staticmethod
    def _purchase_quote(product, amount):
        units = (Decimal(amount) / 100 / Decimal(product["nav"])).quantize(
            Decimal("0.0001"), rounding=ROUND_DOWN
        )
        return units, int((units * Decimal(product["nav"]) * 100).to_integral_value(rounding=ROUND_UP))

    @staticmethod
    def _redemption_quote(product, units):
        return int((units * Decimal(product["nav"]) * 100).to_integral_value(rounding=ROUND_DOWN))

    def execute(self, db, name, arguments, action_id):
        args = validate(name, arguments)
        if not TOOLS[name][1]:
            raise BankError("确认入口只允许变更工具。")
        summary = self._check_action(db, name, args)
        receipt = {
            "action_id": action_id,
            "tool": name,
            "status": "executed",
            "executed_at": now(),
            **summary,
        }
        if name == "transfer":
            amount = minor(args["amount_yuan"])
            target = self._recipients(db, args["recipient"])[0]
            category = "内部转账" if target["kind"] == "own_account" else "转账"
            db.execute(
                "UPDATE accounts SET balance_minor=balance_minor-? WHERE id=?", (amount, args["account_id"])
            )
            self._ledger(
                db,
                action_id + ":out",
                args["account_id"],
                target["name"],
                "out",
                amount,
                category,
                args["note"],
            )
            if target["kind"] == "own_account":
                db.execute(
                    "UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?", (amount, target["id"])
                )
                self._ledger(
                    db, action_id + ":in", target["id"], "本人账户转入", "in", amount, category, args["note"]
                )
                if target["id"] == "savings":
                    plan_id = args.get("plan_id") or uid("plan_")
                    created = now()
                    plan = db.execute("SELECT current_version,goal_minor FROM plans WHERE id=?", (plan_id,)).fetchone()
                    if plan:
                        version = plan["current_version"] + 1
                        goal = max(plan["goal_minor"] or 0, amount + db.execute(
                            "SELECT COALESCE(SUM(amount_minor),0) FROM plan_executions WHERE plan_id=? AND status='executed'",
                            (plan_id,),
                        ).fetchone()[0])
                        db.execute(
                            "UPDATE plans SET goal_minor=?,current_version=?,status='active',updated_at=? WHERE id=?",
                            (goal, version, created, plan_id),
                        )
                    else:
                        version, goal = 1, amount
                        conversation = db.execute(
                            "SELECT t.conversation_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?",
                            (action_id,),
                        ).fetchone()
                        db.execute(
                            "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (plan_id, conversation[0] if conversation else self.database.conversation_id(db), "savings", "储蓄转存目标", "active", version, goal, None, created, created),
                        )
                    db.execute(
                        "INSERT INTO plan_versions VALUES (?,?,?,?,?)",
                        (plan_id, version, dumps({"total_minor": goal}), dumps({"target_minor": goal}), created),
                    )
                    db.execute(
                        "INSERT INTO plan_executions VALUES (?,?,?,?,?,?,?,?)",
                        (uid("exec_"), plan_id, "savings_transfer", action_id, amount, "executed", dumps(receipt), created),
                    )
                    receipt["plan"] = {"plan_id": plan_id, "goal_minor": goal, "completed_minor": goal}
        elif name == "save_risk_profile":
            db.execute("INSERT OR REPLACE INTO risk_profile VALUES (1,?,?)", (dumps(args), risk_level(args)))
        elif name in ("purchase_product", "redeem_product"):
            product = row_required(db, "products", args["product_id"], "理财产品")
            previous = db.execute(
                "SELECT units FROM holdings WHERE product_id=?", (product["id"],)
            ).fetchone()
            previous_units = Decimal(previous[0]) if previous else Decimal(0)
            if name == "purchase_product":
                units, amount = self._purchase_quote(product, minor(args["amount_yuan"]))
                new_units, direction, delta = previous_units + units, "out", -amount
            else:
                units = Decimal(args["units"])
                amount = self._redemption_quote(product, units)
                new_units, direction, delta = previous_units - units, "in", amount
            db.execute(
                "INSERT OR REPLACE INTO holdings VALUES (?,?)",
                (product["id"], str(new_units.quantize(Decimal("0.0001")))),
            )
            db.execute(
                "UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?", (delta, args["account_id"])
            )
            self._ledger(
                db,
                action_id,
                args["account_id"],
                product["name"],
                direction,
                amount,
                "理财",
                summary["title"].replace("确认", ""),
            )
        elif name == "set_card_status":
            db.execute("UPDATE cards SET status=? WHERE id=?", (args["status"], args["card_id"]))
        elif name == "set_card_limit":
            db.execute(
                "UPDATE cards SET daily_limit_minor=? WHERE id=?",
                (minor(args["daily_limit_yuan"]), args["card_id"]),
            )
        elif name == "cancel_subscription":
            db.execute("UPDATE subscriptions SET status='cancelled' WHERE id=?", (args["subscription_id"],))
        elif name == "simulate_expense":
            amount = minor(args["amount_yuan"])
            db.execute("UPDATE accounts SET balance_minor=balance_minor-? WHERE id=?", (amount, args["account_id"]))
            self._ledger(db, action_id, args["account_id"], args["merchant"], "out", amount, args["category"], "模拟消费")
            if args.get("budget_commitment_id"):
                db.execute(
                    "UPDATE commitments SET remaining_minor=remaining_minor-?,status=CASE WHEN remaining_minor-?=0 THEN 'fulfilled' ELSE status END,updated_at=? WHERE id=?",
                    (amount, amount, now(), args["budget_commitment_id"]),
                )
            if args.get("plan_id"):
                db.execute(
                    "INSERT INTO plan_executions VALUES (?,?,?,?,?,?,?,?)",
                    (uid("exec_"), args["plan_id"], "purchase", action_id, amount, "executed", dumps(receipt), now()),
                )
        elif name == "reserve_funds":
            amount = minor(args["amount_yuan"])
            created = now()
            conversation = db.execute(
                "SELECT t.conversation_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?",
                (action_id,),
            ).fetchone()
            db.execute(
                "INSERT INTO commitments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid("commit_"), args.get("plan_id") or "standalone", conversation[0] if conversation else self.database.conversation_id(db), args["account_id"], args["kind"], args["label"], amount, amount, args["due_date"], "active", created, created),
            )
        elif name == "create_trigger_rule":
            created = now()
            conversation = db.execute(
                "SELECT t.conversation_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?",
                (action_id,),
            ).fetchone()
            db.execute(
                "INSERT INTO trigger_rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid("rule_"), args.get("plan_id"), conversation[0] if conversation else self.database.conversation_id(db), args["account_id"], args["source_key"], args["expected_day"], int(Decimal(args["reserve_yuan"]) * 100), args["cadence"], "active", None, created, created),
            )
        elif name == "save_to_goal":
            target = minor(args["total_yuan"])
            completed = db.execute(
                "SELECT COALESCE(SUM(amount_minor),0) FROM plan_executions WHERE plan_id=? AND status='executed'",
                (args["plan_id"],),
            ).fetchone()[0]
            amount = target - completed
            if args.get("installment_yuan"):
                amount = min(amount, minor(args["installment_yuan"]))
            db.execute("UPDATE accounts SET balance_minor=balance_minor-? WHERE id=?", (amount, args["account_id"]))
            db.execute("UPDATE accounts SET balance_minor=balance_minor+? WHERE id='savings'", (amount,))
            self._ledger(db, action_id + ":out", args["account_id"], "储蓄账户", "out", amount, "内部转账", "储蓄目标")
            self._ledger(db, action_id + ":in", "savings", "本人账户转入", "in", amount, "内部转账", "储蓄目标")
            conversation = db.execute(
                "SELECT t.conversation_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?",
                (action_id,),
            ).fetchone()
            created = now()
            plan = db.execute("SELECT 1 FROM plans WHERE id=?", (args["plan_id"],)).fetchone()
            if not plan:
                db.execute(
                    "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (args["plan_id"], conversation[0] if conversation else self.database.conversation_id(db), "savings", "累计储蓄目标", "active", 1, target, None, created, created),
                )
                db.execute(
                    "INSERT INTO plan_versions VALUES (?,?,?,?,?)",
                    (args["plan_id"], 1, dumps({"total_minor": target}), dumps({"target_minor": target}), created),
                )
            else:
                current_version = db.execute(
                    "SELECT current_version FROM plans WHERE id=?", (args["plan_id"],)
                ).fetchone()[0]
                db.execute(
                    "UPDATE plans SET goal_minor=?,status='active',current_version=?,updated_at=? WHERE id=?",
                    (target, current_version + 1, created, args["plan_id"]),
                )
                db.execute(
                    "INSERT INTO plan_versions VALUES (?,?,?,?,?)",
                    (
                        args["plan_id"],
                        current_version + 1,
                        dumps({"total_minor": target}),
                        dumps({"target_minor": target, "completed_before_minor": completed}),
                        created,
                    ),
                )
            db.execute(
                "INSERT INTO plan_executions VALUES (?,?,?,?,?,?,?,?)",
                (uid("exec_"), args["plan_id"], "savings_transfer", action_id, amount, "executed", dumps(receipt), created),
            )
        elif name == "activate_plan":
            plan = db.execute("SELECT * FROM plans WHERE id=?", (args["plan_id"],)).fetchone()
            version = db.execute(
                "SELECT * FROM plan_versions WHERE plan_id=? AND version=?",
                (args["plan_id"], args["plan_version"]),
            ).fetchone()
            result = json.loads(version["result_json"])
            variant = next(item for item in result["variants"] if item["key"] == args["variant_key"])
            created = now()
            db.execute("UPDATE plans SET status='active',updated_at=? WHERE id=?", (created, plan["id"]))
            living_existing = db.execute(
                "SELECT COALESCE(SUM(remaining_minor),0) FROM commitments WHERE account_id='daily' AND kind='living' AND status='active'"
            ).fetchone()[0]
            living_increment = max(0, result["living_reserve_minor"] - living_existing)
            if living_increment and not db.execute(
                "SELECT 1 FROM commitments WHERE plan_id=? AND kind='living' AND status='active'", (plan["id"],)
            ).fetchone():
                db.execute(
                    "INSERT INTO commitments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid("commit_"), plan["id"], plan["conversation_id"], "daily", "living", "生活费预留", living_increment, living_increment, result["deadline"], "active", created, created),
                )
            rent = result["assumptions"].get("rent")
            if rent and not db.execute(
                "SELECT 1 FROM commitments WHERE account_id='daily' AND kind='rent' AND status='active'"
            ).fetchone():
                as_of = datetime.fromisoformat(result["assumptions"]["as_of"]).date()
                rent_due = as_of.replace(day=min(rent["day"], 28))
                if rent_due < as_of:
                    rent_due = (rent_due.replace(day=28) + timedelta(days=4)).replace(day=min(rent["day"], 28))
                db.execute(
                    "INSERT INTO commitments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid("commit_"), plan["id"], plan["conversation_id"], "daily", "rent", "预计房租", rent["amount_minor"], rent["amount_minor"], rent_due.isoformat(), "active", created, created),
                )
            for subscription in result["assumptions"].get("subscriptions", []):
                if db.execute(
                    "SELECT 1 FROM commitments WHERE account_id='daily' AND kind='subscription' AND label=? AND status='active'",
                    (subscription["name"],),
                ).fetchone():
                    continue
                db.execute(
                    "INSERT INTO commitments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (uid("commit_"), plan["id"], plan["conversation_id"], "daily", "subscription", subscription["name"], subscription["amount_minor"], subscription["amount_minor"], subscription["next_charge_date"], "active", created, created),
                )
            receipt["plan"] = {"plan_id": plan["id"], "version": args["plan_version"], "variant_key": args["variant_key"], "action_date": variant["action_date"]}
        elif name == "adjust_commitment":
            remaining = int(Decimal(args["remaining_yuan"]) * 100)
            db.execute(
                "UPDATE commitments SET remaining_minor=?,status=?,updated_at=? WHERE id=?",
                (remaining, "released" if remaining == 0 else "active", now(), args["commitment_id"]),
            )
        receipt["title"] = summary["title"].replace("确认", "") + "已完成"
        return receipt

    @staticmethod
    def _ledger(db, action_id, account_id, counterparty, direction, amount, category, description):
        db.execute(
            "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                uid("tx_"),
                action_id,
                account_id,
                counterparty,
                direction,
                amount,
                category,
                now(),
                None,
                description,
            ),
        )
