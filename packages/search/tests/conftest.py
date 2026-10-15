import json
import sqlite3
from pathlib import Path

import pytest
from devin_internals.fixtures import (
    create_acp_messages_db,
    create_sessions_db,
)

BASE_MS = 1_780_000_000_000
BASE_ISO = "2026-05-28T20:26:40Z"  # == BASE_MS


@pytest.fixture(autouse=True)
def _no_store_autodiscovery(monkeypatch):
    """Deterministic tests: never auto-detect this machine's real stores.

    The index command auto-detects sessions.db/acp/copilot/codex/
    vscode-chat locations when no flag is given; the dev box and CI
    runners may actually have some of them installed.
    """
    monkeypatch.setattr("devin_search.cli.default_sessions_db", lambda: None)
    monkeypatch.setattr("devin_search.cli.default_acp_dir", lambda: None)
    monkeypatch.setattr("devin_search.cli.default_copilot_dirs", list)
    monkeypatch.setattr("devin_search.cli.default_codex_dir", lambda: None)
    monkeypatch.setattr("devin_search.cli.default_vscode_chat_dirs", list)


@pytest.fixture
def db_path(tmp_path):
    return create_sessions_db(tmp_path / "sessions.db")


@pytest.fixture
def acp_dir(tmp_path):
    d = tmp_path / "acp-messages"
    d.mkdir()
    return d


def msg(role, text, **extra):
    return {"role": role, "text": text, **extra}


def add_session(
    db_path,
    session_id,
    *,
    title="Custom session",
    messages=(),
    tool_calls=(),
    prompts=(),
    hidden=0,
    working_directory="/fixture/workspace/custom",
    created_ms=BASE_MS,
    last_ms=None,
):
    """Insert one session row plus optional nodes/tool calls/prompts.

    ``messages`` are dicts serialized into ``chat_message``; ``tool_calls``
    are ``(tool_call_json, tool_call_update_json)`` dict pairs; ``prompts``
    are ``(content, is_shell)`` tuples.
    """
    last_ms = created_ms + 60_000 if last_ms is None else last_ms
    con = sqlite3.connect(db_path)
    with con:
        con.execute(
            "INSERT INTO sessions(id, working_directory, backend_type, model,"
            " agent_mode, created_at, last_activity_at, title, hidden)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                working_directory,
                "fixture-backend",
                "fixture-model",
                "fixture-mode",
                created_ms,
                last_ms,
                title,
                hidden,
            ),
        )
        for i, message in enumerate(messages, start=1):
            con.execute(
                "INSERT INTO message_nodes(session_id, node_id, parent_node_id,"
                " chat_message, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    session_id,
                    i,
                    None if i == 1 else i - 1,
                    json.dumps(message),
                    created_ms + i * 10_000,
                ),
            )
        for j, (call, update) in enumerate(tool_calls):
            con.execute(
                "INSERT INTO tool_call_state(session_id, tool_call_id,"
                " tool_call_json, tool_call_update_json) VALUES (?, ?, ?, ?)",
                (
                    session_id,
                    f"{session_id}-tc{j}",
                    json.dumps(call) if call is not None else None,
                    json.dumps(update) if update is not None else None,
                ),
            )
        for k, (content, is_shell) in enumerate(prompts):
            con.execute(
                "INSERT INTO prompt_history(content, timestamp, session_id,"
                " is_shell) VALUES (?, ?, ?, ?)",
                (content, created_ms + k * 5_000, session_id, is_shell),
            )
    con.close()


def add_acp_db(acp_dir, name, session_id, messages, *, created_ms=BASE_MS):
    """Create one ``acp-messages/<name>.db`` with ``(kind, payload)`` rows."""
    path = create_acp_messages_db(acp_dir / f"{name}.db")
    con = sqlite3.connect(path)
    with con:
        con.execute("DELETE FROM meta")
        con.execute("DELETE FROM messages")
        con.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [
                ("fixture.session_id", session_id),
                ("fixture.created_at", str(created_ms)),
            ],
        )
        con.executemany(
            "INSERT INTO messages(position, kind, payload) VALUES (?, ?, ?)",
            [
                (pos, kind, json.dumps(payload))
                for pos, (kind, payload) in enumerate(messages)
            ],
        )
    con.close()
    return path


# --- copilot-cli -----------------------------------------------------------


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
    newline_at_end=True,
):
    """Write one copilot session under ``copilot_root``.

    ``layout`` covers all spec §6.3 shapes: ``"dir"`` —
    ``session-state/<id>/{events.jsonl,workspace.yaml}``, ``"flat"`` —
    ``session-state/<id>.jsonl``, ``"legacy-dir"``/``"legacy-flat"`` —
    the same under ``history-session-state/``. ``events`` items are dicts
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
    path.write_text("\n".join(lines) + ("\n" if newline_at_end else ""))
    return path


# --- codex-cli -------------------------------------------------------------


def codex_record(record_type, payload, *, ts=BASE_ISO):
    """One rollout JSONL record envelope."""
    return {"timestamp": ts, "type": record_type, "payload": payload}


def add_codex_home(
    codex_root,
    *,
    state_n=None,
    threads=(),
    rollouts=(),
):
    """Build a ``$CODEX_HOME``: optional ``state_<N>.sqlite`` plus rollout
    files. ``threads`` are dicts with any of ``id``, ``created_at``,
    ``updated_at``, ``cwd``, ``has_user_event`` and the text fields
    ``title``/``preview``/``first_user_message``. ``rollouts`` are
    ``(relpath, records)`` pairs — records are dicts or raw strings."""
    root = Path(codex_root)
    root.mkdir(parents=True, exist_ok=True)
    if state_n is not None:
        db_path_ = root / ("state_" + str(state_n) + ".sqlite")
        con = sqlite3.connect(db_path_)
        with con:
            # Idempotent: re-calling rewrites the table, so tests can
            # mutate threads between index runs.
            con.execute(
                "CREATE TABLE IF NOT EXISTS threads("
                "  id TEXT PRIMARY KEY, created_at INTEGER,"
                "  updated_at INTEGER, cwd TEXT, has_user_event INTEGER,"
                "  title TEXT, preview TEXT, first_user_message TEXT)"
            )
            con.execute("DELETE FROM threads")
            for t in threads:
                con.execute(
                    "INSERT INTO threads(id, created_at, updated_at, cwd,"
                    " has_user_event, title, preview, first_user_message)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        t.get("id"),
                        t.get("created_at", BASE_MS),
                        t.get("updated_at", BASE_MS + 60_000),
                        t.get("cwd", "/fixture/workspace/codex"),
                        t.get("has_user_event", 1),
                        t.get("title"),
                        t.get("preview"),
                        t.get("first_user_message"),
                    ),
                )
        con.close()
    for rel_path, records in rollouts:
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
        path.write_text("\n".join(lines) + "\n")
    return root


# --- vscode-chat -----------------------------------------------------------


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
