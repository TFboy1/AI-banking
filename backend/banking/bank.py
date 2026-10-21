import json
import re
from collections import defaultdict
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .analysis_models import AnalysisData, AnalysisCode
from .birthday_models import BirthdayContext, BirthdayPreview, BirthdayCreate, BirthdayList, BirthdayManage
from .account_transfers import OwnTransfer
from .scheduled_models import ScheduleTransfer,CancelSchedule
from .capabilities import Capability, CapabilityRegistry
from .card_models import AddCard
from .db import TZ, Database, dumps, now, uid
from .travel_common import Empty as TravelEmpty, Trip as TravelTrip, Quote as TravelQuote, Purchase as TravelPurchase
from .travel_common import ChangeQuote as TravelChangeQuote, Cancel as TravelCancel
from .food_common import Empty as FoodEmpty
from .food_common import Location as FoodLocation
from .food_common import Purchase as FoodPurchase
from .food_common import Quote as FoodQuote
from .food_common import Search as FoodSearch
from .food_common import Shop as FoodShop
from .securities_common import (
    PurchaseStocks,
    QuoteStockPurchase,
    QuoteStocks,
    SecuritiesEmpty,
    SellStocks,
    StockSearch,
    StockSymbol,
)
from .subscriptions import subscription_record

DEBIT_TOOLS = {"schedule_transfer","change_travel_order","place_travel_order", "place_food_order","transfer_own_funds", "purchase_stock_basket", "transfer", "purchase_product", "simulate_expense", "save_to_goal"}


class BankError(ValueError):
    pass


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


Money = Annotated[str, Field(pattern=r"^(0|[1-9]\d{0,8})(\.\d{1,2})?$")]
Identifier = Annotated[str, Field(min_length=1, max_length=100)]


class Empty(Input):
    pass


class CreateAccountCategory(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=24)


class ContactProfile(Input):
    note: str | None = Field(default=None, max_length=120)
    relationship: Literal["未分类", "家人", "朋友", "爱人", "同事", "熟人", "其他"] | None = None
    birthday: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    gender: Literal["未设置", "男", "女", "其他"] | None = None
    special_attention: bool | None = None

    @field_validator("birthday")
    @classmethod
    def valid_birthday(cls, value):
        if value is not None:
            parsed = date.fromisoformat(value)
            if parsed > date.today():
                raise ValueError("生日不能是未来日期。")
        return value


class CreateContact(ContactProfile):
    name: str = Field(min_length=1, max_length=60)
    phone: str | None = Field(default=None, pattern=r"^1[3-9]\d{9}$")
    bank: str | None = Field(default=None, min_length=1, max_length=60)
    card_number: str | None = Field(default=None, pattern=r"^\d{12,19}$")
    same_person_id: str | None = Field(default=None, max_length=100)
    new_person: bool = False


class ContactQuery(Input):
    keyword: str = Field(default="", max_length=120, description="姓名、备注、手机号或关系分类；空字符串列出全部联系人")


class RecipientQuery(Input):
    query: str = Field(min_length=1, max_length=100, description="姓名、姓氏、联系人编号、银行卡尾号或手机号尾号；例如张、0826、手机尾号1111")


class BillsQuery(Input):
    month: str | None = Field(default=None, description="YYYY-MM，省略且未提供year或days时为本月")
    year: int | None = Field(default=None, ge=1900, le=2100, description="年度报告年份，例如2026；今年/去年按系统北京时间解析。与month、days互斥")
    days: int | None = Field(default=None, ge=1, le=365, description="最近多少个自然日，包含今天；最近异常未指定时间用30，最近一周用7。与month、year互斥")


class Transfer(Input):
    recipient: Identifier = Field(description="姓名、手机号及尾号、银行卡尾号、联系人 ID 或本人目标账户 ID；多个匹配须澄清")
    amount_yuan: Money = Field(description="人民币元，必须是字符串，例如 200.00")
    account_id: Identifier = Field(default="daily", description="付款账户须由用户明确选择；日常账户用 daily，储蓄账户用 savings；未选择时省略此参数，用户会在支付密码弹窗选择账户，不能猜测默认值")
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


class SubscriptionQuote(Input):
    services: list[Identifier] = Field(default_factory=list, max_length=12, description="根据最新请求和同会话指代选择服务名称或编号；不明确时留空，不得猜测")
    months: int = Field(default=1, ge=1, le=1200, description="模型将用户期限换成整月：两年24、一年半18、两个季度6；未指定按1个月；多个待比较期限分别调用")
    inquire_packages: bool = Field(default=False, description="用户是否询问套餐、年付或折扣；价格须来自后台，不能编造")


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
    'get_bill_analysis_data': (AnalysisData, False, '自定义账单分析第一步：按明确日期、类别和趋势/分类/两期对比获取当前账号脱敏流水。仅历史账单，不含银行卡、姓名、密码或订阅协议；内部划转和投资本金排除。返回dataset_id及代码契约，下一步生成受限Python计算代码调用run_bill_analysis_code，默认图表，不必等待用户要求画图。'),
    'run_bill_analysis_code': (AnalysisCode, False, '自定义分析第二步：运行AI生成的受限Python代码，后台逐项核对分组金额，通过后自动展示折线/环形/柱状图。只能使用本对话dataset_id，不能操作资金，不支持任意Python模块。'),
    'get_birthday_context': (BirthdayContext, False, '查询生日联系人，按姓名、备注或关系去重；缺少生日或多人必须澄清。'),
    'preview_birthday_plan': (BirthdayPreview, False, '预览下一次生日、提醒时间、预算来源及缺口，不预留资金。日期限定需传occasion_month/event_date核对。'),
    'create_birthday_plan': (BirthdayCreate, True, '经确认保存生日计划并预留预算，不转账不扣款；仅特别关注联系人到期新建提醒会话并查询演示蛋糕菜单，不能擅自开启关注；选好后仍需确认及支付验证。'),
    'list_birthday_plans': (BirthdayList, False, '查询生日计划、版本、预算、提醒与已完成订单，办理暂停/继续/取消或生日点餐前先查。'),
    'manage_birthday_plan': (BirthdayManage, True, '按当前版本暂停、继续或取消生日计划。暂停保留预算，取消释放未用预算，不撤销已完成订单。'),
    'schedule_transfer':(ScheduleTransfer,True,'单次定时转账提前授权：先查询明确的收款人/收款卡、金额及用户指定日期时刻，执行时间用北京时间ISO。现在只生成授权确认卡，支付窗口选付款账户并验证密码/所需短信，不即时扣款。到点后台复核后自动执行，5分钟执行窗口，失败不重试；修改传replaces_id并重新授权。不能猜测时间、收款银行卡或默认付款账户。'),
    'list_scheduled_transfers':(Empty,False,'查询当前用户单次定时交易的授权、执行时间、状态、失败原因及回执；授权成功不等于已经成交。'),
    'cancel_scheduled_transfer':(CancelSchedule,True,'用户明确取消指定待执行/停止预约时生成取消确认，不撤销已成功转账。schedule_id必须来自查询，取消后不再执行。'),
    "get_travel_context": (TravelEmpty, False, "查询独立旅行演示站关联状态、支持城市和住宿区域。未关联提示在8004旅行站输入有数通用同步码。"),
    "search_travel_options": (TravelTrip, False, "查询往返机票、动车和目的地区域酒店。出发地、目的地、出行与返程日期、住宿区域须来自用户。区域无要求须明确说不限；缺项先问，不能猜。默认1人1间，结果需说明。返回search_id和所有候选编号；每程机票与动车是替代方案，不可同时购买。按用户预算、区域、时段推荐并等待用户选择。全部虚构。"),
    "quote_travel_order": (TravelQuote, False, "用户选择合适方案后，用同一搜索的search_id、outbound_id、inbound_id、hotel_id汇总往返交通及酒店总价，后端按人数、房间数和晚数计算，不扣款；不得自选用户未同意的方案。"),
    "place_travel_order": (TravelPurchase, True, "用当前会话quote_travel_order返回的quote_id生成旅行演示预订确认卡。用户确认、在支付窗口选择付款账户、验证支付密码（红色另需短信）后才原子下单扣虚拟余额。不是真实携程、不出票、不真实预订酒店。"),
    "get_travel_orders": (TravelEmpty, False, "查询当前有数账户实际成功的虚拟旅行预订与扣款。"),
    "quote_travel_change": (TravelChangeQuote, False, "用户提出改签或修改旅行订单时先get_travel_orders核对订单；多笔先请用户指定。search_travel_options查询新日期/班次/酒店，quote_travel_order报价后，传order_id和new_quote_id计算原订单改签差价。change_parts只填写用户明确要求调整的outbound去程、inbound返程、hotels酒店；其他部分保持原样，不能默认整单改变。免费改签，仅补退差价，未执行。"),
    "change_travel_order": (TravelPurchase, True, "使用quote_travel_change返回的quote_id生成免费改签确认卡；确认和支付验证后更新原订单，不另建重复预订。需补差价在支付窗口选择账户；便宜则按原支付记录退款；等价不扣款。不得用place_travel_order代替。"),
    "cancel_travel_order": (TravelCancel, True, "免费退订整个旅行演示订单。先get_travel_orders核对订单id和当前version；多单必须明确对象。确认及密码/所需短信验证后，剩余实付全额原路退款，手续费0；不能仅用文字声称已取消。部分退票暂不支持，需明确说明是整单退订。"),
    "get_food_context": (FoodEmpty, False, "通过独立外卖API查询当前关联外卖账号和演示位置选项；未关联提示去应用设置生成外卖同步码。不能编造真实定位。"),
    "set_food_location": (FoodLocation, False, "用户明确选择演示位置后保存到关联外卖账号，place_id来自get_food_context，非资金操作。不能替用户默认位置。"),
    "search_food_shops": (FoodSearch, False, "按用户想吃的食物query查询当前演示位置附近可配送店铺及距离、配送费；数据来自独立外卖API，不能编造店铺。"),
    "get_food_menu": (FoodShop, False, "查询指定店铺菜单、价格、菜品编号、起送和配送费。shop_id须来自附近店铺结果。"),
    "quote_food_order": (FoodQuote, False, "用户选定店铺、菜品和数量后，后台汇总餐费、打包费、配送费；items填写真实菜单item_id与quantity。缺少店铺或数量时先澄清，不得任意点菜，未扣款。"),
    "place_food_order": (FoodPurchase, True, "用当前会话quote_food_order返回的quote_id生成外卖模拟订单确认卡；支付窗口选择付款账户并验证密码。后台原子创建虚拟订单与扣款；没有真实美团订单，不能声称真实送餐。"),
    "get_food_orders": (FoodEmpty, False, "查询当前有数账号的外卖演示订单与实际虚拟扣款记录。"),
    "search_securities_stocks": (StockSearch, False, "通过独立证券API按名称或代码搜索股票，包括非自选。查询不需要购买数量。名称模糊时展示候选，不猜股票代码。"),
    "get_stock_price": (StockSymbol, False, "通过独立证券API查询单只股票最新可用行情，返回价格、行情类型、日期、来源；不需要数量或预算，不生成订单和扣款。"),
    "add_securities_watchlist": (StockSymbol, False, "用户明确要求添加自选时，通过独立证券API添加到当前用户关联的证券自选，自动执行，不涉及资金。先搜索核对股票。不能仅因买入就自动加入自选，不能用文字假装已加入。"),
    "create_account_category": (CreateAccountCategory, True, "创建用户明确命名的模拟资金分类账户，初始余额为0。先查get_accounts核对同名，用户确认后才写入并返回账户编号；不能声称未执行的创建成功，不能虚构初始余额；投资账户为系统固定汇总不可另建。"),
    "transfer_own_funds": (OwnTransfer, True, "本人日常、储蓄、分类账户之间转账，也支持与已绑定银行卡双向划转。先get_accounts核对账户编号；target_account_id为明确的到账账户，account_id为付款账户（支付窗口可选择）。投资账户不参与，不允许同账户转账，需确认和交易验证。"),
    "quote_stock_sale": (QuoteStocks, False, "先查询实际证券持仓，再按明确股数生成卖出报价；不受自选或当前风险等级限制，不执行资金变更。"),
    "sell_stock_basket": (SellStocks, True, "凭当前会话quote_stock_sale的报价发起模拟卖出确认，必须指定到账account_id，输入密码和短信后执行。"),
    "get_securities_watchlist": (SecuritiesEmpty, False, "通过独立有数证券API读取当前用户自选及连接状态。证券、股票、自选需求使用此工具，不能用本行理财替代。支持按股数或预算金额买入，缺少两者才追问。"),
    "quote_stock_purchase": (QuoteStockPurchase, False, "按用户明确给出的每只股数shares或预算金额amount_yuan（元的字符串）报价，两者只能填一个。金额由后台按行情向下换算100股整数手，返回实际金额与余款；不足一手返回最低所需金额。不执行扣款，不猜数量或默认全选。"),
    "get_securities_positions": (SecuritiesEmpty, False, "查询当前用户证券模拟持仓和订单回执，不是本行理财持仓。"),
    "purchase_stock_basket": (PurchaseStocks, True, "用当前会话quote_stock_purchase返回的报价编号生成股票模拟买入确认，必须R4及以上、支付密码和短信验证。不得自行编造价格或金额。"),
    "list_contacts": (ContactQuery, False, "查询联系人及关系资料，按姓名检查同名候选；联系人编号用于明确选择同一个人。"),
    "create_contact": (CreateContact, True, "添加联系人，需要确认；姓名必填，电话、银行卡、关系、生日、性别、关注、备注可选。先查同名；同名须用户明确选择合并 same_person_id 或另建 new_person=true。不得编造缺失字段。"),
    "get_accounts": (Empty, False, "查询本人账户、可用余额和当前风险等级。"),
    "find_recipients": (
        RecipientQuery,
        False,
        "按姓名或手机号查找联系人及本人账户，多个结果时先请用户选择。",
    ),
    "analyze_bills": (BillsQuery, False, "查询月度/年度/最近天数的收支、分类、主要商户、年度月度趋势与异常流水；month/year/days三选一，金额和异常依据均由后台计算。"),
    "get_risk_questionnaire": (Empty, False, "获取三项风险问题、选项和当前回答；不得替用户编造回答。"),
    "list_products": (Empty, False, "查询模拟产品、风险匹配和当前持仓。所有净值均为演示数据。"),
    "add_bank_card": (AddCard, True, "添加用户本人银行卡，须明确银行名称和完整16至19位卡号；缺少信息先追问，禁止编造。确认后新卡默认模拟余额5000元，重绑恢复原余额，不会重复增加余额；确认卡仅显示尾号。"),
    "list_cards": (Empty, False, "查询银行卡状态和日支付限额。"),
    "list_subscriptions": (Empty, False, "查看周期扣费识别依据、代扣协议状态和预计下次扣费。"),
    "quote_subscriptions": (SubscriptionQuote, False, "查询指定订阅月费，按整月期限计算费用及套餐资料完整性；先用 list_subscriptions 查名称/编号，结合当前会话解析指代。只能报价，不能重新开通。"),
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


# 现有业务也注册到统一权限/参数表。新增业务只需注册输入模型和实现同名后台处理器。
from .scheduled_actions import TOOLS as SCHEDULABLE_TOOLS
from pydantic import create_model
for _scheduled_name in SCHEDULABLE_TOOLS:
    _model,_write,_description=TOOLS[_scheduled_name]
    TOOLS[_scheduled_name]=(create_model(_model.__name__+'Timed',__base__=_model,
        execute_at=(str | None,Field(default=None,min_length=16,max_length=64,description='可选单次定时：用户明确指定的北京时间ISO日期和时刻；省略为立即交易，不得猜测时间'))),
        _write,_description+' 可选execute_at提前验证并授权，到期按原价格和内容复核执行；变化则停止。')
CAPABILITIES = CapabilityRegistry()
_DOMAINS = {
    "travel": {"get_travel_context","search_travel_options","quote_travel_order","place_travel_order","get_travel_orders","quote_travel_change","change_travel_order","cancel_travel_order"},
    "food": {"get_food_context", "set_food_location", "search_food_shops", "get_food_menu", "quote_food_order", "place_food_order", "get_food_orders"},
    "subscriptions": {"list_subscriptions", "quote_subscriptions", "cancel_subscription"},
    "contacts": {"find_recipients", "list_contacts", "create_contact"},
    "transfers": {"transfer", "diagnose_transfer", "transfer_own_funds",'schedule_transfer','list_scheduled_transfers','cancel_scheduled_transfer'},
    "investments": {"list_products", "get_risk_questionnaire", "save_risk_profile", "purchase_product", "redeem_product", "diagnose_purchase"},
    "cards": {"add_bank_card", "list_cards", "set_card_status", "set_card_limit"},
    "accounts": {"get_accounts", "analyze_bills", "create_account_category"},
}
for _name, (_input, _writes, _description) in TOOLS.items():
    _domain = "securities" if _name in {"search_securities_stocks", "get_stock_price", "add_securities_watchlist", "get_securities_watchlist", "quote_stock_purchase", "get_securities_positions", "purchase_stock_basket", "quote_stock_sale", "sell_stock_basket"} else next((domain for domain, names in _DOMAINS.items() if _name in names), "planning")
    _effect = "auto" if _name in {"add_securities_watchlist", "set_food_location"} else "confirm" if _writes else "preview" if _name == "preview_cashflow" else "read"
    CAPABILITIES.register(Capability(_name, _input, _description, _domain, _effect))


def tool_schemas():
    return CAPABILITIES.schemas()


def validate(name, arguments):
    if name not in CAPABILITIES:
        raise BankError("该工具不在允许使用的银行工具列表中。")
    try:
        args = CAPABILITIES[name].input_model.model_validate(arguments).model_dump(mode='json' if name == 'search_travel_options' else 'python')
        if args.get('execute_at') is None and name in SCHEDULABLE_TOOLS:
            args.pop('execute_at',None)
        if "account_id" in args:
            args["account_id"] = {"日常账户": "daily", "储蓄账户": "savings"}.get(args["account_id"], args["account_id"])
        return args
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
    if not result or (table_name == 'accounts' and result['kind'] == 'bank_card_unlinked'):
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
        from .market import sync_prices

        with self.database.connection() as db:
            sync_prices(db)
            accounts = [dict(row) for row in db.execute("SELECT * FROM accounts WHERE kind<>'bank_card_unlinked'")]
            cards = [dict(row) for row in db.execute("SELECT c.*,a.balance_minor FROM cards c JOIN accounts a ON a.id=c.account_id")]
            holdings = self._holdings(db)
            securities_positions = [dict(r) for r in db.execute("SELECT * FROM securities_positions")]
            securities_cost = sum(p["cost_minor"] for p in securities_positions)
            risk = db.execute("SELECT risk_level FROM risk_profile WHERE id=1").fetchone()
            reserved = {r['account_id']: r['amount'] for r in db.execute("SELECT c.account_id,SUM(c.remaining_minor) AS amount FROM commitments c JOIN accounts a ON a.id=c.account_id WHERE c.status='active' AND c.remaining_minor>0 AND a.kind<>'bank_card_unlinked' GROUP BY c.account_id")}
            protected = sum(reserved.values())
            cash = sum(a['balance_minor'] for a in accounts)
            category_cash = sum(a['balance_minor'] for a in accounts if a['kind'] != 'bank_card')
            card_cash = cash-category_cash
            investments = sum(h['value_minor'] for h in holdings)+securities_cost
            business_row = db.execute("SELECT value FROM meta WHERE key='business_date'").fetchone()
            business = datetime.fromisoformat(business_row[0]).date() if business_row else datetime.now(TZ).date()
            next_salary = (business.replace(day=28) + timedelta(days=4)).replace(day=1)
            return {
                "accounts": accounts,
                "cards": cards,
                "holdings": holdings,
                "securities_positions": securities_positions,
                "securities_cost_minor": securities_cost,
                "risk_level": risk[0] if risk else None,
                "account_balance_minor": cash,
                "category_balance_minor": category_cash,
                "card_balance_minor": card_cash,
                "investment_assets_minor": investments,
                "total_assets_minor": cash+investments,
                "transactions": [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM transactions ORDER BY occurred_at DESC,rowid DESC LIMIT 12"
                    )
                ],
                "subscriptions": [subscription_record(row) for row in db.execute("SELECT * FROM subscriptions")],
                "current_month": datetime.now(TZ).strftime("%Y-%m"),
                "protected_minor": protected,
                "free_minor": sum(max(0,a["balance_minor"]-reserved.get(a["id"],0)) for a in accounts),
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
        from .market import sync_prices

        with self.database.connection() as market_db:
            sync_prices(market_db)
        args = validate(name, arguments)
        if CAPABILITIES[name].requires_confirmation:
            raise BankError("变更工具必须经过操作确认。")
        if name == "preview_cashflow":
            args["_conversation_id"] = conversation_id
            with self.database.transaction() as db:
                return getattr(self, CAPABILITIES[name].handler)(db, args)
        if name == "quote_subscriptions":
            args["_conversation_id"] = conversation_id
        with self.database.connection() as db:
            return getattr(self, CAPABILITIES[name].handler)(db, args)

    def _get_birthday_context(self, db, args):
        from .birthdays import query
        return query(self,db,'get_birthday_context',args)

    def _preview_birthday_plan(self, db, args):
        from .birthdays import query
        return query(self,db,'preview_birthday_plan',args)

    def _list_birthday_plans(self, db, args):
        from .birthdays import query
        return query(self,db,'list_birthday_plans',args)

    def _list_contacts(self, db, args):
        from .contacts import contact

        query = args["keyword"].strip().lower()
        items = []
        for row in db.execute("SELECT c.id,c.name FROM contacts c JOIN contact_links l ON l.contact_id=c.id WHERE c.id=l.person_id"):
            item = contact(db, row["id"])
            item["name"] = row["name"]
            phones = [value[0] for value in db.execute("SELECT phone FROM contact_phones WHERE person_id=?", (item["id"],))]
            if not query or query in item["name"].lower() or query in item["note"].lower() or query == item["id"] or query == item["relationship"] or query in phones:
                items.append(item)
        return {"contacts": items, "display": table("联系人及关系", ["姓名", "关系", "手机", "特别关注"], [[p["name"], p["relationship"], p["phone"] or "未设置", "是" if p["special_attention"] else "否"] for p in items])}

    def _list_scheduled_transfers(self,db,args):
        from .scheduled_transfers import query
        return query(db)

    def _get_accounts(self, db, args):
        accounts = [dict(row) for row in db.execute("SELECT * FROM accounts WHERE kind<>'bank_card_unlinked'")]
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
        suffix = re.search(r"(?:尾号|后四位|后4位)\s*(\d{4})(?!\d)", query)
        phone_only = bool(re.search(r"手机|电话", query))
        card_only = bool(re.search(r"银行卡|卡号|账户尾号", query))
        if suffix:
            query = suffix[1]
        surname = re.fullmatch(r"姓(.{1,2}?)(?:的|的联系人|的有没有)?", query)
        if surname:
            query = surname[1]
        result = []
        for row in db.execute("SELECT * FROM accounts WHERE kind<>'bank_card_unlinked'"):
            item = dict(row)
            if query in item["name"].lower() or query == item["id"]:
                result.append(
                    {"id": item["id"], "name": item["name"], "kind": "own_account", "detail": "本人账户"}
                )
        for row in db.execute("SELECT c.*,COALESCE(d.note,'') AS note FROM contacts c LEFT JOIN contact_details d ON d.contact_id=c.id WHERE c.last4<>''"):
            item = dict(row)
            phone_matches = db.execute("SELECT 1 FROM contact_phones p JOIN contact_links l ON l.person_id=p.person_id WHERE l.contact_id=? AND (p.phone=? OR (length(?)=4 AND substr(p.phone,-4)=?))", (item["id"], query, query, query)).fetchone()
            if (
                query in item["name"].lower()
                or (item["note"] and query in item["note"].lower())
                or query == item["id"]
                or (not card_only and phone_matches)
                or (not phone_only and query == item["last4"])
            ):
                masked_phone = item["phone"][:3] + "****" + item["phone"][-4:]
                result.append(
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "kind": "contact",
                        "note": item["note"],
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
                note="有多个匹配结果，请选择收款人或指定尾号、联系人编号。" if len(items) > 1 else "",
            ),
        }

    def _analyze_bills(self, db, args):
        from .bill_reports import report
        return report(db, args)

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
                note="每日模拟波动净值，申赎即时结算，无收益承诺。份额保留 4 位；不足一分的申购款向上取整、赎回款向下取整。",
            ),
        }

    def _list_cards(self, db, args):
        cards = [dict(row) for row in db.execute("SELECT c.*,a.balance_minor FROM cards c JOIN accounts a ON a.id=c.account_id")]
        return {
            "cards": cards,
            "display": table(
                "我的银行卡",
                ["卡片", "尾号", "状态", "可用余额", "日支付限额"],
                [
                    [
                        c["name"],
                        c["last4"],
                        "正常" if c["status"] == "active" else "已冻结",
                        yuan(c["balance_minor"]),
                        yuan(c["daily_limit_minor"]),
                    ]
                    for c in cards
                ],
            ),
        }

    def _list_subscriptions(self, db, args):
        subscriptions = [subscription_record(row) for row in db.execute("SELECT * FROM subscriptions")]
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
                ["服务", "每月金额", "下次扣费", "状态", "当前服务到期（模拟推算）"],
                [
                    [
                        s["name"],
                        yuan(s["amount_minor"]),
                        s["next_charge_date"] or "—",
                        "代扣中" if s["status"] == "active" else "已取消",
                        s["expires_on"] or "未知",
                    ]
                    for s in subscriptions
                ],
                metrics=[{"label": "有效协议每月预计支出", "value": yuan(active_total)}],
                note="依据跨月扣费记录识别周期性，并与模拟代扣协议核对。取消仅阻止未来代扣，不会立即退款。",
            ),
        }

    def _quote_subscriptions(self, db, args):
        from .subscriptions import quote_subscriptions

        return quote_subscriptions(db, args)

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

    def prepare(self, name, arguments, connection=None):
        args = validate(name, arguments)
        if not CAPABILITIES[name].requires_confirmation:
            raise BankError("该操作不需要确认。")
        if name == "save_to_goal" and not args.get("plan_id"):
            args["plan_id"] = uid("plan_")
        with (nullcontext(connection) if connection is not None else self.database.connection()) as db:
            summary = self._check_action(db, name, args)
        return args, summary

    def payment_options(self, db, name, args):
        options = []
        for row in db.execute("SELECT * FROM accounts WHERE kind<>'bank_card_unlinked' ORDER BY rowid").fetchall():
            account = dict(row)
            try:
                self._check_action(db, name, {**args, "account_id": account["id"]})
                reason = ""
            except BankError as exc:
                reason = str(exc)
            options.append({"id": account["id"], "name": account["name"], "balance_minor": account["balance_minor"], "eligible": not reason, "reason": reason})
        return options

    def prepare_payment(self, name, arguments, connection=None):
        if name not in DEBIT_TOOLS:
            return self.prepare(name, arguments, connection=connection)
        if name=='change_travel_order':
            from .travel_changes import checked_change
            with (nullcontext(connection) if connection is not None else self.database.connection()) as db:
                change,_=checked_change(db,arguments['quote_id'])
            if change['delta_minor']<=0:
                args,preview=self.prepare(name,arguments,connection=connection)
                return args,{**preview,'payment_account_required':False}
        chosen = arguments.get("account_id")
        args = validate(name, {**arguments, "account_id": chosen or "daily"})
        chosen = args["account_id"] if chosen else None
        if name == "save_to_goal" and not args.get("plan_id"):
            args["plan_id"] = uid("plan_")
        with (nullcontext(connection) if connection is not None else self.database.connection()) as db:
            options = self.payment_options(db, name, args)
            usable = [option for option in options if option["eligible"]]
            if not usable:
                raise BankError("没有可用付款账户：" + (options[0]["reason"] if options else "请先添加银行账户。"))
            preview_id = chosen or usable[0]["id"]
            args["account_id"] = preview_id
            summary = self._check_action(db, name, args)
        if not chosen:
            args["account_id"] = ""
            for field in summary["fields"]:
                if field["label"] == "付款账户":
                    field["value"] = "支付时选择"
            summary.pop("explanation", None)
        summary["payment_account_required"] = True
        summary["payment_account_id"] = chosen or ""
        return args, summary

    def _check_debit(self, db, account_id, amount, commitment_id=None):
        account, checks = self._debit_assessment(db, account_id, amount, commitment_id)
        require_checks(checks)
        return account

    def _debit_assessment(self, db, account_id, amount, commitment_id=None):
        account_row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        if not account_row or account_row["kind"] == "bank_card_unlinked":
            return None, [check("account", "付款账户", "block", "找不到付款账户。", error="找不到付款账户，请先查询可用选项。")]
        account = dict(account_row)
        checks = [check("account", "付款账户", "pass", account["name"], [account_id])]
        commitments = [
            dict(row)
            for row in db.execute(
                """SELECT c.id,c.label,c.remaining_minor,COALESCE(v.title,'已删除会话') AS conversation_title
                FROM commitments c LEFT JOIN conversations v ON v.id=c.conversation_id
                WHERE c.account_id=? AND c.status='active' AND c.remaining_minor>0 AND (? IS NULL OR c.id<>?) ORDER BY c.due_date""",
                (account_id,commitment_id,commitment_id),
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
        if name in {'list_subscriptions','quote_subscriptions'}:
            return {'title':'确认查询订阅明细' if name=='list_subscriptions' else '确认查询订阅费用',
                'risk_level':'yellow','fields':[{'label':'权限级别','value':'黄色 · 需要明确确认'},
                    {'label':'查询范围','value':'当前账号的订阅、代扣协议及扣费依据' if name=='list_subscriptions' else '所选订阅的月费、期限及套餐资料'},
                    {'label':'操作说明','value':'确认后查询，不取消订阅，不扣款，不需要支付密码。'}]}

        if name in {'create_birthday_plan','manage_birthday_plan'}:
            from .birthdays import summary
            return summary(db,name,args)
        if name in {'simulate_expense','adjust_commitment'}:
            commitment=args.get('budget_commitment_id') or args.get('commitment_id')
            if commitment and db.execute('SELECT 1 FROM birthday_plans WHERE commitment_id=?',(commitment,)).fetchone():
                raise BankError('生日预算请通过生日计划管理或真实演示菜单报价下单，不能用模拟消费绕过。')
        if name in SCHEDULABLE_TOOLS and args.get('execute_at'):
            from .scheduled_actions import summary
            return summary(self,db,name,args)
        if name=='schedule_transfer':
            from .scheduled_transfers import summary
            return summary(self,db,args)
        if name=='cancel_scheduled_transfer':
            row=db.execute('SELECT * FROM scheduled_transfers WHERE id=?',(args['schedule_id'],)).fetchone()
            if not row or row['status'] not in {'scheduled','needs_reauthorization','failed'}:raise BankError('预约不存在或已结束，已成功交易不能撤销。')
            from .scheduled_transfers import public
            item=public(row)
            return {'title':'确认取消定时转账','fields':[{'label':'预约编号','value':row['id']},{'label':'收款人','value':item['recipient_name']},{'label':'原金额','value':yuan(item['amount_minor'])},{'label':'执行时间','value':row['execute_at']},{'label':'说明','value':'取消后不再执行，不会撤销已完成交易。'}]}
        if name == "add_bank_card":
            from .cards import card_summary
            return card_summary(db, args)
        if name in {'change_travel_order','cancel_travel_order'}:
            from .travel_changes import summary
            return summary(self,db,name,args)
        if name == "place_travel_order":
            from .travel_bridge import summary
            return summary(self, db, args)
        if name == "place_food_order":
            from .food_bridge import summary
            return summary(self, db, args)
        if name == 'create_account_category':
            from .account_categories import validate_category_name
            validate_category_name(db, args['name'])
            return {'title':'确认创建账户分类','fields':[{'label':'账户名称','value':args['name']},{'label':'初始余额','value':'¥0.00'},{'label':'说明','value':'创建资金分类，不增加资金；转入或分配需另行确认。'}]}
        if name == "transfer_own_funds":
            from .account_transfers import summary
            return summary(self, db, args)
        if name == "sell_stock_basket":
            from .securities_bridge import sale_summary
            return sale_summary(self, db, args)
        if name == "purchase_stock_basket":
            from .securities_bridge import purchase_summary

            return purchase_summary(self, db, args)
        from .market import sync_prices

        if name in {"purchase_product", "redeem_product"}:
            sync_prices(db)
        fields = []
        extra = {}
        if name == "create_contact":
            from .contacts import check_create

            check_create(db, args)
            title = "确认添加联系人" if not args.get("same_person_id") else "确认合并联系人信息"
            fields = [["姓名", args["name"].strip()], ["保存方式", "另建联系人" if not args.get("same_person_id") else "合并到 " + args["same_person_id"]]]
            for key, label in (("phone", "手机号"), ("bank", "银行"), ("card_number", "银行卡"), ("relationship", "关系"), ("birthday", "生日"), ("gender", "性别"), ("note", "备注")):
                if args.get(key) is not None:
                    fields.append([label, args[key]])
            if args.get("special_attention") is not None:
                fields.append(["特别关注", "是" if args["special_attention"] else "否"])
        elif name == "transfer":
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
                ["当日模拟净值", product["nav"]],
                ["获得份额", str(units)],
            ]
            extra = {"amount_minor": cost, "explanation": self._decision_explanation(checks), "valuation_nav": product["nav"], "valuation_date": db.execute("SELECT MAX(price_date) FROM product_nav_history WHERE product_id=?", (product["id"],)).fetchone()[0]}
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
            extra = {"amount_minor": amount, "valuation_nav": product["nav"], "valuation_date": db.execute("SELECT MAX(price_date) FROM product_nav_history WHERE product_id=?", (product["id"],)).fetchone()[0]}
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
                ["当前服务到期（按模拟续费日推算）", subscription_record(item)["expires_on"] or "未知"],
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
            if args["account_id"] == "savings":
                raise BankError("不能从储蓄账户转存到同一个储蓄账户，请选择其他付款账户。")
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
        from .safety import check_business
        from .security import MONEY_TOOLS
        if name in MONEY_TOOLS:
            check_business(db, name)
        args = validate(name, arguments)
        if not CAPABILITIES[name].requires_confirmation:
            raise BankError("确认入口只允许变更工具。")
        summary = self._check_action(db, name, args)
        receipt = {
            "action_id": action_id,
            "tool": name,
            "status": "executed",
            "executed_at": now(),
            **summary,
        }
        if name in {'list_subscriptions','quote_subscriptions'}:
            approval=db.execute('SELECT t.conversation_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?',(action_id,)).fetchone()
            if not approval:raise BankError('订阅查询必须经过有效确认。')
            args['_conversation_id']=approval[0]
            receipt['query_result']=getattr(self,'_'+name)(db,args)
            receipt['title']='订阅查询已完成'
            return receipt
        if name in SCHEDULABLE_TOOLS and args.get('execute_at'):
            from .scheduled_actions import authorize
            receipt['schedule']=authorize(self,db,name,args,action_id)
            receipt['title']=summary['title'].replace('确认','')+'已完成（未执行交易）'
            return receipt
        if name=='schedule_transfer':
            from .scheduled_transfers import authorize
            receipt['schedule']=authorize(self,db,args,action_id)
        elif name=='cancel_scheduled_transfer':
            from .scheduled_transfers import cancel
            receipt['schedule']=cancel(db,args['schedule_id'])
        elif name in {'create_birthday_plan','manage_birthday_plan'}:
            from .birthdays import execute
            receipt['birthday_plan']=execute(self,db,name,args,action_id)
        elif name == 'add_bank_card':
            from .cards import add_card_record
            receipt['card'] = add_card_record(db, args)
        elif name == 'create_account_category':
            from .account_categories import create_category
            receipt['account'] = create_category(db, args['name'])
        elif name == "transfer_own_funds":
            from .account_transfers import execute
            execute(self, db, args, action_id)
        elif name == "sell_stock_basket":
            from .securities_bridge import execute_sale
            receipt.update(execute_sale(self, db, args, action_id))
        elif name in {'change_travel_order','cancel_travel_order'}:
            from .travel_changes import execute
            receipt.update(execute(self,db,name,args,action_id))
        elif name == "place_travel_order":
            from .travel_bridge import execute
            receipt.update(execute(self, db, args, action_id))
        elif name == "place_food_order":
            from .food_bridge import execute
            receipt.update(execute(self, db, args, action_id))
        elif name == "purchase_stock_basket":
            from .securities_bridge import execute_purchase

            receipt.update(execute_purchase(self, db, args, action_id))
        elif name == "create_contact":
            from .contacts import create_person

            receipt["contact"] = create_person(db, args)
            from .db import event

            event(db, "*", "contacts.updated", {})
        elif name == "transfer":
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
