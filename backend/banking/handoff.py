"""Human handoff, persistent chat and conversation-local half-star feedback."""
import json
import re

from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field

from .bank import BankError
from .db import add_message, dumps, now, uid, update_message, message_dict
from . import human_service as human


def requested(text):
    return bool(re.fullmatch(r'(?:请|我要|我想|帮我|请帮我|麻烦)?(?:转(?:接)?人工(?:客服)?|找人工(?:客服)?|联系人工(?:客服)?|人工客服)(?:一下|吧)?[。！!？?]*', text.strip()))


def create(agent, db, conversation_id, text, channel):
    previous = human.current(db, conversation_id)
    if previous and previous['status'] in ('waiting','active'):
        return human.user_message(db, conversation_id, text, channel)
    active = agent.active(db, conversation_id)
    checkpoint = active['id'] if active else None
    if active:
        from .interruption import interrupt_in_transaction
        interrupt_in_transaction(agent, db, active['id'], conversation_id, announce=False)
    else:
        previous = db.execute('SELECT * FROM tasks WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1', (conversation_id,)).fetchone()
        if previous:
            saved = json.loads(previous['state_json'])
            checkpoint = previous['id'] if previous['status'] in {'failed', 'cancelled'} else saved.get('handoff_checkpoint') if saved.get('handoff') else None
    task_id = uid('task_')
    state = {'handoff': True, 'stopped_task_id': active['id'] if active else None, 'handoff_checkpoint': checkpoint}
    if channel=='feishu':
        from .feishu_channels import recipient
        state['feishu_recipient']=recipient(db)
    db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?)', (task_id, conversation_id, 'completed', channel, dumps(state), None, now()))
    add_message(db, conversation_id, task_id, 'user', 'text', text, {'channel': channel})
    service = human.open_session(db, conversation_id, checkpoint)
    add_message(db, conversation_id, task_id, 'assistant', 'text', '已转交人工客服，正在等待接入。此前的对话记录已提供给客服；等待期间可以继续留言。', {'human_session_id':service['id']})
    agent._task_event(db, task_id)
    return task_id


class RatingBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    conversation_id: str = Field(min_length=1, max_length=100)
    half_stars: int = Field(strict=True, ge=1, le=10)

class ChatBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    conversation_id: str | None = Field(default=None, max_length=100)
    text: str = Field(min_length=1, max_length=4000)

class EndBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    conversation_id: str | None = Field(default=None, max_length=100)


def register(app, database, agent, session, resolve_conversation):
    @app.get('/api/handoff')
    def state(conversation_id: str | None = None, session_key: str = Depends(session)):
        with database.connection() as db:
            conversation = resolve_conversation(db,session_key,conversation_id)
            return {'session':human.current(db,conversation),'messages':human.history(db,conversation)}

    @app.post('/api/handoff/messages')
    async def send(body: ChatBody, session_key: str = Depends(session)):
        if not body.text.strip():
            raise BankError('消息不能为空。')
        with database.transaction() as db:
            conversation=resolve_conversation(db,session_key,body.conversation_id)
            task_id=human.user_message(db,conversation,body.text.strip())
        await agent.notify()
        return {'task_id':task_id}

    @app.post('/api/handoff/end')
    async def end(body: EndBody,session_key: str = Depends(session)):
        with database.transaction() as db:
            conversation=resolve_conversation(db,session_key,body.conversation_id)
            service=human.current(db,conversation)
            if not service:
                raise BankError('当前没有人工会话。')
            result=human.close(db,service['id'],'用户')
        await agent.notify()
        return result

    @app.post('/api/handoffs/{message_id}/rating')
    async def rate(message_id: str, body: RatingBody, session_key: str = Depends(session)):
        with database.transaction() as db:
            conversation = resolve_conversation(db, session_key, body.conversation_id)
            row = db.execute("SELECT * FROM messages WHERE id=? AND conversation_id=? AND role='assistant'", (message_id, conversation)).fetchone()
            if not row:
                raise BankError('找不到当前对话的人工评分。')
            data = json.loads(row['data_json'])
            if 'handoff_rating' not in data:
                raise BankError('这条消息不支持人工评分。')
            data['handoff_rating'] = {'half_stars': body.half_stars, 'updated_at': now()}
            update_message(db, message_id, data)
            message = message_dict(db.execute('SELECT * FROM messages WHERE id=?', (message_id,)).fetchone())
        await agent.notify()
        return message
