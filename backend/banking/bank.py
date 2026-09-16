import json
import re
from collections import defaultdict
from datetime import datetime
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

    def query(self, name, arguments):
        args = validate(name, arguments)
        if TOOLS[name][1]:
            raise BankError("变更工具必须经过操作确认。")
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
                result.append(
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "kind": "contact",
                        "phone": item["phone"],
                        "detail": f"{item['bank']} · 尾号 {item['last4']} · 手机 {item['phone']}",
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

    def prepare(self, name, arguments):
        args = validate(name, arguments)
        if not TOOLS[name][1]:
            raise BankError("该操作不需要确认。")
        with self.database.connection() as db:
            summary = self._check_action(db, name, args)
        return args, summary

    def _check_debit(self, db, account_id, amount):
        account = row_required(db, "accounts", account_id, "付款账户")
        if account["balance_minor"] < amount:
            raise BankError(f"{account['name']}余额不足，当前可用 {yuan(account['balance_minor'])}。")
        card = db.execute("SELECT * FROM cards WHERE account_id=?", (account_id,)).fetchone()
        if card:
            if card["status"] != "active":
                raise BankError("付款账户关联银行卡已冻结，请先解冻。")
            today = datetime.now(TZ).date().isoformat()
            # 种子历史账单不占用本次演示的支付限额；只统计本次操作产生的出账。
            spent = db.execute(
                "SELECT COALESCE(SUM(amount_minor),0) FROM transactions WHERE account_id=? AND direction='out' AND action_id IS NOT NULL AND substr(occurred_at,1,10)=?",
                (account_id, today),
            ).fetchone()[0]
            if amount + spent > card["daily_limit_minor"]:
                raise BankError(
                    f"超过银行卡当日可支付额度，目前剩余 {yuan(max(0, card['daily_limit_minor'] - spent))}。"
                )
        return account

    def _check_action(self, db, name, args):
        fields = []
        extra = {}
        if name == "transfer":
            amount = minor(args["amount_yuan"])
            matches = self._recipients(db, args["recipient"])
            if not matches:
                raise BankError("没有找到收款人，请提供已有联系人的姓名、手机号或本人账户名称。")
            if len(matches) != 1:
                raise BankError("存在多个同名收款人，请让用户指定手机号，不得自行选择。")
            target = matches[0]
            if target["kind"] == "own_account" and target["id"] == args["account_id"]:
                raise BankError("付款与收款账户不能相同。")
            account = self._check_debit(db, args["account_id"], amount)
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
            extra = {"amount_minor": amount}
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
            product = row_required(db, "products", args["product_id"], "理财产品")
            risk = db.execute("SELECT risk_level FROM risk_profile WHERE id=1").fetchone()
            if not risk:
                raise BankError("请先完成并确认风险问卷，再发起申购。")
            if risk[0] < product["risk_level"]:
                raise BankError(f"产品风险 R{product['risk_level']} 高于你的测评等级 R{risk[0]}，不能申购。")
            amount = minor(args["amount_yuan"])
            if amount < product["min_purchase_minor"]:
                raise BankError(f"该产品起购金额为 {yuan(product['min_purchase_minor'])}。")
            account = self._check_debit(db, args["account_id"], amount)
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
            extra = {"amount_minor": cost}
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
        else:
            raise BankError("不允许执行该工具。")
        return {
            "title": title,
            "fields": [{"label": label, "value": value} for label, value in fields],
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
