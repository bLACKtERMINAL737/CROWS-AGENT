import pytest

from crows.config import Settings


def test_model_timeout_defaults_to_45_seconds(monkeypatch):
    monkeypatch.delenv("AETHER_MODEL_TIMEOUT_SECONDS", raising=False)

    settings = Settings.from_env()

    assert settings.model_timeout_seconds == 45.0


@pytest.mark.parametrize("timeout", ["1", "12.5", "600"])
def test_model_timeout_accepts_values_within_supported_range(monkeypatch, timeout):
    monkeypatch.setenv("AETHER_MODEL_TIMEOUT_SECONDS", timeout)

    settings = Settings.from_env()

    assert settings.model_timeout_seconds == float(timeout)


@pytest.mark.parametrize(
    "timeout",
    ["", "abc", "0", "-1", "600.1", "nan", "inf", "-inf"],
)
def test_model_timeout_rejects_invalid_values(monkeypatch, timeout):
    monkeypatch.setenv("AETHER_MODEL_TIMEOUT_SECONDS", timeout)

    with pytest.raises(ValueError, match="CROWS_MODEL_TIMEOUT_SECONDS"):
        Settings.from_env()
