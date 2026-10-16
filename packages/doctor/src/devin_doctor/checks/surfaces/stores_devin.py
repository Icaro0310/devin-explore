"""Surface check — devin-cli: ``cli/sessions.db`` (schema v15–v17+).

Re-home of the original ``stores`` + ``schema`` checks for the Devin CLI
store — the same detection logic (``detect_schema_version`` /
``SessionsStore`` from devin-internals-spec), now expressed as the shared
surface ladder: present → readable → schema → sane.

Absence is not failure: when the Devin data dir itself is missing the
surface is simply not detected and the check emits no findings.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from devin_internals import detect_schema_version
from devin_internals.parsers import SessionsStore
from devin_internals.schema import (
    LATEST_KNOWN_SCHEMA,
    MIN_SUPPORTED_SCHEMA,
    SchemaError,
    UnknownSchemaVersionError,
)

from devin_doctor.checks.surfaces._common import (
    connect_readonly,
    has_sqlite_magic,
    row_count,
    table_names,
)
from devin_doctor.model import Context, Finding, Status
from devin_doctor.paths import human_size, sessions_db_path

CHECK_ID = "devin-cli"

_UPDATE_FIX = (
    "Upgrade devin-doctor / devin-internals-spec — this store was written by "
    "a Devin version the spec does not know."
)


def _readable(path: Path) -> str | None:
    """``None`` when the store opens read-only and looks like SQLite —
    else the error string a FAIL finding reports."""
    if not has_sqlite_magic(path):
        return "not a SQLite database (bad magic)"
    try:
        con = connect_readonly(path)
    except sqlite3.Error as exc:
        return str(exc)
    try:
        table_names(con)
    except sqlite3.Error as exc:
        return str(exc)
    finally:
        con.close()
    return None


def _schema_finding(path: Path) -> Finding:
    try:
        info: dict[str, Any] = detect_schema_version(path)
    except UnknownSchemaVersionError as exc:
        return Finding(
            CHECK_ID,
            Status.WARN,
            f"sessions.db schema v{exc.version} — unknown (known range "
            f"{MIN_SUPPORTED_SCHEMA}..{LATEST_KNOWN_SCHEMA})",
            fix=_UPDATE_FIX,
        )
    except SchemaError as exc:
        return Finding(
            CHECK_ID,
            Status.FAIL,
            f"sessions.db schema detection failed: {exc}",
            fix="The file is missing the migration ledger — is it really a "
            "Devin sessions.db?",
        )
    except (sqlite3.Error, OSError) as exc:
        return Finding(
            CHECK_ID,
            Status.FAIL,
            f"sessions.db is not a readable database: {exc}",
            fix="File looks corrupt — restore from backup or reinstall Devin.",
        )
    version = info["schema_version"]
    if not info["supported"]:
        return Finding(
            CHECK_ID,
            Status.WARN,
            f"sessions.db schema v{version} — known but unsupported "
            f"(supported: {MIN_SUPPORTED_SCHEMA}..{LATEST_KNOWN_SCHEMA})",
            fix=_UPDATE_FIX,
        )
    suffix = "latest known" if info["verified"] else "supported"
    return Finding(
        CHECK_ID, Status.PASS, f"sessions.db schema v{version} ({suffix})"
    )


def _sane_finding(path: Path, supported: bool) -> Finding | None:
    """Non-empty store + cheap row counts. The spec store is preferred; a
    schema the spec refuses still gets raw COUNT(*)s — counts are metadata,
    and 'installed but unused' should not hide behind a version gate."""
    try:
        if supported:
            with SessionsStore(path) as store:
                counts = store.counts()
        else:
            con = connect_readonly(path)
            try:
                tables = table_names(con)
                counts = {
                    t: row_count(con, t)
                    for t in ("sessions", "message_nodes")
                    if t in tables
                }
            finally:
                con.close()
    except (SchemaError, sqlite3.Error, OSError):
        return None
    n_sessions = counts.get("sessions")
    if n_sessions is None:
        return None
    if n_sessions == 0:
        return Finding(
            CHECK_ID,
            Status.WARN,
            "sessions.db contains 0 sessions — installed but unused",
            fix="Fresh install or wiped store — run a Devin session and "
            "re-check.",
        )
    nodes = counts.get("message_nodes", 0)
    return Finding(
        CHECK_ID,
        Status.PASS,
        f"{n_sessions} sessions, {nodes} message_nodes",
    )


def run(ctx: Context) -> list[Finding]:
    if not ctx.data_dir.is_dir():
        return []  # not detected — an uninstalled surface is valid
    path = sessions_db_path(ctx.data_dir)
    if not path.is_file():
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                f"sessions.db missing ({path})",
                fix="The data dir exists but the CLI store was never created "
                "— the CLI may not have run here; safe to ignore for "
                "Desktop-only use.",
            )
        ]
    size = path.stat().st_size
    if size == 0:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                "sessions.db is empty (0 B) — installed but unused",
                fix="A zero-byte store is not corrupt — the CLI just never "
                "wrote to it.",
            )
        ]
    error = _readable(path)
    if error is not None:
        return [
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"sessions.db present ({human_size(size)}) but unreadable: "
                f"{error}",
                fix="Store-corrupt — restore from backup or reinstall Devin.",
            )
        ]
    schema = _schema_finding(path)
    findings = [
        Finding(
            CHECK_ID,
            Status.PASS,
            f"sessions.db present — {human_size(size)}",
        ),
        schema,
    ]
    sane = _sane_finding(path, supported=schema.status is Status.PASS)
    if sane is not None:
        findings.append(sane)
    return findings
