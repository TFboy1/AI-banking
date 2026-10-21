"""Restore the sanitized demo snapshot into a NEW local data directory only."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
TABLES=('accounts','cards','linked_card_accounts','contacts','contact_details','contact_links','contact_phones','contact_profiles','products','product_nav_history','holdings','risk_profile','subscriptions','transactions','securities_positions','securities_orders','payment_policy','daily_report_preferences')


def insert_rows(db,table,rows):
    allowed={r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
    for row in rows:
        columns=list(row)
        if not columns or not set(columns)<=allowed:
            raise ValueError(f'Unexpected columns in {table}')
        names=','.join('"'+key+'"' for key in columns)
        db.execute(f'INSERT OR REPLACE INTO "{table}" ({names}) VALUES ({",".join("?" for _ in columns)})',tuple(row[key] for key in columns))


def restore(data_dir,source):
    data_dir=Path(data_dir).resolve()
    if any((data_dir/name).exists() for name in ('banking.sqlite3','accounts.sqlite3')):
        raise SystemExit('Existing account data found. Refusing to overwrite; choose a NEW --data-dir.')
    snapshot=json.loads(Path(source).read_text(encoding='utf-8'))
    if snapshot.get('format')!=1 or snapshot.get('sanitized') is not True or snapshot['identity']['phone']!='11111111111':
        raise ValueError('Only the sanitized demo snapshot is supported')
    if set(snapshot['bank'])!=set(TABLES):raise ValueError('Unexpected demo table list')
    sys.path.insert(0,str(ROOT/'backend'))
    from banking.db import Database
    from banking.security import encode_pin
    database=Database(data_dir/'banking.sqlite3',seed_demo=False)
    database.initialize()
    with database.transaction() as db:
        for table in reversed(TABLES):db.execute(f'DELETE FROM "{table}"')
        for table in TABLES:insert_rows(db,table,snapshot['bank'][table])
        for key,value in snapshot['meta'].items():
            if key not in {'food_owner','securities_owner','travel_owner'}:raise ValueError('Unexpected demo metadata')
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,value))
        person=snapshot['identity']
        # A public demo PIN is generated locally; no original PIN hash is shipped.
        db.execute('INSERT INTO app_identity(id,username,phone,pin_hash,lock_enabled,contacts_done) VALUES(1,?,?,?,?,1)',(person['username'],'11111111111',encode_pin('0000'),person['lock_enabled']))
        db.execute('UPDATE face_identity SET template=NULL,version=0,updated_at=NULL WHERE id=1')
        db.execute('UPDATE face_onboarding SET done=1 WHERE id=1')
    from banking.stock_site import create_stock_app
    from banking.food_site import create_food_app
    from banking.travel_site import create_travel_app
    create_stock_app(data_dir/'stock-market-cache.sqlite3',broker_path=data_dir/'securities-users.sqlite3',key_path=data_dir/'.securities-bridge-key')
    create_food_app(data_dir/'food-users.sqlite3',data_dir/'.food-bridge-key')
    create_travel_app(data_dir/'travel-users.sqlite3',data_dir/'.travel-bridge-key')
    for kind,filename,user_table in [('securities','securities-users.sqlite3','stock_users'),('food','food-users.sqlite3','food_users'),('travel','travel-users.sqlite3','travel_users')]:
        values=snapshot['platforms'].get(kind,{})
        with sqlite3.connect(data_dir/filename) as db:
            insert_rows(db,user_table,values.get('users',[]))
            extras=('investors','watchlist','stock_identities') if kind=='securities' else ('food_places',) if kind=='food' else ()
            for table in extras:insert_rows(db,table,values.get(table,[]))
    print('Demo restored. Phone: 11111111111; public demo payment PIN: 0000.')
    print('Sign in through simulated SMS, then generate a NEW universal pairing code for other sites.')
    print('No sessions, face templates, API keys or previous transaction authorizations were restored.')
    return data_dir


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=ROOT/'data')
    parser.add_argument('--snapshot',type=Path,default=ROOT/'resources/demo/demo-account.json')
    args=parser.parse_args()
    restore(args.data_dir,args.snapshot)
