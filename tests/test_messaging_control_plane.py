from pathlib import Path

from crows import app as app_module
from crows.messaging_store import MessagingStore


def test_messaging_control_plane_reports_readiness_without_secrets(monkeypatch, tmp_path: Path):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("CROWS_TELEGRAM_BOT_TOKEN", "telegram-secret-token")
    monkeypatch.setenv("CROWS_TELEGRAM_WEBHOOK_SECRET", "telegram-webhook-secret")
    monkeypatch.setenv("CROWS_DISCORD_APPLICATION_ID", "123456789")
    monkeypatch.setenv("CROWS_DISCORD_PUBLIC_KEY", "a" * 64)
    monkeypatch.setenv("CROWS_DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123456/discord-secret-token")
    monkeypatch.setenv("CROWS_MESSAGING_TARGET_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("CROWS_MESSAGING_WORKER_ENABLED", "true")
    monkeypatch.setenv("CROWS_INBOUND_AGENT_WORKER_ENABLED", "true")
    store = MessagingStore(tmp_path / "messaging.sqlite3")
    monkeypatch.setattr(app_module, "messaging_store", store)

    result = app_module.messaging_control_plane_status()
    channels = {item["channel"]: item for item in result["channels"]}

    assert result["workers"] == {"outbound_enabled": True, "inbound_agent_enabled": True}
    assert result["security"]["delivery_target_encryption_configured"] is True
    assert channels["telegram"]["inbound_connector_configured"] is True
    assert channels["telegram"]["outbound_connector_configured"] is True
    assert channels["telegram"]["inbound_agent_replies_ready"] is True
    assert channels["discord"]["inbound_connector_configured"] is True
    assert channels["discord"]["outbound_connector_configured"] is True
    assert channels["discord"]["inbound_agent_replies_ready"] is True
    assert result["delivery"]["queue"]["pending"] == 0
    serialized = str(result)
    assert "telegram-secret-token" not in serialized
    assert "telegram-webhook-secret" not in serialized
    assert "discord-secret-token" not in serialized
    assert "a" * 64 not in serialized


def test_messaging_control_plane_distinguishes_config_from_enabled_workers(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("CROWS_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("AETHER_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CROWS_TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("AETHER_TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("CROWS_DISCORD_APPLICATION_ID", raising=False)
    monkeypatch.delenv("AETHER_DISCORD_APPLICATION_ID", raising=False)
    monkeypatch.delenv("CROWS_DISCORD_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("AETHER_DISCORD_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("CROWS_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AETHER_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("CROWS_MESSAGING_TARGET_KEY", raising=False)
    monkeypatch.delenv("AETHER_MESSAGING_TARGET_KEY", raising=False)
    monkeypatch.setenv("CROWS_MESSAGING_WORKER_ENABLED", "false")
    monkeypatch.setenv("CROWS_INBOUND_AGENT_WORKER_ENABLED", "false")
    store = MessagingStore(tmp_path / "messaging.sqlite3")
    monkeypatch.setattr(app_module, "messaging_store", store)

    result = app_module.messaging_control_plane_status()
    channels = {item["channel"]: item for item in result["channels"]}

    assert result["workers"]["outbound_enabled"] is False
    assert result["workers"]["inbound_agent_enabled"] is False
    assert result["security"]["delivery_target_encryption_configured"] is False
    assert channels["telegram"]["inbound_policy_enabled"] is True
    assert channels["telegram"]["outbound_transport_registered"] is True
    assert channels["telegram"]["inbound_connector_configured"] is False
    assert channels["telegram"]["outbound_connector_configured"] is False
    assert channels["telegram"]["inbound_agent_replies_ready"] is False


def test_messaging_channels_lists_registered_channels_and_real_configuration(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("CROWS_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("AETHER_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CROWS_TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("AETHER_TELEGRAM_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("CROWS_DISCORD_APPLICATION_ID", raising=False)
    monkeypatch.delenv("AETHER_DISCORD_APPLICATION_ID", raising=False)
    monkeypatch.delenv("CROWS_DISCORD_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("AETHER_DISCORD_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("CROWS_DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("AETHER_DISCORD_WEBHOOK_URL", raising=False)
    store = MessagingStore(tmp_path / "messaging.sqlite3")
    monkeypatch.setattr(app_module, "messaging_store", store)

    result = app_module.messaging_channels()

    assert result["channels"] == ["discord", "telegram"]
    assert result["configured"] is False

    monkeypatch.setenv("CROWS_TELEGRAM_BOT_TOKEN", "configured-but-not-returned")
    configured = app_module.messaging_channels()
    assert configured["configured"] is True
    assert "configured-but-not-returned" not in str(configured)
