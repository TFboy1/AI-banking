"""Tenant-local face enrollment, sequential liveness challenges and single-use grants."""
import asyncio
import json
import os
import secrets
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .bank import BankError, DEBIT_TOOLS
from .db import TZ, dumps, event, now
from .security import token_hash, verify_pin
from .safety import check_user, failure, success
from . import face_engine

SCHEMA = '''
CREATE TABLE IF NOT EXISTS face_mode(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL DEFAULT 1);
INSERT OR IGNORE INTO face_mode VALUES(1,1);
CREATE TABLE IF NOT EXISTS face_preferences(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL DEFAULT 1);
INSERT OR IGNORE INTO face_preferences VALUES(1,1);
CREATE TABLE IF NOT EXISTS face_onboarding(id INTEGER PRIMARY KEY CHECK(id=1),done INTEGER NOT NULL DEFAULT 0);
INSERT OR IGNORE INTO face_onboarding SELECT 1,CASE WHEN EXISTS(SELECT 1 FROM app_identity WHERE id=1) THEN 1 ELSE 0 END;
CREATE TABLE IF NOT EXISTS face_identity(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL DEFAULT 0,template TEXT,updated_at TEXT);
INSERT OR IGNORE INTO face_identity(id) VALUES(1);
CREATE TABLE IF NOT EXISTS face_sms(session_key TEXT PRIMARY KEY,purpose TEXT NOT NULL,binding TEXT NOT NULL,code_hash TEXT NOT NULL,expires REAL NOT NULL,sent REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS face_challenges(id TEXT PRIMARY KEY,session_key TEXT NOT NULL,purpose TEXT NOT NULL,binding TEXT NOT NULL,source_version INTEGER NOT NULL,sequence TEXT NOT NULL,step INTEGER NOT NULL DEFAULT 0,streak INTEGER NOT NULL DEFAULT 0,vector TEXT,last_digest TEXT NOT NULL DEFAULT '',last_at REAL NOT NULL DEFAULT 0,expires REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS face_grants(token_hash TEXT PRIMARY KEY,session_key TEXT NOT NULL,purpose TEXT NOT NULL,binding TEXT NOT NULL,source_version INTEGER NOT NULL,expires REAL NOT NULL,consumed INTEGER NOT NULL DEFAULT 0);
'''
PROMPTS={'center':'请正对镜头，保持稳定','left':'← 请轻轻向左转脸，保持片刻','right':'请轻轻向右转脸，保持片刻 →'}


def server_enabled(db):return bool(db.execute('SELECT enabled FROM face_mode WHERE id=1').fetchone()[0])
def enabled(db):return server_enabled(db) and bool(db.execute('SELECT enabled FROM face_preferences WHERE id=1').fetchone()[0])


def verification_enabled(db):
    return enabled(db) and bool(db.execute('SELECT template FROM face_identity WHERE id=1').fetchone()[0])


def invalidate(db):
    db.execute('DELETE FROM face_grants');db.execute('DELETE FROM face_challenges');db.execute('DELETE FROM face_sms');db.execute('DELETE FROM payment_sms')
    db.execute('UPDATE payment_policy SET version=version+1 WHERE id=1')


def configure(database,value):
    with database.transaction() as db:
        if server_enabled(db)!=bool(value):
            db.execute('UPDATE face_mode SET enabled=? WHERE id=1',(int(value),))
            db.execute('DELETE FROM face_grants');db.execute('DELETE FROM face_challenges');db.execute('DELETE FROM face_sms');db.execute('DELETE FROM payment_sms')
            db.execute('UPDATE payment_policy SET version=version+1 WHERE id=1')
            event(db,'*','face.configuration_changed',{'enabled':bool(value)})


def version(db):
    row=db.execute('SELECT version FROM face_identity WHERE id=1').fetchone()
    return row[0] if row else 0


def identity_binding(db):
    person=db.execute('SELECT phone,pin_hash FROM app_identity WHERE id=1').fetchone()
    guard=db.execute("SELECT generation FROM safety_circuits WHERE scope='transactions'").fetchone()
    return token_hash(dumps([list(person) if person else None,version(db),guard[0] if guard else 0]))


def payment_binding(db,approval,args):
    from .payment_security import fingerprint,risk
    return fingerprint(db,approval,args,risk(db,approval,args))


def pin_binding(db,key):
    change=db.execute('SELECT target_hash,old_hash,expires FROM pin_change_sms WHERE session_key=?',(key,)).fetchone()
    return token_hash(dumps([identity_binding(db),list(change) if change else None]))


def verify_grant(db,key,token,binding,consume=True,purpose='payment'):
    if not enabled(db):return '人脸验证已关闭，请使用支付密码。'
    if not db.execute('SELECT template FROM face_identity WHERE id=1').fetchone()[0]:return '请先在应用设置绑定本人人脸。'
    if not isinstance(token,str) or len(token)>150:return '请先完成人脸验证。'
    grant=db.execute('SELECT * FROM face_grants WHERE token_hash=?',(token_hash(token),)).fetchone()
    if not grant or grant['consumed'] or grant['expires']<=time.time() or grant['session_key']!=key or grant['purpose']!=purpose or grant['binding']!=binding or grant['source_version']!=version(db):
        return '人脸验证已失效或交易内容发生变化，请重新刷脸。'
    if consume:db.execute('UPDATE face_grants SET consumed=1 WHERE token_hash=?',(token_hash(token),))
    return None


def cipher(database):
    from cryptography.fernet import Fernet
    path=Path(database.path).with_suffix('.face.key')
    if not path.exists():
        with database.connection() as db:
            if db.execute('SELECT template FROM face_identity WHERE id=1').fetchone()[0]:
                raise HTTPException(503,'人脸加密密钥不可用，请联系人工，不能自动重置。')
        try:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as handle:handle.write(Fernet.generate_key())
        except FileExistsError:pass
    return Fernet(path.read_bytes())


def encrypt(database,vector):return cipher(database).encrypt(dumps(vector).encode()).decode()
def decrypt(database,value):return json.loads(cipher(database).decrypt(value.encode()))


def new_challenge(db,key,purpose,binding):
    db.execute('DELETE FROM face_challenges WHERE expires<? OR status!=?',(time.time(),'pending'))
    db.execute('DELETE FROM face_grants WHERE expires<? OR consumed=1',(time.time(),))
    db.execute("UPDATE face_challenges SET status='cancelled',vector=NULL WHERE session_key=?",(key,))
    sequence=['center',*secrets.SystemRandom().sample(['left','right'],2),'center'] if purpose in {'enroll','replace_enroll'} else ['center']
    identifier=secrets.token_urlsafe(32)
    db.execute('INSERT INTO face_challenges(id,session_key,purpose,binding,source_version,sequence,expires) VALUES(?,?,?,?,?,?,?)',(identifier,key,purpose,binding,version(db),dumps(sequence),time.time()+120))
    return {'challenge_id':identifier,'prompt':PROMPTS[sequence[0]],'step':0,'steps':len(sequence),'expires_in':120}


class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')


class SMS(Body):
    purpose:str=Field(pattern='^(enroll|replace|delete|disable|enable)$')
    pin:str=Field(pattern=r'^[0-9]{4}$')


class Enable(Body):
    pin:str=Field(pattern=r'^[0-9]{4}$')


class Challenge(Body):
    purpose:str=Field(pattern='^(enroll|replace|delete|disable|enable|payment|pin_change)$')
    pin:str|None=Field(default=None,pattern=r'^[0-9]{4}$')
    sms_code:str|None=Field(default=None,pattern=r'^[0-9]{6}$')
    approval_id:str|None=Field(default=None,max_length=100)
    conversation_id:str|None=Field(default=None,max_length=100)
    account_id:str|None=Field(default=None,max_length=100)
    execute_at:str|None=Field(default=None,max_length=64)
    consent:bool=False


def register(app,database,settings,session,resolve_conversation):
    frame_times=deque();start_times=deque()
    def limit(queue,count):
        instant=time.monotonic()
        while queue and queue[0]<instant-60:queue.popleft()
        if len(queue)>=count:raise HTTPException(429,'人脸请求过于频繁，请稍后重试。')
        queue.append(instant)
    def require_enabled():
        if not settings.face_enabled:raise HTTPException(403,'人脸验证已由服务器关闭。')
    @app.get('/api/face/status')
    def status(key:str=Depends(session)):
        with database.connection() as db:
            row=db.execute('SELECT template,updated_at FROM face_identity WHERE id=1').fetchone()
            active=enabled(db);preference=bool(db.execute('SELECT enabled FROM face_preferences WHERE id=1').fetchone()[0])
        return {'enabled':active,'verification_enabled':active and bool(row['template']),'server_enabled':settings.face_enabled,'user_enabled':preference,'bound':bool(row['template']),'updated_at':row['updated_at'],'ready':settings.face_enabled and face_engine.ready(settings.face_model_dir),'local':True}

    @app.post('/api/face/enable')
    def enable_face(body:Enable,key:str=Depends(session)):
        require_enabled();verify_pin(database,body.pin)
        raise BankError('请通过摄像头验证已绑定的人脸后开启；未绑定时请先录入人脸。')

    @app.post('/api/face/sms')
    def sms(body:SMS,key:str=Depends(session)):
        require_enabled()
        verify_pin(database,body.pin)
        with database.transaction() as db:
            check_user(db)
            bound=bool(db.execute('SELECT template FROM face_identity WHERE id=1').fetchone()[0])
            if bound==(body.purpose=='enroll'):raise BankError('人脸绑定状态已变化，请刷新设置。')
            previous=db.execute('SELECT sent FROM face_sms WHERE session_key=?',(key,)).fetchone()
            if previous and time.time()-previous[0]<60:raise BankError('请60秒后重新获取验证码。')
            code=f'{secrets.randbelow(1000000):06d}'
            db.execute('INSERT OR REPLACE INTO face_sms VALUES(?,?,?,?,?,?,0)',(key,body.purpose,identity_binding(db),token_hash(code),time.time()+300,time.time()))
            event(db,'*','face.sms_sent',{'purpose':body.purpose})
        return {'demo_code':code,'expires_in':300,'simulated':True}

    def start(body,key,approval_override=None):
        require_enabled()
        limit(start_times,12)
        if not face_engine.ready(settings.face_model_dir):raise HTTPException(503,'本地人脸模型尚未准备好。')
        if body.purpose!='payment' or body.pin:verify_pin(database,body.pin)
        issue=None
        with database.transaction() as db:
            check_user(db)
            bound=bool(db.execute('SELECT template FROM face_identity WHERE id=1').fetchone()[0])
            if body.purpose=='payment':
                if not enabled(db):raise BankError('本账户已关闭人脸验证，请使用支付密码。')
                current=resolve_conversation(db,key,body.conversation_id)
                row=db.execute('SELECT a.*,t.conversation_id FROM approvals a JOIN tasks t ON t.id=a.task_id WHERE a.id=?',(body.approval_id,)).fetchone()
                if not row or row['conversation_id']!=current or row['status']!='pending' or datetime.fromisoformat(row['expires_at'])<=datetime.now(TZ):raise BankError('找不到当前有效交易。')
                if not bound:raise BankError('尚未绑定人脸，请先到应用设置绑定；黄色交易可以改用密码。')
                args=json.loads(row['args_json'])
                if row['tool_name'] in DEBIT_TOOLS and body.account_id:args['account_id']=body.account_id
                from .scheduled_actions import timing
                from .payment_security import risk,verify_sms
                args=timing(row['tool_name'],args,body.execute_at)
                if risk(db,row,args)['sms_required']:
                    if not body.pin:raise BankError('红色操作请先输入支付密码和短信验证码。')
                    issue=verify_sms(db,row,args,key,body.sms_code,consume=False)
                binding=payment_binding(db,row,args)
            elif body.purpose=='pin_change':
                if not enabled(db):raise BankError('本账户已关闭人脸验证。')
                if not bound:raise BankError('修改密码前请先在设置绑定人脸。')
                change=db.execute('SELECT * FROM pin_change_sms WHERE session_key=?',(key,)).fetchone()
                if not change or change['expires']<=time.time() or change['attempts']>=5:issue='请先获取修改密码短信验证码。'
                elif not body.sms_code or not secrets.compare_digest(change['code_hash'],token_hash(body.sms_code)):
                    db.execute('UPDATE pin_change_sms SET attempts=attempts+1 WHERE session_key=?',(key,));failure(db,'sms');issue='修改密码短信验证码错误。'
                binding=pin_binding(db,key)
            else:
                if body.purpose=='disable' and not enabled(db):raise BankError('本账户的人脸验证已经关闭。')
                if not body.consent:raise BankError('请先同意采集人脸特征用于本账户验证。')
                if bound==(body.purpose=='enroll'):raise BankError('人脸绑定状态已变化，请刷新设置。')
                challenge=db.execute('SELECT * FROM face_sms WHERE session_key=?',(key,)).fetchone()
                if not challenge or challenge['purpose']!=body.purpose or challenge['binding']!=identity_binding(db) or challenge['expires']<=time.time() or challenge['attempts']>=5:
                    issue='请重新获取人脸设置短信验证码。'
                elif not body.sms_code or not secrets.compare_digest(challenge['code_hash'],token_hash(body.sms_code)):
                    db.execute('UPDATE face_sms SET attempts=attempts+1 WHERE session_key=?',(key,));failure(db,'sms');issue='短信验证码错误。'
                else:db.execute('DELETE FROM face_sms WHERE session_key=?',(key,));success(db,'sms')
                binding=identity_binding(db)
            if not issue:
                value=new_challenge(db,key,body.purpose,binding)
                event(db,'*','face.challenge_started',{'purpose':body.purpose,'approval_id':body.approval_id})
        if issue:raise BankError(issue)
        return value

    @app.post('/api/face/challenge')
    def challenge(body:Challenge,key:str=Depends(session)):return start(body,key)

    def retry_enrollment(db,row):
        # Capture mistakes are not failed identity checks and never clear or
        # increment transaction security counters. Keep the authorized session.
        db.execute("UPDATE face_challenges SET step=0,streak=0,vector=NULL,last_digest='',last_at=0 WHERE id=? AND status='pending'",(row['id'],))
        event(db,'*','face.enrollment_retry',{'purpose':row['purpose']})
        return {'done':False,'step':0,'steps':len(json.loads(row['sequence'])),'retry':True,'prompt':'录入未完成，已重新开始。请正对镜头，保持稳定。'}

    async def frame(identifier,request,key):
        require_enabled()
        limit(frame_times,120)
        with database.transaction() as db:
            check_user(db)
            row=db.execute('SELECT * FROM face_challenges WHERE id=? AND session_key=?',(identifier,key)).fetchone()
            if not row or row['status']!='pending' or row['expires']<=time.time() or row['source_version']!=version(db):raise BankError('人脸验证已结束或过期，请重新开始。')
            if row['last_at'] and time.time()-row['last_at']<.25:raise HTTPException(429,'请保持姿势稍等。')
        if request.headers.get('content-type','').split(';')[0]!='image/jpeg':raise HTTPException(415,'请使用摄像头画面。')
        data=bytearray()
        async with asyncio.timeout(10):
            async for part in request.stream():
                data.extend(part)
                if len(data)>250000:raise HTTPException(413,'画面过大，请重新拍摄。')
        if len(data)<100 or not bytes(data).startswith(b'\xff\xd8'):raise HTTPException(422,'无法读取摄像头画面。')
        if not face_engine.LOCK.acquire(blocking=False):raise HTTPException(429,'人脸服务忙，请稍等。')
        try:
            job=asyncio.create_task(asyncio.to_thread(face_engine.inspect,bytes(data),settings.face_model_dir))
            try:analysis=await asyncio.shield(job)
            except asyncio.CancelledError:
                await job;raise
        except face_engine.FaceCheckError as exc:
            if exc.security:
                with database.transaction() as db:
                    if row['purpose'] in {'enroll','replace_enroll'}:
                        return retry_enrollment(db,row)
                    failure(db,'face');db.execute("UPDATE face_challenges SET status='failed',vector=NULL WHERE id=?",(identifier,))
                raise BankError(str(exc))
            return {'done':False,'prompt':str(exc),'retry':True}
        except (ImportError,RuntimeError,OSError) as exc:raise HTTPException(503,'人脸模型暂时不可用，请联系人工或稍后重试。') from exc
        finally:face_engine.LOCK.release()
        issue=None
        with database.transaction() as db:
            check_user(db)
            current=db.execute('SELECT * FROM face_challenges WHERE id=? AND session_key=?',(identifier,key)).fetchone()
            if not current or current['status']!='pending' or current['expires']<=time.time() or current['source_version']!=version(db) or current['step']!=row['step'] or current['last_at']!=row['last_at']:raise BankError('验证状态已变化，请重新开始。')
            import numpy as np
            vector=np.array(analysis['vector']);sequence=json.loads(row['sequence']);pose=analysis['pose'];expected=sequence[row['step']]
            if vector.shape!=(128,) or not np.isfinite(vector).all() or not np.isfinite(pose):raise HTTPException(503,'人脸特征不可用，请重新验证。')
            # The preview is mirrored: the user's left turn moves the nose to
            # the right in the original camera frame, and left in the preview.
            pose_ok=abs(pose)<.18 if expected=='center' else pose>.18 if expected=='left' else pose<-.18
            if not pose_ok:return {'done':False,'prompt':PROMPTS[expected],'retry':True}
            digest=token_hash(bytes(data).hex())
            if row['last_digest']==digest:return {'done':False,'prompt':'请保持本人在镜头前，稍微调整姿势。','retry':True}
            base=decrypt(database,row['vector']) if row['vector'] else analysis['vector']
            identity_threshold=.45 if expected=='center' else .30
            if float(vector@np.array(base))<identity_threshold:issue='验证过程中人脸发生变化，请本人重新开始。'
            profile=db.execute('SELECT template FROM face_identity WHERE id=1').fetchone()[0]
            if row['purpose'] in {'payment','pin_change','replace','delete','disable','enable'} and (not profile or float(vector@np.array(decrypt(database,profile)))<identity_threshold):issue='与本账户绑定的人脸不匹配，请本人验证。'
            if issue:
                if row['purpose'] in {'enroll','replace_enroll'}:
                    return retry_enrollment(db,row)
                failure(db,'face');db.execute("UPDATE face_challenges SET status='failed',vector=NULL WHERE id=?",(identifier,));event(db,'*','face.verification_failed',{'purpose':row['purpose']})
            else:
                if row['purpose'] in {'enroll','replace_enroll'} and expected=='center':
                    combined=np.array(base)+vector;base=(combined/np.linalg.norm(combined)).tolist()
                streak=row['streak']+1;step=row['step']+(streak>=2);streak=streak%2
                db.execute('UPDATE face_challenges SET vector=?,last_digest=?,last_at=?,step=?,streak=? WHERE id=?',(encrypt(database,base),digest,time.time(),step,streak,identifier))
                if step<len(sequence):return {'done':False,'step':step,'steps':len(sequence),'prompt':PROMPTS[sequence[step]],'progress':f'已完成 {step} / {len(sequence)} 步'}
                db.execute("UPDATE face_challenges SET status='done',vector=NULL WHERE id=?",(identifier,))
                if row['purpose'] not in {'enroll','replace_enroll'}:success(db,'face')
                if row['purpose'] in {'payment','pin_change'}:
                    token=secrets.token_urlsafe(32)
                    db.execute('INSERT INTO face_grants VALUES(?,?,?,?,?,?,0)',(token_hash(token),key,row['purpose'],row['binding'],version(db),time.time()+90))
                    event(db,'*','face.verified',{'purpose':row['purpose']})
                    return {'done':True,'face_token':token,'expires_in':90}
                if row['purpose']=='replace':return {'done':False,'replacing':True,**new_challenge(db,key,'replace_enroll',identity_binding(db))}
                if row['binding']!=identity_binding(db):raise BankError('身份信息发生变化，请重新验证。')
                if row['purpose'] in {'disable','enable'}:
                    active=row['purpose']=='enable'
                    db.execute('UPDATE face_preferences SET enabled=? WHERE id=1',(int(active),));invalidate(db)
                    event(db,'*','face.enabled' if active else 'face.disabled',{'enabled':active})
                    return {'done':True,'bound':True,'enabled':active}
                template=None if row['purpose']=='delete' else encrypt(database,base)
                db.execute('UPDATE face_identity SET template=?,version=version+1,updated_at=? WHERE id=1',(template,now()))
                db.execute('UPDATE face_preferences SET enabled=? WHERE id=1',(int(template is not None),))
                if template is not None:db.execute('UPDATE face_onboarding SET done=1 WHERE id=1')
                db.execute('DELETE FROM face_grants');db.execute('DELETE FROM face_sms');db.execute('DELETE FROM payment_sms');db.execute('DELETE FROM face_challenges')
                event(db,'*','face.deleted' if template is None else 'face.enrolled',{'replaced':row['purpose']=='replace_enroll'})
                return {'done':True,'bound':template is not None}
        if issue:raise BankError(issue)

    @app.post('/api/face/challenges/{identifier}/frame')
    async def receive_frame(identifier:str,request:Request,key:str=Depends(session)):return await frame(identifier,request,key)

    @app.delete('/api/face/challenges/{identifier}')
    def cancel(identifier:str,key:str=Depends(session)):
        with database.transaction() as db:db.execute("UPDATE face_challenges SET status='cancelled',vector=NULL WHERE id=? AND session_key=?",(identifier,key))
        return {'cancelled':True}

    # Feishu's short-lived signed link selects the approval; it grants no face bypass.
    @app.post('/api/feishu-pay/face')
    def feishu_face(request:Request,body:Challenge,key:str=Depends(session)):
        from .feishu_payment import authorize
        with database.connection() as db:row=authorize(db,request.headers.get('X-Feishu-Payment',''),settings)
        body=body.model_copy(update={'purpose':'payment','approval_id':row['id'],'conversation_id':row['conversation_id'],'execute_at':None})
        return start(body,key)
