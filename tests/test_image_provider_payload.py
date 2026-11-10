import base64

import pytest

from crows.provider import ModelProvider


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"role": "assistant", "content": "It is a test image."}}]}


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
async def test_basic_chat_sends_multimodal_openai_compatible_content(monkeypatch):
    calls = []

    def client_factory(*_args, **_kwargs):
        return FakeAsyncClient(calls)

    monkeypatch.setattr("crows.provider.httpx.AsyncClient", client_factory)
    provider = ModelProvider("https://example.test/v1", "secret", "vision-model")
    png_bytes = bytes.fromhex("89504e470d0a1a0a") + b"example"
    image_data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")

    answer = await provider.chat("What is shown?", image_data_url=image_data_url)

    assert answer == "It is a test image."
    user_message = calls[0]["payload"]["messages"][-1]
    assert user_message == {
        "role": "user",
        "content": [
            {"type": "text", "text": "What is shown?"},
            {"type": "image_url", "image_url": {"url": image_data_url}},
        ],
    }
