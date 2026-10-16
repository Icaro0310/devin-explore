"""Surface check — devin-chat: ``User/acp-messages/<uuid>.db`` +
``User/globalStorage/state.vscdb``.

The Devin Desktop surface: per-session ACP message stores plus the app's
VS Code-style UI state store (§8 "observed, not spec'd" — checked here as
part of the same product surface). The ledger-less ACP stores gate on
table shape (``meta`` + ``messages``) — SURFACE-HEALTH §4.2.

Ladder: present → readable → schema → sane. Findings carry their ``rung``
plus the §5 ``code``/``note`` fields. When the Devin install shows no
Desktop-specific anchor the surface reports ``present PASS /
not-installed`` — a CLI-only install is a valid environment, not a
failure.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from devin_internals.parsers import AcpMessagesStore, StateVscdbStore
from devin_internals.schema import SchemaError

from devin_doctor.checks.surfaces._common import (
    connect_readonly,
    has_sqlite_magic,
    row_count,
    table_names,
)
from devin_doctor.model import Context, Finding, Status
from devin_doctor.paths import (
    desktop_anchor_present,
    human_size,
    locate_stores,
)

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
    """The acp-messages store ladder (§4.2): container → file → shape →
    message counts."""
    if not acp_dir.is_dir() and not acp_dbs:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                f"acp-messages dir missing ({acp_dir})",
                fix="Devin Desktop has probably never run a session here — "
                "safe to ignore if you only use the CLI.",
                rung="present",
                code="store-missing",
            )
        ]
    findings = [
        Finding(
            CHECK_ID,
            Status.PASS,
            f"acp-messages dir present — {len(acp_dbs)} db file(s)",
            rung="present",
        )
    ]
    if not acp_dbs:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                "acp-messages dir present, 0 db files — installed but "
                "unused",
                rung="sane",
                code="store-empty",
            )
        )
        return findings

    empty: list[str] = []
    corrupt: list[str] = []
    healthy: list[Path] = []
    for p in acp_dbs:
        if p.stat().st_size == 0:
            empty.append(p.name)
            continue
        if _sqlite_error(p) is not None:
            corrupt.append(p.name)
            continue
        healthy.append(p)
    if corrupt:
        shown = ", ".join(corrupt[:3]) + (" …" if len(corrupt) > 3 else "")
        findings.append(
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"acp-messages: {len(corrupt)}/{len(acp_dbs)} db(s) "
                f"unreadable ({shown})",
                fix="Store-corrupt — bad SQLite magic or open failed; "
                "files were damaged.",
                rung="readable",
                code="store-corrupt",
            )
        )
        return findings
    if not healthy:
        # every .db is zero-byte — sane owns emptiness, not readable
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"acp-messages: {len(empty)} zero-byte db file(s) — "
                "empty-store",
                fix="A 0 B store was never written — safe to delete or "
                "ignore.",
                rung="sane",
                code="empty-store",
            )
        )
        return findings
    findings.append(
        Finding(
            CHECK_ID,
            Status.PASS,
            f"acp-messages: {len(healthy)} db(s) opened read-only",
            rung="readable",
        )
    )

    # schema rung — §4.2: the shape check is meta+messages tables present,
    # nothing more (no version marker is known-good here).
    unknown_shape: list[str] = []
    messages = 0
    countable = 0
    for p in healthy:
        try:
            con = connect_readonly(p)
        except sqlite3.Error:
            unknown_shape.append(p.name)
            continue
        try:
            tables = table_names(con)
            if AcpMessagesStore.REQUIRED_TABLES <= tables:
                messages += row_count(con, "messages")
                countable += 1
            else:
                unknown_shape.append(p.name)
        except sqlite3.Error:
            unknown_shape.append(p.name)
        finally:
            con.close()
    if unknown_shape:
        shown = ", ".join(unknown_shape[:3]) + (
            " …" if len(unknown_shape) > 3 else ""
        )
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"acp-messages: {len(unknown_shape)} db(s) without the "
                f"meta+messages shape ({shown})",
                fix="Expected meta + messages tables — written by a newer "
                "Devin than the spec knows.",
                rung="schema",
                code="schema-unknown",
            )
        )
    else:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                "acp-messages: meta+messages shape on every db",
                rung="schema",
            )
        )

    # sane rung — one verdict: a zero-byte store WARNs empty-store, all
    # zero-row messages stores WARN store-empty, otherwise PASS counts.
    if empty:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"acp-messages: {len(empty)} zero-byte db file(s) — "
                "empty-store",
                fix="A 0 B store was never written — safe to delete or "
                "ignore.",
                rung="sane",
                code="empty-store",
            )
        )
    elif countable and messages == 0:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                "acp-messages: 0 message rows across all db files — "
                "installed but unused",
                rung="sane",
                code="store-empty",
            )
        )
    elif countable:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                f"acp-messages: {messages} message rows",
                rung="sane",
            )
        )
    else:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                f"acp-messages: {len(healthy)} db(s) — no messages table "
                "to count",
                rung="sane",
            )
        )
    return findings


def _state_vscdb_findings(path: Path) -> list[Finding]:
    """The companion UI store — evaluated only when it exists (a rung
    with nothing to evaluate emits nothing)."""
    if not path.is_file():
        return []
    size = path.stat().st_size
    if size == 0:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                "state.vscdb is empty (0 B) — installed but unused",
                rung="sane",
                code="empty-store",
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
                rung="readable",
                code="store-corrupt",
            )
        ]
    try:
        with StateVscdbStore(path) as store:
            n_keys = store.counts()["ItemTable"]
            n_ws = len(store.list_prefix())
    except (SchemaError, OSError, sqlite3.Error):
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                "state.vscdb has an unrecognized layout (no ItemTable)",
                fix="Expected a VS Code-style ItemTable — written by a "
                "version the spec does not know.",
                rung="schema",
                code="schema-unknown",
            )
        ]
    return [
        Finding(
            CHECK_ID,
            Status.PASS,
            f"state.vscdb present — {human_size(size)}, {n_keys} keys "
            f"({n_ws} windsurfSpace.*)",
            rung="sane",
        )
    ]


def run(ctx: Context) -> list[Finding]:
    stores = locate_stores(ctx.data_dir, config_dir=ctx.config_dir)
    # The shared Devin data root is NOT evidence of Desktop — CLI and
    # Desktop can share it. Detection needs a Desktop-specific anchor
    # (see paths.desktop_anchor_present): the acp-messages dir/dbs,
    # state.vscdb, or the ``User`` UI profile dir itself.
    if not desktop_anchor_present(stores):
        return [
            Finding(
                CHECK_ID,
                Status.PASS,
                "no Desktop anchors — Devin Desktop not installed",
                rung="present",
                note="not-installed",
            )
        ]
    return [
        *_acp_findings(stores.acp_dir, stores.acp_dbs),
        *_state_vscdb_findings(stores.state_vscdb),
    ]
