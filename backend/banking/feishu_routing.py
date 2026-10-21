"""Resolve a Feishu identity or one-use binding code to exactly one wallet."""
import json
import secrets
from datetime import datetime
from pathlib import Path
from fastapi import Request
from fastapi.responses import JSONResponse
from .db import Database,TZ
from .security import token_hash


async def dispatch(directory,scope,receive,send):
    request=Request(scope,receive)
    expected='Bearer '+directory.settings.feishu_bridge_token
    if not directory.settings.feishu_ready or not secrets.compare_digest(request.headers.get('authorization',''),expected):
        return await JSONResponse({'detail':'飞书桥接未启用或凭证不正确。'},status_code=403)(scope,receive,send)
    raw=await request.body()
    try:
        body=json.loads(raw)
        if len(raw)>20000 or not isinstance(body,dict) or any(not isinstance(body.get(k),str) for k in ('tenant_key','open_id','chat_id','kind')):raise ValueError()
    except (ValueError,TypeError):
        return await JSONResponse({'detail':'飞书事件格式不正确。'},status_code=422)(scope,receive,send)
    async with directory.feishu_lock:
        with directory.connection() as db:users=[dict(r) for r in db.execute('SELECT * FROM users')]
        text=body.get('text','')
        binding_request=body['kind']=='message' and isinstance(text,str) and text.strip().startswith(('绑定 ','/绑定 '))
        code=token_hash(text.strip().split(maxsplit=1)[1].strip().upper()) if binding_request else None
        bound=[];codes=[]
        for user in users:
            with Database(Path(user['database_path'])).connection() as db:
                row=db.execute('SELECT * FROM feishu_binding WHERE id=1').fetchone()
                if row and row['tenant_key']==body['tenant_key'] and row['open_id']==body['open_id']:
                    bound.append((user,row['chat_id']==body['chat_id']))
                match=db.execute('SELECT expires_at FROM binding_codes WHERE code_hash=?',(code,)).fetchone() if code else None
                if match and datetime.fromisoformat(match[0])>datetime.now(TZ):codes.append(user)
        user=None;reply='请先在有数的连接设置生成绑定码，在本私聊发送“绑定 绑定码”。'
        if binding_request:
            if len(codes)!=1:
                reply='绑定码无效或已过期，请在有数重新生成。'
                if not codes and len(bound)==1 and bound[0][1]:user=bound[0][0]
            elif bound and (len(bound)!=1 or bound[0][0]['id']!=codes[0]['id']):reply='这个飞书账号已关联其他有数账号，请先在原账号解除关联。'
            else:user=codes[0]
        elif len(bound)==1 and bound[0][1]:user=bound[0][0]
        if not user:return await JSONResponse({'reply':reply})(scope,receive,send)
        application=await directory.application(user)
        delivered=False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered=True
                return {'type':'http.request','body':raw,'more_body':False}
            return await receive()
        return await application(scope,replay,send)
