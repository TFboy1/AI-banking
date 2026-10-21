"""Download the pinned SenseVoice ONNX model; no user audio is uploaded."""
import argparse
import hashlib
from pathlib import Path
from urllib.request import urlopen

REPOSITORY = 'csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09'
REVISION = '355f4d4884d8afd08aef04b9007a8556d7b463b2'
MODEL_SHA256 = '12ca1a2ae7ecf3e0019ef2822307ee0b5cadc9196569e379b4c4026f8205276d'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--directory',type=Path,default=Path(__file__).resolve().parents[1]/'data/speech')
    args=parser.parse_args();args.directory.mkdir(parents=True,exist_ok=True)
    for name in ['model.int8.onnx','tokens.txt']:
        target=args.directory/name
        if target.exists() and (name!='model.int8.onnx' or hashlib.sha256(target.read_bytes()).hexdigest()==MODEL_SHA256):
            print(f'{name}: already present',flush=True);continue
        temporary=target.with_suffix(target.suffix+'.part')
        url=f'https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}'
        print(f'Downloading {name}',flush=True)
        total=0;reported=0;digest=hashlib.sha256()
        with urlopen(url,timeout=60) as response,temporary.open('wb') as output:
            while chunk:=response.read(1024*1024):
                output.write(chunk);digest.update(chunk);total+=len(chunk)
                if total-reported>=20*1024*1024:
                    reported=total;print(f'{name}: {total//1024//1024} MiB',flush=True)
        if name=='model.int8.onnx' and digest.hexdigest()!=MODEL_SHA256:
            raise RuntimeError('Model checksum mismatch; partial file was not installed.')
        temporary.replace(target);print(f'{name}: verified, {total} bytes',flush=True)
    (args.directory/'SOURCE.txt').write_text(f'Model: {REPOSITORY}\nRevision: {REVISION}\nSHA256: {MODEL_SHA256}\nRuntime: sherpa-onnx (Apache-2.0); model licensing is separate.\nModel provenance: https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html\nOriginal model/license: https://github.com/QwenAudio/SenseVoice\n',encoding='utf-8')


if __name__=='__main__':main()
