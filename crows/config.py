from dataclasses import dataclass
import math
import os
from pathlib import Path


def get_env(name: str, default: str | None = None) -> str | None:
    """Read the current Crows setting, falling back to its legacy AETHER_ name."""
    value = os.getenv(name)
    if value is not None:
        return value
    if name.startswith("CROWS_"):
        legacy = os.getenv("AETHER_" + name[len("CROWS_"):])
        if legacy is not None:
            return legacy
    return default


@dataclass(frozen=True)
class Settings:
    db_path: Path
    workspace: Path
    model_base_url: str | None
    model_api_key: str | None
    model_name: str | None
    coding_model_base_url: str | None
    coding_model_api_key: str | None
    coding_model_name: str | None
    fallback_model_base_url: str | None
    fallback_model_api_key: str | None
    fallback_model_name: str | None
    reasoning_model_base_url: str | None
    reasoning_model_api_key: str | None
    reasoning_model_name: str | None
    model_timeout_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        raw_timeout = get_env("CROWS_MODEL_TIMEOUT_SECONDS", "45")
        try:
            model_timeout_seconds = float(raw_timeout)
        except ValueError as exc:
            raise ValueError("CROWS_MODEL_TIMEOUT_SECONDS must be a number between 1 and 600") from exc
        if not math.isfinite(model_timeout_seconds) or not 1 <= model_timeout_seconds <= 600:
            raise ValueError("CROWS_MODEL_TIMEOUT_SECONDS must be a number between 1 and 600")
        return cls(
            db_path=Path(get_env("CROWS_DB_PATH", "./data/aether.sqlite3")).expanduser(),
            workspace=Path(get_env("CROWS_WORKSPACE", "./workspace")).expanduser(),
            model_base_url=get_env("CROWS_MODEL_BASE_URL") or None,
            model_api_key=get_env("CROWS_MODEL_API_KEY") or None,
            model_name=get_env("CROWS_MODEL_NAME") or None,
            coding_model_base_url=get_env("CROWS_CODING_MODEL_BASE_URL") or None,
            coding_model_api_key=get_env("CROWS_CODING_MODEL_API_KEY") or None,
            coding_model_name=get_env("CROWS_CODING_MODEL_NAME") or None,
            fallback_model_base_url=get_env("CROWS_FALLBACK_MODEL_BASE_URL") or None,
            fallback_model_api_key=get_env("CROWS_FALLBACK_MODEL_API_KEY") or None,
            fallback_model_name=get_env("CROWS_FALLBACK_MODEL_NAME") or None,
            reasoning_model_base_url=get_env("CROWS_REASONING_MODEL_BASE_URL") or None,
            reasoning_model_api_key=get_env("CROWS_REASONING_MODEL_API_KEY") or None,
            reasoning_model_name=get_env("CROWS_REASONING_MODEL_NAME") or None,
            model_timeout_seconds=model_timeout_seconds,
        )
    