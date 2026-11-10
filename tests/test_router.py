import httpx
import pytest

from crows.provider import ModelProvider, ModelRouter


class FakeProvider:
    def __init__(self, name, errors=None, configured=True):
        self.model = name
        self.configured = configured
        self.errors = list(errors or [])
        self.calls = 0

    async def chat(self, message, task_type="general"):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return f"{self.model}: {task_type}: {message}"


def status_error(status_code):
    request = httpx.Request("POST", "https://example.test/chat/completions")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status_code}", request=request, response=response
    )


@pytest.mark.asyncio
async def test_coding_task_uses_specialist_route():
    primary = FakeProvider("primary")
    coding = FakeProvider("coding")
    router = ModelRouter(primary, coding)
    result = await router.chat("find bug", "coding")
    assert result["route"] == "coding"
    assert result["model"] == "coding"
    assert result["fallback_used"] is False
    assert primary.calls == 0
    assert coding.calls == 1


@pytest.mark.asyncio
async def test_transient_primary_error_uses_fallback():
    primary = FakeProvider("primary", errors=[httpx.ConnectError("network down")])
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=1)
    result = await router.chat("hello")
    assert result["answer"] == "fallback: general: hello"
    assert result["fallback_used"] is True
    assert result["attempts"] == 2
    assert primary.calls == 1
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_timeout_retries_primary_before_using_fallback(monkeypatch):
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("crows.provider.asyncio.sleep", fake_sleep)
    primary = FakeProvider(
        "primary",
        errors=[httpx.ReadTimeout("timed out"), httpx.ConnectError("network down")],
    )
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=2)

    result = await router.chat("hello")

    assert result["route"] == "fallback"
    assert result["attempts"] == 3
    assert primary.calls == 2
    assert fallback.calls == 1
    assert sleeps == [0.25]


@pytest.mark.asyncio
async def test_server_error_retries_then_uses_fallback(monkeypatch):
    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr("crows.provider.asyncio.sleep", fake_sleep)
    primary = FakeProvider("primary", errors=[status_error(503), status_error(502)])
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=2)

    result = await router.chat("hello")

    assert result["route"] == "fallback"
    assert result["attempts"] == 3
    assert primary.calls == 2
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_non_transient_error_does_not_use_fallback():
    primary = FakeProvider("primary", errors=[status_error(401)])
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=2)
    with pytest.raises(httpx.HTTPStatusError):
        await router.chat("hello")
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_fallback_failure_raises_last_error_and_counts_attempts(monkeypatch):
    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr("crows.provider.asyncio.sleep", fake_sleep)
    primary = FakeProvider(
        "primary",
        errors=[httpx.ConnectError("primary down"), httpx.ConnectError("primary still down")],
    )
    fallback = FakeProvider(
        "fallback",
        errors=[httpx.ReadTimeout("fallback timed out"), httpx.ReadTimeout("fallback timed out again")],
    )
    router = ModelRouter(primary, fallback=fallback, max_attempts=2)

    with pytest.raises(httpx.ReadTimeout, match="fallback timed out again"):
        await router.chat("hello")

    assert primary.calls == 2
    assert fallback.calls == 2


@pytest.mark.asyncio
async def test_unconfigured_primary_fails_cleanly():
    router = ModelRouter(ModelProvider(None, None, None))
    with pytest.raises(Exception, match="Provider is not configured"):
        await router.chat("hello")



@pytest.mark.parametrize("max_attempts", [0, 4, -1])
def test_router_rejects_attempt_limits_outside_supported_range(max_attempts):
    with pytest.raises(ValueError, match="between 1 and 3"):
        ModelRouter(FakeProvider("primary"), max_attempts=max_attempts)


@pytest.mark.asyncio
async def test_same_provider_as_primary_and_fallback_is_not_called_twice():
    primary = FakeProvider(
        "primary",
        errors=[httpx.ConnectError("network down")],
    )
    router = ModelRouter(primary, fallback=primary, max_attempts=1)

    with pytest.raises(httpx.ConnectError, match="network down"):
        await router.chat("hello")

    assert primary.calls == 1


@pytest.mark.asyncio
async def test_coding_route_transient_failure_falls_back_without_calling_primary(monkeypatch):
    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr("crows.provider.asyncio.sleep", fake_sleep)
    primary = FakeProvider("primary")
    coding = FakeProvider("coding", errors=[httpx.ConnectError("coding endpoint down")])
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, coding=coding, fallback=fallback, max_attempts=1)

    result = await router.chat("fix bug", "coding")

    assert result["route"] == "fallback"
    assert result["fallback_used"] is True
    assert result["attempts"] == 2
    assert coding.calls == 1
    assert primary.calls == 0
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_rate_limit_error_does_not_retry_or_fall_back():
    primary = FakeProvider("primary", errors=[status_error(429)])
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=3)

    with pytest.raises(httpx.HTTPStatusError):
        await router.chat("hello")

    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_remote_protocol_error_retries_before_fallback(monkeypatch):
    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr("crows.provider.asyncio.sleep", fake_sleep)
    primary = FakeProvider(
        "primary",
        errors=[
            httpx.RemoteProtocolError("server closed connection unexpectedly"),
            httpx.RemoteProtocolError("connection interrupted again"),
        ],
    )
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=2)

    result = await router.chat("hello")

    assert result["route"] == "fallback"
    assert result["fallback_used"] is True
    assert result["attempts"] == 3
    assert primary.calls == 2
    assert fallback.calls == 1



@pytest.mark.asyncio
async def test_explicitly_disabled_fallback_is_never_called():
    primary = FakeProvider("primary", errors=[httpx.ConnectError("network down")])
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=1)

    with pytest.raises(httpx.ConnectError, match="network down"):
        await router.chat("hello", allow_fallback=False)

    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_manual_fallback_route_does_not_retry_same_fallback_as_backup():
    fallback = FakeProvider("fallback", errors=[httpx.ConnectError("fallback down")])
    router = ModelRouter(FakeProvider("primary"), fallback=fallback, max_attempts=1)

    with pytest.raises(httpx.ConnectError, match="fallback down"):
        await router.chat("hello", model_route="fallback")

    assert fallback.calls == 1


@pytest.mark.parametrize("max_total_attempts", [0, 7, -1])
def test_router_rejects_total_attempt_budgets_outside_supported_range(max_total_attempts):
    with pytest.raises(ValueError, match="max_total_attempts must be between 1 and 6"):
        ModelRouter(FakeProvider("primary"), max_total_attempts=max_total_attempts)


@pytest.mark.asyncio
async def test_total_attempt_budget_caps_retries_before_fallback(monkeypatch):
    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr("crows.provider.asyncio.sleep", fake_sleep)
    primary = FakeProvider(
        "primary",
        errors=[
            httpx.ConnectError("first failure"),
            httpx.ConnectError("second failure"),
            httpx.ConnectError("third failure"),
        ],
    )
    fallback = FakeProvider("fallback")
    router = ModelRouter(
        primary, fallback=fallback, max_attempts=3, max_total_attempts=2
    )

    with pytest.raises(httpx.ConnectError, match="second failure"):
        await router.chat("hello")

    assert primary.calls == 2
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_total_attempt_budget_allows_fallback_when_budget_remains(monkeypatch):
    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr("crows.provider.asyncio.sleep", fake_sleep)
    primary = FakeProvider("primary", errors=[httpx.ConnectError("primary down")])
    fallback = FakeProvider("fallback", errors=[httpx.ReadTimeout("fallback down")])
    # One attempt per route leaves the second total-budget slot for fallback.
    router = ModelRouter(
        primary, fallback=fallback, max_attempts=1, max_total_attempts=2
    )

    with pytest.raises(httpx.ReadTimeout, match="fallback down"):
        await router.chat("hello")

    assert primary.calls == 1
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_router_reports_sanitized_attempt_history_for_fallback_success(monkeypatch):
    async def fake_sleep(_delay):
        return None

    monkeypatch.setattr("crows.provider.asyncio.sleep", fake_sleep)
    secret_detail = "private provider error detail"
    primary = FakeProvider("primary", errors=[httpx.ConnectError(secret_detail)])
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=1, max_total_attempts=2)

    result = await router.chat("hello")

    assert result["attempt_history"] == [
        {"route": "primary", "attempt": 1, "outcome": "failure", "error_type": "ConnectError"},
        {"route": "fallback", "attempt": 2, "outcome": "success"},
    ]
    assert secret_detail not in str(result["attempt_history"])
    assert result["attempts"] == 2


@pytest.mark.asyncio
async def test_router_reports_single_success_attempt_history():
    router = ModelRouter(FakeProvider("primary"))

    result = await router.chat("hello")

    assert result["attempt_history"] == [
        {"route": "primary", "attempt": 1, "outcome": "success"}
    ]
