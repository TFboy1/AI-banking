"""Minimal temporal records for midnight report snapshots; no passwords or card numbers."""
import json

SCHEMA='''
CREATE TABLE IF NOT EXISTS report_history_meta(id INTEGER PRIMARY KEY CHECK(id=1),started_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_history(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,entity TEXT NOT NULL,entity_id TEXT NOT NULL,
 changed_at TEXT NOT NULL,payload TEXT);
CREATE INDEX IF NOT EXISTS report_history_cutoff ON report_history(entity,changed_at,entity_id,seq);
'''
# Explicit fields, rather than SELECT *, keep verification credentials and
# private phone/card identifiers out of this read-only reporting history.
ENTITIES={
 'accounts':('id',['id','name','kind','balance_minor']),
 'products':('id',['id','name','nav','risk_level']),
 'holdings':('product_id',['product_id','units']),
 'securities_positions':('symbol',['symbol','name','shares','cost_minor']),
 'subscriptions':('id',['id','name','amount_minor','next_charge_date','status']),
 'contacts':('id',['id','name']),
 'contact_details':('contact_id',['contact_id','note']),
 'contact_profiles':('person_id',['person_id','relationship','birthday','special_attention']),
 'commitments':('id',['id','account_id','remaining_minor','status']),
 'transactions':('id',['id','account_id','counterparty','direction','amount_minor','category','occurred_at','description']),
}

def reset_history(db):
    """Rebase an explicit sandbox reset inside its existing transaction."""
    db.execute('DELETE FROM report_history')
    db.execute('DELETE FROM report_history_meta')
    moment="strftime('%Y-%m-%dT%H:%M:%f+00:00','now')"
    for entity,(key,fields) in ENTITIES.items():
        expression='json_object('+','.join("'"+f+"',\""+f+'"' for f in fields)+')'
        db.execute(f'INSERT INTO report_history(entity,entity_id,changed_at,payload) SELECT ?,"{key}",{moment},{expression} FROM "{entity}"',(entity,))
    db.execute(f'INSERT INTO report_history_meta VALUES(1,{moment})')

def install(db):
    db.executescript(SCHEMA)
    # Epoch-derived SQLite UTC timestamps are independent of the host timezone.
    moment="strftime('%Y-%m-%dT%H:%M:%f+00:00','now')"
    db.execute('BEGIN IMMEDIATE')
    try:
        first=not db.execute('SELECT 1 FROM report_history_meta WHERE id=1').fetchone()
        extended=False
        for entity,(key,fields) in ENTITIES.items():
            def expression(prefix=''):
                return 'json_object('+','.join("'"+f+"',"+prefix+'"'+f+'"' for f in fields)+')'
            new_entity=not db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",(f'report_{entity}_insert',)).fetchone()
            extended=extended or new_entity
            if new_entity and not first:db.execute('DELETE FROM report_history WHERE entity=?',(entity,))
            if first or new_entity:
                db.execute(f'INSERT INTO report_history(entity,entity_id,changed_at,payload) SELECT ?,"{key}",{moment},{expression()} FROM "{entity}"',(entity,))
            for operation in ('INSERT','UPDATE','DELETE'):
                prefix='OLD.' if operation=='DELETE' else 'NEW.'
                payload='NULL' if operation=='DELETE' else expression(prefix)
                condition='' if operation!='UPDATE' else 'WHEN '+ ' OR '.join(f'OLD."{f}" IS NOT NEW."{f}"' for f in fields)
                db.execute(f'''CREATE TRIGGER IF NOT EXISTS report_{entity}_{operation.lower()}
                    AFTER {operation} ON "{entity}" {condition} BEGIN
                    INSERT INTO report_history(entity,entity_id,changed_at,payload)
                    VALUES('{entity}',{prefix}"{key}",{moment},{payload}); END''')
        if first:db.execute(f'INSERT INTO report_history_meta VALUES(1,{moment})')
        elif extended:db.execute(f'UPDATE report_history_meta SET started_at={moment} WHERE id=1')
        db.commit()
    except BaseException:
        db.rollback();raise

def snapshot(db,cutoff):
    """Latest committed entity state strictly before the day-ending midnight."""
    beginning=db.execute('SELECT started_at FROM report_history_meta WHERE id=1').fetchone()
    if not beginning or beginning[0]>=cutoff:return None
    rows=db.execute('''SELECT entity,payload FROM (
        SELECT entity,payload,ROW_NUMBER() OVER(PARTITION BY entity,entity_id ORDER BY changed_at DESC,seq DESC) AS rank
        FROM report_history WHERE changed_at<?) WHERE rank=1 AND payload IS NOT NULL''',(cutoff,))
    result={key:[] for key in ENTITIES}
    for row in rows:result[row['entity']].append(json.loads(row['payload']))
    return result
