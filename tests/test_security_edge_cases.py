import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from crows.security import LocalAPIProtection


@pytest.mark.asyncio
async def test_oversized_content_length_is_rejected_before_route(monkeypatch):
    monkeypatch.setenv('AETHER_API_TOKEN', 'test-token')
    reached_app = False

    async def app(scope, receive, send):
        nonlocal reached_app
        reached_app = True

    middleware = LocalAPIProtection(app)
    scope = {
        'type': 'http', 'method': 'POST', 'path': '/mutate', 'scheme': 'http',
        'headers': [(b'host', b'127.0.0.1:8000'), (b'x-aether-token', b'test-token'),
                    (b'content-length', str(3 * 1024 * 1024).encode())],
        'client': ('127.0.0.1', 12345),
    }
    sent = []

    async def receive():
        return {'type': 'http.request', 'body': b'x', 'more_body': False}

    async def send(message):
        sent.append(message)

    await middleware(scope, receive, send)
    assert not reached_app
    assert sent[0]['status'] == 413


@pytest.mark.asyncio
async def test_content_length_limit_does_not_affect_health_or_ui(monkeypatch):
    monkeypatch.setenv('AETHER_API_TOKEN', 'test-token')
    app = FastAPI()
    app.add_middleware(LocalAPIProtection)

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @app.post('/ui/action')
    async def ui_action(request: Request):
        return {'body': await request.body()}

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
        health = await client.get('/health')
        ui = await client.post('/ui/action', content=b'x' * 1024, headers={'Content-Length': '1024'})

    assert health.status_code == 200
    assert ui.status_code == 200
