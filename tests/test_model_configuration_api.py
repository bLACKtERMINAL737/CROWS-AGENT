import os
import pytest
from fastapi.testclient import TestClient

from crows import app as app_module
from crows.provider import ModelProvider, ModelRouter


@pytest.fixture
def client_and_providers(monkeypatch):
    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
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
    monkeypatch.setattr(
        app_module,
        "router",
        ModelRouter(
            providers["primary"],
            providers["coding"],
            providers["fallback"],
            reasoning=providers["reasoning"],
        ),
    )
    with TestClient(app_module.app, base_url="http://127.0.0.1", headers={
            "Origin": "http://127.0.0.1",
            "Host": "127.0.0.1",
            "X-Aether-Token": "test-token",
        }, client=("127.0.0.1", 12345)) as client:
        yield client, providers


def test_model_configuration_endpoint_updates_route_and_never_returns_secret(client_and_providers):
    client, providers = client_and_providers
    secret = "do-not-return-this-key"
    response = client.post(
        "/models/configure",
        json={
            "route": "reasoning",
            "base_url": "https://api.example.com/v1",
            "model": "reasoning-test-model",
            "api_key": secret,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "reasoning"
    assert body["model"] == "reasoning-test-model"
    assert body["configured"] is True
    assert secret not in response.text
    assert providers["reasoning"].api_key == secret

    routes_response = client.get("/models")
    assert routes_response.status_code == 200
    routes = routes_response.json()["routes"]
    reasoning = next(route for route in routes if route["id"] == "reasoning")
    assert reasoning == {
        "id": "reasoning",
        "model": "reasoning-test-model",
        "configured": True,
    }
    assert secret not in routes_response.text


@pytest.mark.parametrize(
    ("route", "base_url", "expected_status"),
    [
        ("unknown", "https://api.example.com/v1", 422),
        ("primary", "http://api.example.com/v1", 422),
        ("primary", "https://user:secret@api.example.com/v1", 422),
        ("primary", "http://127.0.0.1:11434/v1", 200),
    ],
)
def test_model_configuration_endpoint_validates_route_and_provider_url(
    client_and_providers, route, base_url, expected_status
):
    client, _ = client_and_providers
    response = client.post(
        "/models/configure",
        json={
            "route": route,
            "base_url": base_url,
            "model": "test-model",
            "api_key": "test-key",
        },
    )
    assert response.status_code == expected_status


def test_model_configuration_endpoint_rejects_blank_secret(client_and_providers):
    client, _ = client_and_providers
    response = client.post(
        "/models/configure",
        json={
            "route": "primary",
            "base_url": "https://api.example.com/v1",
            "model": "test-model",
            "api_key": "   ",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "base_url",
    [
        "https://@api.example.com/v1",
        "https://:@api.example.com/v1",
        "https://user:@api.example.com/v1",
        "https://:secret@api.example.com/v1",
    ],
)
def test_model_configuration_endpoint_rejects_empty_or_partial_url_userinfo(
    client_and_providers, base_url
):
    client, _ = client_and_providers
    response = client.post(
        "/models/configure",
        json={
            "route": "primary",
            "base_url": base_url,
            "model": "test-model",
            "api_key": "test-key",
        },
    )

    assert response.status_code == 422



@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.example.com/\n/v1",
        "https://api.example.com\\@attacker.example/v1",
        "https://api.example.com/v1 ",
    ],
)
def test_model_configuration_endpoint_rejects_url_control_whitespace_and_backslashes(
    client_and_providers, base_url
):
    client, _ = client_and_providers
    response = client.post(
        "/models/configure",
        json={
            "route": "primary",
            "base_url": base_url,
            "model": "test-model",
            "api_key": "test-key",
        },
    )
    assert response.status_code == 422
