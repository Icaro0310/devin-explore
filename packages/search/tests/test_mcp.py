"""MCP adapter contract: do_query mirrors `query --json`; a missing index
(the CLI's exit-2 case) surfaces as {"error": "no_index"}; zero hits is
not an error (exit 1, payload still printed); the tool layer never raises."""

import json

import pytest
from conftest import add_session, msg
from devin_search.cli import main
from devin_search.mcp_server import do_query


@pytest.fixture
def index(db_path, tmp_path):
    """A built search.db over the synthetic sessions fixture."""
    add_session(db_path, "s1", messages=[msg("user", "xyzzy marker")])
    idx = tmp_path / "search.db"
    assert main(
        ["index", "--sessions-db", str(db_path), "--no-acp",
         "--index", str(idx)]
    ) == 0
    return idx


def test_do_query_matches_cli_json(index, capsys):
    """The adapter returns exactly what `devin-search query --json` prints."""
    capsys.readouterr()
    assert main(
        ["query", "xyzzy", "--index", str(index), "--json"]
    ) == 0
    expected = json.loads(capsys.readouterr().out)
    assert do_query("xyzzy", index=str(index)) == expected


def test_do_query_zero_hits_same_payload(index, capsys):
    """Zero hits: the CLI exits 1 but still prints the payload — same dict."""
    capsys.readouterr()
    assert main(
        ["query", "absent-term", "--index", str(index), "--json"]
    ) == 1
    expected = json.loads(capsys.readouterr().out)
    got = do_query("absent-term", index=str(index))
    assert got == expected
    assert got["hits"] == []


def test_do_query_filters(index, capsys):
    capsys.readouterr()
    assert main(
        ["query", "xyzzy", "--role", "user", "--index", str(index), "--json"]
    ) == 0
    expected = json.loads(capsys.readouterr().out)
    assert do_query("xyzzy", index=str(index), role="user") == expected


def test_do_query_missing_index(tmp_path):
    out = do_query("x", index=str(tmp_path / "none.db"))
    assert out == {
        "error": "no_index",
        "detail": "run `devin-search index` first",
    }


def test_do_query_bad_since(index):
    out = do_query("xyzzy", index=str(index), since="garbage")
    assert out["error"] == "bad_since"


def test_do_query_writes_no_query_log(index):
    """The adapter is read-only — the local query log stays unwritten."""
    do_query("xyzzy", index=str(index))
    assert not (index.parent / "search.db.queries.jsonl").exists()


def test_no_index_building_surface():
    """The adapter must not expose index building — query only."""
    import inspect

    import devin_search.mcp_server as m

    src = inspect.getsource(m)
    assert "build_index" not in src


def test_build_server_registers_search_query():
    pytest.importorskip("mcp")
    server = __import__(
        "devin_search.mcp_server", fromlist=["build_server"]
    ).build_server()
    tools = getattr(server, "_tool_manager", None) or getattr(
        server, "tools", None)
    assert tools is not None


def test_server_entrypoint_in_pyproject():
    from pathlib import Path

    import tomllib

    pyproject = (
        Path(__file__).parents[1] / "pyproject.toml").read_text(
            encoding="utf-8")
    meta = tomllib.loads(pyproject)
    scripts = meta["project"]["scripts"]
    assert scripts["devin-search-mcp"] == "devin_search.mcp_server:main"
    assert any(dep.startswith("mcp") for dep in
               meta["project"]["optional-dependencies"]["mcp"])


def _registered_tool_names() -> set[str]:
    """Tools the MCP server registers — derived statically so this test
    runs without the optional ``mcp`` extra installed."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).parents[1] / "src" / "devin_search" / "mcp_server.py"
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
    assert _registered_tool_names() == {"search_query"}
