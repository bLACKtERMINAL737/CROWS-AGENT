"""Provider-neutral contracts for Maximu Agent's universal messaging layer.

This module deliberately does not claim that any platform connector is enabled.
Concrete adapters must implement the contract and comply with each provider's
API, consent, authentication, and rate-limit requirements.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import re
from typing import Protocol


MAX_TEXT_CHARS = 20_000
MAX_ID_CHARS = 256
_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class MessagingContractError(ValueError):
    """Raised when a channel message or adapter registration is invalid."""


class DeliveryStatus(str, Enum):
    ACCEPTED = "accepted"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InboundMessage:
    """Normalized inbound event; provider-specific payloads stay in adapters."""

    channel: str
    provider_message_id: str
    provider_user_id: str
    text: str
    received_at: str
    reply_to_message_id: str | None = None

    def __post_init__(self) -> None:
        channel = self.channel.strip().lower()
        if not _CHANNEL_RE.fullmatch(channel):
            raise MessagingContractError("channel must be a lowercase provider key")
        object.__setattr__(self, "channel", channel)
        for name in ("provider_message_id", "provider_user_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > MAX_ID_CHARS:
                raise MessagingContractError(f"{name} must be non-empty and at most {MAX_ID_CHARS} characters")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.text, str) or not self.text.strip():
            raise MessagingContractError("message text must not be blank")
        if len(self.text) > MAX_TEXT_CHARS:
            raise MessagingContractError(f"message text exceeds {MAX_TEXT_CHARS} characters")
        object.__setattr__(self, "text", self.text.strip())
        if self.reply_to_message_id is not None:
            reply_id = self.reply_to_message_id.strip()
            if not reply_id or len(reply_id) > MAX_ID_CHARS:
                raise MessagingContractError("reply_to_message_id is invalid")
            object.__setattr__(self, "reply_to_message_id", reply_id)
        try:
            parsed = datetime.fromisoformat(self.received_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise MessagingContractError("received_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise MessagingContractError("received_at must include a timezone")


@dataclass(frozen=True)
class OutboundMessage:
    channel: str
    provider_user_id: str
    text: str
    idempotency_key: str

    def __post_init__(self) -> None:
        if not _CHANNEL_RE.fullmatch(self.channel):
            raise MessagingContractError("channel must be a lowercase provider key")
        for name in ("provider_user_id", "idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > MAX_ID_CHARS:
                raise MessagingContractError(f"{name} is invalid")
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > MAX_TEXT_CHARS:
            raise MessagingContractError("outbound text must be non-empty and within the size limit")


@dataclass(frozen=True)
class DeliveryReceipt:
    channel: str
    status: DeliveryStatus
    provider_message_id: str | None = None
    error_code: str | None = None
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not _CHANNEL_RE.fullmatch(self.channel):
            raise MessagingContractError("channel must be a lowercase provider key")
        if self.provider_message_id is not None and len(self.provider_message_id) > MAX_ID_CHARS:
            raise MessagingContractError("provider_message_id is too long")
        if self.error_code is not None and len(self.error_code) > 100:
            raise MessagingContractError("error_code is too long")
        if not self.updated_at:
            object.__setattr__(self, "updated_at", datetime.now(timezone.utc).isoformat())


class ChannelAdapter(Protocol):
    """Minimal adapter contract implemented by a platform-specific connector."""

    channel_name: str

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        """Send one message and return a truthful delivery state."""


class ChannelRegistry:
    """Explicit allowlist of installed adapters; never dynamically imports plugins."""

    def __init__(self) -> None:
        self._adapters: dict[str, ChannelAdapter] = {}

    def register(self, adapter: ChannelAdapter) -> None:
        name = getattr(adapter, "channel_name", "")
        if not isinstance(name, str) or not _CHANNEL_RE.fullmatch(name):
            raise MessagingContractError("adapter channel_name must be a lowercase provider key")
        if name in self._adapters:
            raise MessagingContractError(f"adapter already registered for channel: {name}")
        if not callable(getattr(adapter, "send", None)):
            raise MessagingContractError("adapter must provide an async send method")
        self._adapters[name] = adapter

    def get(self, channel: str) -> ChannelAdapter:
        try:
            return self._adapters[channel]
        except KeyError as exc:
            raise MessagingContractError(f"channel is not configured: {channel}") from exc

    def list_channels(self) -> list[str]:
        return sorted(self._adapters)


def conversation_scope(channel: str, provider_user_id: str, *, namespace: str) -> str:
    """Derive a stable opaque session key scoped to both channel and provider user.

    The namespace should be a deployment/user-space identifier, not a secret.
    Raw provider IDs are not returned or logged by this helper.
    """
    channel = channel.strip().lower()
    provider_user_id = provider_user_id.strip()
    namespace = namespace.strip()
    if not _CHANNEL_RE.fullmatch(channel):
        raise MessagingContractError("channel is invalid")
    if not provider_user_id or len(provider_user_id) > MAX_ID_CHARS:
        raise MessagingContractError("provider_user_id is invalid")
    if not namespace or len(namespace) > MAX_ID_CHARS:
        raise MessagingContractError("namespace is invalid")
    material = "\0".join((namespace, channel, provider_user_id)).encode("utf-8")
    return "msg_" + hashlib.sha256(material).hexdigest()[:40]
