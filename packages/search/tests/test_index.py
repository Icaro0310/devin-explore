import hashlib
import sqlite3

from conftest import add_acp_db, add_session, msg
from devin_internals.fixtures import create_acp_messages_db
from devin_search.index import build_index, index_doc_count


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
