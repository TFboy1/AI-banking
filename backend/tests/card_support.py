async def add_card_for_test(client, card, pin='2468'):
    """Use the real approval and verification API when preparing a linked card."""
    conversation=(await client.post('/api/conversations',json={'title':'银行卡测试'})).json()['conversation_id']
    response=await client.post('/api/cards',json={**card,'conversation_id':conversation})
    assert response.status_code==200,response.text
    approval=response.json()['data']['approval_id']
    body={'decision':'confirm','conversation_id':conversation}
    response=await client.post(f'/api/approvals/{approval}/decision',json=body)
    if response.status_code==428:
        sms=(await client.post(f'/api/approvals/{approval}/sms',json={'conversation_id':conversation})).json()
        response=await client.post(f'/api/approvals/{approval}/decision',json={**body,'pin':pin,'sms_code':sms['demo_code']})
    assert response.status_code==200,response.text
    assert response.json()['status']=='executed',response.text
    return response.json()['receipt']['card']
