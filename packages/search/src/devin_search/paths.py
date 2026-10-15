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

# Non-Devin surfaces (SESSION-SURFACES.md §6.3–§6.5).
COPILOT_ENV_VARS = ("COPILOT_DIR", "COPILOT_HOME")
COPILOT_DIRNAME = ".copilot"
CODEX_ENV_VAR = "CODEX_HOME"
CODEX_DIRNAME = ".codex"
VSCODE_VARIANTS = ("Code", "Code - Insiders")
VSCODE_SERVER_RELPATH = (".vscode-server", "data", "User")


def _home(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("HOME") or Path.home())


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
        home = Path(env.get("HOME") or Path.home())
        roots = [home / "Library" / "Application Support"]
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


def copilot_root_candidates(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> list[Path]:
    """Copilot roots in scan order: ``$COPILOT_DIR``, ``$COPILOT_HOME``,
    ``~/.copilot`` (spec §6.3 — every existing root is merged)."""
    env = os.environ if environ is None else environ
    roots = [Path(env[v]) for v in COPILOT_ENV_VARS if env.get(v)]
    roots.append(_home(env) / COPILOT_DIRNAME)
    return list(dict.fromkeys(roots))


def codex_root_candidates(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> list[Path]:
    """Codex root candidates (spec §6.4): ``$CODEX_HOME`` then
    ``~/.codex`` — first existing wins, they are not merged."""
    env = os.environ if environ is None else environ
    roots = [Path(env[CODEX_ENV_VAR])] if env.get(CODEX_ENV_VAR) else []
    roots.append(_home(env) / CODEX_DIRNAME)
    return list(dict.fromkeys(roots))


def vscode_chat_userdir_candidates(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> list[Path]:
    """Candidate ``<...>/User`` dirs holding ``chatSessions`` stores
    (spec §6.5): ``<base>/<Variant>/User`` for both VS Code variants plus
    the remote ``~/.vscode-server/data/User``.

    ``<base>`` is ``$APPDATA`` when set, else the platform config root
    (``AppData/Roaming`` on win32, ``Library/Application Support`` on
    darwin, ``~/.config`` elsewhere) — mirrors the TS ``userDirs``.
    """
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    home = _home(env)
    if env.get("APPDATA"):
        base = Path(env["APPDATA"])
    elif plat.startswith("win"):
        base = home / "AppData" / "Roaming"
    elif plat == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = home / ".config"
    return [
        *(base / variant / "User" for variant in VSCODE_VARIANTS),
        home.joinpath(*VSCODE_SERVER_RELPATH),
    ]



def default_copilot_dirs(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> list[Path]:
    """All existing Copilot roots (they merge, unlike other stores)."""
    return [
        c
        for c in copilot_root_candidates(environ=environ, platform=platform)
        if c.is_dir()
    ]


def default_codex_dir(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> Path | None:
    """First existing Codex root candidate, or ``None``."""
    for candidate in codex_root_candidates(environ=environ, platform=platform):
        if candidate.is_dir():
            return candidate
    return None


def default_vscode_chat_dirs(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> list[Path]:
    """All existing vscode-chat ``User`` dirs (variants merge)."""
    return [
        c
        for c in vscode_chat_userdir_candidates(
            environ=environ, platform=platform
        )
        if c.is_dir()
    ]


def default_index_path(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> Path:
    """Where ``search.db`` lives when ``--index`` is not given."""
    data_roots, _ = _platform_roots(environ=environ, platform=platform)
    return data_roots[0] / INDEX_DIRNAME / INDEX_FILENAME
