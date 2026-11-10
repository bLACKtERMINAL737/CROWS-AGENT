import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from crows.security import LocalAPIProtection, _origin_is_local


def make_app(monkeypatch):
    monkeypatch.setenv('AETHER_API_TOKEN', 'test-token')
    app = FastAPI()
    app.add_middleware(LocalAPIProtection)
    @app.get('/health')
    def health():
        return {'status': 'ok'}
    @app.get('/private')
    def private():
        return {'private': True}
    @app.post('/mutate')
    def mutate():
        return {'changed': True}
    return app


@pytest.mark.asyncio
async def test_token_required_and_health_exempt(monkeypatch):
    app = make_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
        assert (await client.get('/health')).status_code == 200
        assert (await client.get('/private')).status_code == 401
        response = await client.get('/private', headers={'X-Aether-Token': 'test-token'})
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_mutation_rejects_cross_port_origin(monkeypatch):
    app = make_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://127.0.0.1:8000') as client:
        response = await client.post('/mutate', headers={
            'X-Aether-Token': 'test-token', 'Origin': 'http://127.0.0.1:9000'
        })
    assert response.status_code == 403


def test_origin_requires_same_local_host_and_port():
    assert _origin_is_local('http://127.0.0.1:8000', '127.0.0.1:8000')
    assert not _origin_is_local('http://127.0.0.1:9000', '127.0.0.1:8000')
    assert not _origin_is_local('https://example.com', '127.0.0.1:8000')
    assert not _origin_is_local('https://127.0.0.1:8000', '127.0.0.1:8000', 'http')


@pytest.mark.asyncio
async def test_remote_client_requires_token_when_token_is_unconfigured(monkeypatch):
    monkeypatch.delenv("AETHER_API_TOKEN", raising=False)
    app = FastAPI()
    app.add_middleware(LocalAPIProtection)

    @app.get("/private")
    def private():
        return {"private": True}

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.0.2.10", 12345)),
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = await client.get("/private")

    assert response.status_code == 403
    assert response.json()["detail"] == "Configure CROWS_API_TOKEN for non-loopback access"


@pytest.mark.asyncio
async def test_loopback_client_can_use_development_fallback_without_token(monkeypatch):
    monkeypatch.delenv("AETHER_API_TOKEN", raising=False)
    app = FastAPI()
    app.add_middleware(LocalAPIProtection)

    @app.get("/private")
    def private():
        return {"private": True}

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = await client.get("/private")

    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", ["", "Basic test-token", "Bearer ", "Bearer wrong"])
async def test_invalid_authorization_cannot_access_protected_route(monkeypatch, authorization):
    app = make_app(monkeypatch)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = await client.get("/private", headers={"Authorization": authorization})

    assert response.status_code == 401



@pytest.mark.parametrize(
    "origin",
    [
        "",
        "127.0.0.1:8000",
        "http:///path",
        "http://user@127.0.0.1:8000",
        "http://127.0.0.1:8000/path",
        "http://127.0.0.1:8000/?next=/private",
        "http://127.0.0.1:8000/#fragment",
        "http://127.0.0.1:invalid",
        "null",
    ],
)
def test_origin_validation_rejects_malformed_or_ambiguous_origins(origin):
    assert not _origin_is_local(origin, "127.0.0.1:8000", "http")


@pytest.mark.parametrize(
    ("origin", "request_host", "scheme"),
    [
        ("https://localhost:8000", "localhost:8000", "http"),
        ("http://localhost:8001", "localhost:8000", "http"),
        ("http://127.0.0.1:8000", "localhost:8000", "http"),
    ],
)
def test_origin_validation_requires_matching_scheme_host_and_port(origin, request_host, scheme):
    assert not _origin_is_local(origin, request_host, scheme)



@pytest.mark.parametrize(
    "origin",
    [
        "http://@127.0.0.1:8000",
        "http://:@127.0.0.1:8000",
        "http://user:@127.0.0.1:8000",
        "http://:password@127.0.0.1:8000",
        "http://127.0.0.1:0",
    ],
)
def test_origin_validation_rejects_empty_userinfo_and_zero_port(origin):
    assert not _origin_is_local(origin, "127.0.0.1:8000", "http")


def test_origin_validation_rejects_zero_port_in_request_host():
    assert not _origin_is_local("http://127.0.0.1", "127.0.0.1:0", "http")

@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
async def test_api_documentation_routes_require_authentication(monkeypatch, path):
    app = make_app(monkeypatch)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = await client.get(path)

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_chunked_request_body_limit_applies_without_content_length(monkeypatch):
    from crows.security import MAX_REQUEST_BODY_BYTES

    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    app_called = False

    async def inner_app(scope, receive, send):
        nonlocal app_called
        app_called = True
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = LocalAPIProtection(inner_app)
    chunks = [
        {"type": "http.request", "body": b"x" * (MAX_REQUEST_BODY_BYTES // 2), "more_body": True},
        {"type": "http.request", "body": b"x" * (MAX_REQUEST_BODY_BYTES // 2 + 1), "more_body": True},
    ]
    async def receive():
        return chunks.pop(0)

    sent = []
    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/private", "raw_path": b"/private",
        "query_string": b"", "headers": [
            (b"host", b"127.0.0.1:8000"),
            (b"x-aether-token", b"test-token"),
            (b"transfer-encoding", b"chunked"),
        ],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000),
    }
    await middleware(scope, receive, send)

    assert not app_called
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_chunked_request_body_within_limit_is_replayed(monkeypatch):
    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    observed = bytearray()

    async def inner_app(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                break
            observed.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = LocalAPIProtection(inner_app)
    chunks = [
        {"type": "http.request", "body": b"hello ", "more_body": True},
        {"type": "http.request", "body": b"world", "more_body": False},
    ]
    async def receive():
        if chunks:
            return chunks.pop(0)
        return {"type": "http.disconnect"}

    sent = []
    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/private", "raw_path": b"/private",
        "query_string": b"", "headers": [
            (b"host", b"127.0.0.1:8000"), (b"x-aether-token", b"test-token"),
        ],
        "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 8000),
    }
    await middleware(scope, receive, send)

    assert bytes(observed) == b"hello world"
    assert sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_invalid_content_length_is_rejected(monkeypatch):
    app = make_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8000") as client:
        response = await client.post(
            "/mutate",
            headers={"X-Aether-Token": "test-token", "Content-Length": "not-a-number"},
            content=b"{}",
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Content-Length"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("duplicate_headers", "method", "path"),
    [
        ([("X-Crows-Token", "test-token"), ("X-Crows-Token", "wrong")], "GET", "/private"),
        ([("Authorization", "Bearer test-token"), ("Authorization", "Bearer wrong")], "GET", "/private"),
        ([("Origin", "http://127.0.0.1:8000"), ("Origin", "http://evil.example")], "POST", "/mutate"),
        ([("Content-Length", "2"), ("Content-Length", "2")], "POST", "/mutate"),
        ([("Host", "127.0.0.1:8000"), ("Host", "evil.example")], "GET", "/private"),
        ([("X-Telegram-Bot-Api-Secret-Token", "expected"), ("X-Telegram-Bot-Api-Secret-Token", "attacker")], "POST", "/messaging/webhooks/telegram"),
        ([("X-Signature-Ed25519", "one"), ("X-Signature-Ed25519", "two")], "POST", "/messaging/webhooks/discord"),
    ],
)
async def test_ambiguous_duplicate_security_headers_are_rejected(
    monkeypatch, duplicate_headers, method, path
):
    app = make_app(monkeypatch)
    headers = [("X-Aether-Token", "test-token"), *duplicate_headers]
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8000",
    ) as client:
        response = await client.request(method, path, headers=headers, content=b"{}" if method == "POST" else None)

    assert response.status_code == 400
    assert response.json()["detail"] == "Ambiguous duplicate request header"
