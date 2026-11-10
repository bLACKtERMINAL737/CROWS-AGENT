import httpx
import pytest

from crows.provider import ModelRouter, ProviderNotConfigured


class FakeProvider:
    def __init__(self, model, configured=True, errors=None):
        self.model = model
        self.configured = configured
        self.errors = list(errors or [])
        self.calls = 0

    async def chat(self, message, task_type="general", history=None, memories=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return f"{self.model}:{task_type}:{message}"


def http_error(status_code):
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("provider error", request=request, response=response)


@pytest.mark.asyncio
async def test_coding_task_automatically_uses_configured_coding_route():
    primary = FakeProvider("primary")
    coding = FakeProvider("coding")
    router = ModelRouter(primary, coding=coding)

    result = await router.chat("implement a function", task_type="coding")

    assert result["route"] == "coding"
    assert coding.calls == 1
    assert primary.calls == 0


@pytest.mark.asyncio
async def test_reasoning_task_uses_primary_when_specialist_is_not_configured():
    primary = FakeProvider("primary")
    reasoning = FakeProvider("reasoning", configured=False)
    router = ModelRouter(primary, reasoning=reasoning)

    result = await router.chat("compare options", task_type="reasoning")

    assert result["route"] == "primary"
    assert primary.calls == 1
    assert reasoning.calls == 0


@pytest.mark.asyncio
async def test_client_error_does_not_trigger_fallback():
    primary = FakeProvider("primary", errors=[http_error(400)])
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=2)

    with pytest.raises(httpx.HTTPStatusError):
        await router.chat("hello", allow_fallback=True)

    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_transient_server_error_uses_fallback_after_bounded_retries():
    primary = FakeProvider("primary", errors=[http_error(503), http_error(503)])
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=2)

    result = await router.chat("hello", allow_fallback=True)

    assert result["route"] == "fallback"
    assert result["fallback_used"] is True
    assert result["attempts"] == 3
    assert primary.calls == 2
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_disabled_fallback_does_not_switch_models_after_transient_failure():
    primary = FakeProvider("primary", errors=[httpx.ConnectError("offline")])
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=1)

    with pytest.raises(httpx.ConnectError):
        await router.chat("hello", allow_fallback=False)

    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_manual_route_selection_does_not_call_other_specialist():
    primary = FakeProvider("primary")
    coding = FakeProvider("coding")
    reasoning = FakeProvider("reasoning")
    router = ModelRouter(primary, coding=coding, reasoning=reasoning)

    result = await router.chat("think about this", task_type="reasoning", model_route="coding")

    assert result["route"] == "coding"
    assert coding.calls == 1
    assert primary.calls == 0
    assert reasoning.calls == 0


def test_router_rejects_unbounded_retry_configuration():
    with pytest.raises(ValueError, match="max_attempts must be between 1 and 3"):
        ModelRouter(FakeProvider("primary"), max_attempts=4)


@pytest.mark.asyncio
async def test_unconfigured_manual_route_fails_explicitly_without_silent_switch():
    primary = FakeProvider("primary")
    coding = FakeProvider("coding", configured=False)
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, coding=coding, fallback=fallback)

    with pytest.raises(ProviderNotConfigured, match="selected coding model route is not configured"):
        await router.chat("change code", model_route="coding", allow_fallback=True)

    assert primary.calls == 0
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_invalid_provider_json_is_not_retried_or_sent_to_fallback():
    class InvalidPayloadProvider(FakeProvider):
        async def chat(self, message, task_type="general", history=None, memories=None):
            self.calls += 1
            raise RuntimeError("Provider returned invalid JSON")

    primary = InvalidPayloadProvider("primary")
    fallback = FakeProvider("fallback")
    router = ModelRouter(primary, fallback=fallback, max_attempts=3)

    with pytest.raises(RuntimeError, match="Provider returned invalid JSON"):
        await router.chat("hello", allow_fallback=True)

    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_retry_budget_stays_bounded_across_primary_and_fallback():
    primary = FakeProvider("primary", errors=[
        http_error(503), http_error(503), http_error(503),
    ])
    fallback = FakeProvider("fallback", errors=[http_error(503), http_error(503)])
    router = ModelRouter(primary, fallback=fallback, max_attempts=3, max_total_attempts=4)

    with pytest.raises(httpx.HTTPStatusError):
        await router.chat("hello", allow_fallback=True)

    assert primary.calls == 3
    assert fallback.calls == 1
