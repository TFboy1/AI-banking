import httpx
from banking.main import create_app
from test_security import register

async def test_face_onboarding_can_be_skipped_and_is_saved(settings):
    settings.app_auth_enabled=True
    app=create_app(settings)
    async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8000') as client:
        assert (await client.post('/api/auth/face-onboarding',json={})).status_code==401
        state=(await register(client)).json()
        assert state['ready'] and state['face_onboarding_done'] is False
        assert not (await client.get('/api/face/status')).json()['bound']
        state=(await client.post('/api/auth/face-onboarding',json={})).json()
        assert state['ready'] and state['face_onboarding_done']
        app.state.database.initialize()
        assert (await client.get('/api/auth/status')).json()['face_onboarding_done']
        assert not (await client.get('/api/face/status')).json()['bound']
