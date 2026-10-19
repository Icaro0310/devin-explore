import hashlib
import json
import shutil
import sqlite3

import pytest
from conftest import (
    add_acp_db,
    add_codex_home,
    add_copilot_session,
    add_session,
    add_vscode_chat,
    codex_record,
    copilot_event,
    msg,
    vscode_request,
)
from devin_internals.fixtures import create_acp_messages_db
from devin_search.index import build_index, index_doc_count
from devin_search.query import search


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _doc_texts(index_path):
    con = sqlite3.connect(index_path)
    try:
        return [
            r[0]
            for r in con.execute("SELECT text FROM docs ORDER BY rowid")
        ]
    finally:
        con.close()


def test_build_indexes_sessions_db(db_path, tmp_path):
    idx = tmp_path / "search.db"
    stats = build_index(idx, sessions_db=db_path)

    assert stats.message_nodes > 0
    assert stats.prompt_history > 0
    assert stats.tool_calls > 0
    assert index_doc_count(idx) == stats.indexed
    texts = "\n".join(_doc_texts(idx))
    assert "fixture message" in texts
    assert "synthetic prompt" in texts


def test_build_is_incremental(db_path, tmp_path):
    idx = tmp_path / "search.db"
    first = build_index(idx, sessions_db=db_path)
    second = build_index(idx, sessions_db=db_path)

    assert first.indexed > 0
    assert second.indexed == 0
    assert index_doc_count(idx) == first.indexed


def test_incremental_picks_up_new_rows_only(db_path, tmp_path):
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)
    add_session(
        db_path,
        "new-session",
        messages=[msg("user", "zebra crossing the release pipeline")],
    )

    stats = build_index(idx, sessions_db=db_path)
    assert stats.indexed == 1
    assert stats.message_nodes == 1
    texts = _doc_texts(idx)
    assert any("zebra" in t for t in texts)


def test_indexes_acp_dir(db_path, acp_dir, tmp_path):
    add_acp_db(
        acp_dir,
        "gui-session",
        "gui-1",
        [
            ("user", {"role": "user", "text": "fix the flaky test"}),
            ("agent", {"role": "assistant", "text": "applied the patch"}),
        ],
    )
    idx = tmp_path / "search.db"
    stats = build_index(idx, sessions_db=db_path, acp_dir=acp_dir)

    assert stats.devin_chat_messages == 2
    assert "devin-chat" in stats.sources
    texts = "\n".join(_doc_texts(idx))
    assert "flaky test" in texts


def test_acp_incremental_and_file_removal(acp_dir, tmp_path):
    idx = tmp_path / "search.db"
    keep = add_acp_db(
        acp_dir, "keep", "s-keep", [("user", {"text": "stay"})]
    )
    gone = add_acp_db(
        acp_dir, "gone", "s-gone", [("user", {"text": "depart"})]
    )
    build_index(idx, acp_dir=acp_dir)
    assert index_doc_count(idx) == 2

    gone.unlink()
    stats = build_index(idx, acp_dir=acp_dir)
    assert stats.removed == 1
    texts = "\n".join(_doc_texts(idx))
    assert "stay" in texts and "depart" not in texts
    assert keep.exists()


def test_rebuild_reindexes_everything(db_path, tmp_path):
    idx = tmp_path / "search.db"
    first = build_index(idx, sessions_db=db_path)
    stats = build_index(idx, sessions_db=db_path, rebuild=True)
    assert stats.rebuilt
    assert stats.indexed == first.indexed


def test_source_stores_are_not_modified(db_path, acp_dir, tmp_path):
    acp_path = create_acp_messages_db(acp_dir / "ro.db")
    before_db = _sha256(db_path)
    before_acp = _sha256(acp_path)

    build_index(
        tmp_path / "search.db", sessions_db=db_path, acp_dir=acp_dir
    )

    assert _sha256(db_path) == before_db
    assert _sha256(acp_path) == before_acp
    assert not list(db_path.parent.glob("sessions.db-*"))
    assert not list(acp_dir.glob("*.db-journal"))


def test_missing_acp_dir_is_not_an_error(db_path, tmp_path):
    stats = build_index(
        tmp_path / "search.db",
        sessions_db=db_path,
        acp_dir=tmp_path / "does-not-exist",
    )
    assert stats.devin_chat_messages == 0
    assert "devin-chat" not in stats.sources


def test_project_and_session_link_stored(db_path, tmp_path):
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)
    con = sqlite3.connect(idx)
    try:
        row = con.execute(
            "SELECT session_id, project, source, ref FROM docs"
            " WHERE source = 'devin-cli' LIMIT 1"
        ).fetchone()
    finally:
        con.close()
    assert row[0]
    assert row[1].startswith("/fixture/workspace/")
    assert row[3].split(":", 1)[0] in {"node", "prompt", "tool"}


def test_stale_schema_index_auto_rebuilds_and_reports(db_path, tmp_path):
    """A v1 index is rebuilt on the next index run and stats say so."""
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)
    con = sqlite3.connect(idx)
    try:
        con.execute(
            "UPDATE index_meta SET value = '1' WHERE key = 'schema_version'"
        )
        con.execute("UPDATE docs SET source = 'sessions' WHERE source = 'devin-cli'")
        con.commit()
    finally:
        con.close()

    stats = build_index(idx, sessions_db=db_path)
    assert stats.rebuilt
    con = sqlite3.connect(idx)
    try:
        stale = con.execute(
            "SELECT COUNT(*) FROM docs WHERE source IN ('sessions', 'acp')"
            " OR ref LIKE 'acp:%' OR ref LIKE 'session:%'"
        ).fetchone()[0]
    finally:
        con.close()
    assert stale == 0


def test_query_rejects_stale_schema_index(db_path, tmp_path):
    """Queries on a pre-upgrade index fail closed instead of returning
    rows labelled with the old source vocabulary."""
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)
    con = sqlite3.connect(idx)
    try:
        con.execute(
            "UPDATE index_meta SET value = '1' WHERE key = 'schema_version'"
        )
        con.commit()
    finally:
        con.close()

    with pytest.raises(RuntimeError, match="stale"):
        search(idx, "kubectl")


# --- vanished sources --------------------------------------------------------
#
# A managed store that disappeared since the last run must sweep its docs
# from the index — otherwise deleted sessions keep surfacing as stale hits
# forever.  ``None`` still means "not managed this run" (``--no-*``) and
# preserves docs.


def test_inplace_row_update_reindexes(db_path, tmp_path):
    """Row ids are positions, not immutability: an UPDATE under an
    unchanged row_id must re-index — the watermark alone cannot see it."""
    add_session(
        db_path,
        "edit-me",
        messages=[msg("user", "walrus before the edit")],
    )
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)

    con = sqlite3.connect(db_path)
    with con:
        con.execute(
            "UPDATE message_nodes SET chat_message = ?"
            " WHERE session_id = 'edit-me'",
            ('{"role": "user", "text": "capybara after the edit"}',),
        )
    con.close()

    stats = build_index(idx, sessions_db=db_path)
    texts = "\n".join(_doc_texts(idx))
    assert "capybara" in texts
    assert "walrus" not in texts
    assert stats.removed > 0


def test_copilot_midfile_edit_reindexes(tmp_path):
    """An in-place events.jsonl edit (same length or longer) must not
    hide behind the line-count watermark."""
    root = tmp_path / "copilot"
    add_copilot_session(
        root, "cs1", [copilot_event("user.message", {"content": "alpha zebra"})]
    )
    idx = tmp_path / "search.db"
    build_index(idx, copilot_dirs=[root])
    assert index_doc_count(idx) == 1

    add_copilot_session(
        root, "cs1", [copilot_event("user.message", {"content": "marmot"})]
    )
    stats = build_index(idx, copilot_dirs=[root])
    texts = "\n".join(_doc_texts(idx))
    assert "marmot" in texts
    assert "alpha zebra" not in texts
    assert index_doc_count(idx) == 1
    assert stats.removed == 1


def test_copilot_append_still_incremental(tmp_path):
    """Pure appends keep the watermark fast path — no re-index."""
    root = tmp_path / "copilot"
    path = add_copilot_session(
        root, "cs1", [copilot_event("user.message", {"content": "first"})]
    )
    idx = tmp_path / "search.db"
    build_index(idx, copilot_dirs=[root])

    with path.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                copilot_event(
                    "user.message", {"content": "pangolin"}, idx=1
                )
            )
            + "\n"
        )
    stats = build_index(idx, copilot_dirs=[root])
    assert stats.indexed == 1
    assert stats.removed == 0
    assert index_doc_count(idx) == 2


def test_predigest_devin_index_rebuilds_once(db_path, tmp_path):
    """An index built before the content digest existed has watermarks but
    no fingerprint: covered content cannot be vouched for, so the first
    run under this code must rebuild it — otherwise pre-upgrade stale
    rows persist forever behind a freshly recorded digest."""
    add_session(
        db_path,
        "s1",
        messages=[msg("user", "walrus before the edit")],
    )
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)

    # Simulate the pre-upgrade index: watermarks survive, digest absent.
    con = sqlite3.connect(idx)
    with con:
        con.execute("DELETE FROM index_meta WHERE key = 'wm:devin-cli:content'")
    con.close()

    # What an old release would silently miss — an in-place row UPDATE.
    con = sqlite3.connect(db_path)
    with con:
        con.execute(
            "UPDATE message_nodes SET chat_message = ?"
            " WHERE session_id = 's1'",
            ('{"role": "user", "text": "capybara after the edit"}',),
        )
    con.close()

    stats = build_index(idx, sessions_db=db_path)
    texts = "\n".join(_doc_texts(idx))
    assert stats.removed > 0
    assert "capybara" in texts
    assert "walrus" not in texts

    # The rebuilt state is trusted afterwards — no reset loop.
    stats = build_index(idx, sessions_db=db_path)
    assert stats.removed == 0


def test_predigest_copilot_index_reindexes(tmp_path):
    """A copilot file indexed before the head fingerprint existed (wm > 0,
    head key absent) must be re-indexed once — in-place edits predating
    the upgrade cannot hide behind the line watermark."""
    root = tmp_path / "copilot"
    path = add_copilot_session(
        root, "cs1", [copilot_event("user.message", {"content": "alpha zebra"})]
    )
    idx = tmp_path / "search.db"
    build_index(idx, copilot_dirs=[root])

    con = sqlite3.connect(idx)
    with con:
        con.execute(
            "DELETE FROM index_meta WHERE key LIKE 'copilot-cli:head:%'"
        )
    con.close()

    path.write_text(
        json.dumps(
            copilot_event("user.message", {"content": "marmot"})
        )
        + "\n",
        encoding="utf-8",
    )
    stats = build_index(idx, copilot_dirs=[root])
    texts = "\n".join(_doc_texts(idx))
    assert "marmot" in texts
    assert "alpha zebra" not in texts
    assert stats.removed == 1


def test_textless_tool_call_no_reset_loop(db_path, tmp_path):
    """A tool call with no extractable text is skipped at insert time; it
    must not enter the coverage digest either — otherwise every run sees
    a mismatch and rebuilds the whole devin-cli slice forever."""
    add_session(db_path, "s1", messages=[msg("user", "baseline")])
    add_session(
        db_path,
        "s2",
        tool_calls=[(None, None)],  # no payload → never indexed
    )
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)

    stats = build_index(idx, sessions_db=db_path)
    assert stats.removed == 0
    stats = build_index(idx, sessions_db=db_path)
    assert stats.removed == 0


def test_vanished_sessions_db_drops_devin_cli_docs(db_path, tmp_path):
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)
    assert index_doc_count(idx) > 0

    db_path.unlink()
    stats = build_index(idx, sessions_db=db_path)
    assert stats.removed > 0
    assert index_doc_count(idx) == 0
    assert search(idx, "fixture") == []


def test_vanished_acp_dir_drops_devin_chat_docs(acp_dir, tmp_path):
    add_acp_db(acp_dir, "gone", "s-gone", [("user", {"text": "depart"})])
    idx = tmp_path / "search.db"
    build_index(idx, acp_dir=acp_dir)
    assert index_doc_count(idx) == 1

    shutil.rmtree(acp_dir)
    stats = build_index(idx, acp_dir=acp_dir)
    assert stats.removed == 1
    assert index_doc_count(idx) == 0


def test_vanished_copilot_root_drops_docs(tmp_path):
    root = tmp_path / "copilot"
    add_copilot_session(
        root, "cs1", [copilot_event("user.message", {"content": "numbat"})]
    )
    idx = tmp_path / "search.db"
    build_index(idx, copilot_dirs=[root])
    assert index_doc_count(idx) == 1

    shutil.rmtree(root)
    stats = build_index(idx, copilot_dirs=[root])
    assert stats.removed == 1
    assert index_doc_count(idx) == 0


def test_vanished_codex_root_drops_docs(tmp_path):
    root = tmp_path / "codex"
    add_codex_home(
        root,
        rollouts=[
            (
                "sessions/x/rollout-r1.jsonl",
                [
                    codex_record(
                        "session_meta",
                        {"id": "cx-1", "timestamp": "2026-05-28T20:26:40Z",
                         "cwd": "/w"},
                    ),
                    codex_record(
                        "event_msg",
                        {"type": "user_message", "message": "quoll"},
                    ),
                ],
            )
        ],
    )
    idx = tmp_path / "search.db"
    build_index(idx, codex_dir=root)
    assert index_doc_count(idx) == 1

    shutil.rmtree(root)
    stats = build_index(idx, codex_dir=root)
    assert stats.removed == 1
    assert index_doc_count(idx) == 0


def test_vanished_vscode_chat_dir_drops_docs(tmp_path):
    user = tmp_path / "Code" / "User"
    add_vscode_chat(
        user, "vs-1",
        doc={"version": 3,
             "requests": [vscode_request("quokka", "resp")]},
    )
    idx = tmp_path / "search.db"
    build_index(idx, vscode_chat_dirs=[user])
    assert index_doc_count(idx) >= 1

    shutil.rmtree(tmp_path / "Code")
    stats = build_index(idx, vscode_chat_dirs=[user])
    assert stats.removed >= 1
    assert index_doc_count(idx) == 0


def test_unpassed_source_preserves_docs(acp_dir, tmp_path):
    """``--no-*`` (``None``) means the source is not managed this run —
    existing docs stay.  Only an *absent* managed path sweeps."""
    add_acp_db(acp_dir, "keep", "s-keep", [("user", {"text": "stay"})])
    idx = tmp_path / "search.db"
    build_index(idx, acp_dir=acp_dir)
    assert index_doc_count(idx) == 1

    stats = build_index(idx)
    assert stats.removed == 0
    assert index_doc_count(idx) == 1


def test_devin_cli_epoch_seconds_normalize_to_ms(db_path, tmp_path):
    """Linux v17 stores write created_at in epoch seconds (spec §6.1
    dual-magnitude) — docs.ts must land as epoch ms, not ~1970."""
    secs = 1_786_000_000  # seconds, not ms — real Linux layout
    add_session(
        db_path,
        "sec-sess",
        created_ms=secs,
        messages=[msg("user", "xyzzy seconds probe")],
        tool_calls=[({"command": "ls"}, {"output": "ok"})],
    )
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)
    con = sqlite3.connect(idx)
    try:
        rows = con.execute(
            "SELECT ts FROM docs WHERE session_id = 'sec-sess'"
        ).fetchall()
    finally:
        con.close()
    assert len(rows) == 2  # 1 message node + 1 tool-call row
    # message derives ts from created_at+10_000 (still <1e12 → seconds);
    # the tool-call row derives from the session's created_at.
    assert all(ts >= 1_700_000_000_000 for (ts,) in rows), rows


def test_tool_call_name_indexed_to_docs_tool(db_path, tmp_path):
    """Tool-call payloads lift their tool name into ``docs.tool`` — the
    ``query --tool`` filter's ground truth. Extraction ports
    devin-graph's heuristic: top-level name keys first, then one level
    into ``rawInput``/``input``-style nests; the call payload wins over
    the update payload; nameless calls keep NULL."""
    add_session(
        db_path,
        "tools-sess",
        tool_calls=[
            ({"name": "shell", "command": "ls"}, {"output": "ok"}),
            # name only on the update payload — still found.
            ({"command": "ls"}, {"tool_name": "file_editor"}),
            # call wins over update on conflicting names.
            ({"name": "call_wins"}, {"name": "update_loses"}),
            # nested one level down (ACP rawInput shape).
            ({"rawInput": {"name": "nested_tool"}}, None),
            # no name anywhere → NULL, but the doc is still indexed.
            ({"command": "ls"}, {"output": "ok"}),
            # no payload at all → never indexed, no row.
            (None, None),
        ],
    )
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)

    con = sqlite3.connect(idx)
    try:
        rows = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT ref, tool FROM docs WHERE session_id = 'tools-sess'"
            )
        }
    finally:
        con.close()
    assert rows == {
        "tool:tools-sess/tools-sess-tc0": "shell",
        "tool:tools-sess/tools-sess-tc1": "file_editor",
        "tool:tools-sess/tools-sess-tc2": "call_wins",
        "tool:tools-sess/tools-sess-tc3": "nested_tool",
        "tool:tools-sess/tools-sess-tc4": None,
    }
