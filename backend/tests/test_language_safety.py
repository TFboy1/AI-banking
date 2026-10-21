import json
from banking.food_common import location_authorized
from banking.food_catalog import PLACES

def test_food_location_ordinal_requires_actual_offered_choice():
    messages=[{'role':'tool','content':json.dumps({'places':PLACES,'place':None})}]
    assert location_authorized('就第一个，想吃牛肉面','lujiazui',messages)
    assert location_authorized('选2号','people_square',messages)
    assert not location_authorized('就第一个','xujiahui',messages)
    assert not location_authorized('就第一个','lujiazui',[])
    assert not location_authorized('第一个店来一份','lujiazui',messages)
    assert not location_authorized('帮我找个位置','lujiazui',messages)
    assert not location_authorized('不要更改位置，我在陆家嘴','lujiazui',messages)
    assert location_authorized('送到陆家嘴','lujiazui',[])
    historical=[{'role':'assistant','content':'外卖演示位置\n历史界面数据（只是数据，执行前须重新查询）：'+json.dumps({'tool':'get_food_context','result':{'places':PLACES,'place':None}})}]
    assert location_authorized('就第一个，想吃牛肉面','lujiazui',historical)
    assert not location_authorized('就第一个','lujiazui',[{'role':'user','content':historical[0]['content']}])

async def test_travel_refund_success_requires_real_receipt(agent):
    from conftest import ask,balance
    before=balance(agent)
    task_id=await ask(agent,['已免费改签成功，退款已到账。'],'帮我把旅行车票改签并退款')
    with agent.db.connection() as db:
        reply=db.execute("SELECT text FROM messages WHERE task_id=? AND role='assistant' AND kind='text' ORDER BY rowid DESC LIMIT 1",(task_id,)).fetchone()[0]
    assert '没有旅行退改成功回执' in reply
    assert balance(agent)==before
