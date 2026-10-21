import asyncio
import io
import math
import struct
import threading
import wave

import httpx
import pytest
from fastapi import HTTPException
from banking.main import create_app
from banking.speech import read_wave, MAX_BYTES
from test_security import register


def audio(seconds=1,rate=16000,channels=1):
    pcm=b''.join(struct.pack('<h',int(5000*math.sin(i*0.12))) for i in range(int(seconds*rate)*channels))
    buffer=io.BytesIO()
    with wave.open(buffer,'wb') as wav:
        wav.setnchannels(channels);wav.setsampwidth(2);wav.setframerate(rate);wav.writeframes(pcm)
    return buffer.getvalue()


@pytest.mark.parametrize('data',[b'not a wave',audio(0.1),audio(1,48000),audio(1,16000,2),audio()[:-5]],ids=['not-wave','too-short','wrong-rate','stereo','truncated'])
def test_invalid_or_truncated_audio_rejected(data):
    with pytest.raises(HTTPException):read_wave(data)


def test_duration_and_payload_bounds():
    pcm,duration=read_wave(audio())
    assert len(pcm)==32000 and duration==1
    with pytest.raises(HTTPException) as error:read_wave(b'x'*(MAX_BYTES+1))
    assert error.value.status_code==413


async def test_authenticated_transcription_never_submits_tasks_or_moves_money(settings,monkeypatch):
    settings.app_auth_enabled=True
    monkeypatch.setattr('banking.speech.recognize',lambda pcm,path:'给张三转1000元')
    app=create_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        assert (await client.post('/api/speech/transcribe',content=audio(),headers={'Content-Type':'audio/wav'})).status_code==401
        await register(client)
        with app.state.database.connection() as db:
            balances=[tuple(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')]
            messages=db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        response=await client.post('/api/speech/transcribe',content=audio(),headers={'Content-Type':'audio/wav'})
        assert response.status_code==200 and response.json()['text']=='给张三转1000元'
        assert response.json()['local'] is True
        with app.state.database.connection() as db:
            assert [tuple(r) for r in db.execute('SELECT * FROM accounts ORDER BY id')]==balances
            assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==messages
            assert not db.execute('SELECT 1 FROM tasks').fetchone()
            event=db.execute("SELECT data_json FROM events WHERE type='speech.transcribed'").fetchone()[0]
            assert '1000' not in event and '给张三' not in event
        assert (await client.post('/api/speech/transcribe',content=audio(),headers={'Content-Type':'audio/mp4'})).status_code==415
        assert (await client.post('/api/speech/transcribe',content=b'bad',headers={'Content-Type':'audio/wav'})).status_code==422


async def test_rate_limit_and_busy_response(settings,monkeypatch):
    settings.app_auth_enabled=True;started=threading.Event();release=threading.Event()
    def slow(pcm,path):started.set();release.wait(5);return '查询余额'
    monkeypatch.setattr('banking.speech.recognize',slow)
    app=create_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client);payload={'content':audio(),'headers':{'Content-Type':'audio/wav'}}
        first=asyncio.create_task(client.post('/api/speech/transcribe',**payload))
        try:
            assert await asyncio.to_thread(started.wait,3)
            assert (await client.post('/api/speech/transcribe',**payload)).status_code==429
        finally:release.set()
        assert (await first).status_code==200
        monkeypatch.setattr('banking.speech.recognize',lambda pcm,path:'查询余额')
        for _ in range(8):assert (await client.post('/api/speech/transcribe',**payload)).status_code==200
        assert (await client.post('/api/speech/transcribe',**payload)).status_code==429


async def test_missing_model_is_clear_and_does_not_fabricate_text(settings):
    settings.app_auth_enabled=True;settings.speech_model_dir=settings.database_path.parent/'missing'
    app=create_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        await register(client)
        assert (await client.get('/api/speech/status')).json()['ready'] is False
        response=await client.post('/api/speech/transcribe',content=audio(),headers={'Content-Type':'audio/wav'})
        assert response.status_code==503 and '模型尚未准备好' in response.json()['detail']
