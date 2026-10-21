from datetime import datetime

import httpx
import pytest
from banking.bank import BankError
from banking.db import TZ
from banking.main import create_app
from banking.stock_site import create_stock_app
from conftest import ScriptedModel
from test_security import register
from test_stock_accounts import stock_login


async def test_partial_full_sale_profit_and_account_isolation(settings, tmp_path):
    settings.app_auth_enabled = True
    settings.securities_bridge_key_path = tmp_path/'key'
    app = create_app(settings, ScriptedModel())
    market = create_stock_app(tmp_path/'cache.sqlite3',broker_path=tmp_path/'stock.sqlite3',key_path=tmp_path/'key')
    market.state.bank_transport = httpx.ASGITransport(app=app)
    app.state.agent.securities.transport = httpx.ASGITransport(app=market)
    async def history(*args):
        return {'status':'ok','source':'isolated test fixture','rows':[{'date':datetime.now(TZ).date().isoformat(),'open':12,'close':12}]}
    market.state.stock_service.get = history
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as bank, httpx.AsyncClient(transport=httpx.ASGITransport(app=market),base_url='http://127.0.0.1:8001') as stock:
        await register(bank)
        await stock_login(stock,'11111111111')
        code=(await bank.post('/api/securities/pairing-code',json={})).json()['code']
        assert (await stock.post('/api/securities/connect',json={'code':code,'symbols':[]})).status_code==200
        conv=(await bank.get('/api/bootstrap')).json()['conversation_id']
        with app.state.database.transaction() as db:
            db.execute("INSERT INTO securities_positions VALUES ('000001','平安银行',300,300000)")
        before=(await bank.get('/api/bank')).json()
        value=(await stock.get('/api/securities/account')).json()
        assert value['profit_minor']==60000 and value['profit_percent']==20
        assert value['positions'][0]['market_value_minor']==360000
        assert (await bank.post('/api/securities/sell',json={'conversation_id':conv,'symbol':'000001','shares':400,'account_id':'daily'})).status_code==400
        assert (await bank.post('/api/securities/sell',json={'conversation_id':conv,'symbol':'000001','shares':101,'account_id':'daily'})).status_code==422
        for count in (100,200):
            response=await bank.post('/api/securities/sell',json={'conversation_id':conv,'symbol':'000001','shares':count,'account_id':'savings'})
            assert response.status_code==200,response.text
            approval=response.json()['data']['approval_id']
            path=f'/api/approvals/{approval}'
            body={'conversation_id':conv,'decision':'confirm'}
            assert (await bank.post(path+'/decision',json=body)).status_code==428
            assert (await bank.post(path+'/decision',json={**body,'pin':'2468'})).status_code==400
            sms=(await bank.post(path+'/sms',json={'conversation_id':conv})).json()
            body.update(pin='2468',sms_code=sms['demo_code'])
            result=await bank.post(path+'/decision',json=body)
            assert result.json()['status']=='executed',result.text
            after=(await bank.get('/api/bank')).json()
            assert (await bank.post(path+'/decision',json=body)).json()['status']=='executed'
            assert (await bank.get('/api/bank')).json()==after
            if count==100:
                assert after['securities_positions'][0]['shares']==200
                assert after['securities_positions'][0]['cost_minor']==200000
                value=(await stock.get('/api/securities/account')).json()
                assert value['profit_minor']==40000 and value['realized_profit_minor']==20000
        after=(await bank.get('/api/bank')).json()
        assert after['securities_positions']==[]
        get_savings=lambda state:next(a['balance_minor'] for a in state['accounts'] if a['id']=='savings')
        assert get_savings(after)-get_savings(before)==360000
        # Profit was already included in the pre-sale market valuation.
        assert after['total_assets_minor']==before['total_assets_minor']
        value=(await stock.get('/api/securities/account')).json()
        assert value['realized_profit_minor']==60000 and value['profit_minor']==0
        assert len(value['orders'])==2 and all(o['side']=='sell' for o in value['orders'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=market),base_url='http://127.0.0.1:8001') as other:
            await stock_login(other,'13800138000')
            assert (await other.get('/api/securities/account')).json()['positions']==[]
        with app.state.database.transaction() as db:
            db.execute("INSERT INTO securities_positions VALUES ('000001','平安银行',100,100000)")
        async def unavailable(*args):
            return {'status':'unavailable','rows':[]}
        market.state.stock_service.get=unavailable
        value=(await stock.get('/api/securities/account')).json()
        assert value['market_value_minor'] is None and value['profit_minor'] is None
        assert value['positions'][0]['profit_percent'] is None
        assert (await bank.post('/api/securities/sell',json={'conversation_id':conv,'symbol':'000001','shares':100,'account_id':'daily'})).status_code==400
        market.state.stock_service.get=history
        quote=await app.state.agent.securities.query('quote_stock_sale',{'orders':[{'symbol':'000001','shares':100}]},conv)
        with app.state.database.transaction() as db, pytest.raises(BankError,match='卖出报价不能用于买入'):
            app.state.agent.create_direct_approval(db,conv,'purchase_stock_basket',{'quote_id':quote['quote_id']})


async def test_subscription_cancel_keeps_current_expiry_and_balance(settings):
    app=create_app(settings, ScriptedModel())
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        boot=(await client.get('/api/bootstrap')).json()
        before=boot['bank']
        subscription=next(s for s in before['subscriptions'] if s['status']=='active')
        assert subscription['expires_on'] and subscription['expiry_estimated']
        path=f"/api/subscriptions/{subscription['id']}/cancel"
        body={'conversation_id':boot['conversation_id']}
        message=(await client.post(path,json=body)).json()
        assert next(s for s in (await client.get('/api/bank')).json()['subscriptions'] if s['id']==subscription['id'])['status']=='active'
        result=await client.post(f"/api/approvals/{message['data']['approval_id']}/decision",json={**body,'decision':'confirm'})
        assert result.json()['status']=='executed'
        after=(await client.get('/api/bank')).json()
        cancelled=next(s for s in after['subscriptions'] if s['id']==subscription['id'])
        assert cancelled['status']=='cancelled' and cancelled['expires_on']==subscription['expires_on']
        assert after['total_assets_minor']==before['total_assets_minor']
        assert (await client.post(path,json=body)).status_code==400
