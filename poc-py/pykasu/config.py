"""Runtime configuration kept deliberately small and server-side."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    """Configuration for one PYKASU installation.

    Limits are values rather than environment switches on purpose: changing the
    free-tier safeguards should require a deliberate code/configuration review.
    Tests can supply a replacement ``Settings`` instance.
    """

    data_dir: Path
    bind: str = "127.0.0.1"
    port: int = 8000
    groq_api_key: str | None = None
    groq_zero_data_retention: bool = False
    max_enabled_sources: int = 10
    entries_per_feed: int = 20
    summaries_per_refresh: int = 10
    summaries_per_day: int = 30
    model_min_interval_seconds: int = 65
    response_size_limit: int = 2 * 1024 * 1024
    request_deadline_seconds: int = 20
    max_redirects: int = 3
    prompt_character_limit: int = 16_000
    model_completion_limit: int = 2_048

    @property
    def database_path(self) -> Path:
        return self.data_dir / "pykasu.sqlite3"

    @classmethod
    def from_env(cls) -> "Settings":
        default_data_dir = "/tmp/pykasu-py" if os.environ.get("VERCEL") else "./data"
        raw_data_dir = os.environ.get("PYKASU_DATA_DIR", default_data_dir)
        key = os.environ.get("PYKASU_GROQ_API_KEY") or os.environ.get("GROQ_API_KEY")
        return cls(
            data_dir=Path(raw_data_dir).expanduser().resolve(),
            bind=os.environ.get("PYKASU_BIND", "127.0.0.1"),
            port=_positive_int(os.environ.get("PYKASU_PORT"), 8000),
            groq_api_key=key or None,
            groq_zero_data_retention=os.environ.get("PYKASU_GROQ_ZERO_DATA_RETENTION", "").lower()
            in {"1", "true", "yes", "enabled"},
        )


def _positive_int(value: str | None, default: int) -> int:
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError("PYKASU_PORT must be an integer") from exc
    if not 1 <= parsed <= 65535:
        raise ValueError("PYKASU_PORT must be between 1 and 65535")
    return parsed
