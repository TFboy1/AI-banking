"""Interruption reports use committed receipts and ledger entries, never model text."""
import json

from . import task_dag
from .bank import CAPABILITIES, BankError, yuan
from .db import add_message, dumps, event, message_dict, now

ACTIVE = {'queued', 'running', 'waiting_confirmation'}


def repair_saved_reports(db, conversation_id):
    """Refresh legacy report amounts only; never replay approvals or money moves."""
    tasks = db.execute("SELECT id,state_json FROM tasks WHERE conversation_id=? AND json_extract(state_json,'$.interruption') IS NOT NULL AND COALESCE(json_extract(state_json,'$.interruption.ledger_version'),0)<2", (conversation_id,)).fetchall()
    for task in tasks:
        state = json.loads(task['state_json'])
        report = state['interruption']
        operations, debit, credit = completed_operations(db, task['id'])
        report.update(ledger_version=2, completed_operations=operations, debit_minor=debit, credit_minor=credit)
        db.execute('UPDATE tasks SET state_json=? WHERE id=?', (dumps(state), task['id']))
        for message in db.execute("SELECT id,data_json FROM messages WHERE task_id=? AND json_extract(data_json,'$.tool')='interrupt_task'", (task['id'],)).fetchall():
            data = json.loads(message['data_json'])
            result = data['result']
            result['interruption'] = report
            display = result['display']
            for metric in display['metrics']:
                if metric['label'] == '已扣款':
                    metric['value'] = yuan(debit)
                elif metric['label'] == '已到账':
                    metric['value'] = yuan(credit)
            if operations:
                display['rows'] = [[o['title'], o['settlement'], o['receipt'].get('action_id','—')] for o in operations]
            db.execute('UPDATE messages SET data_json=? WHERE id=?', (dumps(data), message['id']))


def interrupt_in_transaction(agent, db, task_id, conversation_id, announce=True):
    task = db.execute('SELECT t.* FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE t.id=? AND c.deleted_at IS NULL',(task_id,)).fetchone()
    if not task or task['conversation_id'] != conversation_id:
        raise BankError('找不到当前会话的任务。')
    repair_saved_reports(db, conversation_id)
    task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    state = json.loads(task['state_json'])
    if state.get('interruption'):
        message = db.execute("SELECT * FROM messages WHERE task_id=? AND json_extract(data_json,'$.tool')='interrupt_task' ORDER BY rowid DESC LIMIT 1",(task_id,)).fetchone()
        return {'task':task_dag.public_task(task), 'interruption':state['interruption'], 'message':message_dict(message) if message else None}
    nodes = state.get('dag',{}).get('nodes',[])
    active = next((n for n in nodes if n['status'] in {'running','waiting_confirmation'}), None)
    phase = state.get('execution_phase')
    stage = '等待开始' if task['status']=='queued' else '正在理解需求或规划下一步' if phase=='planning' else active['title'] if active else '正在处理任务'
    finished_nodes = [n for n in nodes if n['status']=='succeeded']
    if task['status']=='running' and phase=='planning' and finished_nodes:
        stage = '已完成' + finished_nodes[-1]['title'] + '，正在规划下一步'
    if task['status']=='waiting_confirmation':
        stage = '等待你确认：' + (active['title'] if active else '业务操作')
    interrupted = task['status'] in ACTIVE
    if not interrupted:
        stage = '任务已经结束'
    uncertain = bool(interrupted and active and active['status']=='running' and active['tool'] in CAPABILITIES and CAPABILITIES[active['tool']].effect=='auto')
    if uncertain:
        active.update(status='uncertain',finished_at=now())
    completed, debit, credit = completed_operations(db, task_id)
    pending = [n['title'] for n in nodes if n['status'] in {'pending','running','waiting_confirmation','uncertain'}]
    report = {'ledger_version':2, 'requested_at':now(), 'interrupted':interrupted, 'status_at_request':task['status'],
              'stage':stage, 'completed_steps':[{'id':n['id'],'title':n['title']} for n in nodes if n['status']=='succeeded'],
              'stopped_steps':pending, 'completed_operations':completed, 'debit_minor':debit, 'credit_minor':credit,
              'external_result_uncertain':uncertain,
              'funds_status':'committed_operations_preserved' if completed else 'no_funds_operation_executed'}
    failure_reason = '用户停止任务，未完成的交易失败，未执行扣款或入账。'
    if interrupted:
        for approval in db.execute("SELECT * FROM approvals WHERE task_id=? AND status='pending'",(task_id,)).fetchall():
            db.execute("UPDATE approvals SET status='failed' WHERE id=?",(approval['id'],))
            agent._update_approval_card(db,approval,'failed',error=failure_reason)
        for node in nodes:
            if node['status'] in {'pending','running','waiting_confirmation'} and node['tool'] in CAPABILITIES and CAPABILITIES[node['tool']].requires_confirmation:
                node.update(status='failed',error=failure_reason,finished_at=now())
        task_dag.close(state,'cancelled')
        state['pending_calls'] = []
        db.execute("UPDATE tasks SET status='failed',error=? WHERE id=?",(failure_reason,task_id))
    state['interruption'] = report
    db.execute('UPDATE tasks SET state_json=? WHERE id=?',(dumps(state),task_id))
    agent._task_event(db,task_id)
    title = '任务已中断 · 执行与资金核对' if interrupted else '任务已结束 · 执行与资金核对'
    note = '后续步骤已停止；未完成的交易按失败处理，不再执行扣款或入账。已完成操作保持不变，停止任务不会撤销成功交易。' if interrupted else '请求停止前任务已结束，请以这些已保存的回执为准。'
    note += ' 金额为本任务及恢复前步骤已落账的流水合计。'
    if interrupted:
        note += ' 进度已保存，输入“继续”可重新核对并处理剩余步骤。'
    if interrupted and pending:
        note += ' 已停止步骤：' + '、'.join(pending[:8]) + ('等' if len(pending)>8 else '') + '。'
    if uncertain:
        note += ' 外部非资金请求可能已处理但响应尚未返回，请重新查询核对；本报告不将其判为成功或失败。'
    display = {'title':title, 'columns':['已完成操作','资金状态','回执编号'],
               'rows':[[r['title'],r['settlement'],r['receipt'].get('action_id','—')] for r in completed] or [['本任务无已执行资金变更','没有本任务的扣款或入账记录；外部非资金结果待核查' if uncertain else '没有本任务的扣款或入账记录','—']],
               'metrics':[{'label':'停止位置','value':stage},{'label':'已扣款','value':yuan(debit)},{'label':'已到账','value':yuan(credit)}], 'note':note}
    message = add_message(db,conversation_id,task_id,'assistant','data',title,{'tool':'interrupt_task','result':{'display':display,'interruption':report}}) if announce else None
    event(db,conversation_id,'task.interrupted',{'task_id':task_id,'interruption':report})
    task = db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    return {'task':task_dag.public_task(task), 'interruption':report, 'message':message}


def completed_operations(db, task_id):
    from .recovery import task_lineage
    task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    tasks = task_lineage(db, task) if task else [task_id]
    completed = []
    debit = credit = 0
    placeholders = ','.join('?' for _ in tasks)
    for approval in db.execute(f"SELECT * FROM approvals WHERE task_id IN ({placeholders}) AND status='executed' ORDER BY rowid",tasks):
        receipt = json.loads(approval['receipt_json'])
        # Older scheduling receipts can omit presentation fields. Preserve the
        # completed operation when building recovery/partial-completion reports.
        title=receipt.get('title') or json.loads(approval['summary_json']).get('title') or '操作已完成'
        # Business adapters append their own suffixes (:stocks, :food, etc.).
        # Match the exact operation namespace, without LIKE wildcard semantics.
        prefix = approval['id'] + ':'
        ledger = [dict(r) for r in db.execute("SELECT id,account_id,direction,amount_minor,category,occurred_at FROM transactions WHERE action_id=? OR substr(action_id,1,?)=? ORDER BY rowid",(approval['id'],len(prefix),prefix))]
        out = sum(r['amount_minor'] for r in ledger if r['direction']=='out')
        incoming = sum(r['amount_minor'] for r in ledger if r['direction']=='in')
        debit += out
        credit += incoming
        target = next((f['value'] for f in receipt.get('fields',[]) if f['label'] in {'到账账户','收款人','收款账户'}), '目标账户')
        if receipt.get('schedule') and not ledger:
            settlement='已保存定时授权，尚未执行交易；预约可在定时交易中取消。'
        elif out and incoming:
            settlement = f'已扣款 {yuan(out)}，{target}已到账 {yuan(incoming)}（模拟）'
        elif out and approval['tool_name']=='transfer':
            settlement = f'已扣款 {yuan(out)}，已取得模拟转账成功回执；未提供收款方独立到账记录'
        elif out:
            settlement = f'已扣款 {yuan(out)}，操作已完成（模拟）'
        elif incoming:
            settlement = f'已到账 {yuan(incoming)}（模拟）'
        else:
            settlement = '操作已完成，无转账扣款或入账流水'
        completed.append({'approval_id':approval['id'], 'tool':approval['tool_name'], 'title':title, 'receipt':receipt, 'ledger':ledger, 'debit_minor':out, 'credit_minor':incoming, 'settlement':settlement})
    return completed, debit, credit
