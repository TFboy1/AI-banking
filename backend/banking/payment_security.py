"""Per-user payment policy and operation-bound simulated SMS challenges."""
import json
import secrets
import time

from .bank import BankError
from .db import dumps, event, now
from .security import token_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS payment_policy (id INTEGER PRIMARY KEY CHECK(id=1), threshold_minor INTEGER NOT NULL CHECK(threshold_minor>=1), version INTEGER NOT NULL);
INSERT OR IGNORE INTO payment_policy VALUES (1,100000,1);
CREATE TABLE IF NOT EXISTS payment_policy_mode(id INTEGER PRIMARY KEY CHECK(id=1),mode TEXT NOT NULL);
UPDATE payment_policy SET version=version+1 WHERE EXISTS(SELECT 1 FROM payment_policy_mode WHERE id=1 AND mode!='user_single_v2');
INSERT OR REPLACE INTO payment_policy_mode VALUES(1,'user_single_v2');
CREATE TABLE IF NOT EXISTS payment_sms (approval_id TEXT NOT NULL,session_key TEXT NOT NULL,fingerprint TEXT NOT NULL,code_hash TEXT NOT NULL,expires_at REAL NOT NULL,sent_at REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,consumed INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(approval_id,session_key));
"""


TRANSFER_TOOLS={'transfer','transfer_own_funds','save_to_goal'}
THRESHOLD=100000

def daily_transferred(db,day=None):
    day=day or now()[:10]
    # Count committed outflows once, including own-account transfers. Seeded
    # examples, cancelled requests and scheduling authorizations are not trades.
    return db.execute("""SELECT COALESCE(SUM(t.amount_minor),0) FROM transactions t
        WHERE t.direction='out' AND t.category IN ('转账','内部转账') AND date(t.occurred_at,'+8 hours')=?
        AND EXISTS(SELECT 1 FROM approvals a WHERE a.status='executed'
          AND a.tool_name IN ('transfer','transfer_own_funds','save_to_goal')
          AND (t.action_id=a.id OR t.action_id=a.id||':out'))""",(day,)).fetchone()[0]

def policy(db):
    return {**dict(db.execute("SELECT * FROM payment_policy WHERE id=1").fetchone()),
        'basis':'single_transaction','fixed':False,'mode':'user_single_v2',
        'day':now()[:10],'daily_transfer_minor':daily_transferred(db),
        'counting_rule':'按单笔交易金额判断，不累计其他交易；单笔不超过用户门槛为黄色，超过为红色。'}


def risk(db, approval, args):
    rules = policy(db)
    summary = json.loads(approval["summary_json"])
    amount = summary.get("amount_minor") or 0
    tool=approval['tool_name']
    transfer=tool in TRANSFER_TOOLS or tool=='schedule_transfer'
    scheduled=transfer and (tool=='schedule_transfer' or bool(args.get('execute_at')))
    red = tool in {"add_bank_card", "purchase_product", "purchase_stock_basket", "sell_stock_basket"} or scheduled or amount>rules['threshold_minor']
    from .face import version,verification_enabled
    return {"level": "red" if red else "yellow", "sms_required": red, "face_enabled":verification_enabled(db),"face_required":red and verification_enabled(db),"face_version":version(db), "threshold_minor": rules['threshold_minor'],
        "policy_version": rules["version"], "basis":'single_transaction',
        "transaction_minor":amount,'scheduled_preverification':scheduled}


def fingerprint(db, approval, args, assessment):
    identity = db.execute("SELECT phone,pin_hash FROM app_identity WHERE id=1").fetchone()
    return token_hash(dumps({"approval": approval["id"], "tool": approval["tool_name"], "args": args, "risk": assessment, "identity": list(identity) if identity else None}))


def send_sms(db, approval, args, session_key):
    from .safety import check_user
    check_user(db)
    assessment = risk(db, approval, args)
    if not assessment["sms_required"]:
        raise BankError("这笔操作无需短信验证。")
    person = db.execute("SELECT phone FROM app_identity WHERE id=1").fetchone()
    if not person:
        raise BankError("请先注册手机号。")
    previous = db.execute("SELECT sent_at FROM payment_sms WHERE approval_id=? AND session_key=?", (approval["id"], session_key)).fetchone()
    if previous and time.time() - previous[0] < 60:
        raise BankError("请 60 秒后重新获取；若未变更交易信息，可填写上一条有效验证码。")
    code = f"{secrets.randbelow(1000000):06d}"
    db.execute("INSERT OR REPLACE INTO payment_sms VALUES (?,?,?,?,?,?,0,0)", (approval["id"], session_key, fingerprint(db, approval, args, assessment), token_hash(code), time.time()+300, time.time()))
    event(db, "*", "payment.sms_sent", {"approval_id": approval["id"], "risk_level": "red"})
    phone = person[0]
    return {"demo_code": code, "phone_masked": phone[:3]+"****"+phone[-4:], "expires_in": 300, "simulated": True}


def verify_sms(db, approval, args, session_key, code, consume=True):
    from .safety import check_user, failure, success
    check_user(db)
    assessment = risk(db, approval, args)
    if not assessment["sms_required"]:
        return None
    challenge = db.execute("SELECT * FROM payment_sms WHERE approval_id=? AND session_key=?", (approval["id"], session_key)).fetchone()
    if not challenge or challenge["consumed"] or challenge["expires_at"] <= time.time() or challenge["attempts"] >= 5:
        return "红色操作需要有效的短信验证码，请重新获取。"
    if challenge["fingerprint"] != fingerprint(db, approval, args, assessment):
        return "交易参数或安全设置已变化，请重新获取验证码。"
    if not isinstance(code, str) or not secrets.compare_digest(challenge["code_hash"], token_hash(code)):
        db.execute("UPDATE payment_sms SET attempts=attempts+1 WHERE approval_id=? AND session_key=?", (approval["id"], session_key))
        failures = failure(db, 'sms', approval['id'])
        if challenge['attempts'] + 1 >= 5:
            db.execute('UPDATE payment_sms SET consumed=1 WHERE approval_id=? AND session_key=?', (approval['id'], session_key))
        return "短信验证码错误，连续5次，已暂停交易60秒，请稍后重新获取。" if failures >= 5 else "短信验证码错误。"
    if consume:
        db.execute("UPDATE payment_sms SET consumed=1 WHERE approval_id=? AND session_key=?", (approval["id"], session_key))
        success(db, 'sms')
        event(db, "*", "payment.sms_verified", {"approval_id": approval["id"], "risk_level": "red"})
    return None
