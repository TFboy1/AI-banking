from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field


class OwnTransfer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account_id: str = Field(min_length=1, max_length=100)
    target_account_id: str = Field(min_length=1, max_length=100)
    amount_yuan: str = Field(pattern=r'^(0|[1-9][0-9]{0,8})([.][0-9]{1,2})?$')


class TransferBody(OwnTransfer):
    conversation_id: str = Field(min_length=1, max_length=100)


def summary(bank, db, args):
    from .bank import BankError, minor, yuan

    source = db.execute('SELECT * FROM accounts WHERE id=?', (args['account_id'],)).fetchone()
    target = db.execute('SELECT * FROM accounts WHERE id=?', (args['target_account_id'],)).fetchone()
    if not source or not target or source['id'] == target['id']:
        raise BankError('请选择当前用户两个不同的账户。')
    account_kinds = {'checking', 'savings', 'category'}
    incoming = source['kind'] == 'bank_card' and target['kind'] in account_kinds
    outgoing = target['kind'] == 'bank_card' and source['kind'] in account_kinds
    internal = source['kind'] in account_kinds and target['kind'] in account_kinds
    if not (incoming or outgoing or internal):
        raise BankError('支持日常、储蓄、分类账户之间转账，以及这些账户与已绑定银行卡之间划转；投资账户不参与。')
    if not internal:
        card_account = source if incoming else target
        card = db.execute('SELECT * FROM cards WHERE account_id=?', (card_account['id'],)).fetchone()
        if not card or card['status'] != 'active':
            raise BankError('银行卡未绑定或已冻结，不能进行划转。')
    amount = minor(args['amount_yuan'])
    bank._check_debit(db, source['id'], amount)
    return {'title': '确认账户间转账' if internal else '确认银行卡转入账户' if incoming else '确认账户提现至银行卡', 'amount_minor': amount, 'source_account_id':source['id'], 'target_account_id':target['id'], 'fields':[
        {'label':'付款账户', 'value':source['name']}, {'label':'到账账户', 'value':target['name']}, {'label':'划转金额', 'value':yuan(amount)}, {'label':'资金说明', 'value':'本人资金内部划转，确认后即时模拟到账，总余额不变。'}]}


def execute(bank, db, args, action_id):
    from .bank import minor

    amount = minor(args['amount_yuan'])
    source, target = args['account_id'], args['target_account_id']
    names = {r['id']:r['name'] for r in db.execute('SELECT id,name FROM accounts WHERE id IN (?,?)', (source,target))}
    db.execute('UPDATE accounts SET balance_minor=balance_minor-? WHERE id=?', (amount,source))
    db.execute('UPDATE accounts SET balance_minor=balance_minor+? WHERE id=?', (amount,target))
    bank._ledger(db, action_id+':out', source, names[target], 'out', amount, '内部转账', '本人资金划转')
    bank._ledger(db, action_id+':in', target, names[source], 'in', amount, '内部转账', '本人资金划转')


def register_account_transfers(app, database, agent, session, resolve_conversation):
    @app.post('/api/account-transfers')
    async def create(body: TransferBody, session_key: str = Depends(session)):
        with database.transaction() as db:
            conversation = resolve_conversation(db, session_key, body.conversation_id)
            message = agent.create_direct_approval(db, conversation, 'transfer_own_funds', body.model_dump(exclude={'conversation_id'}))
        await agent.notify()
        return message
