import pytest
import crows.provider as provider_module
from crows.provider import ModelProvider


class FakeResponse:
    def raise_for_status(self):
        return None
    def json(self):
        return {'choices': [{'message': {'content': 'Context-aware answer'}}]}


class FakeAsyncClient:
    last_request = None
    def __init__(self, timeout=None):
        self.timeout = timeout
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    async def post(self, url, headers, json):
        type(self).last_request = {'url': url, 'headers': headers, 'json': json}
        return FakeResponse()


@pytest.mark.asyncio
async def test_provider_sends_history_and_only_passed_memories(monkeypatch):
    monkeypatch.setattr(provider_module.httpx, 'AsyncClient', FakeAsyncClient)
    provider = ModelProvider('https://example.test/v1', 'test-key', 'test-model')
    answer = await provider.chat(
        'What did I ask earlier?',
        history=[{'role': 'user', 'content': 'Help me with Python'}],
        memories=[{'category': 'preference', 'content': 'Explain in Bangla'}],
    )
    request = FakeAsyncClient.last_request['json']
    messages = request['messages']
    assert answer == 'Context-aware answer'
    assert 'You are Crows' in messages[0]['content']
    assert 'Maximu Agent' not in messages[0]['content']
    assert messages[1]['role'] == 'system'
    assert 'untrusted reference data' in messages[1]['content']
    assert '\\n- [preference]' not in messages[1]['content']
    assert '\n- [preference]' in messages[1]['content']
    assert 'Explain in Bangla' in messages[1]['content']
    assert messages[2] == {'role': 'user', 'content': 'Help me with Python'}
    assert messages[3] == {'role': 'user', 'content': 'What did I ask earlier?'}


@pytest.mark.asyncio
async def test_provider_does_not_add_memory_context_when_not_supplied(monkeypatch):
    monkeypatch.setattr(provider_module.httpx, 'AsyncClient', FakeAsyncClient)
    provider = ModelProvider('https://example.test/v1', 'test-key', 'test-model')
    await provider.chat('Hi')
    messages = FakeAsyncClient.last_request['json']['messages']
    assert len(messages) == 2
    assert messages[-1] == {'role': 'user', 'content': 'Hi'}


@pytest.mark.asyncio
async def test_provider_prompt_says_unknown_training_cutoff_and_uses_real_newlines(monkeypatch):
    monkeypatch.setattr(provider_module.httpx, 'AsyncClient', FakeAsyncClient)
    provider = ModelProvider('https://example.test/v1', 'test-key', 'test-model')

    await provider.chat('How recent is your knowledge?')

    system_prompt = FakeAsyncClient.last_request['json']['messages'][0]['content']
    assert 'do not guess' in system_prompt
    assert 'training-data cutoff' in system_prompt
    assert 'say you do not know the exact cutoff' in system_prompt
    assert '\n\nCurrent date (UTC):' in system_prompt
    assert '\\n\\nCurrent date (UTC):' not in system_prompt
