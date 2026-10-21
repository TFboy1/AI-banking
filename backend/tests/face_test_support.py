"""Synthetic face fixture for legacy PIN/SMS tests; full challenge API is in test_face."""
import json,secrets,time
from banking.face import encrypt,payment_binding,pin_binding,version
from banking.security import token_hash

def bind_test_face(app):
    with app.state.database.transaction() as db:
        db.execute('UPDATE face_identity SET template=?,version=1 WHERE id=1',(encrypt(app.state.database,[1.]+[0.]*127),))

def grant(app,client,approval=None,account=None,purpose='payment'):
    key=token_hash(client.cookies.get('bank_session'))
    token=secrets.token_urlsafe(32)
    with app.state.database.transaction() as db:
        if purpose=='pin_change':binding=pin_binding(db,key)
        else:
            row=db.execute('SELECT * FROM approvals WHERE id=?',(approval,)).fetchone();args=json.loads(row['args_json'])
            if account:args['account_id']=account
            binding=payment_binding(db,row,args)
        db.execute('INSERT INTO face_grants VALUES(?,?,?,?,?,?,0)',(token_hash(token),key,purpose,binding,version(db),time.time()+90))
    return token
