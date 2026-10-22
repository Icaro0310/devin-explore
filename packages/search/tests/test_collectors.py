"""Collectors for the non-Devin surfaces — copilot-cli, codex-cli,
vscode-chat (SESSION-SURFACES.md §6.3–§6.5). All fixtures are synthetic
and deterministic."""

import json
import sqlite3

from conftest import (
    BASE_ISO,
    BASE_MS,
    add_codex_home,
    add_copilot_session,
    add_vscode_chat,
    codex_record,
    copilot_event,
    vscode_request,
)
from devin_search.index import build_index, index_doc_count
from devin_search.query import search


def _docs(index_path, source):
    con = sqlite3.connect(index_path)
    try:
        return con.execute(
            "SELECT session_id, role, ts, project, session_title, ref, text"
            " FROM docs WHERE source = ? ORDER BY rowid",
            (source,),
        ).fetchall()
    finally:
        con.close()


# --- copilot-cli -----------------------------------------------------------


def test_copilot_dir_layout_indexes_message_events(tmp_path):
    root = tmp_path / "copilot"
    events_file = add_copilot_session(
        root,
        "sess-aaa",
        [
            copilot_event("session.start", {"version": "0.1"}, idx=0),
            copilot_event(
                "user.message",
                {"content": "copilot zebra sentinel"},
                ts=BASE_ISO,
                idx=1,
            ),
            copilot_event(
                "assistant.message",
                {"content": "copilot ack the sentinel"},
                ts=BASE_ISO,
                idx=2,
            ),
            copilot_event(
                "tool.execution_start", {"name": "bash", "args": "x"}, idx=3
            ),
        ],
        workspace={"git_root": "/fixture/workspace/copilot", "cwd": "/tmp/x"},
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, copilot_dirs=[root])

    assert "copilot-cli" in stats.sources
    assert stats.copilot_messages == 2
    rows = _docs(idx, "copilot-cli")
    assert len(rows) == 2
    sid, role, ts, project, title, ref, text = rows[0]
    assert sid == "sess-aaa"
    assert role == "user"
    assert ts == BASE_MS
    assert project == "/fixture/workspace/copilot"
    assert title is None
    assert ref == f"copilot-cli:{events_file}:1"
    assert "zebra sentinel" in text
    assert rows[1][1] == "assistant"

    hits = search(idx, "zebra")
    assert len(hits) == 1 and hits[0].source == "copilot-cli"


def test_copilot_flat_and_legacy_layouts(tmp_path):
    root = tmp_path / "copilot"
    add_copilot_session(
        root,
        "flat-1",
        [copilot_event("user.message", {"text": "flat layout marker"})],
        layout="flat",
    )
    add_copilot_session(
        root,
        "legacy-1",
        [copilot_event("user.message", {"content": "legacy dir marker"})],
        layout="legacy-dir",
    )
    add_copilot_session(
        root,
        "legacy-flat",
        [copilot_event("user.message", {"content": "legacy flat marker"})],
        layout="legacy-flat",
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, copilot_dirs=[root])

    assert stats.copilot_messages == 3
    sids = {r[0] for r in _docs(idx, "copilot-cli")}
    assert sids == {"flat-1", "legacy-1", "legacy-flat"}


def test_copilot_merges_roots_and_dedups_same_session(tmp_path):
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    ws = {"git_root": "/fixture/workspace/shared"}
    add_copilot_session(
        root_a, "dup-1", [copilot_event("user.message", {"content": "first copy"})],
        workspace=ws,
    )
    add_copilot_session(
        root_b, "dup-1", [copilot_event("user.message", {"content": "second copy"})],
        workspace=ws,
    )
    add_copilot_session(
        root_b, "unique-1", [copilot_event("user.message", {"content": "b only"})],
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, copilot_dirs=[root_a, root_b])

    assert stats.copilot_messages == 2
    texts = [r[6] for r in _docs(idx, "copilot-cli")]
    assert "first copy" in texts and "second copy" not in texts
    assert "b only" in texts


def test_copilot_incremental_and_file_removal(tmp_path):
    root = tmp_path / "copilot"
    keep = add_copilot_session(
        root, "keep", [copilot_event("user.message", {"content": "stay"})]
    )
    gone = add_copilot_session(
        root, "gone", [copilot_event("user.message", {"content": "depart"})]
    )
    idx = tmp_path / "search.db"
    build_index(idx, copilot_dirs=[root])
    assert index_doc_count(idx) == 2

    second = build_index(idx, copilot_dirs=[root])
    assert second.indexed == 0  # watermarks hold — nothing re-indexed

    # Append-only file: new lines picked up on their own.
    with keep.open("a") as fh:
        fh.write(json.dumps(copilot_event("user.message", {"content": "more"})) + "\n")
    third = build_index(idx, copilot_dirs=[root])
    assert third.copilot_messages == 1

    gone.unlink()
    fourth = build_index(idx, copilot_dirs=[root])
    assert fourth.removed == 1
    texts = "\n".join(r[6] for r in _docs(idx, "copilot-cli"))
    assert "stay" in texts and "more" in texts and "depart" not in texts


def test_copilot_malformed_lines_never_crash(tmp_path):
    root = tmp_path / "copilot"
    add_copilot_session(
        root,
        "messy",
        [
            copilot_event("user.message", {"content": "before"}),
            "{not json at all",
            42,  # valid JSON, wrong envelope → counted, skipped
            copilot_event("user.message", {"content": "after"}),
        ],
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, copilot_dirs=[root])
    assert stats.copilot_messages == 2
    assert stats.skipped_records == 2
    texts = [r[6] for r in _docs(idx, "copilot-cli")]
    assert texts == ["before", "after"]


def test_copilot_truncated_tail_is_skipped(tmp_path):
    root = tmp_path / "copilot"
    add_copilot_session(
        root,
        "trunc",
        [
            copilot_event("user.message", {"content": "complete line"}),
            '{"type": "user.message", "data": {"cont',  # cut mid-line
        ],
        newline_at_end=False,
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, copilot_dirs=[root])
    assert stats.copilot_messages == 1
    assert stats.skipped_records == 1


def test_copilot_completed_tail_is_indexed_next_run(tmp_path):
    """Regression: a truncated tail must NOT be covered by the watermark.

    The writer completing the last line keeps the line count identical,
    so a ``len(lines)`` watermark would miss it forever — the watermark
    only covers well-formed records.
    """
    root = tmp_path / "copilot"
    events = add_copilot_session(
        root,
        "tail",
        [
            copilot_event("user.message", {"content": "first complete"}),
            '{"type": "user.message", "data": {"con',  # truncated tail
        ],
        newline_at_end=False,
    )
    idx = tmp_path / "search.db"
    first = build_index(idx, copilot_dirs=[root])
    assert first.copilot_messages == 1
    assert first.skipped_records == 1

    events.write_text(
        "\n".join(
            [
                json.dumps(
                    copilot_event("user.message", {"content": "first complete"})
                ),
                json.dumps(
                    copilot_event("user.message", {"content": "tail now whole"})
                ),
            ]
        )
        + "\n"
    )
    second = build_index(idx, copilot_dirs=[root])
    assert second.copilot_messages == 1
    texts = [r[6] for r in _docs(idx, "copilot-cli")]
    assert texts == ["first complete", "tail now whole"]


def test_copilot_late_winner_root_drops_lower_copy(tmp_path):
    """Regression: a higher-priority root gaining an already-indexed
    session must evict the losing copy's docs — and if the winner later
    disappears the loser re-indexes from scratch (its watermark was
    reset while it was the duplicate)."""
    root_hi, root_lo = tmp_path / "hi", tmp_path / "lo"
    ws = {"git_root": "/fixture/workspace/shared"}
    add_copilot_session(
        root_lo,
        "dup",
        [copilot_event("user.message", {"content": "low copy"})],
        workspace=ws,
    )
    idx = tmp_path / "search.db"
    build_index(idx, copilot_dirs=[root_hi, root_lo])
    assert [r[6] for r in _docs(idx, "copilot-cli")] == ["low copy"]

    add_copilot_session(
        root_hi,
        "dup",
        [copilot_event("user.message", {"content": "high copy"})],
        workspace=ws,
    )
    stats = build_index(idx, copilot_dirs=[root_hi, root_lo])
    assert stats.copilot_messages == 1
    assert stats.removed == 1
    assert [r[6] for r in _docs(idx, "copilot-cli")] == ["high copy"]

    (root_hi / "session-state" / "dup" / "events.jsonl").unlink()
    build_index(idx, copilot_dirs=[root_hi, root_lo])
    assert [r[6] for r in _docs(idx, "copilot-cli")] == ["low copy"]


def test_copilot_user_named_title_is_kept(tmp_path):
    root = tmp_path / "copilot"
    add_copilot_session(
        root,
        "named",
        [copilot_event("user.message", {"content": "x"})],
        workspace={
            "git_root": "/fixture/ws",
            "user_named": "true",
            "name": "My named session",
        },
    )
    idx = tmp_path / "search.db"
    build_index(idx, copilot_dirs=[root])
    assert _docs(idx, "copilot-cli")[0][4] == "My named session"


def test_copilot_numeric_and_second_timestamps(tmp_path):
    root = tmp_path / "copilot"
    add_copilot_session(
        root,
        "ts",
        [
            copilot_event("user.message", {"content": "ms"}, ts=BASE_MS),
            copilot_event("user.message", {"content": "sec"}, ts=BASE_MS // 1000),
        ],
    )
    idx = tmp_path / "search.db"
    build_index(idx, copilot_dirs=[root])
    assert [r[2] for r in _docs(idx, "copilot-cli")] == [BASE_MS, BASE_MS]


# --- codex-cli -------------------------------------------------------------


def _rollout_records(session_id="codex-1", cwd="/fixture/workspace/codex"):
    return [
        codex_record(
            "session_meta",
            {"id": session_id, "timestamp": BASE_ISO, "cwd": cwd},
        ),
        codex_record(
            "event_msg", {"type": "task_started"}, ts=BASE_ISO
        ),
        codex_record(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "codex zebra ask"}],
            },
        ),
        codex_record(
            "response_item",
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "codex zebra reply"}],
            },
        ),
        codex_record(
            "event_msg",
            {"type": "agent_message", "message": "codex done note"},
        ),
        codex_record("event_msg", {"type": "task_complete"}),
    ]


def test_codex_state_db_indexes_thread_text_fields(tmp_path):
    root = add_codex_home(
        tmp_path / "codex",
        state_n=5,
        threads=[
            {
                "id": "thread-1",
                "created_at": BASE_MS // 1000,  # seconds in the wild
                "updated_at": BASE_MS // 1000 + 60,
                "cwd": "/fixture/workspace/codex",
                "title": "auto derived title",
                "first_user_message": "codex state sentinel",
            },
            {"id": "thread-empty", "cwd": "/x"},  # no text fields → no docs
        ],
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, codex_dir=root)

    assert "codex-cli" in stats.sources
    rows = _docs(idx, "codex-cli")
    assert len(rows) == 2  # title + first_user_message
    by_ref = {r[5]: r for r in rows}
    key = str(root / "state_5.sqlite")
    assert f"codex-cli:{key}:thread-1/first_user_message" in by_ref
    row = by_ref[f"codex-cli:{key}:thread-1/first_user_message"]
    assert row[0] == "thread-1"
    assert row[1] == "user"
    assert row[2] == BASE_MS  # seconds normalized to ms
    assert row[3] == "/fixture/workspace/codex"
    assert row[4] is None
    hits = search(idx, "sentinel")
    assert len(hits) == 1 and hits[0].source == "codex-cli"


def test_codex_newest_state_db_wins(tmp_path):
    root = add_codex_home(
        tmp_path / "codex",
        state_n=3,
        threads=[{"id": "old", "first_user_message": "stale text"}],
    )
    add_codex_home(
        root,
        state_n=9,
        threads=[{"id": "new", "first_user_message": "fresh text"}],
    )
    idx = tmp_path / "search.db"
    build_index(idx, codex_dir=root)

    texts = [r[6] for r in _docs(idx, "codex-cli")]
    assert "fresh text" in texts and "stale text" not in texts


def test_codex_rollout_messages_indexed(tmp_path):
    root = add_codex_home(
        tmp_path / "codex",
        rollouts=[("sessions/2026/05/rollout-1-codex-1.jsonl", _rollout_records())],
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, codex_dir=root)

    rows = _docs(idx, "codex-cli")
    assert stats.codex_messages == len(rows) == 3
    assert all(r[0] == "codex-1" for r in rows)
    assert all(r[3] == "/fixture/workspace/codex" for r in rows)
    roles = [r[1] for r in rows]
    assert roles == ["user", "assistant", "assistant"]
    assert all(r[5].startswith("codex-cli:") for r in rows)
    assert search(idx, "zebra")[0].session_id == "codex-1"


def test_codex_forked_rollout_ignored(tmp_path):
    root = add_codex_home(
        tmp_path / "codex",
        rollouts=[
            (
                "sessions/f/rollout-2-fork.jsonl",
                [
                    codex_record(
                        "session_meta",
                        {
                            "id": "fork-1",
                            "forked_from_id": "parent-1",
                            "cwd": "/fixture/ws",
                        },
                    ),
                    codex_record(
                        "event_msg",
                        {"type": "user_message", "message": "fork text"},
                    ),
                ],
            )
        ],
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, codex_dir=root)
    assert stats.codex_messages == 0
    assert index_doc_count(idx) == 0


def test_codex_open_session_still_indexed(tmp_path):
    root = add_codex_home(
        tmp_path / "codex",
        rollouts=[
            (
                "sessions/o/rollout-3-open.jsonl",
                [
                    codex_record(
                        "session_meta",
                        {"id": "open-1", "timestamp": BASE_ISO, "cwd": "/w"},
                    ),
                    codex_record(
                        "event_msg",
                        {"type": "user_message", "message": "open session text"},
                    ),
                    codex_record("event_msg", {"type": "task_started"}),
                ],
            )
        ],
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, codex_dir=root)
    assert stats.codex_messages == 1
    assert search(idx, "open session")[0].source == "codex-cli"


def test_codex_incremental_and_removal(tmp_path):
    root = tmp_path / "codex"
    rollout = add_codex_home(
        root,
        rollouts=[
            (
                "sessions/r/rollout-4-inc.jsonl",
                [
                    codex_record(
                        "session_meta",
                        {"id": "inc-1", "timestamp": BASE_ISO, "cwd": "/w"},
                    ),
                    codex_record(
                        "event_msg",
                        {"type": "user_message", "message": "first chunk"},
                    ),
                ],
            )
        ],
    ) / "sessions/r/rollout-4-inc.jsonl"
    idx = tmp_path / "search.db"
    build_index(idx, codex_dir=root)
    second = build_index(idx, codex_dir=root)
    assert second.indexed == 0

    with rollout.open("a") as fh:
        fh.write(
            json.dumps(
                codex_record(
                    "event_msg",
                    {"type": "agent_message", "message": "second chunk"},
                )
            )
            + "\n"
        )
    third = build_index(idx, codex_dir=root)
    assert third.codex_messages == 1

    rollout.unlink()
    fourth = build_index(idx, codex_dir=root)
    assert fourth.removed == 2
    assert index_doc_count(idx) == 0


def test_codex_thread_edit_and_delete_refresh(tmp_path):
    """Regression: threads rows are mutable — ref-dedup alone would pin
    stale text forever; a deleted thread must lose its docs."""
    root = add_codex_home(
        tmp_path / "codex",
        state_n=2,
        threads=[
            {"id": "t1", "title": "title alpha"},
            {"id": "t2", "title": "title static"},
        ],
    )
    idx = tmp_path / "search.db"
    build_index(idx, codex_dir=root)
    assert index_doc_count(idx) == 2
    assert build_index(idx, codex_dir=root).indexed == 0  # unchanged

    add_codex_home(  # rewrite: t1's title edited, t2 deleted upstream
        root,
        state_n=2,
        threads=[{"id": "t1", "title": "title bravo"}],
    )
    third = build_index(idx, codex_dir=root)
    assert third.codex_messages == 1
    assert third.removed == 2  # stale t1 doc replaced + t2 doc dropped
    assert [r[6] for r in _docs(idx, "codex-cli")] == ["title bravo"]


def test_codex_completed_rollout_tail_is_indexed(tmp_path):
    """Regression: rollout watermarks must not cover a truncated tail —
    completing the line leaves the line count unchanged."""
    rel = "sessions/t/rollout-9-tail.jsonl"
    good = codex_record(
        "event_msg", {"type": "user_message", "message": "rollout head"}
    )
    root = add_codex_home(
        tmp_path / "codex",
        rollouts=[(rel, [good, '{"type": "event_msg", "pay'])],
    )
    idx = tmp_path / "search.db"
    first = build_index(idx, codex_dir=root)
    assert first.codex_messages == 1
    assert first.skipped_records == 1

    (root / rel).write_text(
        json.dumps(good)
        + "\n"
        + json.dumps(
            codex_record(
                "event_msg",
                {"type": "agent_message", "message": "tail landed"},
            )
        )
        + "\n"
    )
    second = build_index(idx, codex_dir=root)
    assert second.codex_messages == 1
    texts = [r[6] for r in _docs(idx, "codex-cli")]
    assert texts == ["rollout head", "tail landed"]


def test_codex_exec_record_indexes_command_and_output(tmp_path):
    """Regression: exec payloads carry command AND output — first-key
    wins would silently drop whichever comes second."""
    root = add_codex_home(
        tmp_path / "codex",
        rollouts=[
            (
                "sessions/e/rollout-8-exec.jsonl",
                [
                    codex_record(
                        "response_item",
                        {
                            "type": "local_shell_call",
                            "command": ["kubectl", "get", "zebracmd"],
                            "output": "zebraout rows",
                        },
                    ),
                ],
            )
        ],
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, codex_dir=root)
    assert stats.codex_messages == 1
    (text,) = [r[6] for r in _docs(idx, "codex-cli")]
    assert text.index("zebracmd") < text.index("zebraout")  # command first
    assert search(idx, "zebracmd")
    assert search(idx, "zebraout")


def test_codex_malformed_midfile_line_skipped(tmp_path):
    root = add_codex_home(
        tmp_path / "codex",
        rollouts=[
            (
                "sessions/m/rollout-5-m.jsonl",
                [
                    codex_record(
                        "session_meta",
                        {"id": "m-1", "timestamp": BASE_ISO, "cwd": "/w"},
                    ),
                    "{broken json",
                    codex_record(
                        "event_msg",
                        {"type": "user_message", "message": "survivor"},
                    ),
                ],
            )
        ],
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, codex_dir=root)
    assert stats.codex_messages == 1
    assert stats.skipped_records == 1
    assert search(idx, "survivor")


# --- vscode-chat -----------------------------------------------------------


def test_vscode_json_snapshot_indexed(tmp_path):
    user_dir = tmp_path / "Code" / "User"
    doc = {
        "version": 3,
        "sessionId": "vs-1",
        "creationDate": BASE_MS,
        "lastMessageDate": BASE_MS + 5_000,
        "requests": [
            vscode_request("vscode zebra question", "vscode zebra answer")
        ],
    }
    path = add_vscode_chat(user_dir, "vs-1", doc=doc, ws_dir="a1b2c3hash")
    idx = tmp_path / "search.db"
    stats = build_index(idx, vscode_chat_dirs=[user_dir])

    assert "vscode-chat" in stats.sources
    assert stats.vscode_chat_messages == 2
    rows = _docs(idx, "vscode-chat")
    req, resp = rows
    assert req[0] == "vs-1" and req[1] == "user" and req[2] == BASE_MS
    assert req[3] == "a1b2c3hash"  # workspace dir name, verbatim
    assert req[5] == f"vscode-chat:{path}:0:req"
    assert resp[1] == "assistant" and resp[5].endswith(":0:resp")
    assert search(idx, "zebra")[0].source == "vscode-chat"


def test_vscode_jsonl_replay_and_jsonl_wins(tmp_path):
    user_dir = tmp_path / "Code" / "User"
    stale_doc = {
        "version": 3,
        "requests": [vscode_request("stale snapshot text", "stale resp")],
    }
    mutations = [
        {"kind": 0, "v": {"version": 3, "requests": []}},
        {
            "kind": 2,
            "k": ["requests"],
            "v": vscode_request("fresh replayed ask", "fresh replayed resp"),
        },
        {"kind": 1, "k": ["customTitle"], "v": "Renamed chat"},
        {"kind": 99, "k": ["x"], "v": 1},  # unknown kind — skipped
    ]
    add_vscode_chat(
        user_dir, "vs-2", doc=stale_doc, mutations=mutations, ws_dir="hash9"
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, vscode_chat_dirs=[user_dir])

    texts = [r[6] for r in _docs(idx, "vscode-chat")]
    assert stats.vscode_chat_messages == 2
    assert "fresh replayed ask" in texts
    assert all("stale snapshot" not in t for t in texts)


def test_vscode_globalstorage_has_empty_project(tmp_path):
    user_dir = tmp_path / "Code" / "User"
    add_vscode_chat(
        user_dir,
        "g-1",
        doc={"version": 3, "requests": [vscode_request("global ask", "r")]},
        global_dir="emptyWindowChatSessions",
    )
    idx = tmp_path / "search.db"
    build_index(idx, vscode_chat_dirs=[user_dir])
    row = _docs(idx, "vscode-chat")[0]
    assert row[0] == "g-1"
    assert row[3] == ""  # no workspace identity — legitimately hashless


def test_vscode_same_size_edit_reindexed(tmp_path):
    """Regression: the file-size watermark misses equal-size rewrites —
    the content hash catches them."""
    user_dir = tmp_path / "Code" / "User"
    path = add_vscode_chat(
        user_dir,
        "vs-eq",
        doc={
            "version": 3,
            "requests": [vscode_request("alpha ask", "alpha resp")],
        },
        ws_dir="h",
    )
    idx = tmp_path / "search.db"
    build_index(idx, vscode_chat_dirs=[user_dir])

    # Identical length ("alpha"→"bravo") — size alone cannot see this.
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "requests": [vscode_request("bravo ask", "bravo resp")],
            }
        )
    )
    stats = build_index(idx, vscode_chat_dirs=[user_dir])
    assert stats.vscode_chat_messages == 2
    texts = [r[6] for r in _docs(idx, "vscode-chat")]
    assert "bravo ask" in texts and all("alpha ask" not in t for t in texts)


def test_vscode_request_metadata_is_not_indexed(tmp_path):
    """Regression: request objects carry agent/extension metadata —
    only the documented content fields (text/value/content/parts) are
    searchable."""
    request = {
        "requestId": "reqid-noise",
        "message": {
            "text": "real ask text",
            "parts": [
                {
                    "range": {"start": 0, "endExclusive": 5},
                    "agent": {
                        "extensionId": {"value": "acme.agentext"},
                        "description": "agentmetanoise",
                    },
                }
            ],
        },
        "response": [
            {"kind": "mcpServersStarting", "didStartServerIds": ["srvnoise"]},
            {"kind": "markdownContent", "content": {"value": "real reply"}},
        ],
        "timestamp": BASE_MS,
    }
    user_dir = tmp_path / "Code" / "User"
    add_vscode_chat(
        user_dir,
        "vs-meta",
        doc={"version": 3, "requests": [request]},
        ws_dir="h",
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, vscode_chat_dirs=[user_dir])
    assert stats.vscode_chat_messages == 2
    texts = "\n".join(r[6] for r in _docs(idx, "vscode-chat"))
    assert "real ask text" in texts and "real reply" in texts
    assert not search(idx, "agentmetanoise")
    assert not search(idx, "agentext")
    assert not search(idx, "srvnoise")
    assert not search(idx, "reqid")


def test_vscode_incremental_unchanged_and_rewrite(tmp_path):
    user_dir = tmp_path / "Code" / "User"
    path = add_vscode_chat(
        user_dir,
        "vs-3",
        doc={"version": 3, "requests": [vscode_request("v1 text", "v1 r")]},
        ws_dir="h",
    )
    idx = tmp_path / "search.db"
    build_index(idx, vscode_chat_dirs=[user_dir])
    second = build_index(idx, vscode_chat_dirs=[user_dir])
    assert second.indexed == 0

    # Rewrite the snapshot — size change triggers a full file re-index.
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "requests": [vscode_request("v2 replacement text", "v2 r")],
            }
        )
    )
    third = build_index(idx, vscode_chat_dirs=[user_dir])
    assert third.vscode_chat_messages == 2
    texts = [r[6] for r in _docs(idx, "vscode-chat")]
    assert "v2 replacement text" in texts
    assert all("v1 text" not in t for t in texts)


def test_vscode_file_removal_and_unrecognized(tmp_path):
    user_dir = tmp_path / "Code" / "User"
    gone = add_vscode_chat(
        user_dir,
        "vs-gone",
        doc={"version": 3, "requests": [vscode_request("depart", "r")]},
        ws_dir="h",
    )
    # Not a chat session doc — no known keys.
    (user_dir / "workspaceStorage" / "h" / "chatSessions" / "noise.json").write_text(
        json.dumps({"unrelated": True})
    )
    idx = tmp_path / "search.db"
    build_index(idx, vscode_chat_dirs=[user_dir])
    assert index_doc_count(idx) == 2

    gone.unlink()
    stats = build_index(idx, vscode_chat_dirs=[user_dir])
    assert stats.removed == 2
    assert index_doc_count(idx) == 0


def test_vscode_server_remote_dir(tmp_path):
    server_dir = tmp_path / ".vscode-server" / "data" / "User"
    add_vscode_chat(
        server_dir,
        "remote-1",
        doc={"version": 3, "requests": [vscode_request("remote text", "r")]},
        ws_dir="remotehash",
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, vscode_chat_dirs=[server_dir])
    assert stats.vscode_chat_messages == 2

def test_codex_deep_rollout_line_skipped(tmp_path):
    """Same RecursionError recovery for codex rollout JSONL
    (devin-review #83)."""
    root = add_codex_home(
        tmp_path / "codex",
        rollouts=[
            (
                "sessions/2026/05/rollout-deep.jsonl",
                [
                    codex_record(
                        "session_meta",
                        {"id": "codex-deep", "timestamp": BASE_ISO, "cwd": "/w"},
                    ),
                    _DEEP_JSON_LINE,
                    codex_record(
                        "response_item",
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "codex deepok ask",
                                }
                            ],
                        },
                    ),
                ],
            )
        ],
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, codex_dir=root)

    assert stats.skipped_records >= 1
    assert search(idx, "deepok")


def test_codex_thread_metadata_change_reindexes(tmp_path):
    """A cwd/created_at change with unchanged text must still update the
    indexed doc — filters read the stored project/ts (devin-review #83)."""
    root = tmp_path / "codex"
    add_codex_home(
        root,
        state_n=1,
        threads=[
            {
                "id": "t-meta",
                "created_at": BASE_MS,
                "cwd": "/old",
                "title": "codex metachange",
            }
        ],
    )
    idx = tmp_path / "search.db"
    build_index(idx, codex_dir=root)
    rows = _docs(idx, "codex-cli")
    assert rows and all(r[3] == "/old" for r in rows)

    add_codex_home(
        root,
        state_n=1,
        threads=[
            {
                "id": "t-meta",
                "created_at": BASE_MS,
                "cwd": "/new",
                "title": "codex metachange",
            }
        ],
    )
    stats = build_index(idx, codex_dir=root)
    rows = _docs(idx, "codex-cli")
    assert stats.removed >= 1
    assert rows and all(r[3] == "/new" for r in rows)


def test_copilot_deep_json_line_skipped(tmp_path):
    """A JSONL record too deep for json.loads raises RecursionError — it
    must skip, not abort the run (devin-review #83)."""
    root = tmp_path / "copilot"
    add_copilot_session(
        root,
        "cp-deepjson",
        [
            copilot_event("user.message", {"text": "copilot predeep"}),
            _DEEP_JSON_LINE,
            copilot_event(
                "user.message", {"text": "copilot postdeep"}, idx=1
            ),
        ],
        workspace={"git_root": "/fixture/workspace/copilot"},
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, copilot_dirs=[root])

    assert stats.copilot_messages == 2
    assert stats.skipped_records >= 1
    assert search(idx, "postdeep")


def test_copilot_unhashable_event_type_skipped(tmp_path):
    """``{"type": []}`` is valid JSONL but unhashable — it must skip, not
    TypeError the whole index run (devin-review on #83)."""
    root = tmp_path / "copilot"
    add_copilot_session(
        root,
        "cp-badtype",
        [
            copilot_event("user.message", {"text": "copilot prebad marker"}),
            '{"type": [], "data": {"text": "unhashable"}}',
            copilot_event(
                "user.message", {"text": "copilot postbad marker"}, idx=1
            ),
        ],
        workspace={"git_root": "/fixture/workspace/copilot"},
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, copilot_dirs=[root])

    assert stats.copilot_messages == 2
    assert stats.skipped_records >= 1
    assert search(idx, "postbad")


def test_vanished_source_root_drops_stale_docs(tmp_path):
    """When every root of a source disappears, its docs/markers must be
    dropped — not linger as phantom hits (devin-review #83)."""
    root = tmp_path / "copilot"
    add_copilot_session(
        root,
        "cp-gone",
        [copilot_event("user.message", {"text": "copilot ghostline"})],
        workspace={"git_root": "/w"},
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, copilot_dirs=[root])
    assert stats.copilot_messages == 1
    assert search(idx, "ghostline")

    import shutil

    shutil.rmtree(root)
    stats = build_index(idx, copilot_dirs=[root])
    assert stats.removed == 1
    assert not search(idx, "ghostline")
    assert index_doc_count(idx) == 0


_DEEP_JSON_LINE = "[" * 200_000 + "]" * 200_000


def test_vscode_deep_content_nesting_field_walk():
    """~5000 nested ``content`` fields must not RecursionError the walk —
    a recursive traversal dies on every supported Python at this depth
    (devin-review on #82). Called directly because json.loads of a
    5000-deep doc itself RecursionErrors on 3.11, before the walk ever
    runs — the file-level deep-JSON path is covered by
    test_vscode_deep_json_files_skipped."""
    from devin_search.index import _vscode_field_text

    node = "vscode deepnest leaf"
    for _ in range(5_000):
        node = {"content": node}
    assert _vscode_field_text(node) == "vscode deepnest leaf"
    # Lists and interleaved keys survive too, in traversal order.
    node = {"content": [{"content": "a"}, {"text": "b"}]}
    for _ in range(2_000):
        node = {"content": node}
    assert _vscode_field_text(node) == "a\nb"


def test_vscode_deep_json_files_skipped(tmp_path):
    """A snapshot .json or mutations .jsonl too deep for json.loads is
    skipped while sibling sessions still index (devin-review #83)."""
    user_dir = tmp_path / "Code" / "User"
    base = (
        user_dir / "workspaceStorage" / "wshash-deep" / "chatSessions"
    )
    base.mkdir(parents=True)
    (base / "deep-snap.json").write_text(_DEEP_JSON_LINE)
    (base / "deep-mut.jsonl").write_text(_DEEP_JSON_LINE + "\n")
    doc = {
        "version": 3,
        "sessionId": "vs-shallow",
        "creationDate": BASE_MS,
        "lastMessageDate": BASE_MS + 5_000,
        "requests": [
            {
                "message": {"text": "vscode shallow ask"},
                "response": [{"value": "vscode shallow reply"}],
                "timestamp": BASE_MS,
                "responseTimestamp": BASE_MS + 5_000,
            }
        ],
    }
    add_vscode_chat(user_dir, "vs-shallow", doc=doc, ws_dir="wshash-ok")
    idx = tmp_path / "search.db"
    stats = build_index(idx, vscode_chat_dirs=[user_dir])

    assert stats.vscode_chat_messages == 2
    assert search(idx, "shallow")

