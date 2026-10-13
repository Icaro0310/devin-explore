"""MCP adapter contract: do_list/do_export mirror `list|export --json`;
store-open failures (the CLI's exit-2 cases) surface as
{"error": "bad_store"}; the tool layer never raises."""

import json

import pytest
from devin_history.cli import main
from devin_history.mcp_server import do_export, do_list
from devin_internals.fixtures import create_sessions_db


def test_do_list_matches_cli_json(db_path, capsys):
    """The adapter returns exactly what `devin-history list --json` prints."""
    assert main(
        ["list", "--sessions-db", str(db_path), "--json"]
    ) == 0
    expected = json.loads(capsys.readouterr().out)
    assert do_list(sessions_db=str(db_path)) == expected


def test_do_list_limit(db_path):
    out = do_list(sessions_db=str(db_path), limit=1)
    assert len(out["sessions"]) == 1


def test_do_list_default_is_uncapped(tmp_path):
    """The uncapped default returns every session, not just twenty."""
    many = create_sessions_db(tmp_path / "many.db", n_sessions=25)
    out = do_list(sessions_db=str(many))
    assert len(out["sessions"]) == 25


def test_do_export_dry_run_matches_cli_json(db_path, tmp_path, capsys):
    """dry_run gives exact payload parity: nothing written either side."""
    out = tmp_path / "dump"
    assert main(
        [
            "export", "--sessions-db", str(db_path), "--out", str(out),
            "--format", "json", "--dry-run", "--json",
        ]
    ) == 0
    expected = json.loads(capsys.readouterr().out)
    got = do_export(
        str(out), sessions_db=str(db_path), fmt="json", dry_run=True
    )
    assert got == expected


def test_do_export_matches_cli_json(db_path, tmp_path, capsys):
    """A real export yields the same payload modulo the out_dir itself."""
    cli_out, tool_out = tmp_path / "a", tmp_path / "b"
    assert main(
        ["export", "--sessions-db", str(db_path),
         "--out", str(cli_out), "--json"]
    ) == 0
    expected = json.loads(capsys.readouterr().out)
    got = do_export(str(tool_out), sessions_db=str(db_path))
    assert {
        k: v for k, v in got.items() if k != "out_dir"
    } == {k: v for k, v in expected.items() if k != "out_dir"}
    # and the notes really landed
    assert len(list(tool_out.glob("2*.md"))) == len(expected["written"])
    assert (tool_out / "index.md").is_file()


def test_do_export_session_id(db_path, tmp_path, capsys):
    assert main(
        ["list", "--sessions-db", str(db_path), "--json"]
    ) == 0
    sessions = json.loads(capsys.readouterr().out)["sessions"]
    sid = sessions[0]["id"]
    out = do_export(
        str(tmp_path / "one"), sessions_db=str(db_path), session_id=sid
    )
    assert "error" not in out
    assert all(sid in name for name in out["written"])


def test_do_list_missing_db_is_bad_store(tmp_path):
    out = do_list(sessions_db=str(tmp_path / "nope.db"))
    assert out["error"] == "bad_store"
    assert "no such file" in out["detail"]


def test_do_list_unknown_schema_is_bad_store(tmp_path):
    bad = create_sessions_db(tmp_path / "future.db", schema_version=18)
    out = do_list(sessions_db=str(bad))
    assert out["error"] == "bad_store"
    assert "schema" in out["detail"]


def test_do_export_missing_db_is_bad_store(tmp_path):
    out = do_export(str(tmp_path / "o"), sessions_db=str(tmp_path / "no.db"))
    assert out["error"] == "bad_store"


def test_build_server_registers_tools():
    pytest.importorskip("mcp")
    server = __import__(
        "devin_history.mcp_server", fromlist=["build_server"]
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
    assert scripts["devin-history-mcp"] == "devin_history.mcp_server:main"
    assert any(dep.startswith("mcp") for dep in
               meta["project"]["optional-dependencies"]["mcp"])


def _registered_tool_names() -> set[str]:
    """Tools the MCP server registers — derived statically so this test
    runs without the optional ``mcp`` extra installed."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).parents[1] / "src" / "devin_history" / "mcp_server.py"
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
    assert _registered_tool_names() == {"history_export","history_list"}
