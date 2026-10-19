"""Fixtures — generated synthetically via devin_internals.fixtures plus
small file builders for the non-Devin surfaces.

No real Devin data is ever touched: every test database is produced by the
deterministic generator (DDL is real, rows are synthetic). An autouse
fixture points HOME (and every surface env var) at a disposable dir so the
dev box's real agent stores can never leak into a test run — the same
doctrine as devin-search's ``_no_store_autodiscovery``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from devin_doctor.model import Context
from devin_internals.fixtures import (
    _BASE_TS_MS,
    create_devin_data_dir,
)

BASE_MS = _BASE_TS_MS
BASE_ISO = "2026-05-28T20:26:40Z"  # == BASE_MS


@pytest.fixture(autouse=True)
def _hermetic_surfaces(tmp_path, monkeypatch):
    """No real agent store is ever auto-detected in a test.

    Surface roots resolve from HOME + env vars; repoint both at a scratch
    home so a machine with copilot/codex/VS Code installed stays invisible.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "COPILOT_DIR",
        "COPILOT_HOME",
        "CODEX_HOME",
        "CODEX_SESSIONS_DIR",
        "DEVIN_DATA_DIR",
        "DEVIN_CONFIG_DIR",
        "APPDATA",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture(autouse=True)
def _offline_updates(monkeypatch):
    # the updates check talks to the network and inspects real PATH installs;
    # pin it offline so full-scan tests stay hermetic on any machine.
    # tests for the check itself delete the variable.
    monkeypatch.setenv("DEVIN_DOCTOR_OFFLINE", "1")


@pytest.fixture(autouse=True)
def _no_scheduler_probes(monkeypatch):
    """The automation check probes crontab/systemctl/schtasks — keep every
    test hermetic by reporting each surface tool as missing. Tests for the
    check itself inject their own fake runner."""
    from devin_doctor.checks import automation

    monkeypatch.setattr(automation, "_run", lambda argv: None)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A healthy synthetic Devin data dir (schema v17, 3 sessions, 2 acp DBs)."""
    root = tmp_path / "devin"
    create_devin_data_dir(root)
    (root / "credentials.toml").write_text(
        '[auth]\ntoken = "fixture-token"\n', encoding="utf-8"
    )
    return root


@pytest.fixture
def ctx(data_dir: Path, tmp_path: Path) -> Context:
    """Context pointed at the healthy fixture; ``now_ms`` pinned just after
    the fixture's timestamps so nothing reads as stale by accident."""
    return Context(data_dir=data_dir, cwd=tmp_path, now_ms=_BASE_TS_MS + 10**9)


@pytest.fixture
def broken_data_dir(tmp_path: Path) -> Path:
    """Broken variant: a corrupt (non-SQLite) sessions.db — present but
    unreadable — and no acp-messages, state.vscdb or credentials.toml."""
    root = tmp_path / "devin-broken"
    db = root / "cli" / "sessions.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a sqlite database at all")
    return root


@pytest.fixture
def broken_ctx(broken_data_dir: Path, tmp_path: Path) -> Context:
    return Context(data_dir=broken_data_dir, cwd=tmp_path)


def lock_db_exclusively(path: Path) -> sqlite3.Connection:
    """Hold an EXCLUSIVE lock on ``path`` (rollback-journal mode) so readers
    hit SQLITE_BUSY. Caller must keep the returned connection open."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("BEGIN EXCLUSIVE")
    con.execute(
        "INSERT INTO app_state(key, value) VALUES ('fixture.lock', '1')"
    )
    return con


# --- copilot-cli ------------------------------------------------------------


def copilot_event(event_type, data, *, ts=BASE_ISO, event_id=None, idx=0):
    """One ``events.jsonl`` envelope (spec §6.3)."""
    return {
        "id": event_id or f"ev-{idx}",
        "parentId": None,
        "timestamp": ts,
        "type": event_type,
        "data": data,
    }


def add_copilot_session(
    copilot_root,
    session_id,
    events,
    *,
    layout="dir",
    workspace=None,
):
    """Write one copilot session under ``copilot_root``.

    ``layout`` covers all spec §6.3 shapes: ``"dir"`` —
    ``session-state/<id>/{events.jsonl,workspace.yaml}``, ``"flat"`` —
    ``session-state/<id>.jsonl``, ``"legacy-dir"``/``"legacy-flat"`` — the
    same under ``history-session-state/``. ``events`` items are dicts
    (dumped to JSON) or raw strings (malformed lines); ``workspace`` is a
    scalar dict written as flat ``workspace.yaml``.
    """
    state_dir = (
        "history-session-state" if layout.startswith("legacy") else "session-state"
    )
    base = Path(copilot_root) / state_dir
    base.mkdir(parents=True, exist_ok=True)
    if layout.endswith("flat"):
        path = base / f"{session_id}.jsonl"
    else:
        session_dir = base / session_id
        session_dir.mkdir(exist_ok=True)
        path = session_dir / "events.jsonl"
        if workspace:
            yaml_lines = "".join(
                f"{k}: {v}\n" for k, v in workspace.items()
            )
            (session_dir / "workspace.yaml").write_text(yaml_lines)
    lines = [e if isinstance(e, str) else json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n")
    return path


# --- codex-cli --------------------------------------------------------------


def codex_record(record_type, payload, *, ts=BASE_ISO):
    """One rollout JSONL record envelope."""
    return {"timestamp": ts, "type": record_type, "payload": payload}


def add_codex_home(codex_root, *, state_n=None, threads=(), rollouts=()):
    """Build a ``$CODEX_HOME``: optional ``state_<N>.sqlite`` plus rollout
    files. ``threads`` are dicts with ``id``/``created_at``/``updated_at``/
    ``cwd``/``has_user_event``; ``rollouts`` are ``(relpath, records)``
    pairs — records are dicts or raw strings."""
    root = Path(codex_root)
    root.mkdir(parents=True, exist_ok=True)
    if state_n is not None:
        db_path = root / f"state_{state_n}.sqlite"
        con = sqlite3.connect(db_path)
        with con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS threads("
                "  id TEXT PRIMARY KEY, created_at INTEGER,"
                "  updated_at INTEGER, cwd TEXT, has_user_event INTEGER)"
            )
            con.execute("DELETE FROM threads")
            for t in threads:
                con.execute(
                    "INSERT INTO threads(id, created_at, updated_at, cwd,"
                    " has_user_event) VALUES (?, ?, ?, ?, ?)",
                    (
                        t.get("id"),
                        t.get("created_at", BASE_MS),
                        t.get("updated_at", BASE_MS + 60_000),
                        t.get("cwd", "/fixture/workspace/codex"),
                        t.get("has_user_event", 1),
                    ),
                )
        con.close()
    for rel_path, records in rollouts:
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
        path.write_text("\n".join(lines) + "\n")
    return root


# --- vscode-chat ------------------------------------------------------------


def vscode_request(message_text, response_text, *, ts=BASE_MS):
    """One ``requests[]`` entry as stored in a chatSessions document."""
    return {
        "message": {"text": message_text},
        "response": [{"value": response_text}],
        "timestamp": ts,
        "responseTimestamp": ts + 5_000,
    }


def add_vscode_chat(
    user_dir,
    session_id,
    *,
    doc=None,
    mutations=(),
    ws_dir=None,
    global_dir=None,
):
    """Write ``<id>.json`` (``doc`` snapshot) and/or ``<id>.jsonl``
    (``mutations`` replay log) under a workspace or global storage dir."""
    if global_dir is not None:
        base = Path(user_dir) / "globalStorage" / global_dir
    else:
        base = (
            Path(user_dir)
            / "workspaceStorage"
            / (ws_dir or "wshash-0001")
            / "chatSessions"
        )
    base.mkdir(parents=True, exist_ok=True)
    written = None
    if doc is not None:
        written = base / f"{session_id}.json"
        written.write_text(json.dumps(doc))
    if mutations:
        written = base / f"{session_id}.jsonl"
        written.write_text(
            "\n".join(json.dumps(m) for m in mutations) + "\n"
        )
    return written
