"""Surface check — codex-cli: ``state_<N>.sqlite`` + rollout JSONL.

Root: ``$CODEX_HOME`` else ``~/.codex`` — first existing wins (spec §6.4).
Precedence: the ``state_<N>.sqlite`` with the largest N plus rollout JSONL
under ``sessions/``, ``archived_sessions/`` and ``$CODEX_SESSIONS_DIR``.

Ladder: present → readable → schema → sane. ``threads`` rows are counted,
never selected — no content columns are read.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from devin_doctor.checks.surfaces._common import (
    connect_readonly,
    first_json_line,
    has_sqlite_magic,
    row_count,
    table_names,
)
from devin_doctor.model import Context, Finding, Status
from devin_doctor.paths import default_codex_dir, human_size

CHECK_ID = "codex-cli"

_STATE_RE = re.compile(r"^state_(\d+)\.sqlite$")
_ROLLOUT_DEPTH = 8


def _pick_state_db(root: Path) -> Path | None:
    """The ``state_<N>.sqlite`` with the largest N (spec §6.4)."""
    best: tuple[int, Path] | None = None
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None
    for entry in entries:
        match = _STATE_RE.match(entry.name)
        if entry.is_file() and match and (
            best is None or int(match.group(1)) > best[0]
        ):
            best = (int(match.group(1)), entry)
    return best[1] if best else None


def _walk_rollouts(base: Path, out: list[Path], depth: int = 0) -> None:
    if depth > _ROLLOUT_DEPTH:
        return
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            _walk_rollouts(entry, out, depth + 1)
        elif entry.is_file() and entry.name.startswith("rollout-") and (
            entry.name.endswith(".jsonl")
        ):
            out.append(entry)


def _rollout_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for base in (root / "sessions", root / "archived_sessions"):
        _walk_rollouts(base, out)
    extra = os.environ.get("CODEX_SESSIONS_DIR", "").strip()
    if extra:
        _walk_rollouts(Path(extra), out)
    return sorted(set(out))


def _state_findings(state: Path) -> list[Finding]:
    size = state.stat().st_size
    if size == 0:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{state.name} is empty (0 B) — installed but unused",
            )
        ]
    if not has_sqlite_magic(state):
        return [
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"{state.name} present ({human_size(size)}) but not a "
                "SQLite database (bad magic)",
                fix="Store-corrupt — restore from backup or reinstall "
                "codex-cli.",
            )
        ]
    try:
        con = connect_readonly(state)
    except sqlite3.Error as exc:
        return [
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"{state.name} present ({human_size(size)}) but "
                f"unreadable: {exc}",
                fix="Store-corrupt — restore from backup or reinstall "
                "codex-cli.",
            )
        ]
    try:
        tables = table_names(con)
        if "threads" not in tables:
            return [
                Finding(
                    CHECK_ID,
                    Status.WARN,
                    f"{state.name} lacks a threads table — unrecognized "
                    "layout (schema-unknown)",
                    fix="Written by a codex-cli newer/older than the spec "
                    "knows — report it if the surface matters.",
                )
            ]
        n = row_count(con, "threads")
    except sqlite3.Error as exc:
        return [
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"{state.name} unreadable: {exc}",
                fix="Store-corrupt — restore from backup or reinstall "
                "codex-cli.",
            )
        ]
    finally:
        con.close()
    if n == 0:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{state.name} recognized ({human_size(size)}) — 0 "
                "threads, installed but unused",
            )
        ]
    return [
        Finding(
            CHECK_ID,
            Status.PASS,
            f"{state.name} recognized — {human_size(size)}, {n} threads",
        )
    ]


def _rollout_finding(rollouts: list[Path]) -> Finding | None:
    if not rollouts:
        return None
    unreadable: list[str] = []
    for path in rollouts:
        try:
            if path.stat().st_size == 0:
                continue
        except OSError:
            unreadable.append(path.name)
            continue
        if first_json_line(path) is None:
            unreadable.append(path.name)
    if unreadable:
        shown = ", ".join(unreadable[:3]) + (
            " …" if len(unreadable) > 3 else ""
        )
        return Finding(
            CHECK_ID,
            Status.FAIL,
            f"{len(unreadable)}/{len(rollouts)} rollout file(s) unreadable "
            f"({shown})",
            fix="Store-corrupt — first line is not JSON; the log was "
            "truncated mid-write or damaged.",
        )
    return Finding(
        CHECK_ID, Status.PASS, f"{len(rollouts)} rollout file(s) readable"
    )


def run(ctx: Context) -> list[Finding]:
    del ctx
    root = default_codex_dir()
    if root is None:
        return []  # not detected — codex-cli is not installed here
    state = _pick_state_db(root)
    rollouts = _rollout_files(root)
    if state is None and not rollouts:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                "codex root present, no state db or rollouts — installed "
                "but unused",
            )
        ]
    findings: list[Finding] = []
    if state is not None:
        findings += _state_findings(state)
    rollout = _rollout_finding(rollouts)
    if rollout is not None:
        findings.append(rollout)
    return findings
