"""Trusted local demo operator only. No HTTP approval or self-service unlock."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
from banking.db import Database
from banking.account_lock import review

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='人工审核演示账户解锁申请')
    parser.add_argument('--database',required=True,type=Path)
    parser.add_argument('--request-id',required=True)
    parser.add_argument('--decision',required=True,choices=['approve','reject'])
    parser.add_argument('--operator',required=True)
    parser.add_argument('--reason',required=True)
    args=parser.parse_args()
    if not args.database.is_file():parser.error('必须指定已存在的当前用户数据库。')
    result=review(Database(args.database,seed_demo=False),args.request_id,args.decision=='approve',args.operator,args.reason)
    print('人工审核完成：'+('已解锁，原交易须重新确认。' if not result['system_locked'] else '保持锁定。'))
