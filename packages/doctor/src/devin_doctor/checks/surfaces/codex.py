"""Surface check — codex-cli: ``state_<N>.sqlite`` + rollout JSONL.

Root: ``$CODEX_HOME`` else ``~/.codex`` — first existing wins (spec §6.4).
The ``state_<N>.sqlite`` with the largest N is selected; rollout JSONL
lives under ``sessions/``, ``archived_sessions/`` and the out-of-root
``$CODEX_SESSIONS_DIR`` — a rollout-only install is still a present
surface.

Ladder (SURFACE-HEALTH §4.4): present → readable → schema → sane, findings
tagged with their rung + §5 code. ``threads`` rows are counted, never
selected — no content columns are read.
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from devin_doctor.checks.surfaces._common import (
    connect_readonly,
    has_sqlite_magic,
    row_count,
    scan_jsonl,
    table_names,
)
from devin_doctor.model import Context, Finding, Status
from devin_doctor.paths import default_codex_dir, human_size

CHECK_ID = "codex-cli"

_STATE_GLOB = "state_*.sqlite"
_STATE_RE = re.compile(r"^state_(\d+)\.sqlite$")
_ROLLOUT_DIRS = ("sessions", "archived_sessions")
_ROLLOUT_DEPTH = 8


def _state_files(root: Path) -> list[Path]:
    """Every ``state_*.sqlite`` under the root (numeric suffix or not)."""
    try:
        return sorted(
            p
            for p in root.glob(_STATE_GLOB)
            if p.is_file()
        )
    except OSError:
        return []


def _pick_state_db(
    state_files: list[Path],
) -> tuple[Path, bool, list[Path]] | None:
    """``(path, numeric_suffix, nonnumeric)`` — largest N wins; a purely
    non-numeric set falls back to the lexicographic last (its N is
    unparseable → schema rung flags it unknown). ``nonnumeric`` carries
    every ``state_*.sqlite`` whose suffix is not numeric — coexisting
    foreign artifacts that still owe a schema-unknown WARN."""
    numeric = [
        (int(m.group(1)), p)
        for p in state_files
        if (m := _STATE_RE.match(p.name))
    ]
    nonnumeric = [p for p in state_files if not _STATE_RE.match(p.name)]
    if numeric:
        _n, path = max(numeric, key=lambda t: t[0])
        return path, True, nonnumeric
    if state_files:
        return state_files[-1], False, nonnumeric
    return None


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


def _rollout_files(root: Path | None, sessions_dir: Path | None) -> list[Path]:
    out: list[Path] = []
    if root is not None:
        for name in _ROLLOUT_DIRS:
            _walk_rollouts(root / name, out)
    if sessions_dir is not None:
        _walk_rollouts(sessions_dir, out)
    return sorted(set(out))


def _sessions_dir() -> Path | None:
    """``$CODEX_SESSIONS_DIR`` — the rollout-only leg may live entirely
    outside ``.codex``."""
    raw = os.environ.get("CODEX_SESSIONS_DIR", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_dir() else None


def _classify_rollouts(
    rollouts: list[Path],
) -> tuple[list[str], list[Path], list[Path]]:
    """``(unreadable names, live paths, dead paths)`` — a terminated line
    that fails to parse is corruption (readable FAIL); zero-byte or
    record-less logs are dead artifacts the sane rung must still flag as
    empty-store even when a healthy state db exists."""
    unreadable: list[str] = []
    live: list[Path] = []
    dead: list[Path] = []
    for path in rollouts:
        try:
            size = path.stat().st_size
        except OSError:
            unreadable.append(path.name)
            continue
        if size == 0:
            dead.append(path)
            continue
        scan = scan_jsonl(path)
        if not scan.ok:
            # terminated malformed line → corrupt
            unreadable.append(path.name)
            continue
        if scan.records == 0:
            # whitespace-only or a single unterminated partial — nothing
            # computable (an unterminated-only file can't be told apart
            # from a writer mid-append of its first record — WARN, not
            # FAIL).
            dead.append(path)
            continue
        live.append(path)
    return unreadable, live, dead


def _state_ladder(
    state: Path, numeric: bool
) -> tuple[Finding | None, Finding | None, int | str | None]:
    """``(readable, schema, threads-count)`` for the selected state db —
    the third return is ``"empty"`` for a zero-byte file, ``None`` when
    the db has no threads table, else the row count."""
    size = state.stat().st_size
    if size == 0:
        return None, None, "empty"
    if not has_sqlite_magic(state):
        return (
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"{state.name} present ({human_size(size)}) but not a "
                "SQLite database (bad magic)",
                fix="Store-corrupt — restore from backup or reinstall "
                "codex-cli.",
                rung="readable",
                code="store-corrupt",
            ),
            None,
            None,
        )
    try:
        con = connect_readonly(state)
    except sqlite3.Error as exc:
        return (
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"{state.name} present ({human_size(size)}) but "
                f"unreadable: {exc}",
                fix="Store-corrupt — restore from backup or reinstall "
                "codex-cli.",
                rung="readable",
                code="store-corrupt",
            ),
            None,
            None,
        )
    try:
        tables = table_names(con)
        n = row_count(con, "threads") if "threads" in tables else None
    except sqlite3.Error as exc:
        return (
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"{state.name} unreadable: {exc}",
                fix="Store-corrupt — restore from backup or reinstall "
                "codex-cli.",
                rung="readable",
                code="store-corrupt",
            ),
            None,
            None,
        )
    finally:
        con.close()
    readable = Finding(
        CHECK_ID,
        Status.PASS,
        f"{state.name} opens read-only ({human_size(size)})",
        rung="readable",
    )
    if not numeric or n is None:
        schema = Finding(
            CHECK_ID,
            Status.WARN,
            f"{state.name} lacks a numeric suffix or threads table — "
            "unrecognized layout (schema-unknown)",
            fix="Written by a codex-cli newer/older than the spec "
            "knows — report it if the surface matters.",
            rung="schema",
            code="schema-unknown",
        )
    else:
        schema = Finding(
            CHECK_ID,
            Status.PASS,
            f"{state.name} recognized — numeric suffix, threads table "
            "present",
            rung="schema",
        )
    return readable, schema, n


def run(ctx: Context) -> list[Finding]:
    del ctx
    root = default_codex_dir()
    sessions_dir = _sessions_dir()
    if root is None and sessions_dir is None:
        return [
            Finding(
                CHECK_ID,
                Status.PASS,
                "no codex root — codex-cli not installed",
                rung="present",
                note="not-installed",
            )
        ]
    state_files = _state_files(root) if root is not None else []
    rollout_dirs = (
        [d for d in (root / n for n in _ROLLOUT_DIRS) if d.is_dir()]
        if root is not None
        else []
    )
    rollouts = _rollout_files(root, sessions_dir)
    if not state_files and not rollout_dirs and sessions_dir is None:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                f"codex root present ({root}), no state db or rollout "
                "dirs — installed but unused",
                fix="The root exists but no store was ever created — "
                "safe to ignore.",
                rung="present",
                code="store-missing",
            )
        ]
    findings = [
        Finding(
            CHECK_ID,
            Status.PASS,
            f"codex root present — {len(state_files)} state db(s), "
            f"{len(rollouts)} rollout file(s)",
            rung="present",
        )
    ]

    picked = _pick_state_db(state_files)
    if picked is None:
        # rollout-only store — the JSONL leg carries every rung (§4.4)
        if not rollouts:
            findings.append(
                Finding(
                    CHECK_ID,
                    Status.WARN,
                    "rollout dirs present, 0 rollout files — installed "
                    "but unused",
                    rung="sane",
                    code="store-empty",
                )
            )
            return findings
        unreadable, live, _dead = _classify_rollouts(rollouts)
        if unreadable:
            shown = ", ".join(unreadable[:3]) + (
                " …" if len(unreadable) > 3 else ""
            )
            findings.append(
                Finding(
                    CHECK_ID,
                    Status.FAIL,
                    f"{len(unreadable)}/{len(rollouts)} rollout file(s) "
                    f"unreadable ({shown})",
                    fix="Store-corrupt — a log line is not JSON; the "
                    "file was truncated mid-write or damaged.",
                    rung="readable",
                    code="store-corrupt",
                )
            )
            return findings
        if not live:
            findings.append(
                Finding(
                    CHECK_ID,
                    Status.WARN,
                    f"{len(rollouts)} rollout file(s) with no usable "
                    "records — empty-store",
                    fix="Zero-byte, whitespace-only or mid-append partial "
                    "files were never written to completion — safe to "
                    "delete or ignore.",
                    rung="sane",
                    code="empty-store",
                )
            )
            return findings
        findings += [
            Finding(
                CHECK_ID,
                Status.PASS,
                f"{len(live)} rollout file(s) readable",
                rung="readable",
            ),
            Finding(
                CHECK_ID,
                Status.PASS,
                "rollout logs carry no version marker — schema assumed",
                rung="schema",
            ),
            Finding(
                CHECK_ID,
                Status.PASS,
                f"{len(live)} rollout file(s) with session records",
                rung="sane",
            ),
        ]
        return findings

    state, numeric, nonnumeric = picked
    readable, schema, marker = _state_ladder(state, numeric)
    if readable is not None and readable.status is Status.FAIL:
        findings.append(readable)
        return findings
    # rollouts join the readable verdict: a malformed terminated line is
    # corruption whatever the state db says
    unreadable, live_rollouts, dead_rollouts = _classify_rollouts(rollouts)
    if unreadable:
        shown = ", ".join(unreadable[:3]) + (
            " …" if len(unreadable) > 3 else ""
        )
        findings.append(
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"{len(unreadable)}/{len(rollouts)} rollout file(s) "
                f"unreadable ({shown})",
                fix="Store-corrupt — a log line is not JSON; the file "
                "was truncated mid-write or damaged.",
                rung="readable",
                code="store-corrupt",
            )
        )
        return findings
    if isinstance(marker, str):  # "empty" — zero-byte state db
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{state.name} is empty (0 B) — installed but unused",
                fix="A zero-byte store is not corrupt — codex-cli just "
                "never wrote to it.",
                rung="sane",
                code="empty-store",
            )
        )
        return findings
    findings.append(readable)
    findings.append(schema)
    if schema is not None and schema.status is Status.FAIL:
        return findings
    if nonnumeric:
        # state_*.sqlite files without a numeric suffix — foreign
        # artifacts the schema rung must flag, not silently ignore
        shown = ", ".join(p.name for p in nonnumeric[:3]) + (
            " …" if len(nonnumeric) > 3 else ""
        )
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(nonnumeric)} non-numeric state db name(s) — "
                f"unrecognized layout ({shown})",
                fix="Written by a codex-cli newer/older than the spec "
                "knows — report it if the surface matters.",
                rung="schema",
                code="schema-unknown",
            )
        )

    # sane — one verdict: dead rollout artifacts WARN empty-store even
    # beside a healthy state db; then the threads count / rollout
    # presence answers "plausibly used" (§4.4).
    n_threads = marker if isinstance(marker, int) else None
    if dead_rollouts:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(dead_rollouts)} rollout file(s) with no usable "
                "records — empty-store",
                fix="Zero-byte, whitespace-only or mid-append partial "
                "files were never written to completion — safe to "
                "delete or ignore.",
                rung="sane",
                code="empty-store",
            )
        )
    elif n_threads is None and not rollouts:
        # no countable signal — schema-unknown shape with nothing else;
        # a rung with nothing to evaluate emits no finding
        return findings
    elif n_threads == 0 and not live_rollouts:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{state.name} recognized — 0 threads, no live "
                "rollouts — installed but unused",
                rung="sane",
                code="store-empty",
            )
        )
    elif n_threads is not None:
        extra = (
            f", {len(live_rollouts)} rollout file(s)"
            if live_rollouts
            else ""
        )
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                f"{state.name}: {n_threads} threads{extra}",
                rung="sane",
            )
        )
    else:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                f"{len(live_rollouts)} rollout file(s) with session "
                "records",
                rung="sane",
            )
        )
    return findings
