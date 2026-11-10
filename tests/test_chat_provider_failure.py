import pytest
from fastapi import HTTPException

from crows import app as app_module
from crows.provider import ProviderNotConfigured
from crows.store import TaskStore


class UnconfiguredRouter:
    async def chat(self, *args, **kwargs):
        raise ProviderNotConfigured(
            "Provider is not configured. Set its base URL, API key, and model name."
        )


@pytest.mark.asyncio
async def test_unconfigured_provider_returns_503_without_persisting_failed_turn(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "chat-provider-unconfigured.sqlite3")
    session = store.create_session("Existing conversation")
    store.add_session_message(session["id"], "user", "Earlier question")
    store.add_session_message(session["id"], "assistant", "Earlier answer")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", UnconfiguredRouter())

    with pytest.raises(HTTPException) as exc_info:
        await app_module.chat(
            app_module.ChatRequest(
                message="Question that cannot be answered",
                session_id=session["id"],
            )
        )

    assert exc_info.value.status_code == 503
    assert "Provider is not configured" in exc_info.value.detail
    messages = store.list_session_messages(session["id"])
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Earlier question"),
        ("assistant", "Earlier answer"),
    ]



class TimedOutRouter:
    async def chat(self, *args, **kwargs):
        import httpx

        raise httpx.ReadTimeout("simulated provider timeout")


@pytest.mark.asyncio
async def test_provider_timeout_returns_gateway_timeout_without_persisting_turn(
    tmp_path, monkeypatch
):
    store = TaskStore(tmp_path / "chat-provider-timeout.sqlite3")
    session = store.create_session("Existing conversation")
    store.add_session_message(session["id"], "user", "Earlier question")
    store.add_session_message(session["id"], "assistant", "Earlier answer")
    monkeypatch.setattr(app_module, "store", store)
    monkeypatch.setattr(app_module, "router", TimedOutRouter())

    with pytest.raises(HTTPException) as exc_info:
        await app_module.chat(
            app_module.ChatRequest(
                message="Question that times out",
                session_id=session["id"],
            )
        )

    assert exc_info.value.status_code == 504
    assert "timed out" in exc_info.value.detail
    messages = store.list_session_messages(session["id"])
    assert [(item["role"], item["content"]) for item in messages] == [
        ("user", "Earlier question"),
        ("assistant", "Earlier answer"),
    ]
