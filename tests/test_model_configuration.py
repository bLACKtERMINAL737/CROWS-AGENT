import pytest
from fastapi import HTTPException

from crows import app as app_module
from crows.provider import ModelProvider
from crows.app import ModelConfigurationRequest, configure_model


@pytest.mark.parametrize(
    "route",
    ["primary", "coding", "reasoning", "fallback"],
)
def test_configure_model_route_is_in_memory_and_does_not_return_key(monkeypatch, route):
    providers = {
        "primary": ModelProvider(None, None, None),
        "coding": ModelProvider(None, None, None),
        "reasoning": ModelProvider(None, None, None),
        "fallback": ModelProvider(None, None, None),
    }
    monkeypatch.setattr(app_module, "primary_provider", providers["primary"])
    monkeypatch.setattr(app_module, "coding_provider", providers["coding"])
    monkeypatch.setattr(app_module, "reasoning_provider", providers["reasoning"])
    monkeypatch.setattr(app_module, "fallback_provider", providers["fallback"])

    result = configure_model(ModelConfigurationRequest(
        route=route,
        base_url="https://api.example.com/v1",
        model="example-model",
        api_key="secret-test-key",
    ))

    assert result["route"] == route
    assert result["model"] == "example-model"
    assert result["configured"] is True
    assert "secret-test-key" not in str(result)
    assert "memory-only" in result["persistence"]
    assert providers[route].api_key == "secret-test-key"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example.com/v1",
        "https://user:password@api.example.com/v1",
        "https://api.example.com/v1?token=secret",
        "https://api.example.com/v1#fragment",
        "file:///tmp/provider",
    ],
)
def test_configure_model_rejects_insecure_or_ambiguous_urls(base_url):
    with pytest.raises(HTTPException) as error:
        configure_model(ModelConfigurationRequest(
            route="primary",
            base_url=base_url,
            model="example-model",
            api_key="secret-test-key",
        ))
    assert error.value.status_code == 422


@pytest.mark.parametrize("base_url", ["http://localhost:11434/v1", "http://127.0.0.1:11434/v1", "https://api.example.com/v1"])
def test_configure_model_accepts_https_and_loopback(base_url, monkeypatch):
    monkeypatch.setattr(app_module, "primary_provider", ModelProvider(None, None, None))
    result = configure_model(ModelConfigurationRequest(
        route="primary",
        base_url=base_url,
        model="example-model",
        api_key="secret-test-key",
    ))
    assert result["configured"] is True


def test_configure_model_rejects_blank_credentials_and_model():
    for payload in [
        {"route": "primary", "base_url": "https://api.example.com/v1", "model": " ", "api_key": "key"},
        {"route": "primary", "base_url": "https://api.example.com/v1", "model": "model", "api_key": " "},
    ]:
        with pytest.raises(HTTPException) as error:
            configure_model(ModelConfigurationRequest(**payload))
        assert error.value.status_code == 422
