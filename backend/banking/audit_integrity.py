"""Transactional hash journal. Signatures and trusted heads live outside this DB."""
import hashlib,json,sqlite3,uuid
from .db import now

ZERO='0'*64
TABLES=('accounts','cards','holdings','conversations','messages','tasks','approvals','transactions',
        'events','safety_events','human_sessions','human_audit','scheduled_transfers','scheduled_actions',
        'travel_orders','travel_order_state','travel_changes','food_orders','securities_positions','admin_audit')
FIELDS=('seq','table_name','row_key','operation','old_hash','new_hash','occurred_at','prev_hash')
SCHEMA=(
 '''CREATE TABLE IF NOT EXISTS audit_integrity_meta(id INTEGER PRIMARY KEY CHECK(id=1),instance_id TEXT NOT NULL,started_at TEXT NOT NULL,tables_json TEXT NOT NULL)''',
 '''CREATE TABLE IF NOT EXISTS audit_integrity_records(seq INTEGER PRIMARY KEY,table_name TEXT NOT NULL,row_key TEXT NOT NULL,
 operation TEXT NOT NULL,old_hash TEXT,new_hash TEXT,occurred_at TEXT NOT NULL,prev_hash TEXT NOT NULL,record_hash TEXT NOT NULL)''',
)

def encoded(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True,allow_nan=False)
def hashed(value):return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()
def sql_hash(value):return hashed(json.loads(value))
def q(name):return '"'+name.replace('"','""')+'"'
def record_hash(row):return hashed([row[k] for k in FIELDS])
def protection_sql(table,action):
    return f'''CREATE TRIGGER IF NOT EXISTS {q('protect_'+table+'_'+action.lower())} BEFORE {action} ON {table}
 BEGIN SELECT RAISE(ABORT,'audit journal is append-only'); END'''


def register_functions(db):db.create_function('audit_integrity_hash',1,sql_hash,deterministic=True)


def append(db,table,key,operation,old,new):
    tail=db.execute('SELECT seq,record_hash FROM audit_integrity_records ORDER BY seq DESC LIMIT 1').fetchone()
    values=[tail[0]+1 if tail else 1,table,key,operation,old,new,now(),tail[1] if tail else ZERO]
    db.execute('INSERT INTO audit_integrity_records VALUES(?,?,?,?,?,?,?,?,?)',(*values,hashed(values)))


def trigger_sql(table,spec,action):
    reference='OLD' if action=='DELETE' else 'NEW'
    # Drop SQLite's JSON subtype: row_key is TEXT inside the record hash.
    key='(json_array('+','.join(reference+'.'+q(c) for c in spec['keys'])+") || '')"
    def rowhash(prefix):return 'audit_integrity_hash(json_array('+','.join(prefix+'.'+q(c) for c in spec['columns'])+'))'
    old=rowhash('OLD') if action!='INSERT' else 'NULL'
    new=rowhash('NEW') if action!='DELETE' else 'NULL'
    return f'''CREATE TRIGGER IF NOT EXISTS {q('integrity_'+table+'_'+action.lower())} AFTER {action} ON {q(table)} BEGIN
 INSERT INTO audit_integrity_records
 SELECT n,'{table}',{key},'{action.lower()}',{old},{new},stamp,previous,
 audit_integrity_hash(json_array(n,'{table}',{key},'{action.lower()}',{old},{new},stamp,previous))
 FROM (SELECT COALESCE(MAX(seq),0)+1 AS n,
 COALESCE((SELECT record_hash FROM audit_integrity_records ORDER BY seq DESC LIMIT 1),'{ZERO}') AS previous,
 strftime('%Y-%m-%dT%H:%M:%fZ','now') AS stamp FROM audit_integrity_records);
 END'''


def install(db):
    """First adoption establishes a baseline, never rewrites existing journal history."""
    db.execute('SAVEPOINT integrity_install')
    try:
        for sql in SCHEMA:db.execute(sql)
        meta=db.execute('SELECT * FROM audit_integrity_meta WHERE id=1').fetchone()
        if not meta:
            specs={}
            for table in TABLES:
                columns=db.execute('PRAGMA table_info('+q(table)+')').fetchall()
                if not columns:continue
                keys=[c['name'] for c in sorted(columns,key=lambda c:c['pk']) if c['pk']]
                if not keys:continue
                specs[table]={'columns':[c['name'] for c in columns],'keys':keys}
            db.execute('INSERT INTO audit_integrity_meta VALUES(1,?,?,?)',(uuid.uuid4().hex,now(),encoded(specs)))
            for table,spec in specs.items():
                for row in db.execute('SELECT * FROM '+q(table)).fetchall():
                    append(db,table,encoded([row[k] for k in spec['keys']]),'baseline',None,hashed([row[c] for c in spec['columns']]))
        else:specs=json.loads(meta['tables_json'])
        # Ordinary application SQL cannot rewrite the journal or its identity.
        for table in ('audit_integrity_records','audit_integrity_meta'):
            for action in ('UPDATE','DELETE'):
                db.execute(protection_sql(table,action))
        for table,spec in specs.items():
            if table not in TABLES:raise ValueError('Unknown audit scope')
            for action in ('INSERT','UPDATE','DELETE'):db.execute(trigger_sql(table,spec,action))
        db.execute('RELEASE integrity_install')
    except BaseException:
        db.execute('ROLLBACK TO integrity_install');db.execute('RELEASE integrity_install');raise


def inspect(db):
    """Validate chain, row transitions, expected triggers and current row fingerprints."""
    meta=db.execute('SELECT * FROM audit_integrity_meta WHERE id=1').fetchone()
    if not meta:raise ValueError('审计基线缺失。')
    meta=dict(meta);specs=json.loads(meta['tables_json']);state={};head=ZERO;records=[]
    for table in ('audit_integrity_records','audit_integrity_meta'):
        for action in ('UPDATE','DELETE'):
            actual=db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",('protect_'+table+'_'+action.lower(),)).fetchone()
            if not actual or actual[0].replace('IF NOT EXISTS ','').strip()!=protection_sql(table,action).replace('IF NOT EXISTS ','').strip():
                raise ValueError('审计只追加保护缺失或发生变化。')
    for index,row in enumerate(db.execute('SELECT * FROM audit_integrity_records ORDER BY seq'),1):
        row=dict(row)
        if row['seq']!=index or row['prev_hash']!=head or record_hash(row)!=row['record_hash']:
            raise ValueError(f'审计链第{index}条记录不一致。')
        if row['table_name'] not in specs:raise ValueError(f'审计链第{index}条范围不一致。')
        key=(row['table_name'],row['row_key']);before=state.get(key)
        if row['operation'] in {'baseline','insert'}:
            if before is not None or row['old_hash'] is not None or row['new_hash'] is None:raise ValueError(f'审计链第{index}条新增记录不一致。')
        elif row['operation'] in {'update','delete'}:
            if before is None or before!=row['old_hash']:raise ValueError(f'审计链第{index}条变更依据不一致。')
            if (row['operation']=='delete')!=(row['new_hash'] is None):raise ValueError(f'审计链第{index}条变更结果不一致。')
        else:raise ValueError(f'审计链第{index}条操作无效。')
        if row['new_hash'] is None:state.pop(key,None)
        else:state[key]=row['new_hash']
        head=row['record_hash'];records.append(row)
    for table,spec in specs.items():
        if table not in TABLES:raise ValueError('审计范围无效。')
        columns=[c['name'] for c in db.execute('PRAGMA table_info('+q(table)+')')]
        if columns!=spec['columns']:raise ValueError('审计对象结构发生变化：'+table)
        for action in ('INSERT','UPDATE','DELETE'):
            actual=db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",('integrity_'+table+'_'+action.lower(),)).fetchone()
            # SQLite strips CREATE's optional IF NOT EXISTS from stored SQL.
            if not actual or actual[0].replace('IF NOT EXISTS ','').strip()!=trigger_sql(table,spec,action).replace('IF NOT EXISTS ','').strip():
                raise ValueError('审计记录触发器缺失或发生变化：'+table)
        for row in db.execute('SELECT * FROM '+q(table)):
            key=(table,encoded([row[k] for k in spec['keys']]))
            if state.pop(key,None)!=hashed([row[c] for c in spec['columns']]):raise ValueError('当前数据与审计记录不一致：'+table)
    if state:raise ValueError('存在没有对应当前数据的审计记录。')
    return meta,records,head
