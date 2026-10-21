import json,sqlite3
import pytest
from banking.audit_integrity import inspect,record_hash,encoded,hashed,protection_sql,trigger_sql
from banking.audit_witness import Witness
from banking.db import Database,add_message

def witness(tmp_path):return Witness(tmp_path/'independent-witness')

def unsafe(db):
    for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE 'integrity_%' OR name LIKE 'protect_audit_%')").fetchall():
        db.execute('DROP TRIGGER "'+r[0]+'"')

def test_baseline_and_normal_changes_are_consistent(database,tmp_path):
    w=witness(tmp_path)
    first=w.check(database,True);assert first['status']=='verified',first
    with database.transaction() as db:db.execute("UPDATE accounts SET balance_minor=balance_minor+1 WHERE id='daily'")
    pending=w.check(database);assert pending['status']=='pending',pending
    fresh=w.check(database,True);assert fresh['status']=='verified' and fresh['count']>first['count'],fresh
    assert fresh['proof']['signature']
    with database.connection() as db:
        with pytest.raises(sqlite3.IntegrityError):db.execute("UPDATE audit_integrity_records SET new_hash='bad' WHERE seq=1")
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM audit_integrity_records WHERE seq=1')

def test_changed_current_data_without_journal_is_detected(database,tmp_path):
    w=witness(tmp_path);assert w.check(database,True)['status']=='verified'
    with database.connection() as db:
        spec=json.loads(db.execute('SELECT tables_json FROM audit_integrity_meta').fetchone()[0])['accounts']
        db.execute('DROP TRIGGER integrity_accounts_update')
        db.execute("UPDATE accounts SET balance_minor=balance_minor+1 WHERE id='daily'")
        db.execute(trigger_sql('accounts',spec,'UPDATE'))
    result=w.check(database,True);assert result['status']=='alert',result
    assert '当前数据' in result['message']

@pytest.mark.parametrize('mode',['modify','delete_tail','swap','rewrite'])
def test_journal_tampering_cannot_be_resigned(database,tmp_path,mode):
    w=witness(tmp_path);first=w.check(database,True);assert first['status']=='verified',first
    with database.connection() as db:
        for action in ('update','delete'):db.execute('DROP TRIGGER protect_audit_integrity_records_'+action)
        if mode=='modify':db.execute("UPDATE audit_integrity_records SET new_hash='bad' WHERE seq=1")
        elif mode=='delete_tail':db.execute('DELETE FROM audit_integrity_records WHERE seq=(SELECT MAX(seq) FROM audit_integrity_records)')
        elif mode=='swap':
            db.execute('UPDATE audit_integrity_records SET seq=-1 WHERE seq=1')
            db.execute('UPDATE audit_integrity_records SET seq=1 WHERE seq=2')
            db.execute('UPDATE audit_integrity_records SET seq=2 WHERE seq=-1')
        else:
            db.execute("UPDATE audit_integrity_records SET occurred_at='changed' WHERE seq=1")
            previous='0'*64
            for row in db.execute('SELECT * FROM audit_integrity_records ORDER BY seq').fetchall():
                value=dict(row);value['prev_hash']=previous;previous=record_hash(value)
                db.execute('UPDATE audit_integrity_records SET prev_hash=?,record_hash=? WHERE seq=?',(value['prev_hash'],previous,value['seq']))
        for action in ('UPDATE','DELETE'):db.execute(protection_sql('audit_integrity_records',action))
    result=w.check(database,True);assert result['status']=='alert',result
    if mode=='rewrite':assert '重算' in result['message']
    assert result['proof']['signature']==first['proof']['signature']
    assert result['anchored_count']==first['count']
    with w.connection() as db:assert db.execute('SELECT COUNT(*) FROM checkpoints').fetchone()[0]==1

def test_old_database_restore_is_detected(database,tmp_path):
    w=witness(tmp_path);w.check(database,True)
    copy=tmp_path/'old.sqlite3'
    with database.connection() as source,sqlite3.connect(copy) as dest:source.backup(dest)
    with database.transaction() as db:db.execute("UPDATE accounts SET balance_minor=balance_minor+1 WHERE id='daily'")
    assert w.check(database,True)['status']=='verified'
    with sqlite3.connect(copy) as source,database.connection() as dest:source.backup(dest)
    result=w.check(database,True);assert result['status']=='alert',result

def test_changed_signature_and_missing_keys_never_reset_trust(database,tmp_path):
    w=witness(tmp_path);assert w.check(database,True)['status']=='verified'
    with w.connection() as db:db.execute("UPDATE checkpoints SET signature='AAAA'")
    assert w.check(database,True)['status']=='alert'
    (w.root/'public.raw').unlink()
    assert w.check(database,True)['status']=='alert'

def test_scope_and_instance_cannot_silently_change(database,tmp_path):
    w=witness(tmp_path);w.check(database,True)
    with database.connection() as db:
        db.execute('DROP TRIGGER protect_audit_integrity_meta_update')
        db.execute("UPDATE audit_integrity_meta SET instance_id='new'")
    assert w.check(database,True)['status']=='alert'

def test_hash_records_do_not_copy_message_secrets(database,tmp_path):
    w=witness(tmp_path)
    with database.transaction() as db:
        conv=database.conversation_id(db)
        add_message(db,conv,None,'user','text','密码2468，手机号13800123456',{})
    assert w.check(database,True)['status']=='verified'
    with w.connection() as db:raw=str([tuple(r) for r in db.execute('SELECT * FROM copies')])
    assert '手机号' not in raw and '13800123456' not in raw and '密码2468' not in raw

async def test_admin_requires_authentication_and_exports_public_proof(settings,tmp_path):
    import httpx
    from banking.admin_site import create_app
    settings.audit_witness_enabled=False;settings.audit_witness_path=tmp_path/'witness'
    database=Database(settings.database_path);database.initialize()
    app=create_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8003') as client:
        path='/api/admin/audit/users/legacy/integrity'
        assert (await client.get(path)).status_code==401
        assert (await client.post(path+'/checkpoint',json={})).status_code==401
        await client.post('/api/admin/login',json={'password':'000000'})
        result=(await client.post(path+'/checkpoint',json={})).json();assert result['status']=='verified',result
        proof=(await client.get(path+'/export')).json()
        assert proof['proof']['signature'] and proof['proof']['public_key']
        adminproof=(await client.post('/api/admin/audit/users/__admin__/integrity/checkpoint',json={})).json()
        assert adminproof['status']=='verified',adminproof
        assert adminproof['scope']==['admin_audit']
        assert 'signing-private' not in json.dumps(proof)
        assert (await client.get('/api/admin/audit/users/other/integrity')).status_code==404

def test_export_signature_is_verifiable_with_trusted_key(database,tmp_path):
    import importlib.util,base64
    from pathlib import Path
    path=Path(__file__).parents[2]/'scripts/verify_audit_proof.py'
    spec=importlib.util.spec_from_file_location('verify_proof',path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    w=witness(tmp_path);proof=w.check(database,True);public=(w.root/'public.raw').read_bytes()
    assert module.verify(proof,public)['count']==proof['count']
    proof['proof']['payload']['count']+=1
    with pytest.raises(Exception):module.verify(proof,public)

def test_independent_store_and_private_key_loss_are_not_silently_repaired(database,tmp_path):
    w=witness(tmp_path);w.check(database,True)
    (w.root/'signing-private.raw').unlink()
    with database.transaction() as db:db.execute("UPDATE accounts SET balance_minor=balance_minor+1 WHERE id='daily'")
    assert w.check(database,True)['status']=='unavailable'
    w.store.unlink()
    assert w.check(database,True)['status']=='alert'

def test_transaction_rollback_does_not_leave_false_audit_records(database,tmp_path):
    w=witness(tmp_path);before=w.check(database,True)
    with pytest.raises(RuntimeError),database.transaction() as db:
        db.execute("UPDATE accounts SET balance_minor=balance_minor+1 WHERE id='daily'")
        raise RuntimeError('rollback')
    after=w.check(database)
    assert after['status']=='verified' and after['count']==before['count']

@pytest.mark.parametrize('with_history',[True,False])
def test_signed_scope_extension_preserves_old_checkpoint(tmp_path,monkeypatch,with_history):
    from banking import audit_integrity as integrity
    db=Database(tmp_path/'legacy.sqlite3')
    with monkeypatch.context() as m:
        m.setattr(integrity,'TABLES',tuple(t for t in integrity.TABLES if t!='human_audit'))
        db.initialize()
    w=witness(tmp_path);first=w.check(db,True)
    assert first['status']=='verified' and 'human_audit' not in first['scope']
    if with_history:
        with db.transaction() as connection:connection.execute("INSERT INTO human_audit VALUES('test',NULL,'test','operator','now')")
    result=w.extend_known_scope(db)
    assert result['status']=='verified',result
    assert 'human_audit' in result['scope']
    assert result['proof']['payload']['scope_extension']['added_tables']==['human_audit']
    assert result['count']==first['count']+int(with_history)
    with w.connection() as connection:
        rows=connection.execute('SELECT * FROM checkpoints ORDER BY id').fetchall()
        assert len(rows)==2 and rows[0]['signature']==first['proof']['signature']
    assert w.extend_known_scope(db)['count']==result['count']
