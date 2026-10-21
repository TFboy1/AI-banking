"""Independent local operator console. No banking-tool or payment endpoints."""
import asyncio
import hashlib
import hmac
import secrets
import sqlite3
import time
from contextlib import contextmanager, asynccontextmanager
from pathlib import Path
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .config import ROOT, Settings
from .db import Database, now, uid, TZ
from .bank import BankError
from . import human_service as human
from . import account_lock

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

def password_hash(password,salt):
    return hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),300_000).hex()

class Login(BaseModel):
    model_config=ConfigDict(extra='forbid')
    password: str = Field(min_length=1,max_length=128)

class Reply(BaseModel):
    model_config=ConfigDict(extra='forbid')
    text: str = Field(min_length=1,max_length=4000)

class Review(BaseModel):
    model_config=ConfigDict(extra='forbid')
    approve: bool = Field(strict=True)
    note: str = Field(min_length=1,max_length=500)

def create_app(settings=None):
    settings=settings or Settings()
    root=settings.database_path.parent.resolve()
    root.mkdir(parents=True,exist_ok=True)
    from .audit_witness import Witness
    witness=Witness(settings.audit_witness_path or root.parent/'audit-witness')

    @contextmanager
    def console():
        db=sqlite3.connect(root/'admin.sqlite3',timeout=10)
        db.row_factory=sqlite3.Row
        from .audit_integrity import register_functions
        register_functions(db)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize():
        with console() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS admin_identity(id INTEGER PRIMARY KEY CHECK(id=1),salt TEXT NOT NULL,password_hash TEXT NOT NULL,failures INTEGER NOT NULL DEFAULT 0,blocked_until REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS admin_sessions(token_hash TEXT PRIMARY KEY,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS admin_audit(id TEXT PRIMARY KEY,action TEXT NOT NULL,user_id TEXT,target TEXT,created_at TEXT NOT NULL);
            ''')
            if not db.execute('SELECT 1 FROM admin_identity WHERE id=1').fetchone():
                salt=secrets.token_hex(16)
                db.execute('INSERT INTO admin_identity(id,salt,password_hash) VALUES(1,?,?)',(salt,password_hash('000000',salt)))
            from .audit_integrity import install as install_integrity
            install_integrity(db)

    @asynccontextmanager
    async def lifespan(app):
        initialize()
        async def seal_loop():
            while True:
                try:
                    for _,database in users():await asyncio.to_thread(witness.extend_known_scope,database)
                    await asyncio.to_thread(witness.check,Database(root/'admin.sqlite3',seed_demo=False),True)
                except Exception:pass  # Verification endpoints report failure; never replace evidence.
                await asyncio.sleep(10)
        worker=asyncio.create_task(seal_loop()) if settings.audit_witness_enabled else None
        try:yield
        finally:
            if worker:
                worker.cancel()
                try:await worker
                except asyncio.CancelledError:pass

    app=FastAPI(title='有数 · 人工服务工作台',lifespan=lifespan)

    @app.middleware('http')
    async def origin(request,call_next):
        if request.url.path.startswith('/api/'):
            supplied=request.headers.get('origin')
            if supplied and supplied!=str(request.base_url).rstrip('/'):
                return JSONResponse({'detail':'不接受其他站点的请求。'},status_code=403)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        return response

    @app.exception_handler(BankError)
    async def bank_error(request,error):
        return JSONResponse({'detail':str(error)},status_code=400)

    def authenticated(request: Request):
        key=digest(request.cookies.get('youshu_admin',''))
        with console() as db:
            valid=db.execute('SELECT 1 FROM admin_sessions WHERE token_hash=? AND expires>?',(key,time.time())).fetchone()
        if not valid:
            raise HTTPException(401,'请先登录管理员工作台。')
        return '管理员'

    def users():
        path=root/'accounts.sqlite3'
        if path.exists():
            with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
                db.row_factory=sqlite3.Row
                result=[dict(row) for row in db.execute('SELECT id,username,phone,database_path FROM users')]
        else:
            username='当前用户';phone=''
            if settings.database_path.exists():
                with Database(settings.database_path).connection() as db:
                    person=db.execute('SELECT username,phone FROM app_identity WHERE id=1').fetchone()
                    if person:
                        username,phone=person['username'],person['phone']
            result=[{'id':'legacy','username':username,'phone':phone,'database_path':str(settings.database_path)}]
        for user in result:
            path=Path(user['database_path']).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                continue
            yield user,Database(path,seed_demo=False)

    def tenant(user_id):
        for user,database in users():
            if user['id']==user_id:
                # A tenant not yet opened since deployment still gets the human schema.
                with database.connection() as db:
                    db.executescript(human.SCHEMA)
                return user,database
        raise HTTPException(404,'用户不存在。')

    def log(action,user_id=None,target=None):
        with console() as db:
            if action in {'view_history','audit_view','audit_security'} and db.execute('SELECT 1 FROM admin_audit WHERE action=? AND user_id=? AND target IS ? AND created_at>?',(action,user_id,target,(datetime.now(TZ)-timedelta(seconds=60)).isoformat())).fetchone():
                return
            db.execute('INSERT INTO admin_audit VALUES(?,?,?,?,?)',(uid('aa_'),action,user_id,target,now()))

    @app.get('/api/health')
    def health():
        return {'ok':True,'service':'human-admin'}

    @app.get('/api/admin/status')
    def status(operator=Depends(authenticated)):
        return {'authenticated':True,'operator':operator}

    @app.post('/api/admin/login')
    def login(body: Login,response: Response):
        success=False
        with console() as db:
            person=db.execute('SELECT * FROM admin_identity WHERE id=1').fetchone()
            if person['blocked_until']>time.time():
                raise HTTPException(429,'密码连续错误，请60秒后重试。')
            success=hmac.compare_digest(password_hash(body.password,person['salt']),person['password_hash'])
            if success:
                db.execute('UPDATE admin_identity SET failures=0,blocked_until=0 WHERE id=1')
                token=secrets.token_urlsafe(32)
                db.execute('DELETE FROM admin_sessions WHERE expires<?',(time.time(),))
                db.execute('INSERT INTO admin_sessions VALUES(?,?)',(digest(token),time.time()+8*3600))
            else:
                failures=person['failures']+1
                db.execute('UPDATE admin_identity SET failures=?,blocked_until=? WHERE id=1',(0 if failures>=5 else failures,time.time()+60 if failures>=5 else 0))
        if not success:
            log('login_failed')
            raise HTTPException(401,'管理员密码不正确。')
        response.set_cookie('youshu_admin',token,httponly=True,samesite='strict',secure=False,max_age=8*3600)
        log('login')
        return {'authenticated':True,'operator':'管理员'}

    @app.post('/api/admin/logout')
    def logout(request: Request,response: Response,operator=Depends(authenticated)):
        with console() as db:
            db.execute('DELETE FROM admin_sessions WHERE token_hash=?',(digest(request.cookies.get('youshu_admin','')),))
        response.delete_cookie('youshu_admin')
        return {'ok':True}

    @app.get('/api/admin/queue')
    def queue(operator=Depends(authenticated)):
        sessions=[]; reviews=[]
        for user,database in users():
            with database.connection() as db:
                db.executescript(human.SCHEMA)
                label={'user_id':user['id'],'username':user['username'],'phone_masked':user['phone'][:3]+'****'+user['phone'][-4:] if user['phone'] else ''}
                for row in db.execute('SELECT h.*,c.title FROM human_sessions h JOIN conversations c ON c.id=h.conversation_id WHERE c.deleted_at IS NULL ORDER BY h.rowid DESC'):
                    sessions.append({**dict(row),**label})
                if db.execute("SELECT 1 FROM sqlite_master WHERE name='manual_unlock_requests'").fetchone():
                    for row in db.execute('SELECT * FROM manual_unlock_requests ORDER BY rowid DESC'):
                        reviews.append({**dict(row),**label})
        sessions.sort(key=lambda x:x['updated_at'],reverse=True)
        reviews.sort(key=lambda x:x['created_at'],reverse=True)
        return {'sessions':sessions,'reviews':reviews}

    @app.get('/api/admin/users/{user_id}/sessions/{session_id}')
    def conversation(user_id: str,session_id: str,operator=Depends(authenticated)):
        user,database=tenant(user_id)
        with database.transaction() as db:
            service=human.valid(db,session_id)
            result={'session':service,'messages':human.history(db,service['conversation_id'])}
        log('view_history',user_id,session_id)
        return result

    @app.post('/api/admin/users/{user_id}/sessions/{session_id}/claim')
    def claim(user_id: str,session_id: str,operator=Depends(authenticated)):
        _,database=tenant(user_id)
        with database.transaction() as db:
            result=human.claim(db,session_id,operator)
        log('claim',user_id,session_id)
        return result

    @app.post('/api/admin/users/{user_id}/sessions/{session_id}/messages')
    def reply(user_id: str,session_id: str,body: Reply,operator=Depends(authenticated)):
        if not body.text.strip():
            raise BankError('回复不能为空。')
        _,database=tenant(user_id)
        with database.transaction() as db:
            human.reply(db,session_id,body.text.strip(),operator)
        log('reply',user_id,session_id)
        return {'ok':True}

    @app.post('/api/admin/users/{user_id}/sessions/{session_id}/close')
    def close(user_id: str,session_id: str,operator=Depends(authenticated)):
        _,database=tenant(user_id)
        with database.transaction() as db:
            result=human.close(db,session_id,operator)
        log('close',user_id,session_id)
        return result

    @app.post('/api/admin/users/{user_id}/reviews/{request_id}')
    def review(user_id: str,request_id: str,body: Review,operator=Depends(authenticated)):
        _,database=tenant(user_id)
        result=account_lock.review(database,request_id,body.approve,operator,body.note)
        log('unlock_approved' if body.approve else 'unlock_rejected',user_id,request_id)
        return result

    from .audit_view import register as register_audit
    register_audit(app,authenticated,users,tenant,log,console)
    def integrity_database(user_id):
        if user_id=='__admin__':return Database(root/'admin.sqlite3',seed_demo=False)
        return tenant(user_id)[1]
    @app.get('/api/admin/audit/users/{user_id}/integrity')
    def integrity(user_id:str,operator=Depends(authenticated)):
        database=integrity_database(user_id)
        log('audit_integrity',user_id,None)
        return witness.check(database)

    @app.post('/api/admin/audit/users/{user_id}/integrity/checkpoint')
    def checkpoint(user_id:str,operator=Depends(authenticated)):
        database=integrity_database(user_id)
        log('audit_checkpoint',user_id,None)
        return witness.check(database,True)

    @app.get('/api/admin/audit/users/{user_id}/integrity/export')
    def export_integrity(user_id:str,operator=Depends(authenticated)):
        from fastapi.responses import Response
        from .audit_integrity import encoded
        database=integrity_database(user_id)
        log('audit_integrity_export',user_id,None)
        result=witness.check(database)
        return Response(encoded(result),media_type='application/json',headers={'Content-Disposition':'attachment; filename="audit-integrity.json"'})
    dist=ROOT/'frontend'/'dist'
    @app.get('/')
    def page():
        if not (dist/'admin.html').exists():
            raise HTTPException(503,'请先构建前端。')
        return FileResponse(dist/'admin.html')
    if (dist/'assets').exists():
        app.mount('/assets',StaticFiles(directory=dist/'assets'),name='assets')
    return app

app=create_app()
