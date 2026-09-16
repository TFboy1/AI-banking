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
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
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
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def initialize(self):
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
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
            if not db.execute("SELECT 1 FROM meta WHERE key='conversation_id'").fetchone():
                self.seed(db)

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
                 AND status IN ('queued','running','waiting_confirmation') ORDER BY rowid DESC LIMIT 1) AS task_status
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
    if not message["task_id"] or message["role"] != "assistant":
        return
    task = db.execute("SELECT channel FROM tasks WHERE id=?", (message["task_id"],)).fetchone()
    if task and task["channel"] == "feishu":
        db.execute(
            """INSERT INTO feishu_outbox(message_id,payload_json,status) VALUES (?,?,'pending')
            ON CONFLICT(message_id) DO UPDATE SET payload_json=excluded.payload_json,status='pending',attempts=0""",
            (message["id"], dumps(message)),
        )


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
