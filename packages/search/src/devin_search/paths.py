"""Default locations for Devin's local stores and the search index.

Linux session data uses ``$XDG_DATA_HOME/devin``; UI ACP logs use
``$XDG_CONFIG_HOME/Devin``. The generated index lives in a separate
``devin-search`` data directory — it is the only file this tool writes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIRNAME = "devin"
INDEX_DIRNAME = "devin-search"
INDEX_FILENAME = "search.db"
SESSIONS_DB_RELPATH = ("cli", "sessions.db")
ACP_MESSAGES_RELPATH = ("User", "acp-messages")


def _platform_roots(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> tuple[list[Path], list[Path]]:
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    if plat.startswith("win"):
        roots = [Path(env["APPDATA"])] if env.get("APPDATA") else []
        roots.append(Path.home() / "AppData" / "Roaming")
        return list(dict.fromkeys(roots)), list(dict.fromkeys(roots))
    if plat == "darwin":
        roots = [Path.home() / "Library" / "Application Support"]
        return roots, roots
    data_home = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    config_home = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    data_roots = [data_home, config_home, Path.home()]
    config_roots = [config_home / "Devin", config_home / "devin", Path.home() / "devin"]
    return list(dict.fromkeys(data_roots)), list(dict.fromkeys(config_roots))


def sessions_db_candidates(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> list[Path]:
    """Candidate paths for the CLI ``sessions.db``, newest layout first."""
    data_roots, _ = _platform_roots(environ=environ, platform=platform)
    return [r.joinpath(APP_DIRNAME, *SESSIONS_DB_RELPATH) for r in data_roots]


def acp_dir_candidates(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> list[Path]:
    """Candidate paths for GUI stores, whose Linux root is XDG_CONFIG_HOME."""
    _, config_roots = _platform_roots(environ=environ, platform=platform)
    if (platform or sys.platform).startswith("win"):
        return [r.joinpath("Devin", *ACP_MESSAGES_RELPATH) for r in config_roots]
    if (platform or sys.platform) == "darwin":
        # UI/config dir is capitalized "Devin" on macOS too (canonical
        # paths in devin-internals-spec) — lowercase misses on
        # case-sensitive volumes.
        return [r.joinpath("Devin", *ACP_MESSAGES_RELPATH) for r in config_roots]
    return [r.joinpath(*ACP_MESSAGES_RELPATH) if r.name in ("Devin", "devin")
            else r.joinpath(APP_DIRNAME, *ACP_MESSAGES_RELPATH)
            for r in config_roots]


def default_sessions_db(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> Path | None:
    """First existing ``sessions.db`` candidate, or ``None``."""
    for candidate in sessions_db_candidates(environ=environ, platform=platform):
        if candidate.is_file():
            return candidate
    return None


def default_acp_dir(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> Path | None:
    """First existing ``acp-messages`` directory candidate, or ``None``."""
    for candidate in acp_dir_candidates(environ=environ, platform=platform):
        if candidate.is_dir():
            return candidate
    return None


def default_index_path(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> Path:
    """Where ``search.db`` lives when ``--index`` is not given."""
    data_roots, _ = _platform_roots(environ=environ, platform=platform)
    return data_roots[0] / INDEX_DIRNAME / INDEX_FILENAME
