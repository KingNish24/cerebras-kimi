"""Runtime configuration for the vtx-powered DeepTutor backend.

Everything is env-driven so the same code runs in dev (`python -m app.acp_stdio`)
and behind the Next.js proxy (`DEEPTUTOR_API_BASE_URL`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Root for all persisted state (sessions, uploads, logs).
    data_dir: Path = field(
        default_factory=lambda: Path(_env("DT_DATA_DIR") or "./data").resolve()
    )
    # Model used when a turn carries no llm_selection.
    default_model: str = field(
        default_factory=lambda: _env("DT_DEFAULT_MODEL") or ""
    )
    # Cap on agentic model turns per user turn.
    max_turns: int = field(
        default_factory=lambda: int(_env("DT_MAX_TURNS") or "25")
    )
    # Directory tools (read_file/write_file/...) are jailed to this path.
    workspace_dir: Path = field(
        default_factory=lambda: Path(
            _env("DT_WORKSPACE_DIR") or "./data/workspace"
        ).resolve()
    )
    # When true, /api/auth/status reports auth enabled and unauthenticated
    # requests are rejected. Login itself is not implemented yet — keep false
    # for local single-user deployments.
    auth_enabled: bool = field(
        default_factory=lambda: _env_bool("DT_AUTH_ENABLED", False)
    )
    cors_origins: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            o.strip()
            for o in _env("DT_CORS_ORIGINS", "http://localhost:3782,http://127.0.0.1:3782,http://localhost:3000,http://127.0.0.1:3000").split(",")
            if o.strip()
        )
    )

    @property
    def sessions_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def files_dir(self) -> Path:
        return self.data_dir / "files"


def get_settings() -> Settings:
    return Settings()
