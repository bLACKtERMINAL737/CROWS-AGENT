import pytest
import httpx

from crows.app import infer_task_type
from crows.provider import ModelRouter


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


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Please debug this Python function", "coding"),
        ("Help me implement an API endpoint", "coding"),
        ("Prove this theorem step by step", "reasoning"),
        ("Compare these architecture trade-offs", "reasoning"),
        ("Hello, how are you?", "general"),
    ],
)
def test_auto_task_inference_is_local_and_deterministic(message, expected):
    assert infer_task_type(message) == expected


@pytest.mark.asyncio
async def test_manual_route_selects_configured_coding_provider():
    primary = FakeProvider("primary-model")
    coding = FakeProvider("coding-model")
    router = ModelRouter(primary, coding)
    result = await router.chat("change code", "general", model_route="coding")
    assert result["route"] == "coding"
    assert result["model"] == "coding-model"
    assert coding.calls == 1
    assert primary.calls == 0


@pytest.mark.asyncio
async def test_manual_route_rejects_unconfigured_route():
    primary = FakeProvider("primary-model")
    coding = FakeProvider("coding-model", configured=False)
    router = ModelRouter(primary, coding)
    with pytest.raises(Exception, match="selected coding model route is not configured"):
        await router.chat("change code", "general", model_route="coding")
    assert primary.calls == 0


@pytest.mark.asyncio
async def test_manual_route_can_disable_fallback():
    primary = FakeProvider("primary-model", errors=[httpx.ConnectError("offline")])
    fallback = FakeProvider("fallback-model")
    router = ModelRouter(primary, fallback=fallback, max_attempts=1)
    with pytest.raises(httpx.ConnectError):
        await router.chat("hello", allow_fallback=False)
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_manual_route_can_enable_fallback_for_transient_errors():
    primary = FakeProvider("primary-model", errors=[httpx.ConnectError("offline")])
    fallback = FakeProvider("fallback-model")
    router = ModelRouter(primary, fallback=fallback, max_attempts=1)
    result = await router.chat("hello", allow_fallback=True)
    assert result["route"] == "fallback"
    assert result["fallback_used"] is True
    assert fallback.calls == 1


def test_available_routes_do_not_expose_credentials():
    primary = FakeProvider("primary-model")
    router = ModelRouter(primary)
    routes = router.available_routes()
    assert routes == [{"id": "primary", "model": "primary-model", "configured": True}]
    assert all("api_key" not in item and "key" not in item for item in routes)


@pytest.mark.asyncio
async def test_auto_reasoning_uses_reasoning_specialist_when_configured():
    primary = FakeProvider("primary-model")
    reasoning = FakeProvider("reasoning-model")
    router = ModelRouter(primary, reasoning=reasoning)
    result = await router.chat("compare these approaches", "reasoning")
    assert result["route"] == "reasoning"
    assert result["model"] == "reasoning-model"
    assert reasoning.calls == 1
    assert primary.calls == 0


def test_reasoning_route_is_listed_without_credentials():
    router = ModelRouter(FakeProvider("primary-model"), reasoning=FakeProvider("reasoning-model"))
    assert router.available_routes() == [
        {"id": "primary", "model": "primary-model", "configured": True},
        {"id": "reasoning", "model": "reasoning-model", "configured": True},
    ]
