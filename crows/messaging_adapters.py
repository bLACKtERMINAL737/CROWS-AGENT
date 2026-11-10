"""Explicit provider transports for the durable outbound messaging queue."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable
import json

import httpx

from .messaging import MessagingContractError


@dataclass(frozen=True)
class OutboundTransportMessage:
    channel: str
    delivery_target: str
    text: str
    idempotency_key: str


@dataclass(frozen=True)
class OutboundTransportReceipt:
    success: bool
    status_code: int
    provider_message_id: str | None = None
    retry_after_seconds: int | None = None


class OutboundTransportAdapter:
    channel_name: str

    async def send(self, message: OutboundTransportMessage) -> OutboundTransportReceipt:
        raise NotImplementedError


class TelegramOutboundAdapter(OutboundTransportAdapter):
    channel_name = "telegram"

    def __init__(self, config: Callable[[], tuple[str, str]]) -> None:
        self._config = config

    async def send(self, message: OutboundTransportMessage) -> OutboundTransportReceipt:
        bot_token, _ = self._config()
        if not bot_token:
            return OutboundTransportReceipt(False, 503)
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": message.delivery_target, "text": message.text}
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            response = await client.post(url, json=payload)
        retry_after = _retry_after(response)
        body = response.json() if response.content else {}
        ok = response.is_success and isinstance(body, dict) and body.get("ok") is True
        result = body.get("result") if isinstance(body, dict) else None
        provider_id = str(result.get("message_id")) if ok and isinstance(result, dict) and result.get("message_id") is not None else None
        return OutboundTransportReceipt(ok, response.status_code, provider_id, retry_after)


class DiscordOutboundAdapter(OutboundTransportAdapter):
    channel_name = "discord"

    def __init__(self, webhook_url: Callable[[], str]) -> None:
        self._webhook_url = webhook_url

    async def send(self, message: OutboundTransportMessage) -> OutboundTransportReceipt:
        method = "post"
        if message.delivery_target == "configured-webhook":
            url = self._webhook_url()
            if not url:
                return OutboundTransportReceipt(False, 503)
        else:
            try:
                target = json.loads(message.delivery_target)
            except (TypeError, json.JSONDecodeError):
                target = {}
            if (
                not isinstance(target, dict)
                or target.get("kind") != "discord_interaction"
                or not re.fullmatch(r"[0-9]{1,25}", str(target.get("application_id", "")))
                or not re.fullmatch(r"[A-Za-z0-9._~=-]{20,200}", str(target.get("token", "")))
            ):
                return OutboundTransportReceipt(False, 400)
            url = (
                "https://discord.com/api/webhooks/"
                + target["application_id"] + "/" + target["token"]
                + "/messages/@original"
            )
            method = "patch"
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
            if method == "patch":
                response = await client.patch(url, json={"content": message.text})
            else:
                response = await client.post(
                    url, json={"content": message.text}, params={"wait": "true"}
                )
        retry_after = _retry_after(response)
        body = response.json() if response.content else {}
        ok = response.is_success and isinstance(body, dict) and bool(body.get("id"))
        provider_id = str(body.get("id")) if ok else None
        return OutboundTransportReceipt(ok, response.status_code, provider_id, retry_after)


class OutboundTransportRegistry:
    """Explicit allowlist; connector code is never imported dynamically."""

    def __init__(self) -> None:
        self._adapters: dict[str, OutboundTransportAdapter] = {}

    def register(self, adapter: OutboundTransportAdapter) -> None:
        name = getattr(adapter, "channel_name", "")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
            raise MessagingContractError("adapter channel_name must be a lowercase provider key")
        if name in self._adapters:
            raise MessagingContractError(f"adapter already registered for channel: {name}")
        if not callable(getattr(adapter, "send", None)):
            raise MessagingContractError("adapter must provide a send method")
        self._adapters[name] = adapter

    def get(self, channel: str) -> OutboundTransportAdapter:
        try:
            return self._adapters[channel]
        except KeyError as exc:
            raise MessagingContractError(f"channel is not configured: {channel}") from exc

    def list_channels(self) -> list[str]:
        return sorted(self._adapters)


def _retry_after(response: httpx.Response) -> int | None:
    if response.status_code != 429:
        return None
    try:
        return int(response.headers.get("Retry-After", "5"))
    except (TypeError, ValueError):
        return 5


def build_outbound_adapter_registry(
    *, telegram_config: Callable[[], tuple[str, str]],
    discord_webhook_url: Callable[[], str],
) -> OutboundTransportRegistry:
    registry = OutboundTransportRegistry()
    registry.register(TelegramOutboundAdapter(telegram_config))
    registry.register(DiscordOutboundAdapter(discord_webhook_url))
    return registry
