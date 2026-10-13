"""MCP adapter contract: do_query mirrors `query <what> --json`; a missing
graph.db (the CLI's exit-2 case) surfaces as {"error": "no_graph"} — the
adapter must not create the file; the tool layer never raises."""

import json

import pytest
from devin_graph.cli import main
from devin_graph.mcp_server import do_query


@pytest.fixture
def graph(sessions_db, tmp_path):
    """A built graph.db over the synthetic 3-session/2-project fixture."""
    g = tmp_path / "graph.db"
    assert main(
        ["build", "--sessions-db", str(sessions_db), "--vscdb", "none",
         "--graph", str(g)]
    ) == 0
    return g


@pytest.mark.parametrize(("query_type", "target"), [
    ("file", "src/app.py"),
    ("tool", "read"),
    ("project", "alpha"),
    ("projects-graph", ""),
    ("shared-files", ""),
])
def test_do_query_matches_cli_json(graph, query_type, target, capsys):
    """The adapter returns exactly what `devin-graph query --json` prints."""
    capsys.readouterr()
    argv = ["query", query_type]
    if target:
        argv.append(target)
    argv += ["--graph", str(graph), "--json"]
    assert main(argv) == 0
    expected = json.loads(capsys.readouterr().out)
    assert do_query(query_type, target=target, graph=str(graph)) == expected


def test_do_query_missing_graph(tmp_path):
    missing = tmp_path / "none.db"
    out = do_query("file", "x.py", graph=str(missing))
    assert out["error"] == "no_graph"
    assert "devin-graph build" in out["detail"]
    # GraphStore would create the db on open — the adapter must check first.
    assert not missing.exists()


def test_do_query_default_graph_name(tmp_path, monkeypatch, graph):
    """graph="" falls back to ./graph.db, like the CLI."""
    monkeypatch.chdir(tmp_path)  # the fixture built tmp_path/graph.db
    out = do_query("projects-graph")
    assert "error" not in out
    assert out["nodes"]


def test_do_query_unknown_type(graph):
    out = do_query("nope", graph=str(graph))
    assert out["error"] == "bad_query"


def test_do_query_never_mutates_existing_graph(tmp_path):
    """An existing db with an incomplete schema is reported, not silently
    repaired — the query path opens mode=ro and runs no schema DDL."""
    import sqlite3

    g = tmp_path / "graph.db"
    con = sqlite3.connect(g)
    con.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    con.commit()
    con.close()

    # missing edges table: the query fails instead of the open creating it
    with pytest.raises(sqlite3.OperationalError):
        do_query("projects-graph", graph=str(g))

    con = sqlite3.connect(f"file:{g}?mode=ro", uri=True)
    tables = {
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    con.close()
    assert tables == {"nodes"}


def test_no_build_sql_view_surface():
    """The adapter never references build/export/sql/view code paths."""
    import inspect

    import devin_graph.mcp_server as m

    src = inspect.getsource(m)
    for banned in (".build(", "build_index", "write_view", "sqlite3"):
        assert banned not in src


def test_build_server_registers_graph_query():
    """The published tool name must reach the MCP surface — a named
    decorator or helper-based registration would otherwise pass the AST
    pin while the skill calls a tool that does not exist."""
    pytest.importorskip("mcp")
    import asyncio
    import inspect

    server = __import__(
        "devin_graph.mcp_server", fromlist=["build_server"]
    ).build_server()
    list_tools = getattr(server, "list_tools", None)
    if callable(list_tools):
        tools = list_tools()
        if inspect.isawaitable(tools):
            tools = asyncio.run(tools)
        names = {getattr(t, "name", t) for t in tools}
    else:  # tool-manager internals differ across SDK versions
        manager = getattr(server, "_tool_manager", None) or getattr(
            server, "tools", None)
        assert manager is not None
        names = set(getattr(manager, "_tools", manager))
    assert "graph_query" in names


def test_server_entrypoint_in_pyproject():
    from pathlib import Path

    import tomllib

    pyproject = (
        Path(__file__).parents[1] / "pyproject.toml").read_text(
            encoding="utf-8")
    meta = tomllib.loads(pyproject)
    scripts = meta["project"]["scripts"]
    assert scripts["devin-graph-mcp"] == "devin_graph.mcp_server:main"
    assert any(dep.startswith("mcp") for dep in
               meta["project"]["optional-dependencies"]["mcp"])


def _registered_tool_names() -> set[str]:
    """Tools the MCP server registers — derived statically so this test
    runs without the optional ``mcp`` extra installed."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).parents[1] / "src" / "devin_graph" / "mcp_server.py"
    )
    tree = ast.parse(src.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "tool"
            for dec in node.decorator_list
        )
    }


def test_mcp_tool_surface_is_pinned():
    """Regression contract: the AI surface is exactly this set. A new
    tool only lands after a deliberate edit here — check it stays
    read-only before widening."""
    assert _registered_tool_names() == {"graph_query"}
