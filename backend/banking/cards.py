import hashlib

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .bank import BankError
from .card_models import AddCard
from .db import event, uid
from .security import verify_pin

SCHEMA = """
CREATE TABLE IF NOT EXISTS linked_card_accounts (
 number_hash TEXT PRIMARY KEY,account_id TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'active',daily_limit_minor INTEGER NOT NULL DEFAULT 1000000);
"""


class RemoveCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str
    pin: str | None = Field(default=None, pattern=r"^[0-9]{4}$")


class AddCardRequest(AddCard):
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)


def card_summary(db, args):
    digest = hashlib.sha256(args['card_number'].encode()).hexdigest()
    linked = db.execute('SELECT * FROM linked_card_accounts WHERE number_hash=?',(digest,)).fetchone()
    if linked and db.execute('SELECT 1 FROM cards WHERE account_id=?',(linked['account_id'],)).fetchone():
        raise BankError('这张银行卡已经添加，请勿重复添加。')
    balance = db.execute('SELECT balance_minor FROM accounts WHERE id=?',(linked['account_id'],)).fetchone()[0] if linked else 500000
    return {'title':'确认添加银行卡','fields':[{'label':'银行名称','value':args['bank_name']},{'label':'银行卡尾号','value':args['card_number'][-4:]},{'label':'模拟余额','value':f'¥{balance/100:,.2f}'},{'label':'说明','value':'重新绑定恢复原卡余额；新卡默认模拟余额5000元。'}]}


def add_card_record(db, args):
    body = AddCard.model_validate(args)
    card_summary(db, body.model_dump())
    digest = hashlib.sha256(body.card_number.encode()).hexdigest()
    last4 = body.card_number[-4:]
    db.execute("INSERT OR IGNORE INTO accounts VALUES ('daily','日常账户','checking',0)")
    linked = db.execute("SELECT * FROM linked_card_accounts WHERE number_hash=?", (digest,)).fetchone()
    if linked:
        account_id = linked["account_id"]
        if db.execute("SELECT 1 FROM cards WHERE account_id=?", (account_id,)).fetchone():
            raise BankError("这张银行卡已经添加，请勿重复添加。")
        db.execute("UPDATE accounts SET kind='bank_card',name=? WHERE id=?", (f"{body.bank_name} · 尾号{last4}", account_id))
    else:
        account_id = uid("bankcard_")
        db.execute("INSERT INTO accounts VALUES (?,?,?,500000)", (account_id, f"{body.bank_name} · 尾号{last4}", "bank_card"))
        db.execute("INSERT INTO linked_card_accounts(number_hash,account_id) VALUES (?,?)", (digest, account_id))
    card_id = uid("card_")
    db.execute("INSERT INTO cards VALUES (?,?,?,?,?,?)", (card_id, account_id, body.bank_name, last4, linked["status"] if linked else "active", linked["daily_limit_minor"] if linked else 1000000))
    balance = db.execute("SELECT balance_minor FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
    event(db, "*", "bank.updated", {"operation": "card.added", "card_id": card_id, "balance_minor": balance, "simulated": True})
    return {"id": card_id, "account_id": account_id, "balance_minor": balance, "simulated": True}


def register_cards(app, database, agent, settings, session, resolve_conversation):
    @app.post("/api/cards")
    async def add_card(body: AddCardRequest, session_key: str = Depends(session)):
        with database.transaction() as db:
            conversation = resolve_conversation(db, session_key, body.conversation_id)
            result = agent.create_direct_approval(db, conversation, 'add_bank_card', body.model_dump(exclude={'conversation_id'}))
        await agent.notify()
        return result

    @app.delete("/api/cards/{card_id}", dependencies=[Depends(session)])
    async def remove_card(card_id: str, body: RemoveCard):
        if body.confirmation != "DELETE":
            raise BankError("请确认删除银行卡。")
        if settings.app_auth_enabled:
            if not body.pin:
                raise HTTPException(428, "删除银行卡需要验证交易密码。")
            verify_pin(database, body.pin)
        with database.transaction() as db:
            card = db.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
            if not card:
                raise BankError("银行卡不存在或已删除。")
            account = db.execute("SELECT * FROM accounts WHERE id=?", (card["account_id"],)).fetchone()
            returned = 0
            db.execute("UPDATE linked_card_accounts SET status=?,daily_limit_minor=? WHERE account_id=?", (card["status"], card["daily_limit_minor"], card["account_id"]))
            if account and account["kind"] == "bank_card":
                # Unlinking a card is not a transfer. Keep its balance and
                # reservations in its original account for a later rebind.
                db.execute("UPDATE accounts SET kind='bank_card_unlinked' WHERE id=?", (account['id'],))
            db.execute("DELETE FROM cards WHERE id=?", (card_id,))
            event(db, "*", "bank.updated", {"operation": "card.deleted", "card_id": card_id, "returned_minor": returned})
        await agent.notify()
        return {"status": "deleted", "returned_minor": returned}
