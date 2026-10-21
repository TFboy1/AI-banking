"""Persistent, conversation-scoped human service. Operators cannot execute banking tools."""
import json
from .db import add_message, dumps, event, message_dict, now, uid
from .bank import BankError

SCHEMA = """
CREATE TABLE IF NOT EXISTS human_sessions(
 id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,status TEXT NOT NULL,
 checkpoint TEXT,operator TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,closed_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS human_active ON human_sessions(conversation_id) WHERE status IN ('waiting','active');
CREATE TABLE IF NOT EXISTS human_audit(id TEXT PRIMARY KEY,session_id TEXT,action TEXT NOT NULL,operator TEXT NOT NULL,created_at TEXT NOT NULL);
"""

def current(db, conversation):
    row = db.execute('SELECT * FROM human_sessions WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1', (conversation,)).fetchone()
    return dict(row) if row else None

def audit(db, session_id, action, operator):
    db.execute('INSERT INTO human_audit VALUES(?,?,?,?,?)', (uid('ha_'), session_id, action, operator, now()))

def task(db, conversation, checkpoint=None, channel='web'):
    key = uid('task_')
    state={'handoff':True,'handoff_checkpoint':checkpoint}
    if channel=='feishu':
        from .feishu_channels import recipient
        state['feishu_recipient']=recipient(db)
    db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?)', (key, conversation, 'completed', channel, dumps(state), None, now()))
    event(db, conversation, 'task', {'id':key,'conversation_id':conversation,'status':'completed'})
    return key

def open_session(db, conversation, checkpoint=None):
    previous = current(db, conversation)
    if previous and previous['status'] in ('waiting','active'):
        return previous
    key = uid('human_')
    db.execute('INSERT INTO human_sessions VALUES(?,?,?,?,?,?,?,?)', (key, conversation, 'waiting', checkpoint, None, now(), now(), None))
    audit(db,key,'requested','用户')
    return current(db,conversation)

def user_message(db, conversation, text, channel='web'):
    service = current(db, conversation)
    if not service or service['status'] not in ('waiting','active'):
        raise BankError('当前对话未在人工服务中。')
    key = task(db,conversation,service['checkpoint'],channel)
    add_message(db,conversation,key,'user','text',text,{'channel':channel,'human_session_id':service['id']})
    db.execute('UPDATE human_sessions SET updated_at=? WHERE id=?',(now(),service['id']))
    return key

def valid(db, key):
    row = db.execute('SELECT h.* FROM human_sessions h JOIN conversations c ON c.id=h.conversation_id WHERE h.id=? AND c.deleted_at IS NULL',(key,)).fetchone()
    if not row:
        raise BankError('人工会话不存在或对话已删除。')
    return dict(row)

def claim(db,key,operator):
    service=valid(db,key)
    if service['status']=='closed':
        raise BankError('该人工服务已结束。')
    if service['status']=='waiting':
        db.execute("UPDATE human_sessions SET status='active',operator=?,updated_at=? WHERE id=?",(operator,now(),key))
        add_message(db,service['conversation_id'],None,'assistant','text','人工客服已接入，可以直接在这里继续交流。',{'source':'human','operator':operator,'human_session_id':key})
        audit(db,key,'claimed',operator)
    return valid(db,key)

def reply(db,key,text,operator):
    service=valid(db,key)
    if service['status']!='active':
        raise BankError('请先接入该对话，已结束的服务不能继续回复。')
    result=add_message(db,service['conversation_id'],None,'assistant','text',text,{'source':'human','operator':operator,'human_session_id':key})
    db.execute('UPDATE human_sessions SET updated_at=? WHERE id=?',(now(),key))
    audit(db,key,'replied',operator)
    return result

def close(db,key,operator):
    service=valid(db,key)
    if service['status']=='closed':
        return service
    db.execute("UPDATE human_sessions SET status='closed',closed_at=?,updated_at=? WHERE id=?",(now(),now(),key))
    task_id=task(db,service['conversation_id'],service['checkpoint'])
    add_message(db,service['conversation_id'],task_id,'assistant','text','本次人工服务已结束，后续消息由有数助手处理。',{'human_session_id':key})
    add_message(db,service['conversation_id'],task_id,'assistant','data','请给人工打分。',{'handoff_rating':{'half_stars':None},'human_session_id':key})
    audit(db,key,'closed',operator)
    return valid(db,key)

def history(db,conversation):
    return [message_dict(row) for row in db.execute('SELECT * FROM messages WHERE conversation_id=? ORDER BY rowid',(conversation,))]
