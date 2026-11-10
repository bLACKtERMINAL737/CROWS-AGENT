import base64

import pytest

from crows.provider import ModelProvider


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "I can inspect the image."}}]}


class FakeAsyncClient:
    def __init__(self, calls):
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "payload": json})
        return FakeResponse()


@pytest.mark.asyncio
async def test_tool_enabled_chat_preserves_multimodal_user_content(monkeypatch):
    calls = []

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "vision-model")
    image_bytes = bytes.fromhex("89504e470d0a1a0a") + b"test-image"
    image_data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")

    result = await provider.chat_with_tools(
        "What is shown?",
        tool_specs=[{"type": "function", "function": {"name": "read_file", "description": "Read a file", "parameters": {"type": "object", "properties": {}}}}],
        execute_tool=lambda _name, _arguments: {"ok": True},
        image_data_url=image_data_url,
    )

    assert result["answer"] == "I can inspect the image."
    payload = calls[0]["payload"]
    user_message = payload["messages"][-1]
    assert user_message == {
        "role": "user",
        "content": [
            {"type": "text", "text": "What is shown?"},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ],
    }
    assert payload["tools"]


@pytest.mark.asyncio
async def test_regular_chat_preserves_multimodal_user_content(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "crows.provider.httpx.AsyncClient",
        lambda *_args, **_kwargs: FakeAsyncClient(calls),
    )
    provider = ModelProvider("https://example.test/v1", "secret", "vision-model")
    image_bytes = bytes.fromhex("89504e470d0a1a0a") + b"test-image"
    image_data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")

    answer = await provider.chat("What is shown?", image_data_url=image_data_url)

    assert answer == "I can inspect the image."
    user_message = calls[0]["payload"]["messages"][-1]
    assert user_message == {
        "role": "user",
        "content": [
            {"type": "text", "text": "What is shown?"},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ],
    }


@pytest.mark.asyncio
async def test_regular_chat_keeps_plain_text_content_without_image(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "crows.provider.httpx.AsyncClient",
        lambda *_args, **_kwargs: FakeAsyncClient(calls),
    )
    provider = ModelProvider("https://example.test/v1", "secret", "text-model")

    answer = await provider.chat("Hello")

    assert answer == "I can inspect the image."
    assert calls[0]["payload"]["messages"][-1] == {
        "role": "user",
        "content": "Hello",
    }
