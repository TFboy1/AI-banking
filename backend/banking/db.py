import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="milliseconds")


def uid(prefix: str = "") -> str:
    return prefix + uuid.uuid4().hex


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, balance_minor INTEGER NOT NULL CHECK(balance_minor>=0));
CREATE TABLE IF NOT EXISTS contacts (id TEXT PRIMARY KEY,name TEXT NOT NULL,phone TEXT NOT NULL,bank TEXT NOT NULL,last4 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contact_links (contact_id TEXT PRIMARY KEY,person_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contact_phones (person_id TEXT NOT NULL,phone TEXT NOT NULL,PRIMARY KEY(person_id,phone));
CREATE TABLE IF NOT EXISTS contact_details (contact_id TEXT PRIMARY KEY,note TEXT NOT NULL DEFAULT '',card_number TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversation_contacts (conversation_id TEXT PRIMARY KEY,contact_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contact_profiles (
 person_id TEXT PRIMARY KEY,relationship TEXT NOT NULL DEFAULT '未分类',birthday TEXT,
 gender TEXT NOT NULL DEFAULT '未设置',special_attention INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS cards (id TEXT PRIMARY KEY,account_id TEXT NOT NULL,name TEXT NOT NULL,last4 TEXT NOT NULL,
 status TEXT NOT NULL,daily_limit_minor INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS transactions (
 id TEXT PRIMARY KEY,action_id TEXT UNIQUE,account_id TEXT NOT NULL,counterparty TEXT NOT NULL,
 direction TEXT NOT NULL,amount_minor INTEGER NOT NULL CHECK(amount_minor>0),category TEXT NOT NULL,
 occurred_at TEXT NOT NULL,merchant_key TEXT,description TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS transactions_date ON transactions(occurred_at);
CREATE TABLE IF NOT EXISTS products (id TEXT PRIMARY KEY,name TEXT NOT NULL,risk_level INTEGER NOT NULL,
 nav TEXT NOT NULL,min_purchase_minor INTEGER NOT NULL,description TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS holdings (product_id TEXT PRIMARY KEY,units TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_profile (id INTEGER PRIMARY KEY CHECK(id=1),answers_json TEXT NOT NULL,risk_level INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS subscriptions (id TEXT PRIMARY KEY,name TEXT NOT NULL,merchant_key TEXT NOT NULL,
 amount_minor INTEGER NOT NULL,next_charge_date TEXT NOT NULL,status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY,created_at TEXT NOT NULL,
 title TEXT NOT NULL DEFAULT '新会话',updated_at TEXT NOT NULL DEFAULT '',deleted_at TEXT);
CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,status TEXT NOT NULL,
 channel TEXT NOT NULL,state_json TEXT NOT NULL,error TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,task_id TEXT,
 role TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,data_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation_id);
CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY,task_id TEXT NOT NULL,message_id TEXT NOT NULL,
 tool_name TEXT NOT NULL,args_json TEXT NOT NULL,summary_json TEXT NOT NULL,status TEXT NOT NULL,
 receipt_json TEXT,expires_at TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT NOT NULL,
 type TEXT NOT NULL,data_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY,created_at TEXT NOT NULL,active_conversation_id TEXT);
CREATE TABLE IF NOT EXISTS binding_codes (code_hash TEXT PRIMARY KEY,expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feishu_binding (id INTEGER PRIMARY KEY CHECK(id=1),tenant_key TEXT NOT NULL,
 open_id TEXT NOT NULL,chat_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feishu_inbox (event_id TEXT PRIMARY KEY,result_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feishu_outbox (message_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,
 external_id TEXT,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT);
CREATE TABLE IF NOT EXISTS plans (
 id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,kind TEXT NOT NULL,title TEXT NOT NULL,status TEXT NOT NULL,
 current_version INTEGER NOT NULL,goal_minor INTEGER,deadline TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_versions (
 plan_id TEXT NOT NULL,version INTEGER NOT NULL,input_json TEXT NOT NULL,result_json TEXT NOT NULL,
 created_at TEXT NOT NULL,PRIMARY KEY(plan_id,version));
CREATE TABLE IF NOT EXISTS commitments (
 id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,conversation_id TEXT NOT NULL,account_id TEXT NOT NULL,
 kind TEXT NOT NULL,label TEXT NOT NULL,amount_minor INTEGER NOT NULL,remaining_minor INTEGER NOT NULL,
 due_date TEXT,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_executions (
 id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,step_key TEXT NOT NULL,approval_id TEXT NOT NULL UNIQUE,
 amount_minor INTEGER NOT NULL,status TEXT NOT NULL,receipt_json TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trigger_rules (
 id TEXT PRIMARY KEY,plan_id TEXT,conversation_id TEXT NOT NULL,account_id TEXT NOT NULL,
 source_key TEXT NOT NULL,expected_day INTEGER NOT NULL,reserve_minor INTEGER NOT NULL,
 cadence TEXT NOT NULL,status TEXT NOT NULL,last_period TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bank_events (
 id TEXT PRIMARY KEY,event_key TEXT NOT NULL UNIQUE,kind TEXT NOT NULL,period TEXT NOT NULL,
 payload_json TEXT NOT NULL,occurred_at TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trigger_runs (
 id TEXT PRIMARY KEY,rule_id TEXT NOT NULL,event_id TEXT,period TEXT NOT NULL,kind TEXT NOT NULL,
 result_json TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(rule_id,period,kind));
CREATE TABLE IF NOT EXISTS notifications (
 id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,kind TEXT NOT NULL,title TEXT NOT NULL,
 data_json TEXT NOT NULL,read_at TEXT,created_at TEXT NOT NULL);
"""


class Database:
    def __init__(self, path: Path, seed_demo=True):
        self.seed_demo = seed_demo
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        from .audit_integrity import register_functions
        register_functions(db)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            has_history=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_history'").fetchone()
            history_sequence=db.execute('SELECT COALESCE(MAX(seq),0) FROM report_history').fetchone()[0] if has_history else None
            history_start=db.execute('SELECT started_at FROM report_history_meta WHERE id=1').fetchone() if has_history else None
            try:
                yield db
                if history_sequence is not None:
                    # An atomic transfer must not appear half-completed in a
                    # midnight report. Every row version shares its commit boundary.
                    instant=db.execute("SELECT strftime('%Y-%m-%dT%H:%M:%f+00:00','now')").fetchone()[0]
                    db.execute('UPDATE report_history SET changed_at=? WHERE seq>?',(instant,history_sequence))
                    current_start=db.execute('SELECT started_at FROM report_history_meta WHERE id=1').fetchone()
                    if current_start and (not history_start or current_start[0]!=history_start[0]):
                        db.execute('UPDATE report_history_meta SET started_at=? WHERE id=1',(instant,))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def initialize(self):
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            from .cards import SCHEMA as CARDS_SCHEMA
            from .safety import SCHEMA as SAFETY_SCHEMA
            from .account_lock import SCHEMA as GUARD_SCHEMA
            from .human_service import SCHEMA as HUMAN_SCHEMA
            from .scheduled_transfers import SCHEMA as SCHEDULE_SCHEMA
            from .connections import SCHEMA as CONNECTION_SCHEMA
            from .food_common import BANK_SCHEMA as FOOD_SCHEMA
            from .market import SCHEMA as MARKET_SCHEMA
            from .payment_security import SCHEMA as PAYMENT_SCHEMA
            from .securities_common import BANK_SCHEMA as SECURITIES_SCHEMA
            from .security import SCHEMA as AUTH_SCHEMA
            from .daily_reports import SCHEMA as DAILY_SCHEMA
            from .birthdays import SCHEMA as BIRTHDAY_SCHEMA
            from .analysis_service import SCHEMA as ANALYSIS_SCHEMA
            from .face import SCHEMA as FACE_SCHEMA

            from .travel_common import BANK_SCHEMA as TRAVEL_SCHEMA
            db.executescript(TRAVEL_SCHEMA + CONNECTION_SCHEMA + FOOD_SCHEMA + SCHEMA + AUTH_SCHEMA + PAYMENT_SCHEMA + CARDS_SCHEMA + MARKET_SCHEMA + SECURITIES_SCHEMA + SAFETY_SCHEMA + GUARD_SCHEMA + HUMAN_SCHEMA + SCHEDULE_SCHEMA + DAILY_SCHEMA + BIRTHDAY_SCHEMA + ANALYSIS_SCHEMA + FACE_SCHEMA)
            from .scheduled_transfers import migrate as migrate_scheduled
            migrate_scheduled(db)
        with self.transaction() as db:
            # 原有演示库原位升级，保留会话、资金流水与确认回执。
            columns = {row["name"] for row in db.execute("PRAGMA table_info(conversations)")}
            for name, definition in (
                ("title", "TEXT NOT NULL DEFAULT '新会话'"),
                ("updated_at", "TEXT NOT NULL DEFAULT ''"),
                ("deleted_at", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE conversations ADD COLUMN {name} {definition}")
            session_columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
            if "active_conversation_id" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN active_conversation_id TEXT")
            if "title" not in columns:
                db.execute("""UPDATE conversations SET title=COALESCE(
                    (SELECT substr(text,1,32) FROM messages WHERE conversation_id=conversations.id
                     AND role='user' ORDER BY rowid LIMIT 1),'新会话')""")
            db.execute("""UPDATE conversations SET updated_at=COALESCE(
                (SELECT MAX(created_at) FROM messages WHERE conversation_id=conversations.id),created_at)
                WHERE updated_at=''""")
            empty_user = not self.seed_demo or bool(db.execute("SELECT 1 FROM meta WHERE key='empty_user'").fetchone())
            if not db.execute("SELECT 1 FROM meta WHERE key='conversation_id'").fetchone():
                self.seed_empty(db) if empty_user else self.seed(db)
            elif not empty_user:
                self.seed_extra_demo_data(db)
            self.seed_contact_details(db)
            from .market import sync_prices

            db.execute("UPDATE products SET description=replace(description,'固定模拟净值','每日模拟波动净值') WHERE description LIKE '%固定模拟净值%'")
            sync_prices(db)

        from .report_history import install as install_report_history
        with self.connection() as db:install_report_history(db)
        from .audit_integrity import install as install_integrity
        with self.connection() as db:install_integrity(db)

    def seed_empty(self, db):
        """New users retain only the shared product catalog, with no personal bank settings."""
        db.execute("INSERT OR REPLACE INTO meta VALUES ('empty_user','1')")
        conversation = self.create_conversation(db)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('conversation_id',?)", (conversation,))
        db.executemany("INSERT OR IGNORE INTO products VALUES (?,?,?,?,?,?)", [
            ("cash", "安心现金", 1, "1.0000", 10000, "R1 · 模拟现金管理产品，固定模拟净值。"),
            ("bond", "稳享固收", 2, "1.0250", 10000, "R2 · 模拟固收产品，存在净值波动风险。"),
            ("growth", "远见成长", 4, "1.1560", 100000, "R4 · 模拟混合产品，较高波动风险。"),
            ("flex_cash", "灵活零钱", 1, "1.0000", 1000, "R1 · 模拟现金产品，10 元起购。"),
            ("steady_bond", "稳健债券", 2, "1.0180", 20000, "R2 · 模拟债券产品，不承诺收益。"),
            ("balanced", "均衡配置", 3, "1.0860", 50000, "R3 · 模拟混合配置产品，不保本。"),
            ("tech", "科技精选", 4, "1.2345", 200000, "R4 · 模拟权益产品，不保本。"),
        ])
        return conversation

    def seed_contact_details(self, db):
        for index, row in enumerate(db.execute("SELECT id,last4 FROM contacts ORDER BY id").fetchall(), 1):
            # 原始演示库只有尾号，补齐模拟号码，绝不冒充真实银行卡。
            existing = db.execute("SELECT l.person_id FROM contacts c JOIN contact_links l ON l.contact_id=c.id WHERE c.name=(SELECT name FROM contacts WHERE id=?) AND c.phone=(SELECT phone FROM contacts WHERE id=?) ORDER BY c.rowid LIMIT 1", (row["id"], row["id"])).fetchone()
            person_id = existing[0] if existing else row["id"]
            db.execute("INSERT OR IGNORE INTO contact_links VALUES (?,?)", (row["id"], person_id))
            person_id = db.execute("SELECT person_id FROM contact_links WHERE contact_id=?", (row["id"],)).fetchone()[0]
            db.execute("INSERT OR IGNORE INTO contact_phones SELECT ?,phone FROM contacts WHERE id=? AND phone<>''", (person_id, row["id"]))
            db.execute("INSERT OR IGNORE INTO contact_details VALUES (?,?,?)",
                       (row["id"], "", f"622200{index:06d}{row['last4']}"))

    def seed_extra_demo_data(self, db):
        """增量补齐测试样本；已有配置和取消状态不覆盖，历史样本只添加一次。"""
        db.executemany(
            "INSERT OR IGNORE INTO contacts VALUES (?,?,?,?,?)",
            [
                ("chen_yue", "陈悦", "13700004444", "演示银行", "2468"),
                ("chen_yue_2", "陈悦", "13700004444", "模拟商业银行", "5810"),
                ("wang_wei_1", "王伟", "13600005555", "演示银行", "1357"),
                ("wang_wei_2", "王伟", "13500006666", "模拟商业银行", "9753"),
                ("zhao_min", "赵敏", "13400007777", "模拟商业银行", "8642"),
                ("liu_yang", "刘洋", "13300008888", "演示银行", "3690"),
            ],
        )
        db.executemany(
            "INSERT OR IGNORE INTO products VALUES (?,?,?,?,?,?)",
            [
                ("flex_cash", "灵活零钱", 1, "1.0000", 1000, "R1 · 演示现金产品；10 元起购，固定模拟净值。"),
                ("steady_bond", "稳健债券", 2, "1.0180", 20000, "R2 · 演示债券产品；净值可波动，不承诺收益。"),
                ("balanced", "均衡配置", 3, "1.0860", 50000, "R3 · 演示混合配置产品；中高波动，不保本。"),
                ("tech_growth", "科技精选", 4, "1.2345", 200000, "R4 · 演示权益产品；高波动，不保本。"),
            ],
        )
        first = datetime.now(TZ).replace(day=1, hour=9, minute=0, second=0, microsecond=0)
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        for identifier, name, amount, day in (
            ("learning", "知行学习会员", 3900, 10),
            ("fitness", "活力健身会员", 5900, 15),
            ("office", "轻办公会员", 2500, 20),
        ):
            db.execute(
                "INSERT OR IGNORE INTO subscriptions VALUES (?,?,?,?,?,?)",
                (identifier, name, identifier, amount, next_month.replace(day=day).date().isoformat(), "active"),
            )
            if not db.execute("SELECT changes()").fetchone()[0]:
                continue
            # 种子历史属于期初快照的依据，不再次扣减用户当前余额。
            for offset in range(3):
                month_index = first.year * 12 + first.month - 1 - offset
                month = first.replace(year=month_index // 12, month=month_index % 12 + 1)
                when = month.replace(day=min(day, datetime.now(TZ).day) if offset == 0 else day)
                db.execute(
                    "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (uid("tx_"), None, "daily", name, "out", amount, "订阅", when.isoformat(), identifier, "演示历史交易"),
                )

    def seed(self, db):
        conversation_id = self.create_conversation(db)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('conversation_id',?)", (conversation_id,))
        db.executemany(
            "INSERT INTO accounts VALUES (?,?,?,?)",
            [("daily", "日常账户", "checking", 1286050), ("savings", "储蓄账户", "savings", 820000)],
        )
        db.executemany(
            "INSERT INTO contacts VALUES (?,?,?,?,?)",
            [
                ("zhang_san", "张三", "13800001111", "演示银行", "0826"),
                ("li_ming_1", "李明", "13800002222", "演示银行", "1920"),
                ("li_ming_2", "李明", "13900003333", "模拟商业银行", "6712"),
            ],
        )
        db.execute("INSERT INTO cards VALUES ('card_daily','daily','日常借记卡','8821','active',1000000)")
        db.executemany(
            "INSERT INTO products VALUES (?,?,?,?,?,?)",
            [
                (
                    "cash",
                    "安心现金",
                    1,
                    "1.0000",
                    10000,
                    "R1 · 演示现金管理产品；固定模拟净值，申赎即时到账。",
                ),
                (
                    "bond",
                    "稳享固收",
                    2,
                    "1.0250",
                    10000,
                    "R2 · 演示固收产品；存在净值波动风险，申赎即时结算。",
                ),
                (
                    "growth",
                    "远见成长",
                    4,
                    "1.1560",
                    100000,
                    "R4 · 演示混合产品；较高波动风险，不保证本金或收益。",
                ),
            ],
        )
        db.execute("INSERT INTO holdings VALUES ('cash','2000.0000')")
        today = datetime.now(TZ)
        first = today.replace(day=1, hour=9, minute=0, second=0, microsecond=0)
        next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        db.executemany(
            "INSERT INTO subscriptions VALUES (?,?,?,?,?,?)",
            [
                (
                    "video",
                    "星河视频会员",
                    "video",
                    2900,
                    next_month.replace(day=3).date().isoformat(),
                    "active",
                ),
                (
                    "music",
                    "声动音乐会员",
                    "music",
                    1500,
                    next_month.replace(day=5).date().isoformat(),
                    "active",
                ),
                ("cloud", "云端存储", "cloud", 1200, next_month.replace(day=8).date().isoformat(), "active"),
            ],
        )
        # 历史流水用于分析；当前余额是演示期初余额与这些流水结算后的快照。
        for offset in range(3):
            month_index = first.year * 12 + first.month - 1 - offset
            month = first.replace(year=month_index // 12, month=month_index % 12 + 1)
            samples = [
                (1, "工资收入", "in", 1800000, "工资", "salary"),
                (2, "房租", "out", 320000, "居住", "rent"),
                (3, "星河视频会员", "out", 2900, "订阅", "video"),
                (5, "声动音乐会员", "out", 1500, "订阅", "music"),
                (6, "街角咖啡", "out", 2800, "餐饮", "coffee"),
                (7, "日常超市", "out", 23650 + offset * 1200, "购物", "market"),
                (8, "云端存储", "out", 1200, "订阅", "cloud"),
                (9, "地铁出行", "out", 1200, "交通", "metro"),
                (10, "周末餐厅", "out", 16800, "餐饮", "restaurant"),
                (11, "电子产品商店", "out", 389900 if offset == 0 else 19900, "购物", "electronics"),
                (12, "星河视频会员", "out", 2900, "订阅", "video"),
            ]
            for day, merchant, direction, amount, category, key in samples:
                when = month.replace(day=min(day, today.day) if offset == 0 else day)
                db.execute(
                    "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        uid("tx_"),
                        None,
                        "daily",
                        merchant,
                        direction,
                        amount,
                        category,
                        when.isoformat(),
                        key,
                        "演示历史交易",
                    ),
                )
        self.seed_extra_demo_data(db)
        return conversation_id

    def create_conversation(self, db, title="新会话"):
        conversation_id = uid("conv_")
        created = now()
        db.execute(
            "INSERT INTO conversations(id,created_at,title,updated_at) VALUES (?,?,?,?)",
            (conversation_id, created, title, created),
        )
        add_message(
            db,
            conversation_id,
            None,
            "assistant",
            "text",
            "你好，我是你的银行助手。查账单、转账，或是安排一笔储蓄，直接告诉我就好。",
            {},
        )
        return conversation_id

    def conversations(self, db):
        return [
            dict(row)
            for row in db.execute("""
            SELECT c.id,c.title,c.created_at,c.updated_at,
                (SELECT text FROM messages WHERE conversation_id=c.id ORDER BY rowid DESC LIMIT 1) AS preview,
                (SELECT status FROM tasks WHERE conversation_id=c.id
                 AND status IN ('queued','running','waiting_confirmation') ORDER BY rowid DESC LIMIT 1) AS task_status,
                (SELECT COUNT(*) FROM notifications n WHERE n.conversation_id=c.id AND n.read_at IS NULL) AS unread_count
            FROM conversations c WHERE c.deleted_at IS NULL ORDER BY c.updated_at DESC,c.rowid DESC
        """)
        ]

    def conversation_id(self, db=None):
        if db is not None:
            return db.execute("SELECT value FROM meta WHERE key='conversation_id'").fetchone()[0]
        with self.connection() as conn:
            return self.conversation_id(conn)


def event(db, conversation_id, kind, data):
    db.execute(
        "INSERT INTO events(conversation_id,type,data_json,created_at) VALUES (?,?,?,?)",
        (conversation_id, kind, dumps(data), now()),
    )


def message_dict(row):
    item = dict(row)
    item["data"] = json.loads(item.pop("data_json"))
    return item


def queue_outbox(db, message):
    from .feishu_channels import queue_message
    queue_message(db,message)


def add_message(db, conversation_id, task_id, role, kind, text, data):
    if role == "user":
        db.execute(
            """UPDATE conversations SET title=? WHERE id=? AND title='新会话'
            AND NOT EXISTS(SELECT 1 FROM messages WHERE conversation_id=? AND role='user')""",
            (" ".join(text.split())[:32], conversation_id, conversation_id),
        )
    item = {
        "id": uid("msg_"),
        "conversation_id": conversation_id,
        "task_id": task_id,
        "role": role,
        "kind": kind,
        "text": text,
        "data": data,
        "created_at": now(),
    }
    db.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
        (item["id"], conversation_id, task_id, role, kind, text, dumps(data), item["created_at"]),
    )
    db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (item["created_at"], conversation_id))
    event(db, conversation_id, "message", item)
    event(db, "*", "conversations.updated", {"conversation_id": conversation_id})
    queue_outbox(db, item)
    return item


def update_message(db, message_id, data):
    db.execute("UPDATE messages SET data_json=? WHERE id=?", (dumps(data), message_id))
    item = message_dict(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())
    event(db, item["conversation_id"], "message", item)
    queue_outbox(db, item)
