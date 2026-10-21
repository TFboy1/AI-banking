"""Persistent, incremental DAG scheduling; business permissions stay in Agent."""
from .db import now

TITLES = {'get_bill_analysis_data':'准备脱敏账单','run_bill_analysis_code':'受限计算与图表核对','get_birthday_context':'核对生日联系人','preview_birthday_plan':'预览生日预算','create_birthday_plan':'确认生日计划','list_birthday_plans':'查询生日计划','manage_birthday_plan':'管理生日计划',
    'schedule_transfer':'提前授权定时转账','list_scheduled_transfers':'查询定时转账','cancel_scheduled_transfer':'取消定时转账',
    'get_accounts':'查询账户', 'find_recipients':'查找收款人', 'transfer':'转账',
    'list_subscriptions':'查询订阅', 'quote_subscriptions':'计算订阅费用', 'cancel_subscription':'取消订阅',
    'get_stock_price':'查询股价', 'get_securities_watchlist':'查询自选股票',
    'get_securities_positions':'查询证券持仓', 'quote_stock_purchase':'计算股票买入报价',
    'purchase_stock_basket':'买入股票', 'quote_stock_sale':'计算股票卖出报价', 'sell_stock_basket':'卖出股票',
    'list_products':'查询理财产品', 'purchase_product':'申购理财', 'redeem_product':'赎回理财',
    'list_contacts':'查询联系人', 'create_contact':'添加联系人', 'analyze_bills':'分析账单',
    'add_bank_card':'添加银行卡', 'list_cards':'查询银行卡', 'set_card_status':'修改卡片状态', 'set_card_limit':'调整限额',
    'create_account_category':'创建账户分类', 'transfer_own_funds':'本人资金划转',
    'quote_travel_change':'计算免费改签差价','change_travel_order':'免费改签旅行订单','cancel_travel_order':'免费退订旅行订单',
    'get_travel_context':'核对旅行账户', 'search_travel_options':'推荐交通与酒店', 'quote_travel_order':'计算旅行方案总价', 'place_travel_order':'支付旅行预订', 'get_travel_orders':'查询旅行订单',
    'search_food_shops':'搜索附近店铺', 'get_food_menu':'查询菜单', 'quote_food_order':'计算订单金额',
    'place_food_order':'支付外卖订单', 'get_food_orders':'查询外卖订单',
}
# Only genuinely independent read operations can fan out within one model round.
INDEPENDENT = {'get_accounts','find_recipients','list_subscriptions','list_products','list_cards',
               'list_contacts','get_stock_price','get_securities_watchlist','get_securities_positions',
               'search_securities_stocks','get_food_context','search_food_shops','get_food_menu','get_food_orders','get_travel_context','search_travel_options','get_travel_orders'}
TERMINAL = {'succeeded','failed','cancelled','blocked','needs_input','uncertain'}


def graph(state):
    return state.setdefault('dag', {'version':1, 'mode':'incremental', 'nodes':[]})


def validate(plan):
    nodes = plan['nodes']
    ids = [n['id'] for n in nodes]
    if len(ids) != len(set(ids)) or len(ids) > 64:
        raise ValueError('任务节点重复或超过上限。')
    known = set(ids)
    if any(not set(n['depends_on']) <= known for n in nodes):
        raise ValueError('任务依赖指向不存在的节点。')
    remaining = {n['id']:set(n['depends_on']) for n in nodes}
    while remaining:
        ready = {key for key,deps in remaining.items() if not deps}
        if not ready:
            raise ValueError('任务存在循环依赖，已阻止执行。')
        remaining = {key:deps-ready for key,deps in remaining.items() if key not in ready}


def add_calls(state, calls):
    plan = graph(state)
    existing = {n['id'] for n in plan['nodes']}
    new = [c for c in calls if c['id'] not in existing]
    referenced = {dep for n in plan['nodes'] for dep in n['depends_on']}
    barrier = [n['id'] for n in plan['nodes'] if n['id'] not in referenced]
    reads = []
    for call in new:
        name = call['function']['name']
        independent = name in INDEPENDENT
        deps = list(dict.fromkeys(barrier + ([] if independent else reads)))
        plan['nodes'].append({'id':call['id'], 'tool':name, 'title':TITLES.get(name,'处理业务步骤'),
                              'depends_on':deps, 'status':'pending', 'created_at':now(),
                              'started_at':None, 'finished_at':None, 'receipt_id':None})
        if independent:
            reads.append(call['id'])
        else:
            barrier, reads = [call['id']], []
    validate(plan)


def select(state):
    plan = graph(state)
    validate(plan)
    statuses = {n['id']:n['status'] for n in plan['nodes']}
    ready = {n['id'] for n in plan['nodes'] if n['status']=='pending' and all(statuses[d]=='succeeded' for d in n['depends_on'])}
    for index,call in enumerate(state['pending_calls']):
        if call['id'] in ready:
            state['dag_current'] = call['id']
            mark(state, 'running')
            return state['pending_calls'].pop(index)
    raise ValueError('没有满足依赖条件的步骤，已停止后续执行。')


def mark(state, status, *, receipt=None, error=None, tool=None):
    node = next((n for n in graph(state)['nodes'] if n['id']==state.get('dag_current')), None)
    if node is None:
        return
    if tool:
        node.update(tool=tool, title=TITLES.get(tool,'处理业务步骤'))
    node['status'] = status
    if status == 'running':
        node['started_at'] = node['started_at'] or now()
    if status in TERMINAL:
        node['finished_at'] = now()
    if receipt:
        node['receipt_id'] = receipt.get('action_id')
    if error:
        node['error'] = str(error)[:240]


def close(state, status, error=None):
    if status == 'failed':
        active = next((n for n in graph(state)['nodes'] if n['id']==state.get('dag_current')), None)
        if active and active['status'] not in TERMINAL:
            mark(state, 'failed', error=error)
    elif status == 'completed':
        # A clarification is a completed conversation turn, not an executed trade.
        if any(n['status']=='running' for n in graph(state)['nodes']):
            mark(state, 'needs_input')
    for node in graph(state)['nodes']:
        if node['status'] not in TERMINAL:
            node.update(status='blocked' if status=='failed' else 'cancelled', finished_at=now())


def public_task(row):
    import json
    state = json.loads(row['state_json'])
    return {'id':row['id'], 'conversation_id':row['conversation_id'], 'status':row['status'],
            'error':row['error'], 'dag':state.get('dag'), 'interruption':state.get('interruption')}


def latest(db, conversation_id):
    row = db.execute('SELECT * FROM tasks WHERE conversation_id=? ORDER BY rowid DESC LIMIT 1',(conversation_id,)).fetchone()
    return public_task(row) if row else None
