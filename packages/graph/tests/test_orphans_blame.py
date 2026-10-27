"""``graph orphans`` + ``graph blame-session`` — report-only diagnostics."""

import json
import sqlite3

import pytest
from conftest import ALPHA
from devin_graph.cli import main
from devin_graph.query import blame_session, orphaned_nodes
from devin_graph.store import GraphStore
from devin_internals.parsers import SessionsStore


@pytest.fixture
def graph_path(sessions_db, tmp_path):
    p = tmp_path / "graph.db"
    with SessionsStore(sessions_db) as ss, GraphStore(p) as gs:
        gs.build(ss)
    return p


def _insert_orphan(graph_path, node_id="file:/ghost", owner=None):
    """An edgeless shared node straight into the db — the fixture graph
    prunes real orphans at build time, so the only way to see one is to
    write it after the build."""
    con = sqlite3.connect(graph_path)
    with con:
        con.execute(
            "INSERT INTO nodes(id, kind, key, owner, attrs)"
            " VALUES (?, ?, ?, ?, '{}')",
            (node_id, "file", node_id.split(":", 1)[1], owner),
        )
    con.close()


# -- orphaned_nodes ------------------------------------------------------------


def test_orphans_empty_in_healthy_graph(graph_path):
    with GraphStore(graph_path, readonly=True) as gs:
        data = orphaned_nodes(gs)
    assert data == {"count": 0, "nodes": []}


def test_orphans_reports_and_marks_prunable(graph_path):
    _insert_orphan(graph_path)
    with GraphStore(graph_path, readonly=True) as gs:
        data = orphaned_nodes(gs)
    assert data["count"] == 1
    (n,) = data["nodes"]
    assert n["id"] == "file:/ghost"
    assert n["kind"] == "file" and n["owner"] is None
    assert n["prunable"] is True


def test_orphans_report_only_never_deletes(graph_path):
    _insert_orphan(graph_path)
    with GraphStore(graph_path, readonly=True) as gs:
        orphaned_nodes(gs)
    con = sqlite3.connect(graph_path)
    try:
        assert con.execute(
            "SELECT COUNT(*) FROM nodes WHERE id = 'file:/ghost'"
        ).fetchone()[0] == 1
    finally:
        con.close()


def test_owned_orphan_not_prunable(graph_path):
    # an owner-carried orphan persists until its owner re-extracts —
    # report it, but flag it as kept-by-prune.
    _insert_orphan(graph_path, "session:ghost-sess", owner="ghost-sess")
    with GraphStore(graph_path, readonly=True) as gs:
        data = orphaned_nodes(gs)
    (n,) = data["nodes"]
    assert n["prunable"] is False and n["owner"] == "ghost-sess"


# -- blame_session --------------------------------------------------------------


def test_blame_session_contents(graph_path):
    with GraphStore(graph_path, readonly=True) as gs:
        data = blame_session(gs, "sess-a")
    s = data["session"]
    assert s["title"] == "Alpha work"
    assert data["projects"] == [ALPHA]
    assert data["extracted_at"] is not None
    # produced: 1 session node + 3 tool_call nodes
    kinds = [n["kind"] for n in data["nodes_owned"]]
    assert kinds.count("session") == 1 and kinds.count("tool_call") == 3
    assert data["counts"]["nodes"] == 4
    # touched: the read of ALPHA/src/app.py (resolve joins it under cwd)
    assert f"{ALPHA}/src/app.py" in data["files"]
    assert data["counts"]["files"] == len(data["files"])
    # edges: runs_in + 3× made_call + call_used/tool_used/file_touched
    assert data["counts"]["edges"] == len(data["edges_owned"])
    assert any(e["kind"] == "runs_in" for e in data["edges_owned"])
    assert any(e["kind"] == "file_touched" for e in data["edges_owned"])


def test_blame_session_no_match(graph_path):
    with GraphStore(graph_path, readonly=True) as gs:
        data = blame_session(gs, "zzz")
    assert data["session"] is None
    assert data["candidates"] == []


def test_blame_session_ambiguous_prefix(graph_path):
    with GraphStore(graph_path, readonly=True) as gs:
        data = blame_session(gs, "sess")
    assert data["session"] is None
    assert set(data["candidates"]) == {"sess-a", "sess-b", "sess-c"}


# -- CLI ------------------------------------------------------------------------


def test_cli_orphans_empty(graph_path, capsys):
    assert main(["orphans", "--graph", str(graph_path)]) == 0
    assert "no orphaned nodes" in capsys.readouterr().out


def test_cli_orphans_json(graph_path, capsys):
    _insert_orphan(graph_path)
    assert main(["orphans", "--graph", str(graph_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 1
    assert data["nodes"][0]["id"] == "file:/ghost"


def test_cli_blame_text(graph_path, capsys):
    assert main(["blame-session", "sess-b", "--graph", str(graph_path)]) == 0
    out = capsys.readouterr().out
    assert "Beta work" in out
    assert "produced:" in out and "touched:" in out
    assert f"{ALPHA}/src/app.py" in out  # shared file sess-b also read


def test_cli_blame_json(graph_path, capsys):
    assert main(
        ["blame-session", "sess-c", "--graph", str(graph_path), "--json"]
    ) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["session_id"] == "sess-c"
    assert data["session"]["title"] == "Alpha docs"
    assert data["counts"]["nodes"] >= 2


def test_cli_blame_no_match_exit_2(graph_path, capsys):
    rc = main(["blame-session", "nope", "--graph", str(graph_path)])
    assert rc == 2
    assert "no session matches" in capsys.readouterr().err


def test_cli_blame_ambiguous_lists_candidates(graph_path, capsys):
    rc = main(["blame-session", "sess", "--graph", str(graph_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "sess-a" in err and "sess-b" in err
