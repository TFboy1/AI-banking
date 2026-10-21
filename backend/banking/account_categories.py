from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .bank import BankError, Money
from .db import event, uid
from .security import verify_pin


class CategoryName(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=24)


class CategoryDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str
    pin: str | None = Field(default=None, pattern=r"^[0-9]{4}$")


class CategoryAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str
    amount_yuan: Money
    direction: str = Field(pattern=r"^(in|out)$")


def validate_category_name(db, name, exclude=''):
    if name == '投资账户':
        raise BankError('投资账户是系统持仓汇总，不能创建或改成同名分类。')
    if db.execute('SELECT 1 FROM accounts WHERE name=? COLLATE NOCASE AND id<>?', (name,exclude)).fetchone():
        raise BankError('已有同名账户，请换一个名称。')


def create_category(db, name):
    validate_category_name(db, name)
    account_id = uid('category_')
    db.execute('INSERT INTO accounts VALUES (?,?,?,0)', (account_id,name,'category'))
    event(db, '*', 'bank.updated', {'operation':'account.created','account_id':account_id,'name':name})
    return {'id':account_id,'name':name,'balance_minor':0,'kind':'category'}


def register_categories(app, database, agent, settings, session, resolve_conversation):
    def named(db, name, exclude=""):
        validate_category_name(db, name, exclude)

    @app.post("/api/account-categories", dependencies=[Depends(session)])
    async def create(body: CategoryName):
        with database.transaction() as db:
            account = create_category(db, body.name)
        await agent.notify()
        return account

    @app.patch("/api/account-categories/{account_id}", dependencies=[Depends(session)])
    async def rename(account_id: str, body: CategoryName):
        with database.transaction() as db:
            if account_id == "daily":
                raise BankError("日常账户是默认账户，请保留其名称。")
            if not db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
                raise BankError("账户不存在。")
            if db.execute("SELECT 1 FROM accounts WHERE id=? AND kind LIKE 'bank_card%'", (account_id,)).fetchone():
                raise BankError("请在卡片区域管理银行卡。")
            named(db, body.name, account_id)
            db.execute("UPDATE accounts SET name=? WHERE id=?", (body.name, account_id))
            event(db, "*", "bank.updated", {"operation": "account.renamed", "account_id": account_id, "name": body.name})
        await agent.notify()
        return {"status": "renamed"}

    @app.delete("/api/account-categories/{account_id}", dependencies=[Depends(session)])
    async def delete(account_id: str, body: CategoryDelete):
        if account_id == "daily":
            raise BankError("日常账户用于接收归回的资金，不能删除。")
        if body.confirmation != "DELETE":
            raise BankError("请先确认删除账户并将余额归回日常账户。")
        if settings.app_auth_enabled:
            if not body.pin:
                raise HTTPException(428, "删除账户需要验证交易密码。")
            verify_pin(database, body.pin)
        with database.transaction() as db:
            from .safety import check_user
            check_user(db)
            account = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not account:
                raise BankError("账户不存在或已经删除。")
            if not db.execute("SELECT 1 FROM accounts WHERE id='daily'").fetchone():
                raise BankError("日常账户不存在，暂时无法删除。")
            if account["kind"].startswith("bank_card"):
                raise BankError("请在卡片区域删除银行卡。")
            amount = account["balance_minor"]
            db.execute("UPDATE accounts SET balance_minor=balance_minor+? WHERE id='daily'", (amount,))
            # Preserve reservations and history when removing a balance category.
            db.execute("UPDATE commitments SET account_id='daily' WHERE account_id=?", (account_id,))
            db.execute("UPDATE cards SET account_id='daily' WHERE account_id=?", (account_id,))
            if amount:
                agent.bank._ledger(db, None, account_id, "日常账户", "out", amount, "内部转账", "删除分类账户，余额归回日常账户")
                agent.bank._ledger(db, None, "daily", account["name"], "in", amount, "内部转账", "删除分类账户，余额归回日常账户")
            db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            event(db, "*", "bank.updated", {"operation": "account.deleted", "account_id": account_id, "name": account["name"], "returned_minor": amount})
        await agent.notify()
        return {"status": "deleted", "returned_minor": amount}

    @app.post("/api/account-categories/{account_id}/allocation")
    async def allocate(account_id: str, body: CategoryAllocation, session_key: str = Depends(session)):
        with database.transaction() as db:
            if account_id == "daily" or not db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
                raise BankError("请选择日常账户之外的分类账户。")
            conversation_id = resolve_conversation(db, session_key, body.conversation_id)
            source, target = ("daily", account_id) if body.direction == "in" else (account_id, "daily")
            message = agent.create_direct_approval(db, conversation_id, "transfer", {"recipient": target, "account_id": source, "amount_yuan": body.amount_yuan, "note": "账户资金分类"})
        await agent.notify()
        return message
