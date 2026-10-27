"""Default locations for Devin's local session database.

Linux uses ``$XDG_DATA_HOME/devin/cli/sessions.db`` (default
``~/.local/share/devin/cli/sessions.db``), with older config-dir candidates as
fallbacks.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIRNAME = "devin"
SESSIONS_DB_RELPATH = ("cli", "sessions.db")


def _roots(environ: dict[str, str], platform: str) -> list[Path]:
    if platform.startswith("win"):
        roots = []
        if environ.get("APPDATA"):
            roots.append(Path(environ["APPDATA"]))
        roots.append(Path.home() / "AppData" / "Roaming")
    elif platform == "darwin":
        roots = [Path.home() / "Library" / "Application Support"]
    else:
        roots = [
            Path(environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"),
            Path(environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"),
            Path.home(),
        ]
    return list(dict.fromkeys(roots))


def sessions_db_candidates(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> list[Path]:
    """Candidate paths for the CLI ``sessions.db``, newest layout first."""
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    return [
        root.joinpath(APP_DIRNAME, *SESSIONS_DB_RELPATH)
        for root in _roots(env, plat)
    ]


def default_sessions_db(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> Path | None:
    """First existing candidate, or ``None`` when no store is present."""
    for candidate in sessions_db_candidates(environ=environ, platform=platform):
        if candidate.is_file():
            return candidate
    return None


VSCDB_RELPATH = ("User", "globalStorage", "state.vscdb")


def state_vscdb_candidates(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> list[Path]:
    """Candidate paths for the GUI ``state.vscdb`` (Electron storage)."""
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    if plat.startswith("win"):
        roots = []
        if env.get("APPDATA"):
            roots.append(Path(env["APPDATA"]))
        roots.append(Path.home() / "AppData" / "Roaming")
    elif plat == "darwin":
        roots = [Path.home() / "Library" / "Application Support"]
    else:
        roots = [
            Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config"),
            Path.home(),
        ]
    names = ("Devin", "devin")
    return [
        r.joinpath(n, *VSCDB_RELPATH)
        for r in dict.fromkeys(roots) for n in names
    ]


def default_state_vscdb(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> Path | None:
    """First existing ``state.vscdb`` candidate, or ``None``."""
    for candidate in state_vscdb_candidates(environ=environ, platform=platform):
        if candidate.is_file():
            return candidate
    return None


def acp_dir_candidates(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> list[Path]:
    """Candidate paths for ``User/acp-messages`` (the per-session
    transcript dbs) — derived from each ``state.vscdb`` candidate's
    ``User/`` parent."""
    return [
        c.parent.parent / "acp-messages"
        for c in state_vscdb_candidates(environ=environ, platform=platform)
    ]


def default_acp_dir(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> Path | None:
    """First existing ``acp-messages`` dir candidate, or ``None``."""
    for candidate in acp_dir_candidates(environ=environ, platform=platform):
        if candidate.is_dir():
            return candidate
    return None
