"""User-controlled settlement of partially completed tasks. No automatic reversal."""
import json
from .bank import BankError
from .db import add_message,dumps,now,update_message,event


def payload(agent,db,task,previous=None):
    from .interruption import completed_operations
    completed,debit,credit=completed_operations(db,task['id'])
    completed=[o for o in completed if o['tool'] not in {'list_subscriptions','quote_subscriptions'}]
    if not completed:return None
    saved=json.loads(task['state_json']);old=previous or {}
    items=[]
    for item in completed:
        receipt=item['receipt'];order_id=receipt.get('order_id') if item['tool']=='place_travel_order' else None
        order_status=None;can_cancel=False;refund_status=None;refunded=0
        refund_id=old.get('refund_approvals',{}).get(order_id)
        if order_id:
            from .travel_changes import current
            try:
                order=current(db,order_id);order_status=order['status_code'];refunded=order.get('refunded_minor',0)
                approval=db.execute('SELECT status FROM approvals WHERE id=?',(refund_id,)).fetchone() if refund_id else None
                refund_status=approval[0] if approval else None
                can_cancel=order_status=='active' and refund_status!='pending'
            except BankError:order_status='needs_review'
        items.append({k:item[k] for k in ('approval_id','tool','title','debit_minor','credit_minor','settlement')} |
                     {'order_id':order_id,'order_status':order_status,'can_cancel':can_cancel,'refund_status':refund_status,'refunded_minor':refunded})
    unfinished=[{'title':n['title'],'status':n['status']} for n in saved.get('dag',{}).get('nodes',[]) if n['status']!='succeeded']
    decision='kept' if saved.get('partial_resolution')=='kept' else 'replanning' if saved.get('resumed_by') else old.get('decision','pending')
    return {'task_id':task['id'],'version':old.get('version',1),'decision':decision,
            'reason':task['error'] or '后续步骤未完成','completed':items,'unfinished':unfinished,
            'debit_minor':debit,'credit_minor':credit,'refund_approvals':old.get('refund_approvals',{}),
            'can_replan':not any(i['order_status'] in {'cancelled','needs_review'} for i in items),
            'note':'已完成操作不会自动撤销。重新规划只处理未完成部分，重新报价、确认和验证；退订是新的确认操作。旅行订单仅支持整单免费退订，股票及已成功转账不提供自动撤销。'}


def ensure(agent,db,task_id):
    task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
    state=json.loads(task['state_json'])
    if state.get('partial_completion_message'):return
    value=payload(agent,db,task)
    if not value:return
    message=add_message(db,task['conversation_id'],task_id,'assistant','data','部分操作已完成，请选择后续处理',
                        {'tool':'partial_completion','result':{'partial_completion':value}})
    state['partial_completion_message']=message['id']
    db.execute('UPDATE tasks SET state_json=? WHERE id=?',(dumps(state),task_id))


def refresh(agent,db,conversation_id):
    for row in db.execute("SELECT m.id,m.task_id,m.data_json FROM messages m WHERE m.conversation_id=? AND json_extract(m.data_json,'$.tool')='partial_completion'",(conversation_id,)).fetchall():
        task=db.execute('SELECT * FROM tasks WHERE id=?',(row['task_id'],)).fetchone()
        if not task:continue
        data=json.loads(row['data_json']);old=data['result']['partial_completion'];value=payload(agent,db,task,old)
        if value and value!=old:
            value['version']=old['version']+1
            data['result']['partial_completion']=value;update_message(db,row['id'],data)


def choose(agent,db,task_id,conversation_id,action,version,order_id=None):
    from .account_lock import check
    check(db)
    from .human_service import current as human_current
    human=human_current(db,conversation_id)
    if human and human['status'] in {'waiting','active'}:raise BankError('当前对话已转人工，请先结束人工服务。')
    task=db.execute('SELECT t.* FROM tasks t JOIN conversations c ON c.id=t.conversation_id WHERE t.id=? AND t.conversation_id=? AND c.deleted_at IS NULL',(task_id,conversation_id)).fetchone()
    if not task:raise BankError('找不到当前对话的收尾记录。')
    refresh(agent,db,conversation_id)
    state=json.loads(task['state_json']);message_id=state.get('partial_completion_message')
    row=db.execute('SELECT data_json FROM messages WHERE id=?',(message_id,)).fetchone()
    if not row:raise BankError('本任务没有待处理的部分成功记录。')
    data=json.loads(row[0]);value=data['result']['partial_completion']
    if value['version']!=version:raise BankError('处理状态已变化，请刷新后重试。')
    if value['decision']!='pending':raise BankError('已经选择后续处理，请查看新的处理记录。')
    if agent.active(db,conversation_id):raise BankError('当前对话还有待完成或待确认操作，请先处理。')
    result={};queue=None
    if action=='keep':
        value['decision']='kept';state['partial_resolution']='kept'
        add_message(db,conversation_id,task_id,'assistant','text','已保留完成的操作，结束原任务剩余步骤；没有退款或新增扣款。',{})
    elif action=='replan':
        if not value['can_replan']:raise BankError('成功项目已退订或需要核对，请先结束本任务，再明确提出新的安排。')
        # This is an explicitly selected checkpoint, not whichever task is newest.
        queue=agent.submit_in_transaction(db,'继续',channel=task['channel'],conversation_id=conversation_id,recovery_parent_id=task_id)
        fresh=db.execute('SELECT status FROM tasks WHERE id=?',(queue,)).fetchone()
        if fresh['status']!='queued':raise BankError('此任务已不能再次恢复，请查看后续任务。')
        value['decision']='replanning';state=json.loads(db.execute('SELECT state_json FROM tasks WHERE id=?',(task_id,)).fetchone()[0])
        resumed=json.loads(db.execute('SELECT state_json FROM tasks WHERE id=?',(queue,)).fetchone()[0])
        resumed['partial_preserve_orders']=[i['order_id'] for i in value['completed'] if i['order_id']]
        resumed['messages'][0]['content']+='\n用户在部分成功收尾卡选择重新规划：只重新核对并推荐未完成项目，保留已成功订单和操作。需要改变成功项目时先询问，不得替换、退订或重复购买。'
        db.execute('UPDATE tasks SET state_json=? WHERE id=?',(dumps(resumed),queue))
        result['task_id']=queue
    elif action=='cancel_order':
        item=next((i for i in value['completed'] if i['order_id']==order_id and i['can_cancel']),None)
        if not item:raise BankError('该项目不可从本记录退订，请先核对订单。')
        from .travel_changes import current
        order=current(db,order_id)
        message=agent.create_direct_approval(db,conversation_id,'cancel_travel_order',{'order_id':order_id,'order_version':order['version']},'请核对整单免费退订与退款去向，确认并验证后才退款。',channel=task['channel'])
        value['refund_approvals'][order_id]=message['data']['approval_id'];result['message']=message
        add_message(db,conversation_id,task_id,'assistant','text','已生成退订确认卡，当前尚未退款；可确认退订，也可取消本次退订。',{})
    else:raise BankError('不支持的收尾操作。')
    value['version']+=1;data['result']['partial_completion']=value
    db.execute('UPDATE tasks SET state_json=? WHERE id=?',(dumps(state),task_id));update_message(db,message_id,data)
    refresh(agent,db,conversation_id)
    event(db,conversation_id,'task.partial_resolved',{'task_id':task_id,'action':action,'order_id':order_id,'queued_task_id':queue})
    return result,queue


def protect_preserved_orders(db,state,name,args):
    preserved=state.get('partial_preserve_orders',[])
    if not preserved or name not in {'cancel_travel_order','change_travel_order'}:return
    order_id=args.get('order_id')
    if name=='change_travel_order':
        row=db.execute('SELECT payload FROM travel_change_quotes WHERE id=?',(args.get('quote_id'),)).fetchone()
        order_id=json.loads(row[0]).get('order_id') if row else None
    if order_id in preserved:raise BankError('本次只重新安排未完成项目，已成功订单保持不变；如需退改，请结束当前处理并明确提出新的需求。')
