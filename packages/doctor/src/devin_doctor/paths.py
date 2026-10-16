"""Locations of Devin's local stores and config files.

Windows stores use ``%APPDATA%/devin`` for sessions and
``%APPDATA%/Devin`` for UI config. Linux uses ``$XDG_DATA_HOME/devin`` for
session data and ``$XDG_CONFIG_HOME/Devin`` for UI stores; defaults are
``~/.local/share/devin`` and ``~/.config/Devin`` respectively.

The non-Devin agent surfaces (copilot-cli, codex-cli, vscode-chat —
SESSION-SURFACES.md §6.3–§6.5) get the same candidate treatment as
``devin-search/paths.py``: every documented root is listed, callers test
existence.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


def default_data_dir(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> Path:
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    override = env.get("DEVIN_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if plat.startswith("win"):
        appdata = env.get("APPDATA")
        if appdata:
            return Path(appdata) / "devin"
        return Path.home() / "AppData" / "Roaming" / "devin"
    if plat == "darwin":
        return Path.home() / "Library" / "Application Support" / "devin"
    data_home = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    config_home = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    candidates = [data_home / "devin", config_home / "devin", Path.home() / "devin"]
    return next(
        (root for root in candidates if sessions_db_path(root).is_file()),
        candidates[0],
    )


def default_config_dir(
    environ: dict[str, str] | None = None, platform: str | None = None
) -> Path:
    env = os.environ if environ is None else environ
    plat = sys.platform if platform is None else platform
    override = env.get("DEVIN_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if plat.startswith("win"):
        appdata = env.get("APPDATA")
        return Path(appdata) / "Devin" if appdata else Path.home() / "AppData" / "Roaming" / "Devin"
    if plat == "darwin":
        return Path.home() / "Library" / "Application Support" / "Devin"
    config_home = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    candidates = [config_home / "Devin", config_home / "devin"]
    return next((root for root in candidates if (root / "User").is_dir()), candidates[0])


def sessions_db_path(root: Path) -> Path:
    return root / "cli" / "sessions.db"


def acp_messages_dir(root: Path) -> Path:
    return root / "User" / "acp-messages"


def state_vscdb_path(root: Path) -> Path:
    return root / "User" / "globalStorage" / "state.vscdb"


def credentials_path(root: Path) -> Path:
    return root / "credentials.toml"


def user_config_path(root: Path) -> Path:
    return root / "config.json"


def user_mcp_config_path(root: Path) -> Path:
    return root / "mcp_config.json"


def session_locks_dir(root: Path) -> Path:
    return root / "cli" / "session_locks"


@dataclass(frozen=True)
class StorePaths:
    """Everything the checks need to know about where stores live."""

    sessions_db: Path
    acp_dir: Path
    acp_dbs: tuple[Path, ...]
    state_vscdb: Path

    def all_db_files(self) -> list[Path]:
        files = [self.sessions_db, *self.acp_dbs, self.state_vscdb]
        return [p for p in files if p.is_file()]


def locate_stores(root: Path, config_dir: Path | None = None) -> StorePaths:
    root = Path(root).expanduser()
    if config_dir is not None:
        user_root = Path(config_dir).expanduser()
    elif (root / "User").is_dir() or root != default_data_dir():
        user_root = root
    else:
        user_root = default_config_dir()
    acp_dir = acp_messages_dir(user_root)
    acp_dbs = (
        tuple(sorted(acp_dir.glob("*.db"))) if acp_dir.is_dir() else ()
    )
    return StorePaths(
        sessions_db=sessions_db_path(root),
        acp_dir=acp_dir,
        acp_dbs=acp_dbs,
        state_vscdb=state_vscdb_path(user_root),
    )


# --- non-Devin surfaces (SESSION-SURFACES.md §6.3–§6.5) --------------------
# Same candidates devin-search/paths.py scans — keep the two in sync.

COPILOT_ENV_VARS = ("COPILOT_DIR", "COPILOT_HOME")
COPILOT_DIRNAME = ".copilot"
CODEX_ENV_VAR = "CODEX_HOME"
CODEX_DIRNAME = ".codex"
VSCODE_VARIANTS = ("Code", "Code - Insiders")
VSCODE_SERVER_RELPATH = (".vscode-server", "data", "User")


def _home(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get("HOME") or Path.home())


def copilot_root_candidates(
    environ: dict[str, str] | None = None,
) -> list[Path]:
    """Copilot roots in scan order: ``$COPILOT_DIR``, ``$COPILOT_HOME``,
    ``~/.copilot`` (spec §6.3 — every existing root is merged)."""
    env = os.environ if environ is None else environ
    roots = [Path(env[v]) for v in COPILOT_ENV_VARS if env.get(v)]
    roots.append(_home(env) / COPILOT_DIRNAME)
    return list(dict.fromkeys(roots))


def codex_root_candidates(
    environ: dict[str, str] | None = None,
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
    environ: dict[str, str] | None = None,
) -> list[Path]:
    """All existing Copilot roots (they merge, unlike other stores)."""
    return [
        c for c in copilot_root_candidates(environ=environ) if c.is_dir()
    ]


def default_codex_dir(
    environ: dict[str, str] | None = None,
) -> Path | None:
    """First existing Codex root candidate, or ``None``."""
    for candidate in codex_root_candidates(environ=environ):
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


def human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"
