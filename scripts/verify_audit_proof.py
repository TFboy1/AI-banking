"""Verify exported checkpoint using a previously trusted public key, not its own key."""
import argparse,base64,json
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

def verify(document,public):
    proof=document['proof']
    if base64.b64decode(proof['public_key'],validate=True)!=public:raise ValueError('导出公钥与预先保存的可信公钥不一致。')
    payload=json.dumps(proof['payload'],ensure_ascii=False,separators=(',',':'),sort_keys=True,allow_nan=False).encode()
    Ed25519PublicKey.from_public_bytes(public).verify(base64.b64decode(proof['signature'],validate=True),payload)
    return proof['payload']

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='验证已导出检查点的签名；不单独证明当前数据库完整。')
    parser.add_argument('proof',type=Path);parser.add_argument('--public-key',required=True,type=Path)
    args=parser.parse_args()
    try:
        document=json.loads(args.proof.read_text(encoding='utf-8'))
        body=verify(document,args.public_key.read_bytes())
        print(f"检查点签名有效：{body['count']}条记录，链头{body['head_hash']}，签名时间{body['sealed_at']}。")
        print('导出时的数据校验状态：'+str(document.get('status'))+'；本工具不单独证明当前数据库完整。')
    except Exception:raise SystemExit('校验失败：签名、内容或可信公钥不一致；请核查原始凭据。')
