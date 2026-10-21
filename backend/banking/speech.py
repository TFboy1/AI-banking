"""Authenticated, bounded local ASR. Recognition never submits a chat or bank action."""
import asyncio
import io
import re
import threading
import time
import wave
from collections import deque
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from .db import event

RATE = 16000
MAX_SECONDS = 60
MAX_BYTES = RATE * 2 * MAX_SECONDS + 4096
_inference_lock = threading.Lock()
_engine = None
_engine_path = None


def read_wave(data):
    if not data or len(data)>MAX_BYTES:
        raise HTTPException(413,'录音最长60秒，请分段录制。')
    try:
        with wave.open(io.BytesIO(data),'rb') as audio:
            if (audio.getnchannels(),audio.getsampwidth(),audio.getframerate(),audio.getcomptype())!=(1,2,RATE,'NONE'):
                raise HTTPException(422,'请使用16kHz单声道PCM录音。')
            frames=audio.getnframes()
            if not RATE//5<=frames<=RATE*MAX_SECONDS:
                raise HTTPException(422,'请录制0.2至60秒的语音。')
            pcm=audio.readframes(frames)
            if len(pcm)!=frames*2:
                raise HTTPException(422,'录音文件不完整，请重新录制。')
            return pcm,frames/RATE
    except (wave.Error,EOFError,ValueError) as exc:
        raise HTTPException(422,'无法读取录音，请重新录制。') from exc


def recognize(pcm, model_dir):
    global _engine,_engine_path
    model_dir=Path(model_dir).resolve()
    if not (model_dir/'model.int8.onnx').is_file() or not (model_dir/'tokens.txt').is_file():
        raise HTTPException(503,'本地语音模型尚未准备好，请稍后重试或使用文字输入。')
    try:
        import numpy as np
        import sherpa_onnx
    except ImportError as exc:
        raise HTTPException(503,'本地语音服务尚未安装，请暂时使用文字输入。') from exc
    samples=np.frombuffer(pcm,dtype='<i2').astype(np.float32)/32768.0
    if float(np.sqrt(np.mean(samples*samples)))<0.002:
        raise HTTPException(422,'没有听到清晰语音，请靠近麦克风再试。')
    if _engine is None or _engine_path!=model_dir:
        _engine=sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_dir/'model.int8.onnx'),tokens=str(model_dir/'tokens.txt'),
            num_threads=2,provider='cpu',language='auto',use_itn=True,debug=False)
        _engine_path=model_dir
    stream=_engine.create_stream();stream.accept_waveform(RATE,samples);_engine.decode_stream(stream)
    text=re.sub(r'<\|[^|]*\|>','',stream.result.text).strip()
    if not text:
        raise HTTPException(422,'没有识别到清晰语音，请重新录制。')
    if len(text)>4000:
        raise HTTPException(422,'识别内容过长，请分段录制。')
    return text


def register_speech(app,database,settings,session):
    attempts=deque()

    @app.get('/api/speech/status',dependencies=[Depends(session)])
    async def status():
        model=Path(settings.speech_model_dir)
        return {'ready':(model/'model.int8.onnx').is_file() and (model/'tokens.txt').is_file(),
                'local':True,'max_seconds':MAX_SECONDS,'engine':'SenseVoice / sherpa-onnx'}

    @app.post('/api/speech/transcribe')
    async def transcribe(request:Request,session_key:str=Depends(session)):
        if request.headers.get('content-type','').split(';')[0].lower() not in {'audio/wav','audio/wave','audio/x-wav'}:
            raise HTTPException(415,'请上传浏览器录制的WAV音频。')
        current=time.monotonic()
        while attempts and attempts[0]<current-60:attempts.popleft()
        if len(attempts)>=10:raise HTTPException(429,'语音输入过于频繁，请稍后再试。')
        attempts.append(current)
        if not _inference_lock.acquire(blocking=False):
            raise HTTPException(429,'语音服务正在识别另一段录音，请稍后重试。')
        try:
            data=bytearray()
            async with asyncio.timeout(15):
                async for chunk in request.stream():
                    data.extend(chunk)
                    if len(data)>MAX_BYTES:raise HTTPException(413,'录音最长60秒，请分段录制。')
            pcm,duration=read_wave(data)
            del data
            # Shield inference during disconnect/cancellation: release the CPU gate only when it finishes.
            task=asyncio.create_task(asyncio.to_thread(recognize,pcm,settings.speech_model_dir))
            try:text=await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            with database.transaction() as db:
                event(db,'*','speech.transcribed',{'duration_seconds':round(duration,2),'characters':len(text),'local':True})
            return {'text':text,'duration_seconds':round(duration,2),'local':True}
        except HTTPException:raise
        except Exception as exc:
            raise HTTPException(503,'语音识别暂时未完成，请重试或使用文字输入。') from exc
        finally:_inference_lock.release()
