"""Check 3 — health-of-data: empty sessions, orphan ``message_nodes``,
sessions inactive for more than ``ctx.stale_days`` days, and databases that
answer ``SQLITE_BUSY`` (usually meaning Devin is running).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from devin_internals.parsers import SessionsStore
from devin_internals.schema import SchemaError

from devin_doctor.model import Context, Finding, Status
from devin_doctor.paths import locate_stores

CHECK_ID = "health-of-data"

_DAY_MS = 86_400_000


def _is_locked(path: Path) -> bool:
    try:
        con = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro", uri=True, timeout=0
        )
        try:
            con.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        finally:
            con.close()
    except sqlite3.OperationalError as exc:
        return "locked" in str(exc).lower()
    except sqlite3.Error:
        return False
    return False


def _lock_finding(ctx: Context) -> Finding:
    stores = locate_stores(ctx.data_dir)
    locked = [p for p in stores.all_db_files() if _is_locked(p)]
    if not locked:
        return Finding(CHECK_ID, Status.PASS, "no locked database files")
    names = ", ".join(p.name for p in locked[:5])
    return Finding(
        CHECK_ID,
        Status.WARN,
        f"{len(locked)} database file(s) are locked (SQLITE_BUSY): {names}",
        fix="Devin is probably running — close it and re-run the check.",
    )


def run(ctx: Context) -> list[Finding]:
    if not ctx.data_dir.is_dir():
        return []  # no Devin store — the check reports 'not detected'
    sessions_db = ctx.data_dir / "cli" / "sessions.db"
    if not sessions_db.is_file():
        return []  # Desktop-only layout — no CLI store to inspect
    findings: list[Finding] = []

    try:
        store = SessionsStore(sessions_db)
    except (SchemaError, sqlite3.Error, OSError) as exc:
        if "locked" in str(exc).lower():
            message = (
                "sessions.db is locked (SQLITE_BUSY) — Devin is probably "
                "running; data-health checks skipped"
            )
            fix = "Close Devin and re-run the check."
        else:
            message = (
                f"sessions.db unreadable — data-health checks skipped ({exc})"
            )
            fix = "Fix the store/schema problem reported above first."
        findings.append(Finding(CHECK_ID, Status.WARN, message, fix=fix))
        findings.append(_lock_finding(ctx))
        return findings

    with store:
        sessions = store.sessions()
        nodes = store.message_nodes()

    session_ids = {s.id for s in sessions}
    node_counts: dict[str, int] = {}
    for n in nodes:
        node_counts[n.session_id] = node_counts.get(n.session_id, 0) + 1
    empty = [s for s in sessions if node_counts.get(s.id, 0) == 0]
    orphans = [n for n in nodes if n.session_id not in session_ids]
    cutoff = ctx.now() - ctx.stale_days * _DAY_MS
    stale = [s for s in sessions if s.last_activity_at < cutoff]

    if not sessions:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                "sessions.db contains 0 sessions",
                fix="Fresh install or wiped store — run a Devin session and "
                "re-check.",
            )
        )
    elif empty:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(empty)} empty session(s) (no message_nodes)",
                fix="Usually abandoned sessions — harmless; a --fix cleanup is "
                "planned for M2.",
            )
        )
    else:
        findings.append(Finding(CHECK_ID, Status.PASS, "no empty sessions"))

    if orphans:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(orphans)} orphan message_nodes (session_id not in "
                "sessions)",
                fix="Leftover rows from deleted sessions — safe to ignore; "
                "cleanup planned for --fix (M2).",
            )
        )
    else:
        findings.append(
            Finding(CHECK_ID, Status.PASS, "no orphan message_nodes")
        )

    if stale:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(stale)} session(s) inactive for >{ctx.stale_days} days",
                fix="Consider exporting them with devin-history before "
                "pruning.",
            )
        )
    else:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                f"no sessions inactive for >{ctx.stale_days} days",
            )
        )

    findings.append(_lock_finding(ctx))
    return findings
