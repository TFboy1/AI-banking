"""Red-level password changes, bound to user/session, old hash and new PIN."""
import secrets,time

SCHEMA='''CREATE TABLE IF NOT EXISTS pin_change_sms(
 session_key TEXT PRIMARY KEY,phone TEXT NOT NULL,old_hash TEXT NOT NULL,target_hash TEXT NOT NULL,
 code_hash TEXT NOT NULL,expires REAL NOT NULL,sent REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0);'''

def validate(body):
    from .bank import BankError
    if body.pin!=body.repeat_pin:raise BankError('两次输入的密码不一致。')

def send(database,key,body):
    from .security import verify_pin,encode_pin,token_hash
    from .bank import BankError
    from .safety import check_user,audit
    validate(body)
    with database.connection() as db:before=db.execute('SELECT pin_hash FROM app_identity WHERE id=1').fetchone()[0]
    verify_pin(database,body.current_pin)
    with database.transaction() as db:
        check_user(db)
        person=db.execute('SELECT * FROM app_identity WHERE id=1').fetchone()
        if person['pin_hash']!=before:raise BankError('密码状态已变化，请重新验证。')
        previous=db.execute('SELECT sent FROM pin_change_sms WHERE session_key=?',(key,)).fetchone()
        if previous and time.time()-previous[0]<60:raise BankError('请60秒后重新获取修改密码验证码。')
        code=f'{secrets.randbelow(1000000):06d}'
        db.execute('INSERT OR REPLACE INTO pin_change_sms VALUES(?,?,?,?,?,?,?,0)',(key,person['phone'],person['pin_hash'],encode_pin(body.pin),token_hash(code),time.time()+300,time.time()))
        audit(db,'transactions','pin_change_sms_sent','修改密码红色验证：已发送模拟短信。')
    return {'demo_code':code,'expires_in':300,'simulated':True,'phone_masked':person['phone'][:3]+'****'+person['phone'][-4:]}

def apply(database,key,body):
    from .security import verify_pin,encode_pin,pin_matches,token_hash
    from .bank import BankError
    from .safety import check_user,failure,success,audit
    validate(body)
    with database.connection() as db:person=db.execute('SELECT * FROM app_identity WHERE id=1').fetchone()
    existing=bool(person['pin_hash'])
    if existing:verify_pin(database,body.current_pin)
    issue=None
    with database.transaction() as db:
        check_user(db)
        current=db.execute('SELECT * FROM app_identity WHERE id=1').fetchone()
        if current['pin_hash']!=person['pin_hash']:raise BankError('密码状态已变化，请重新获取验证码。')
        if existing:
            challenge=db.execute('SELECT * FROM pin_change_sms WHERE session_key=?',(key,)).fetchone()
            if not challenge or challenge['expires']<=time.time() or challenge['attempts']>=5:
                issue='修改密码需要有效的短信验证码，请重新获取。'
            elif challenge['phone']!=current['phone'] or challenge['old_hash']!=current['pin_hash'] or not pin_matches(body.pin,challenge['target_hash']):
                issue='新密码或身份信息已变化，请重新获取修改密码验证码。'
            elif not isinstance(body.sms_code,str) or not secrets.compare_digest(challenge['code_hash'],token_hash(body.sms_code)):
                db.execute('UPDATE pin_change_sms SET attempts=attempts+1 WHERE session_key=?',(key,))
                failure(db,'sms')
                issue='修改密码短信验证码错误；连续5次将暂停交易60秒。'
            from .face import verification_enabled,verify_grant,pin_binding
            if not issue and verification_enabled(db):
                issue=verify_grant(db,key,body.face_token,pin_binding(db,key),purpose='pin_change')
            if issue:audit(db,'transactions','pin_change_denied',issue)
        if not issue:
            db.execute('UPDATE app_identity SET pin_hash=?,failures=0,blocked_until=0 WHERE id=1',(encode_pin(body.pin),))
            db.execute('UPDATE app_sessions SET authenticated=0,unlocked=0')
            db.execute('UPDATE app_sessions SET authenticated=1,unlocked=1 WHERE token_hash=?',(key,))
            db.execute('DELETE FROM pin_change_sms');db.execute('DELETE FROM payment_sms')
            success(db,'sms')
            audit(db,'transactions','pin_changed' if existing else 'pin_created',('支付密码已修改（旧密码、模拟短信及本人面容均通过）。' if verification_enabled(db) else '支付密码已修改（旧密码及模拟短信均通过）。') if existing else '注册引导：首次设置支付密码。')
    if issue:raise BankError(issue)
