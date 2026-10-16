"""Surface check — devin-chat: ``User/acp-messages/<uuid>.db`` +
``User/globalStorage/state.vscdb``.

The Devin Desktop surface: per-session ACP message stores plus the app's
VS Code-style UI state store (§8 "observed, not spec'd" — checked here as
part of the same product surface). The ledger-less stores gate on table
shape (``meta`` + ``messages`` / ``ItemTable``) via devin-internals-spec.

Ladder: present → readable → schema → sane. When the Devin install shows
no trace of the Desktop stores the surface is not detected (no findings).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from devin_internals.parsers import AcpMessagesStore, StateVscdbStore
from devin_internals.schema import SchemaError

from devin_doctor.checks.surfaces._common import (
    connect_readonly,
    has_sqlite_magic,
)
from devin_doctor.model import Context, Finding, Status
from devin_doctor.paths import human_size, locate_stores

CHECK_ID = "devin-chat"


def _sqlite_error(path: Path) -> str | None:
    """``None`` when ``path`` opens read-only as SQLite; else the error."""
    if not has_sqlite_magic(path):
        return "not a SQLite database (bad magic)"
    try:
        con = connect_readonly(path)
    except sqlite3.Error as exc:
        return str(exc)
    con.close()
    return None


def _acp_findings(acp_dir: Path, acp_dbs: tuple[Path, ...]) -> list[Finding]:
    if not acp_dir.is_dir():
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                f"acp-messages dir missing ({acp_dir})",
                fix="Devin Desktop has probably never run a session here — "
                "safe to ignore if you only use the CLI.",
            )
        ]
    if not acp_dbs:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                "acp-messages dir present, 0 db files — installed but "
                "unused",
            )
        ]
    total = sum(p.stat().st_size for p in acp_dbs)
    unreadable: list[str] = []
    bad_shape: list[str] = []
    unknown_version: list[str] = []
    messages = 0
    for p in acp_dbs:
        if p.stat().st_size == 0:
            continue  # empty file — counted by the sane rung below
        if _sqlite_error(p) is not None:
            unreadable.append(p.name)
            continue
        try:
            with AcpMessagesStore(p) as store:
                if not store.typed_meta().known_schema:
                    unknown_version.append(p.name)
                messages += store.counts()["messages"]
        except (SchemaError, sqlite3.Error, OSError):
            bad_shape.append(p.name)
    findings: list[Finding] = []
    if unreadable or bad_shape:
        bad = unreadable + bad_shape
        shown = ", ".join(bad[:3]) + (" …" if len(bad) > 3 else "")
        return [
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"acp-messages: {len(bad)}/{len(acp_dbs)} db(s) unreadable "
                f"or unrecognized layout ({shown})",
                fix="Store-corrupt — files lack the meta+messages tables; "
                "possibly written by a newer Devin or damaged.",
            )
        ]
    findings.append(
        Finding(
            CHECK_ID,
            Status.PASS,
            f"acp-messages: {len(acp_dbs)} db(s), {human_size(total)} total",
        )
    )
    if unknown_version:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"acp-messages: {len(unknown_version)} db(s) with unknown "
                "schema_version (known: 1, 6)",
                fix="Written by a newer Devin than the spec knows — upgrade "
                "devin-doctor / devin-internals-spec.",
            )
        )
    if messages == 0:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                "acp-messages: 0 message rows across all db files — "
                "installed but unused",
            )
        )
    else:
        findings.append(
            Finding(
                CHECK_ID, Status.PASS, f"acp-messages: {messages} message rows"
            )
        )
    return findings


def _state_vscdb_findings(path: Path) -> list[Finding]:
    if not path.is_file():
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                f"state.vscdb missing ({path})",
                fix="Desktop UI state store not found — Devin Desktop may "
                "never have launched here.",
            )
        ]
    size = path.stat().st_size
    if size == 0:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                "state.vscdb is empty (0 B) — installed but unused",
            )
        ]
    if _sqlite_error(path) is not None:
        return [
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"state.vscdb present ({human_size(size)}) but unreadable "
                "— bad SQLite magic or open failed",
                fix="Store-corrupt — the file does not look like a "
                "state.vscdb.",
            )
        ]
    try:
        with StateVscdbStore(path) as store:
            n_keys = store.counts()["ItemTable"]
            n_ws = len(store.list_prefix())
    except (SchemaError, OSError, sqlite3.Error) as exc:
        return [
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"state.vscdb has an unrecognized layout: {exc}",
                fix="Expected a VS Code-style ItemTable — file may be "
                "corrupt or from an unknown version.",
            )
        ]
    return [
        Finding(
            CHECK_ID,
            Status.PASS,
            f"state.vscdb present — {human_size(size)}, {n_keys} keys "
            f"({n_ws} windsurfSpace.*)",
        )
    ]


def run(ctx: Context) -> list[Finding]:
    stores = locate_stores(ctx.data_dir, config_dir=ctx.config_dir)
    detected = (
        ctx.data_dir.is_dir()
        or stores.acp_dir.is_dir()
        or stores.state_vscdb.is_file()
    )
    if not detected:
        return []  # not detected — the Desktop surface simply is not there
    return [
        *_acp_findings(stores.acp_dir, stores.acp_dbs),
        *_state_vscdb_findings(stores.state_vscdb),
    ]
