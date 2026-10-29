"""CLI contract: build / query file|tool|project / export."""

import hashlib
import json

import pytest
from conftest import ALPHA
from devin_graph.cli import main
from devin_internals.fixtures import create_sessions_db
from devin_internals.schema import LATEST_KNOWN_SCHEMA


@pytest.fixture
def built(sessions_db, tmp_path):
    graph = tmp_path / "graph.db"
    assert main(["build", "--sessions-db", str(sessions_db), "--vscdb", "none",
                 "--graph", str(graph)]) == 0
    return sessions_db, graph


def test_cli_build_text(sessions_db, tmp_path, capsys):
    g = tmp_path / "graph.db"
    assert main(["build", "--sessions-db", str(sessions_db), "--vscdb", "none",
                 "--graph", str(g)]) == 0
    out = capsys.readouterr().out
    assert "extracted: 3" in out
    assert "nodes: 20" in out and "edges: 25" in out
    assert g.exists()


def test_cli_build_json(sessions_db, tmp_path, capsys):
    g = tmp_path / "graph.db"
    assert main(["build", "--sessions-db", str(sessions_db), "--vscdb", "none",
                 "--graph", str(g), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["extracted"] == 3
    assert data["nodes"] == 20 and data["edges"] == 25
    assert data["graph"].endswith("graph.db")


def test_cli_build_incremental(built, capsys):
    sessions_db, graph = built
    capsys.readouterr()
    assert main(["build", "--sessions-db", str(sessions_db), "--vscdb", "none",
                 "--graph", str(graph), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["extracted"] == 0 and data["skipped"] == 3


def test_cli_query_file(built, capsys):
    _, graph = built
    capsys.readouterr()
    assert main(["query", "file", "src/app.py", "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert f"{ALPHA}/src/app.py" in out
    assert "sess-a" in out and "sess-b" in out


def test_cli_query_file_json(built, capsys):
    _, graph = built
    capsys.readouterr()
    assert main(["query", "file", "src/app.py", "--graph", str(graph),
                 "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in data["sessions"]] == ["sess-a", "sess-b"]


def test_cli_query_tool(built, capsys):
    _, graph = built
    capsys.readouterr()
    assert main(["query", "tool", "read", "--graph", str(graph),
                 "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in data["sessions"]] == ["sess-a", "sess-b"]
    assert len(data["projects"]) == 2


def test_cli_query_project(built, capsys):
    _, graph = built
    capsys.readouterr()
    assert main(["query", "project", "alpha", "--graph", str(graph),
                 "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["project"] == ALPHA
    assert data["tools"] == ["execute", "fs_write", "read"]
    assert len(data["files"]) == 4


def test_cli_query_projects_graph(built, capsys):
    _, graph = built
    capsys.readouterr()
    assert main(["query", "projects-graph", "--graph", str(graph),
                 "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["nodes"]) == 2
    assert data["links"][0]["weight"] == 2


def test_cli_query_shared_files(built, capsys):
    _, graph = built
    capsys.readouterr()
    assert main(["query", "shared-files", "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert f"{ALPHA}/src/app.py" in out
    assert "2 projects" in out


def test_cli_query_shared_files_json(built, capsys):
    _, graph = built
    capsys.readouterr()
    assert main(["query", "shared-files", "--graph", str(graph),
                 "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["count"] == 1
    (entry,) = data["files"]
    assert entry["file"] == f"{ALPHA}/src/app.py"
    assert len(entry["projects"]) == 2
    assert entry["sessions"] == ["sess-a", "sess-b"]


def test_cli_export_stdout(built, capsys):
    _, graph = built
    capsys.readouterr()
    assert main(["export", "--format", "json", "--graph", str(graph)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["nodes"]) == 20 and len(data["edges"]) == 25
    # fixture emits the installed spec's latest schema (v17 at 0.3.1,
    # v18 at 0.3.3) — pin to the constant, not a literal
    assert data["meta"]["schema_version"] == LATEST_KNOWN_SCHEMA


def test_cli_export_out_file(built, tmp_path, capsys):
    _, graph = built
    out = tmp_path / "graph.json"
    capsys.readouterr()
    assert main(["export", "--format", "json", "--graph", str(graph),
                 "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["nodes"]) == 20


def test_cli_missing_sessions_db(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["build", "--sessions-db", str(tmp_path / "nope.db"),
              "--graph", str(tmp_path / "g.db")])
    assert exc.value.code == 2
    assert "no such file" in capsys.readouterr().err


def test_cli_unknown_schema_fails_loud(tmp_path, capsys):
    bad = create_sessions_db(tmp_path / "future.db", schema_version=LATEST_KNOWN_SCHEMA + 1)
    with pytest.raises(SystemExit) as exc:
        main(["build", "--sessions-db", str(bad),
              "--graph", str(tmp_path / "g.db")])
    assert exc.value.code == 2
    assert "schema version" in capsys.readouterr().err


def test_cli_query_missing_graph(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["query", "file", "x.py", "--graph", str(tmp_path / "none.db")])
    assert exc.value.code == 2
    assert "graph.db" in capsys.readouterr().err


def test_cli_no_default_db(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty"))
    monkeypatch.setattr(
        "devin_graph.paths.Path.home", lambda: tmp_path / "nohome")
    with pytest.raises(SystemExit) as exc:
        main(["build", "--graph", str(tmp_path / "g.db")])
    assert exc.value.code == 2
    assert "no sessions.db found" in capsys.readouterr().err


def test_cli_never_writes_source_db(built, tmp_path, capsys):
    sessions_db, graph = built
    before = hashlib.sha256(sessions_db.read_bytes()).hexdigest()
    capsys.readouterr()
    main(["build", "--sessions-db", str(sessions_db), "--vscdb", "none", "--graph", str(graph)])
    main(["query", "file", "app.py", "--graph", str(graph)])
    main(["export", "--format", "json", "--graph", str(graph)])
    capsys.readouterr()
    assert hashlib.sha256(sessions_db.read_bytes()).hexdigest() == before


def test_sql_select_and_rejects_writes(tmp_path, capsys):
    import sqlite3
    db = tmp_path / "g.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t(a)"); con.execute("INSERT INTO t VALUES (7)")
    con.commit(); con.close()
    from devin_graph.cli import main
    assert main(["sql", "SELECT a FROM t", "--graph", str(db)]) == 0
    assert "7" in capsys.readouterr().out
    assert main(["sql", "DROP TABLE t", "--graph", str(db)]) == 2


def test_view_writes_offline_html(tmp_path, capsys, sessions_db):
    from devin_graph.cli import main
    gdb = tmp_path / "g.db"
    main(["build", "--sessions-db", str(sessions_db), "--vscdb", "none",
          "--graph", str(gdb)])
    out = tmp_path / "v.html"
    capsys.readouterr()
    assert main(["view", "--graph", str(gdb), "--out", str(out)]) == 0
    html = out.read_text()
    assert "devin-graph" in html and "DATA" in html
    # no external assets (the svg xmlns namespace string is fine)
    assert 'src="http' not in html and 'href="http' not in html
    assert "cdn" not in html.lower()
