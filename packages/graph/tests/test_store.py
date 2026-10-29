"""GraphStore: SQLite persistence, incremental re-extract, JSON export."""

import json
import sqlite3

from conftest import ALPHA, BETA, add_tool_call, bump_last_activity, delete_session
from devin_graph.store import GraphStore
from devin_internals.parsers import SessionsStore
from devin_internals.schema import LATEST_KNOWN_SCHEMA

EXPECTED_NODE_COUNTS = {
    "session": 3, "project": 2, "tool": 4, "tool_call": 6, "file": 5}
EXPECTED_EDGE_COUNTS = {
    "runs_in": 3, "made_call": 6, "call_used": 5,
    "tool_used": 5, "file_touched": 6}


def _counts(rows):
    out = {}
    for r in rows:
        out[r.kind] = out.get(r.kind, 0) + 1
    return out


def _build(sessions_db, graph_path):
    with SessionsStore(sessions_db) as ss, GraphStore(graph_path) as gs:
        return gs.build(ss)


def test_build_populates_graph(sessions_db, tmp_path):
    res = _build(sessions_db, tmp_path / "graph.db")
    assert sorted(res.extracted) == ["sess-a", "sess-b", "sess-c"]
    assert res.skipped == 0

    with GraphStore(tmp_path / "graph.db") as gs:
        assert _counts(gs.nodes()) == EXPECTED_NODE_COUNTS
        assert _counts(gs.edges()) == EXPECTED_EDGE_COUNTS


def test_incremental_build_skips_unchanged(sessions_db, tmp_path):
    g = tmp_path / "graph.db"
    _build(sessions_db, g)
    res = _build(sessions_db, g)
    assert res.extracted == []
    assert res.skipped == 3
    with GraphStore(g) as gs:
        assert _counts(gs.nodes()) == EXPECTED_NODE_COUNTS
        assert _counts(gs.edges()) == EXPECTED_EDGE_COUNTS


def test_incremental_reextracts_changed_session(sessions_db, tmp_path):
    g = tmp_path / "graph.db"
    _build(sessions_db, g)

    add_tool_call(
        sessions_db, "sess-c", "sess-c-tc9",
        {"kind": "edit", "rawInput": {"file_path": f"{ALPHA}/new.py"}})
    bump_last_activity(sessions_db, "sess-c", 1_780_100_000_000)

    res = _build(sessions_db, g)
    assert res.extracted == ["sess-c"]
    assert res.skipped == 2

    with GraphStore(g) as gs:
        assert _counts(gs.nodes()) == {
            **EXPECTED_NODE_COUNTS, "tool_call": 7, "file": 6}
        new_edge = ("file_touched", "tool_call:sess-c:sess-c-tc9",
                    f"file:{ALPHA}/new.py")
        assert new_edge in {(e.kind, e.src, e.dst) for e in gs.edges()}


def test_incremental_removes_deleted_session(sessions_db, tmp_path):
    g = tmp_path / "graph.db"
    _build(sessions_db, g)
    delete_session(sessions_db, "sess-b")

    res = _build(sessions_db, g)
    assert res.removed == ["sess-b"]
    with GraphStore(g) as gs:
        ids = {n.id for n in gs.nodes()}
        assert "session:sess-b" not in ids
        assert not any(i.startswith("tool_call:sess-b:") for i in ids)
        # /repo/beta lost its only session → orphan project/file pruned.
        assert f"project:{BETA}" not in ids
        assert f"file:{BETA}/main.py" not in ids
        # shared nodes survive: alpha project, app.py, read tool.
        assert f"project:{ALPHA}" in ids
        assert f"file:{ALPHA}/src/app.py" in ids
        assert "tool:read" in ids


def test_export_json_shape(sessions_db, tmp_path):
    g = tmp_path / "graph.db"
    _build(sessions_db, g)
    with GraphStore(g) as gs:
        data = gs.export()

    assert set(data) == {"meta", "nodes", "edges"}
    # fixture emits the installed spec's latest schema (v17 at 0.3.1,
    # v18 at 0.3.3) — pin to the constant, not a literal
    assert data["meta"]["schema_version"] == LATEST_KNOWN_SCHEMA
    json.dumps(data)  # must be JSON-serializable

    node = next(n for n in data["nodes"] if n["id"] == "session:sess-a")
    assert node["kind"] == "session"
    assert node["title"] == "Alpha work"

    edge = next(e for e in data["edges"] if e["kind"] == "runs_in")
    assert edge["source"].startswith("session:")
    assert edge["target"].startswith("project:")


def test_export_deterministic(sessions_db, tmp_path):
    g = tmp_path / "graph.db"
    _build(sessions_db, g)
    with GraphStore(g) as gs:
        assert gs.export() == gs.export()


def test_build_records_source_meta(sessions_db, tmp_path):
    g = tmp_path / "graph.db"
    _build(sessions_db, g)
    with GraphStore(g) as gs:
        meta = gs.export()["meta"]
    assert meta["source_db"] == str(sessions_db)


def test_graph_db_is_writable_store_sessions_untouched(sessions_db, tmp_path):
    """graph.db opens r/w; the source sessions.db stays byte-identical."""
    import hashlib
    before = hashlib.sha256(sessions_db.read_bytes()).hexdigest()
    _build(sessions_db, tmp_path / "graph.db")
    _build(sessions_db, tmp_path / "graph.db")
    assert hashlib.sha256(sessions_db.read_bytes()).hexdigest() == before
    # and graph.db really is a sqlite db we own
    con = sqlite3.connect(tmp_path / "graph.db")
    assert con.execute(
        "SELECT COUNT(*) FROM nodes").fetchone()[0] == 20
    con.close()
