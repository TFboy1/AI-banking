"""Download pinned official models; never downloads while handling biometric requests."""
import hashlib
import json
from pathlib import Path
import httpx

root=Path(__file__).resolve().parents[1]
manifest=json.loads((root/'scripts/face-models.json').read_text(encoding='utf-8'))
with httpx.Client(timeout=240,follow_redirects=True,trust_env=False) as client:
    for entry in manifest:
        dest=root/entry['path']
        if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest()==entry['sha256']:continue
        response=client.get(entry['url']);response.raise_for_status()
        if hashlib.sha256(response.content).hexdigest()!=entry['sha256']:raise RuntimeError('Model checksum mismatch: '+dest.name)
        dest.parent.mkdir(parents=True,exist_ok=True)
        temp=dest.with_suffix(dest.suffix+'.partial');temp.write_bytes(response.content);temp.replace(dest)
        print('Installed',dest.name)
print('Local face models ready. No biometric data collected.')
