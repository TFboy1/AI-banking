"""Admin-owned Ed25519 witness. Banking code never receives its signing key."""
import base64,hashlib,json,os,sqlite3,threading
from contextlib import contextmanager
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey,Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,PublicFormat,NoEncryption
from .audit_integrity import encoded,hashed,inspect,install,ZERO,append,q,protection_sql,trigger_sql
from .db import now

LIMIT='当前进程和目录分离，但未隔离系统账号；有权访问银行与凭据目录的同账号进程或主机管理员仍可破坏证据。生产应异机、独立权限并使用WORM等存储保护。'
BASELINE='启用前的已有记录仅建立基线，不能追溯证明启用前未被修改；完整性校验不证明业务内容真实或从未漏记。'

class Witness:
    def __init__(self,root):self.root=Path(root);self.store=self.root/'witness.sqlite3';self.lock=threading.RLock()

    def prepare(self):
        self.root.mkdir(parents=True,exist_ok=True)
        public=self.root/'public.raw';private=self.root/'signing-private.raw'
        if not self.store.exists() and (public.exists() or private.exists()):raise ValueError('独立凭据库缺失，禁止自动重建基线。')
        if not self.store.exists():
            key=Ed25519PrivateKey.generate()
            for path,data in [(private,key.private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption())),(public,key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw))]:
                with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'wb') as f:f.write(data)
            with self.connection() as db:
                db.execute('CREATE TABLE checkpoints(id INTEGER PRIMARY KEY AUTOINCREMENT,tenant TEXT NOT NULL,payload TEXT NOT NULL,signature TEXT NOT NULL)')
                db.execute('CREATE TABLE copies(tenant TEXT NOT NULL,seq INTEGER NOT NULL,record_hash TEXT NOT NULL,record_json TEXT NOT NULL,PRIMARY KEY(tenant,seq))')
        if not public.exists():raise ValueError('可信公钥缺失，禁止自动替换。')
        return public.read_bytes()

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.store,timeout=10);db.row_factory=sqlite3.Row
        try:
            db.execute('BEGIN IMMEDIATE');yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def tenant(self,database):return hashlib.sha256(str(database.path.resolve()).encode()).hexdigest()

    def latest(self,db,tenant,public):
        previous=ZERO;last=None;verifier=Ed25519PublicKey.from_public_bytes(public)
        for row in db.execute('SELECT * FROM checkpoints WHERE tenant=? ORDER BY id',(tenant,)):
            try:
                body=json.loads(row['payload']);verifier.verify(base64.b64decode(row['signature'],validate=True),row['payload'].encode())
            except Exception:raise ValueError('独立检查点签名无效。') from None
            if body['previous_checkpoint']!=previous or body['tenant']!=tenant:raise ValueError('独立检查点历史不连续。')
            if last:
                changed=body['meta_hash']!=last['meta_hash']
                if body['count']<last['count'] or body['instance_id']!=last['instance_id'] or (body['count']==last['count'] and not changed):raise ValueError('独立检查点出现回退或身份变化。')
                if changed and body.get('scope_extension',{}).get('previous_meta_hash')!=last['meta_hash']:raise ValueError('保护范围变化缺少已签名的衔接记录。')
            previous=hashed([row['payload'],row['signature']]);last={**body,'signature':row['signature'],'checkpoint_hash':previous}
        return last

    def check(self,database,seal=False):
        with self.lock:return self._check(database,seal)

    def _check(self,database,seal=False):
        checkpoint=None;public=None
        try:
            public=self.prepare();tenant=self.tenant(database)
            with self.connection() as witness:
                checkpoint=self.latest(witness,tenant,public)
                with database.connection() as db:
                    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audit_integrity_meta'").fetchone()
                    if not exists:
                        if checkpoint:raise ValueError('银行审计记录缺失，拒绝覆盖已有检查点。')
                        if not seal:return self.result('unanchored','尚未启用此账户的审计基线。')
                        install(db)
                    db.execute('BEGIN')
                    meta,records,head=inspect(db)
                    meta_hash=hashed([meta['instance_id'],meta['started_at'],json.loads(meta['tables_json'])])
                    if checkpoint:
                        if checkpoint['instance_id']!=meta['instance_id'] or checkpoint['meta_hash']!=meta_hash:raise ValueError('审计身份或保护范围与独立检查点不一致。')
                        count=checkpoint['count']
                        if len(records)<count:raise ValueError('记录数少于已签名检查点，可能删除了末尾记录或恢复了旧数据库。')
                        if (records[count-1]['record_hash'] if count else ZERO)!=checkpoint['head_hash']:raise ValueError('已签名历史被修改或重算，拒绝更新检查点。')
                        copies=list(witness.execute('SELECT * FROM copies WHERE tenant=? ORDER BY seq',(tenant,)))
                        if len(copies)!=count:raise ValueError('独立记录副本数量与签名检查点不一致。')
                        for original,copy in zip(records,copies):
                            if original['record_hash']!=copy['record_hash'] or encoded(original)!=copy['record_json']:raise ValueError('审计记录与独立历史副本不一致。')
                    elif witness.execute('SELECT 1 FROM copies WHERE tenant=?',(tenant,)).fetchone():raise ValueError('独立签名检查点缺失，禁止重新建立信任。')
                    if seal and (not checkpoint or len(records)>checkpoint['count']):
                        private=Ed25519PrivateKey.from_private_bytes((self.root/'signing-private.raw').read_bytes())
                        if private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)!=public:raise ValueError('签名密钥与可信公钥不一致。')
                        body={'version':1,'tenant':tenant,'instance_id':meta['instance_id'],'meta_hash':meta_hash,
                              'count':len(records),'head_hash':head,'sealed_at':now(),
                              'previous_checkpoint':checkpoint['checkpoint_hash'] if checkpoint else ZERO}
                        raw=encoded(body);signature=base64.b64encode(private.sign(raw.encode())).decode()
                        start=checkpoint['count'] if checkpoint else 0
                        for row in records[start:]:witness.execute('INSERT INTO copies VALUES(?,?,?,?)',(tenant,row['seq'],row['record_hash'],encoded(row)))
                        witness.execute('INSERT INTO checkpoints(tenant,payload,signature) VALUES(?,?,?)',(tenant,raw,signature))
                        checkpoint={**body,'signature':signature,'checkpoint_hash':hashed([raw,signature])}
                    status='verified' if checkpoint and checkpoint['count']==len(records) else 'pending' if checkpoint else 'unanchored'
                    return self.result(status,{'verified':'校验通过，当前记录已锚定到独立签名检查点。','pending':'记录链与当前数据一致，但新增记录尚未签名。','unanchored':'记录链有效，尚无独立签名检查点。'}[status],
                        count=len(records),anchored_count=checkpoint['count'] if checkpoint else 0,head_hash=head,baseline_at=meta['started_at'],
                        checkpoint_at=checkpoint['sealed_at'] if checkpoint else None,key_id=hashlib.sha256(public).hexdigest()[:16],
                        scope=list(json.loads(meta['tables_json'])),proof={'payload':{k:v for k,v in checkpoint.items() if k not in {'signature','checkpoint_hash'}},'signature':checkpoint['signature'],'public_key':base64.b64encode(public).decode()} if checkpoint else None)
        except (ValueError,sqlite3.DatabaseError) as exc:return self.result('alert',str(exc),**self.trusted(checkpoint,public))
        except (OSError,KeyError,TypeError):return self.result('unavailable','审计凭据或签名服务不可用，未重新签名；请检查独立存储与密钥。',**self.trusted(checkpoint,public))

    def trusted(self,checkpoint,public):
        if not checkpoint or not public:return {}
        return {'anchored_count':checkpoint['count'],'checkpoint_at':checkpoint['sealed_at'],
                'key_id':hashlib.sha256(public).hexdigest()[:16],
                'proof':{'payload':{k:v for k,v in checkpoint.items() if k not in {'signature','checkpoint_hash'}},
                         'signature':checkpoint['signature'],'public_key':base64.b64encode(public).decode()}}

    def result(self,status,message,**kw):return {'status':status,'message':message,'checked_at':now(),'protection_note':LIMIT,'baseline_note':BASELINE,**kw}

    def extend_known_scope(self,database):
        """Trusted deployment migration only; no HTTP endpoint, reset or shrink."""
        with self.lock:
            verified=self._check(database,True)
            if verified['status']!='verified':return verified
            public=(self.root/'public.raw').read_bytes();tenant=self.tenant(database)
            with database.connection() as read:
                specs=json.loads(read.execute('SELECT tables_json FROM audit_integrity_meta WHERE id=1').fetchone()[0])
                missing=[t for t in ('human_audit',) if t not in specs and read.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(t,)).fetchone()]
            if not missing:return verified
            try:
                with database.transaction() as db,self.connection() as witness:
                    checkpoint=self.latest(witness,tenant,public)
                    meta,records,_=inspect(db)
                    old_hash=hashed([meta['instance_id'],meta['started_at'],json.loads(meta['tables_json'])])
                    if old_hash!=checkpoint['meta_hash'] or len(records)<checkpoint['count'] or (records[checkpoint['count']-1]['record_hash'] if checkpoint['count'] else ZERO)!=checkpoint['head_hash']:
                        raise ValueError('范围扩展前记录发生变化，拒绝迁移。')
                    specs=json.loads(meta['tables_json'])
                    for table in missing:
                        columns=db.execute('PRAGMA table_info('+q(table)+')').fetchall()
                        spec={'columns':[c['name'] for c in columns],'keys':[c['name'] for c in sorted(columns,key=lambda c:c['pk']) if c['pk']]}
                        if not spec['keys']:raise ValueError('新增审计对象缺少主键。')
                        specs[table]=spec
                    db.execute('DROP TRIGGER protect_audit_integrity_meta_update')
                    db.execute('UPDATE audit_integrity_meta SET tables_json=? WHERE id=1',(encoded(specs),))
                    db.execute(protection_sql('audit_integrity_meta','UPDATE'))
                    for table in missing:
                        spec=specs[table]
                        for row in db.execute('SELECT * FROM '+q(table)).fetchall():
                            append(db,table,encoded([row[k] for k in spec['keys']]),'baseline',None,hashed([row[c] for c in spec['columns']]))
                        for action in ('INSERT','UPDATE','DELETE'):db.execute(trigger_sql(table,spec,action))
                    meta,records,head=inspect(db)
                    body={'version':1,'tenant':tenant,'instance_id':meta['instance_id'],
                          'meta_hash':hashed([meta['instance_id'],meta['started_at'],json.loads(meta['tables_json'])]),
                          'count':len(records),'head_hash':head,'sealed_at':now(),'previous_checkpoint':checkpoint['checkpoint_hash'],
                          'scope_extension':{'previous_meta_hash':old_hash,'added_tables':missing,'baseline_at':now()}}
                    private=Ed25519PrivateKey.from_private_bytes((self.root/'signing-private.raw').read_bytes())
                    if private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)!=public:raise ValueError('迁移签名密钥与可信公钥不一致。')
                    raw=encoded(body);signature=base64.b64encode(private.sign(raw.encode())).decode()
                    for row in records[checkpoint['count']:]:witness.execute('INSERT INTO copies VALUES(?,?,?,?)',(tenant,row['seq'],row['record_hash'],encoded(row)))
                    witness.execute('INSERT INTO checkpoints(tenant,payload,signature) VALUES(?,?,?)',(tenant,raw,signature))
                return self._check(database)
            except (ValueError,sqlite3.DatabaseError,OSError):return self.result('alert','保护范围扩展未完成，请保留原凭据并人工核对；未重置历史。')
