"""Check — hooks-windows: Devin hooks/MCP entries whose command would pop a
visible console window on Windows.

Hooks fire unattended — every ``cmd /c`` without a hidden wrapper, every
``powershell.exe`` without ``-WindowStyle Hidden``, every direct
``.bat``/``.ps1`` invocation and every ``python.exe`` (instead of
``pythonw.exe``) flashes a console window in front of the user. This check
scans user-level config (``config.json``, ``mcp_config.json``,
``User/settings.json`` under the data/config roots) and project-level
``.devin/`` files (``hooks.v1.json``, ``config*.json``, ``mcp_config*.json``)
for those patterns and suggests a hidden wrapper per entry.

The patterns are Windows-specific by construction, so the check is
meaningful on any host that prepares config for a Windows install.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from devin_doctor.checks.config import _load_jsonc
from devin_doctor.model import Context, Finding, Status

CHECK_ID = "hooks-windows"

_PROJECT_CONFIG_FILES = (
    "hooks.v1.json",
    "config.json",
    "config.local.json",
    "mcp_config.json",
    "mcp_config.local.json",
)

_MAX_REPOS = 20

_RE_CMD_SHELL = re.compile(r"\bcmd(?:\.exe)?\b[^|&]*/[ck]\b", re.IGNORECASE)
_RE_POWERSHELL = re.compile(r"\b(?:powershell|pwsh)(?:\.exe)?\b", re.IGNORECASE)
_RE_WINDOW_STYLE = re.compile(
    r"-w(?:indowstyle)?\s+(?:hidden|minimized)\b", re.IGNORECASE
)
_RE_HIDDEN_WRAP = re.compile(
    r"\bstart\b[^|&]*\s/(?:b|min)\b", re.IGNORECASE
)
_RE_PYTHON_CONSOLE = re.compile(r"\bpython[\d.]*\.exe\b", re.IGNORECASE)
_RE_BATCH_FILE = re.compile(r"\S+\.(?:bat|cmd)\b", re.IGNORECASE)
_RE_PS1_FILE = re.compile(r"\S+\.ps1\b", re.IGNORECASE)


def _has_hidden_wrapper(command: str) -> bool:
    return bool(
        _RE_HIDDEN_WRAP.search(command) or _RE_WINDOW_STYLE.search(command)
    )


def console_window_issues(command: str) -> list[tuple[str, str]]:
    """Return ``(problem, fix)`` pairs for a hook/MCP command string that
    would open a visible console window on Windows."""
    issues: list[tuple[str, str]] = []
    hidden = _has_hidden_wrapper(command)
    if _RE_CMD_SHELL.search(command) and not hidden:
        issues.append(
            (
                "cmd /c|/k without a hidden wrapper pops a console window on Windows",
                ("wrap it: 'cmd /c start /min \"\" <command>' or move the payload "
                 "behind 'powershell -WindowStyle Hidden'"),
            )
        )
    if _RE_POWERSHELL.search(command) and not _RE_WINDOW_STYLE.search(command):
        issues.append(
            (
                ("powershell/pwsh without '-WindowStyle Hidden' pops a console "
                 "window on Windows"),
                "add '-NoProfile -WindowStyle Hidden' to the invocation",
            )
        )
    if _RE_PYTHON_CONSOLE.search(command):
        issues.append(
            (
                "'python*.exe' pops a console window on Windows",
                ("use 'pythonw.exe' (no console) for hooks that run without a "
                 "terminal"),
            )
        )
    if _RE_BATCH_FILE.search(command) and not hidden:
        issues.append(
            (
                "direct .bat/.cmd invocation opens a console window on Windows",
                ("wrap it: 'cmd /c start /min \"\" <script>.bat' or call it from "
                 "'powershell -WindowStyle Hidden'"),
            )
        )
    if (
        _RE_PS1_FILE.search(command)
        and not _RE_POWERSHELL.search(command)
        and not hidden
    ):
        issues.append(
            (
                "direct .ps1 invocation opens a console window on Windows",
                ("run it via 'powershell -NoProfile -WindowStyle Hidden -File "
                 "<script>.ps1'"),
            )
        )
    return issues


# ---------------------------------------------------------------------------
# entry extraction
# ---------------------------------------------------------------------------


def _iter_hook_commands(obj: Any, label: str) -> Iterator[tuple[str, str]]:
    """Yield ``(where, command)`` for each command hook in a hooks object."""
    if not isinstance(obj, dict):
        return
    for event, groups in obj.items():
        if not isinstance(groups, list):
            continue
        for gi, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            hooks = group.get("hooks")
            if not isinstance(hooks, list):
                continue
            for hi, hook in enumerate(hooks):
                if (
                    isinstance(hook, dict)
                    and hook.get("type") == "command"
                    and isinstance(hook.get("command"), str)
                ):
                    yield (
                        f"{label} hooks.{event}[{gi}].hooks[{hi}]",
                        hook["command"],
                    )


def _iter_mcp_commands(obj: Any, label: str) -> Iterator[tuple[str, str]]:
    """Yield ``(where, command)`` for each stdio MCP server."""
    servers = obj.get("mcpServers") if isinstance(obj, dict) else None
    if not isinstance(servers, dict):
        return
    for name, server in servers.items():
        if not isinstance(server, dict) or not isinstance(
            server.get("command"), str
        ):
            continue
        cmdline = server["command"]
        args = server.get("args")
        if isinstance(args, list):
            cmdline += " " + " ".join(str(a) for a in args)
        yield f"{label} mcpServers.{name}", cmdline


def _entries_for_file(path: Path, label: str) -> list[tuple[str, str]]:
    """Extract every runnable command a config file declares. Unparseable
    files are skipped — the ``config`` check already reports them."""
    try:
        obj = _load_jsonc(path)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    entries: list[tuple[str, str]] = []
    if path.name == "hooks.v1.json":
        entries += _iter_hook_commands(obj, label)
    else:
        if isinstance(obj, dict):
            entries += _iter_hook_commands(obj.get("hooks"), label)
            entries += _iter_mcp_commands(obj, label)
    return entries


def _user_entries(ctx: Context) -> list[tuple[str, str]]:
    """User-level files under BOTH roots: the data dir (``~/.config/devin``
    holds ``config.json``/``mcp_config.json`` on this layout) and the UI
    config dir (``~/.config/Devin``, incl. ``User/settings.json``)."""
    entries: list[tuple[str, str]] = []
    seen: set[Path] = set()
    for root in {ctx.data_dir, ctx.config_dir} - {None}:
        for name in ("config.json", "mcp_config.json"):
            path = root / name
            if path.is_file() and path not in seen:
                seen.add(path)
                entries += _entries_for_file(path, path.name)
        settings = root / "User" / "settings.json"
        if settings.is_file() and settings not in seen:
            seen.add(settings)
            entries += _entries_for_file(settings, "User/settings.json")
    return entries


def _project_entries(cwd: Path) -> list[tuple[str, str]]:
    """``.devin/`` config in ``cwd`` and each immediate child dir that has
    one — same 'repos under cwd' layout as the ``config`` check."""
    candidates = [cwd]
    try:
        children = sorted(
            d for d in cwd.iterdir() if d.is_dir() and (d / ".devin").is_dir()
        )
    except OSError:
        children = []
    candidates += children[:_MAX_REPOS]

    entries: list[tuple[str, str]] = []
    for repo in candidates:
        devin_dir = repo / ".devin"
        if not devin_dir.is_dir():
            continue
        prefix = f"{repo.name}/.devin" if repo != cwd else ".devin"
        for name in _PROJECT_CONFIG_FILES:
            path = devin_dir / name
            if path.is_file():
                entries += _entries_for_file(path, f"{prefix}/{name}")
    return entries


def run(ctx: Context) -> list[Finding]:
    entries = _user_entries(ctx) + _project_entries(ctx.cwd)
    findings: list[Finding] = []
    for where, command in entries:
        issues = console_window_issues(command)
        if not issues:
            continue
        problems = "; ".join(problem for problem, _ in issues)
        fixes = " | ".join(fix for _, fix in issues)
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{where}: {problems}",
                fix=fixes,
            )
        )
    if findings:
        return findings
    devin_present = ctx.data_dir.is_dir() or (
        ctx.config_dir is not None and ctx.config_dir.is_dir()
    )
    if not devin_present and not entries:
        return []  # nothing Devin-shaped anywhere — 'not detected'
    return [
        Finding(
            CHECK_ID,
            Status.PASS,
            f"no hook/MCP command (of {len(entries)} scanned) would pop a "
            "console window on Windows",
        )
    ]
