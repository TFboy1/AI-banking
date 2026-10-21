import asyncio
import json
from banking.feishu_actions import handle, dedicated_conversation
from banking.main import FeishuBody
from conftest import ScriptedModel, tool


async def test_feishu_messages_have_their_own_persistent_conversation(agent):
    with agent.db.transaction() as db:
        original = agent.db.conversation_id(db)
        db.execute("INSERT INTO feishu_binding VALUES(1,'tenant','owner','chat')")
    agent.model = ScriptedModel([[tool('get_accounts')], '查询完成。', [tool('get_accounts')], '查询完成。'])
    ids=[]
    for index in range(2):
        request=FeishuBody(event_id=f'dedicated{index}',kind='message',
            tenant_key='tenant',open_id='owner',chat_id='chat',chat_type='p2p',text='查询余额')
        result=await handle(agent,agent.db,request)
        assert result['receipt_queued'] and not result['reply']
        assert (await handle(agent,agent.db,request))['duplicate']
        ids.append(result['task_id'])
        await asyncio.wait_for(agent.queue.join(),5)
    with agent.db.connection() as db:
        rows=db.execute('SELECT conversation_id FROM tasks WHERE id IN (?,?)',ids).fetchall()
        assert len(rows)==2 and rows[0][0]==rows[1][0] and rows[0][0]!=original
        conversation=rows[0][0]
        assert db.execute('SELECT title FROM conversations WHERE id=?',(conversation,)).fetchone()[0]=='飞书'
        assert agent.db.conversation_id(db)==original
        assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='user'",(original,)).fetchone()[0]==0
        outbox=[json.loads(row[0]) for row in db.execute('SELECT payload_json FROM feishu_outbox ORDER BY rowid')]
        assert outbox[0]['kind']=='feishu_receipt'
        assert outbox[0]['text']=='有数已收到，正在处理你的需求。'
        assert sum(row['kind']=='feishu_receipt' for row in outbox)==2


async def test_deleted_feishu_chat_is_recreated_without_reviving_old_history(agent):
    with agent.db.transaction() as db:
        first=dedicated_conversation(agent.db,db)
        assert dedicated_conversation(agent.db,db)==first
        db.execute("UPDATE conversations SET deleted_at='deleted' WHERE id=?",(first,))
        second=dedicated_conversation(agent.db,db)
        assert second!=first
        assert db.execute('SELECT deleted_at FROM conversations WHERE id=?',(first,)).fetchone()[0]=='deleted'
        assert db.execute('SELECT title FROM conversations WHERE id=?',(second,)).fetchone()[0]=='飞书'
