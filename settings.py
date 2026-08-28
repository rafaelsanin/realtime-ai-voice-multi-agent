"""Typed, validated environment configuration (commit 6).

Centralizes every env-var read behind one `Settings` object, constructed once
in `main()` and passed into `BellaVistaBot` -- the config half of this
commit's dependency injection (previously scattered `os.environ`/`os.getenv`
calls through `bot.py`).
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Any

from pydantic import Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Deployment(StrEnum):
    """Where the process is running -- detected, not configured, in the common case."""

    LOCAL = "local"
    FLY_IO = "fly_io"
    AWS_ECS = "aws_ecs"


class LogFormat(StrEnum):
    """How log lines are rendered to stdout."""

    CONSOLE = "console"
    JSON = "json"


def _detect_deployment() -> Deployment:
    """Best-effort detection from platform-injected env vars.

    Fly.io sets `FLY_APP_NAME` on every machine; AWS ECS sets
    `ECS_CONTAINER_METADATA_URI_V4` on every task. Neither is set locally.
    """
    if os.getenv("FLY_APP_NAME"):
        return Deployment.FLY_IO
    if os.getenv("ECS_CONTAINER_METADATA_URI_V4"):
        return Deployment.AWS_ECS
    return Deployment.LOCAL


class Settings(BaseSettings):
    """All runtime configuration, validated once at startup.

    Field names map to env vars case-insensitively (pydantic-settings
    default), e.g. `livekit_url` <- `LIVEKIT_URL`. `deployment` can also be
    set explicitly (e.g. `DEPLOYMENT=aws_ecs`) to override auto-detection.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _blank_means_unset(cls, values: Any) -> Any:
        """Drop empty values so `.env`'s placeholder `KEY=` lines fall back to defaults.

        Without this, `OPENAI_MODEL=` would override the default with "".
        """
        if isinstance(values, dict):
            return {k: v for k, v in values.items() if v != ""}
        return values

    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    # Each inbound call gets its own room (commit 9), named by LiveKit's SIP
    # dispatch rule as `<prefix>_<caller>_<random>`. The dispatcher treats
    # every room starting with this prefix as a call to answer, so it has to
    # match `dispatch-rule.json`'s roomPrefix.
    livekit_room_prefix: str = Field(default="call", min_length=1)
    # Concurrent calls one process will run. Each call is its own pipeline
    # (VAD + STT + LLM + TTS), so this is really a sizing knob for the
    # container: raise it with CPU, not on its own.
    max_concurrent_calls: int = Field(default=3, ge=1)

    openai_api_key: str
    openai_model: str = "gpt-4o-mini"

    deepgram_api_key: str

    cartesia_api_key: str
    cartesia_voice_id: str | None = None
    cartesia_booking_voice_id: str | None = None

    supabase_url: str
    supabase_key: str

    # None means "derive from `deployment`" -- see effective_log_format.
    log_format: LogFormat | None = None

    deployment: Deployment = Field(default_factory=_detect_deployment)

    @property
    def is_local(self) -> bool:
        """True unless running on a detected/declared cloud deployment."""
        return self.deployment is Deployment.LOCAL

    @property
    def effective_log_format(self) -> LogFormat:
        """Structured JSON for a cloud deployment's log drain, console locally, unless overridden."""
        if self.log_format is not None:
            return self.log_format
        return LogFormat.CONSOLE if self.is_local else LogFormat.JSON


def load_settings() -> Settings | None:
    """Load `Settings` from the environment.

    Returns None (after printing which fields are missing/invalid) instead of
    raising, so `main()` can fail fast with a one-line message the same way
    the old `check_env()` did -- no traceback for the common "forgot to set a
    key" case.
    """
    try:
        # Every field is populated from the environment, which mypy can't see.
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        names = sorted({str(err["loc"][0]).upper() for err in exc.errors()})
        print(f"missing: {', '.join(names)}")
        return None
