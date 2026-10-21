"""Recipient-scoped delivery; binding changes never retarget old messages."""
import json
from .db import dumps


def recipient(db):
    row=db.execute('SELECT tenant_key,open_id,chat_id FROM feishu_binding WHERE id=1').fetchone()
    nonce=db.execute("SELECT value FROM meta WHERE key='feishu_binding_nonce'").fetchone()
    return {**dict(row),'generation':nonce[0] if nonce else ''} if row else None


def task_recipient(db, task):
    state=json.loads(task['state_json'])
    return state.get('feishu_recipient') or recipient(db)


def enqueue(db,message,destination):
    if not destination:return
    old=db.execute('SELECT payload_json FROM feishu_outbox WHERE message_id=?',(message['id'],)).fetchone()
    if old:
        old_destination=json.loads(old[0]).get('_feishu_recipient')
        if old_destination and old_destination!=destination:return
    payload={**message,'_feishu_recipient':destination}
    db.execute("""INSERT INTO feishu_outbox(message_id,payload_json,status) VALUES(?,?,'pending')
        ON CONFLICT(message_id) DO UPDATE SET payload_json=excluded.payload_json,status='pending',attempts=0""",(message['id'],dumps(payload)))


def queue_message(db,message):
    if message['role']!='assistant':return
    task=db.execute('SELECT * FROM tasks WHERE id=?',(message['task_id'],)).fetchone() if message['task_id'] else None
    destination=None
    if task and task['channel']=='feishu':destination=task_recipient(db,task)
    elif message['data'].get('_feishu_notify'):
        preference=db.execute("SELECT value FROM meta WHERE key='feishu_notifications'").fetchone()
        destination=recipient(db) if not preference or preference[0]!='0' else None
    elif message['data'].get('human_session_id'):
        # Only the service actually opened from Feishu is mirrored. Other Web
        # conversations and unrelated human services remain private to Web.
        origin=db.execute("""SELECT t.* FROM tasks t JOIN messages m ON m.task_id=t.id
            WHERE t.channel='feishu' AND json_extract(m.data_json,'$.human_session_id')=?
            ORDER BY t.rowid LIMIT 1""",(message['data']['human_session_id'],)).fetchone()
        if origin:destination=task_recipient(db,origin)
    enqueue(db,message,destination)


def progress(db,task):
    if task['channel']!='feishu':return
    state=json.loads(task['state_json'])
    if state.get('handoff'):return
    nodes=state.get('dag',{}).get('nodes',[])
    current=next((n['title'] for n in nodes if n['status'] in {'running','waiting_confirmation'}),'正在处理需求')
    enqueue(db,{'id':'fsprogress_'+task['id'],'task_id':task['id'],'conversation_id':task['conversation_id'],
        'role':'assistant','kind':'feishu_task','text':current,
        'data':{'task_id':task['id'],'conversation_id':task['conversation_id'],'status':task['status'],'stage':current}},task_recipient(db,task))
