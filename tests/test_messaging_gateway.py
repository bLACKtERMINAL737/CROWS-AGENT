import pytest

from crows.messaging_gateway import (
    InboundChannelPolicy,
    InboundChannelRegistry,
    build_default_inbound_channels,
)


def test_default_registry_contains_only_supported_inbound_reply_channels():
    registry = build_default_inbound_channels()
    assert registry.list_channels() == ["discord", "telegram"]
    assert registry.get("telegram").max_reply_chars == 4096
    assert registry.get("discord").max_reply_chars == 2000


def test_registry_supports_explicit_additional_channel_policy_without_dynamic_imports():
    registry = build_default_inbound_channels()
    policy = InboundChannelPolicy("matrix", "Matrix", 8000)
    registry.register(policy)
    assert registry.get("matrix") is policy
    assert registry.list_channels() == ["discord", "matrix", "telegram"]


def test_registry_rejects_duplicate_or_malformed_channel_policy():
    registry = InboundChannelRegistry()
    registry.register(InboundChannelPolicy("telegram", "Telegram", 4096))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(InboundChannelPolicy("telegram", "Telegram", 4096))
    with pytest.raises(ValueError, match="lowercase provider key"):
        InboundChannelPolicy("../telegram", "Telegram", 4096)


@pytest.mark.parametrize("limit", [0, -1, 20_001, True, "2000"])
def test_policy_rejects_invalid_reply_limits(limit):
    with pytest.raises(ValueError, match="max_reply_chars"):
        InboundChannelPolicy("matrix", "Matrix", limit)


def test_registry_fails_closed_for_unregistered_channel():
    registry = build_default_inbound_channels()
    with pytest.raises(ValueError, match="not enabled"):
        registry.get("whatsapp")
