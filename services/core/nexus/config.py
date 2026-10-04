"""Application paths and settings.

Follows the XDG base directory spec so NEXUS data lives where a Linux desktop
application is expected to put it, rather than inside the source tree.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_NAME = "nexus"

HOME = Path.home()
REPO_ROOT = Path(__file__).resolve().parents[3]


def _xdg(var: str, default: Path) -> Path:
    raw = os.environ.get(var)
    return (Path(raw) if raw else default) / APP_NAME


DATA_DIR = _xdg("XDG_DATA_HOME", HOME / ".local" / "share")
CONFIG_DIR = _xdg("XDG_CONFIG_HOME", HOME / ".config")
CACHE_DIR = _xdg("XDG_CACHE_HOME", HOME / ".cache")
STATE_DIR = _xdg("XDG_STATE_HOME", HOME / ".local" / "state")


class Settings(BaseSettings):
    """Runtime configuration.

    Every field can be overridden with a ``NEXUS_``-prefixed environment
    variable, e.g. ``NEXUS_PORT=9000``.
    """

    model_config = SettingsConfigDict(
        env_prefix="NEXUS_",
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8787
    log_level: str = "INFO"
    dev: bool = False

    db_path: Path = DATA_DIR / "nexus.db"
    screenshot_dir: Path = CACHE_DIR / "screenshots"
    # Speech in and out. Kept in the cache tree, not the data tree: audio is
    # regenerable and the user should be able to delete all of it without losing
    # conversations or memory.
    audio_dir: Path = CACHE_DIR / "audio"

    # Tool execution guards. Deliberately conservative defaults; the permission
    # engine is the authority, these are the blast-radius limits.
    tool_timeout_s: float = 30.0
    terminal_timeout_s: float = 20.0
    max_file_read_bytes: int = 2_000_000
    max_terminal_output_bytes: int = 200_000

    # A stall guard for model streaming (E8): the cold-start deadline is cleared
    # on the first token, the idle deadline resets on every chunk.
    stream_cold_start_s: float = 45.0
    stream_idle_s: float = 30.0


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings()


def ensure_dirs() -> None:
    """Create the directories NEXUS writes to. Safe to call repeatedly."""
    s = settings()
    for path in (DATA_DIR, CONFIG_DIR, CACHE_DIR, STATE_DIR, s.screenshot_dir, s.audio_dir):
        path.mkdir(parents=True, exist_ok=True)
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
