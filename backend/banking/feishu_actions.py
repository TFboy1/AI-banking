"""Bound-user Feishu commands, idempotent callbacks, and task controls."""
import json
import re
import secrets
from datetime import datetime
from .db import TZ,dumps,event,update_message
from .security import token_hash
from .bank import BankError


def dedicated_conversation(database, db):
    """Keep ordinary Feishu chat separate from the web user's active conversation."""
    saved = db.execute("SELECT value FROM meta WHERE key='feishu_chat_conversation'").fetchone()
    if saved:
        row = db.execute('SELECT id FROM conversations WHERE id=? AND deleted_at IS NULL', (saved[0],)).fetchone()
        if row:
            db.execute("UPDATE conversations SET title='飞书' WHERE id=?", (row[0],))
            return row[0]
    conversation = database.create_conversation(db, '飞书')
    db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('feishu_chat_conversation',?)", (conversation,))
    return conversation


def scoped_task(db,key,conversation):
    from .feishu_channels import recipient,task_recipient
    task=db.execute('''SELECT t.* FROM tasks t JOIN conversations c ON c.id=t.conversation_id
        WHERE t.id=? AND t.conversation_id=? AND c.deleted_at IS NULL''',(key,conversation)).fetchone()
    if not task or task['channel']!='feishu' or task_recipient(db,task)!=recipient(db):
        raise BankError('该卡片不属于当前飞书绑定的任务。')
    return task


async def handle(agent,database,body):
    queued=None;stopped=None
    # Include the actor in the inbox key, so an unrelated user cannot suppress
    # another user's callback by supplying the same platform event identifier.
    inbox=token_hash(dumps([body.tenant_key,body.open_id,body.chat_id,body.event_id]))
    with database.transaction() as db:
        previous=db.execute('SELECT result_json FROM feishu_inbox WHERE event_id=?',(inbox,)).fetchone()
        if previous:return {**json.loads(previous[0]),'duplicate':True}
        binding=db.execute('SELECT * FROM feishu_binding WHERE id=1').fetchone()
        bound=bool(binding and all(binding[k]==getattr(body,k) for k in ('tenant_key','open_id','chat_id')))
        text=body.text.strip()
        if body.kind=='message' and text.startswith(('绑定 ','/绑定 ')):
            from .account_lock import locked
            code=db.execute('SELECT expires_at FROM binding_codes WHERE code_hash=?',(token_hash(text.split(maxsplit=1)[1].strip().upper()),)).fetchone()
            if locked(db):result={'reply':'系统已安全锁定，请使用现有绑定发送“转人工”。'}
            elif code and datetime.fromisoformat(code[0])>datetime.now(TZ):
                db.execute('DELETE FROM binding_codes')
                db.execute('INSERT OR REPLACE INTO feishu_binding VALUES(1,?,?,?)',(body.tenant_key,body.open_id,body.chat_id))
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('feishu_binding_nonce',?)",(secrets.token_hex(16),))
                dedicated_conversation(database, db)
                result={'reply':'绑定成功。飞书已关联你的有数账户。'}
                event(db,'*','binding.updated',{'bound':True})
            else:result={'reply':'绑定码无效或已过期，请在有数重新生成。'}
        elif not bound:result={'reply':'请先在本机 Web 设置中生成绑定码，再在本私聊发送“绑定 绑定码”。'}
        else:
            if body.kind == 'message' and text:
                from .feishu_channels import enqueue, recipient
                enqueue(db, {'id': 'fsack_' + inbox[:40], 'task_id': None,
                    'conversation_id': dedicated_conversation(database, db),
                    'role': 'assistant', 'kind': 'feishu_receipt',
                    'text': '有数已收到，正在处理你的需求。', 'data': {}}, recipient(db))
            try:
                from .account_lock import locked
                from . import handoff,human_service
                focus=db.execute("SELECT value FROM meta WHERE key='feishu_conversation'").fetchone()
                conversation = (dedicated_conversation(database, db) if body.kind == 'message'
                    else body.conversation_id or (focus[0] if focus else dedicated_conversation(database, db)))
                if not db.execute('SELECT 1 FROM conversations WHERE id=? AND deleted_at IS NULL',(conversation,)).fetchone():conversation=database.conversation_id(db)
                if locked(db):
                    service=human_service.current(db,conversation)
                    if body.kind=='message' and (handoff.requested(text) or (service and service['status'] in {'waiting','active'})):
                        queued=handoff.create(agent,db,conversation,text,'feishu') if handoff.requested(text) else human_service.user_message(db,conversation,text,'feishu')
                        result={'reply':'已转交人工客服。系统锁定期间只能联系人工。'}
                    else:raise BankError('系统已安全锁定，仅可发送“转人工”联系人工处理。')
                elif body.kind=='card':
                    if body.action=='focus':
                        from .feishu_channels import recipient
                        sent=db.execute('SELECT payload_json FROM feishu_outbox WHERE message_id=?',(body.message_id,)).fetchone()
                        message=json.loads(sent[0]) if sent else None
                        if not message or message.get('_feishu_recipient')!=recipient(db) or message['conversation_id']!=conversation:raise BankError('该通知已失效，请使用最新通知。')
                        from .db import add_message
                        chat = dedicated_conversation(database, db)
                        add_message(db, chat, None, 'assistant', 'text',
                            '你在飞书选择继续处理这条通知：\n' + (message.get('text') or '') + '\n' +
                            dumps({k:v for k,v in message.get('data',{}).items() if not k.startswith('_')}), {})
                        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('feishu_conversation',?)",(conversation,))
                        result={'reply':'已将这条通知加入“飞书”对话，请直接描述下一步需求。'}
                    elif body.action in {'stop','resume','partial'}:
                        scoped_task(db,body.task_id,conversation)
                        if body.action=='stop':
                            from .interruption import interrupt_in_transaction
                            result=interrupt_in_transaction(agent,db,body.task_id,conversation)
                            stopped=body.task_id;result={'reply':'已停止后续步骤，请核对已返回的执行与资金结果。'}
                        elif body.action=='resume':
                            queued=agent.submit_in_transaction(db,'继续','feishu',conversation,recovery_parent_id=body.task_id)
                            result={'reply':'正在核对原任务，只继续未完成部分。','task_id':queued}
                        else:
                            from .partial_completion import choose
                            result,queued=choose(agent,db,body.task_id,conversation,body.choice,body.version,body.order_id)
                            result['reply']='已记录你的选择，请核对后续卡片。'
                    elif body.action in {'human_end','rating'}:
                        row=db.execute('SELECT * FROM messages WHERE id=? AND conversation_id=?',(body.message_id,conversation)).fetchone()
                        if not row:raise BankError('人工服务卡片不存在。')
                        data=json.loads(row['data_json']);key=data.get('human_session_id')
                        if not key:raise BankError('人工服务卡片已失效。')
                        if body.action=='human_end':
                            human_service.close(db,key,'用户');result={'reply':'本次人工服务已结束。'}
                        else:
                            if 'handoff_rating' not in data or not body.half_stars:raise BankError('评分参数不正确。')
                            data['handoff_rating']={'half_stars':body.half_stars};update_message(db,row['id'],data)
                            result={'reply':f'评分已保存：{body.half_stars/2:g}星。'}
                    else:
                        if not body.approval_id or not body.decision:raise BankError('确认卡片参数不完整。')
                        approval=db.execute('SELECT t.* FROM tasks t JOIN approvals a ON a.task_id=t.id WHERE a.id=?',(body.approval_id,)).fetchone()
                        if not approval:raise BankError('找不到当前飞书交易。')
                        scoped_task(db,approval['id'],approval['conversation_id'])
                        result,queued=agent.confirm_in_transaction(db,body.approval_id,body.decision,approval['conversation_id'])
                        result['reply']={'executed':'操作已完成','cancelled':'已取消','expired':'卡片已过期','failed':'操作未完成'}.get(result['status'],'已处理')
                elif text:
                    if re.fullmatch(r'(?:请|帮我)?(?:停止|暂停|停止任务|暂停任务|取消任务|别继续了)[。！!]*',text):
                        task=agent.active(db,conversation)
                        if not task:result={'reply':'当前没有正在执行的任务，已完成交易不会撤销。'}
                        else:
                            scoped_task(db,task['id'],conversation)
                            from .interruption import interrupt_in_transaction
                            interrupt_in_transaction(agent,db,task['id'],conversation);stopped=task['id']
                            result={'reply':'已停止后续步骤，请核对执行与资金结果。'}
                    else:
                        queued=agent.submit_in_transaction(db,text,'feishu',conversation)
                        saved=json.loads(db.execute('SELECT state_json FROM tasks WHERE id=?',(queued,)).fetchone()[0])
                        stopped=saved.get('stopped_task_id')
                        # The durable receipt precedes task progress/results in the same sender queue.
                        # Empty inline reply prevents the bridge sending a second acknowledgement.
                        result={'task_id':queued,'reply':'','receipt_queued':True}
                else:result={'reply':'目前支持文本消息，请直接描述你的需求。'}
            except BankError as exc:result={'reply':str(exc)}
        db.execute('INSERT INTO feishu_inbox VALUES(?,?)',(inbox,dumps(result)))
        if queued:
            state=db.execute('SELECT status FROM tasks WHERE id=?',(queued,)).fetchone()
            if not state or state[0]!='queued':queued=None
    if stopped:
        running=agent.running_tasks.get(stopped)
        if running and not running.done():running.cancel()
    if queued:agent.queue.put_nowait(queued)
    await agent.notify()
    return result
