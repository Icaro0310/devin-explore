"""Surface check — devin-cli: ``cli/sessions.db`` (schema v15–v17+).

Re-home of the original ``stores`` + ``schema`` checks for the Devin CLI
store — the same detection logic (``detect_schema_version`` /
``SessionsStore`` from devin-internals-spec), now expressed as the shared
surface ladder: present → readable → schema → sane (SURFACE-HEALTH §2).
Every finding carries its ``rung`` plus the §5 ``code`` on WARN/FAIL.

Absence is not failure: when the Devin data dir itself is missing the
surface emits a single ``present PASS / not-installed`` finding — the
report groups those under 'not detected'.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
from devin_doctor.paths import cli_anchor_present, human_size, sessions_db_path

CHECK_ID = "devin-cli"

_UPDATE_FIX = (
    "Upgrade devin-doctor / devin-internals-spec — this store was written by "
    "a Devin version the spec does not know."
)


def _readable_error(path: Path) -> str | None:
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


def _schema_finding(path: Path) -> tuple[Finding, bool]:
    """The ``schema`` rung finding plus whether the spec store can parse
    the db (drives the ``sane`` rung's count strategy)."""
    try:
        info = detect_schema_version(path)
    except UnknownSchemaVersionError as exc:
        return (
            Finding(
                CHECK_ID,
                Status.WARN,
                f"sessions.db schema v{exc.version} — unknown (known range "
                f"{MIN_SUPPORTED_SCHEMA}..{LATEST_KNOWN_SCHEMA})",
                fix=_UPDATE_FIX,
                rung="schema",
                code="schema-unknown",
            ),
            False,
        )
    except SchemaError as exc:
        # §4.1: a missing/empty ledger is schema-unknown, never a FAIL —
        # the file opened fine (readable passed) but carries no version
        # marker we can classify.
        return (
            Finding(
                CHECK_ID,
                Status.WARN,
                f"sessions.db has no readable schema version ({exc}) — "
                "schema-unknown",
                fix="The file is missing the migration ledger — is it "
                "really a Devin sessions.db?",
                rung="schema",
                code="schema-unknown",
            ),
            False,
        )
    except (sqlite3.Error, OSError) as exc:
        # readable passed (file opened + table list) yet schema evaluation
        # hit a sqlite error — corruption surfacing at this layer
        return (
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"sessions.db schema evaluation failed: {exc}",
                fix="Store-corrupt — restore from backup or reinstall "
                "Devin.",
                rung="schema",
                code="store-corrupt",
            ),
            False,
        )
    version = info["schema_version"]
    if not info["supported"]:
        if version < MIN_SUPPORTED_SCHEMA:
            return (
                Finding(
                    CHECK_ID,
                    Status.WARN,
                    f"sessions.db schema v{version} — below the supported "
                    f"floor ({MIN_SUPPORTED_SCHEMA}); migrations pending",
                    fix="Let Devin run its migrations once, then re-check.",
                    rung="schema",
                    code="schema-old",
                ),
                False,
            )
        return (
            Finding(
                CHECK_ID,
                Status.WARN,
                f"sessions.db schema v{version} — known but unsupported "
                f"(supported: {MIN_SUPPORTED_SCHEMA}..{LATEST_KNOWN_SCHEMA})",
                fix=_UPDATE_FIX,
                rung="schema",
                code="schema-unknown",
            ),
            False,
        )
    suffix = "latest known" if info["verified"] else "supported"
    return (
        Finding(
            CHECK_ID,
            Status.PASS,
            f"sessions.db schema v{version} ({suffix})",
            rung="schema",
        ),
        True,
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
            rung="sane",
            code="store-empty",
        )
    nodes = counts.get("message_nodes", 0)
    return Finding(
        CHECK_ID,
        Status.PASS,
        f"{n_sessions} sessions, {nodes} message_nodes",
        rung="sane",
    )


def run(ctx: Context) -> list[Finding]:
    if not ctx.data_dir.is_dir() or not cli_anchor_present(ctx.data_dir):
        # not installed — an uninstalled surface is valid (§3.1). The
        # bare data root is not CLI evidence: on case-insensitive
        # filesystems it unifies with the Desktop config root.
        return [
            Finding(
                CHECK_ID,
                Status.PASS,
                "no Devin data root — devin-cli not installed",
                rung="present",
                note="not-installed",
            )
        ]
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
                rung="present",
                code="store-missing",
            )
        ]
    size = path.stat().st_size
    findings = [
        Finding(
            CHECK_ID,
            Status.PASS,
            f"sessions.db present — {human_size(size)}",
            rung="present",
        )
    ]
    if size == 0:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                "sessions.db is empty (0 B) — installed but unused",
                fix="A zero-byte store is not corrupt — the CLI just never "
                "wrote to it.",
                rung="sane",
                code="empty-store",
            )
        )
        return findings
    error = _readable_error(path)
    if error is not None:
        findings.append(
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"sessions.db unreadable: {error}",
                fix="Store-corrupt — restore from backup or reinstall "
                "Devin.",
                rung="readable",
                code="store-corrupt",
            )
        )
        return findings
    findings.append(
        Finding(
            CHECK_ID,
            Status.PASS,
            "sessions.db opens read-only (SQLite format ok)",
            rung="readable",
        )
    )
    schema_finding, supported = _schema_finding(path)
    findings.append(schema_finding)
    if schema_finding.status is Status.FAIL:
        return findings  # ladder stops on FAIL
    sane = _sane_finding(path, supported=supported)
    if sane is not None:
        findings.append(sane)
    return findings
