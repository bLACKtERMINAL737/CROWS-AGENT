"""Explicit channel policy registry for the shared inbound agent runtime.

Provider webhooks normalize and authenticate events before enqueueing them.
This registry controls which channels may invoke the conservative, tool-free
agent reply path; it does not dynamically load code or enable outbound
transports. Outbound delivery still requires a configured transport.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class InboundChannelPolicy:
    """Bounded reply policy applied after a verified message is dispatched."""

    channel_name: str
    display_name: str
    max_reply_chars: int

    def __post_init__(self) -> None:
        if not isinstance(self.channel_name, str) or not _CHANNEL_RE.fullmatch(self.channel_name):
            raise ValueError("channel_name must be a lowercase provider key")
        if (
            not isinstance(self.display_name, str)
            or not self.display_name.strip()
            or len(self.display_name.strip()) > 64
        ):
            raise ValueError("display_name must contain 1 to 64 characters")
        if (
            isinstance(self.max_reply_chars, bool)
            or not isinstance(self.max_reply_chars, int)
            or not 1 <= self.max_reply_chars <= 20_000
        ):
            raise ValueError("max_reply_chars must be between 1 and 20000")
        object.__setattr__(self, "display_name", self.display_name.strip())


class InboundChannelRegistry:
    """Explicit allowlist for inbound agent replies; registration is never dynamic."""

    def __init__(self) -> None:
        self._policies: dict[str, InboundChannelPolicy] = {}

    def register(self, policy: InboundChannelPolicy) -> None:
        if not isinstance(policy, InboundChannelPolicy):
            raise ValueError("policy must be an InboundChannelPolicy")
        if policy.channel_name in self._policies:
            raise ValueError(f"inbound reply policy already registered: {policy.channel_name}")
        self._policies[policy.channel_name] = policy

    def get(self, channel: str) -> InboundChannelPolicy:
        if not isinstance(channel, str):
            raise ValueError("channel must be text")
        try:
            return self._policies[channel]
        except KeyError as exc:
            raise ValueError(f"inbound agent replies are not enabled for channel: {channel}") from exc

    def list_channels(self) -> list[str]:
        return sorted(self._policies)


def build_default_inbound_channels() -> InboundChannelRegistry:
    """Return the built-in reply policies for currently supported connectors."""
    registry = InboundChannelRegistry()
    registry.register(InboundChannelPolicy("telegram", "Telegram", 4096))
    registry.register(InboundChannelPolicy("discord", "Discord", 2000))
    return registry


DEFAULT_INBOUND_CHANNELS = build_default_inbound_channels()
