"""MCP adapter contract: do_check mirrors `check --json`; the tool layer
never raises — errors surface as {error, detail}."""

import json

import pytest
from devin_doctor import doctor
from devin_doctor.mcp_server import do_capabilities, do_check


def test_do_check_matches_cli_json(ctx, capsys):
    """The adapter returns exactly what `devin-doctor check --json` prints."""
    expected = json.loads(doctor.render_json(doctor.run_all(ctx), ctx))
    out = do_check(
        data_dir=str(ctx.data_dir),
        cwd=str(ctx.cwd),
        config_dir=str(ctx.config_dir) if ctx.config_dir else None,
        now_ms=ctx.now_ms,
    )
    assert out == expected


def test_do_check_healthy_fixture(ctx):
    out = do_check(
        data_dir=str(ctx.data_dir), cwd=str(ctx.cwd), now_ms=ctx.now_ms)
    assert out["overall"] == "PASS"
    assert isinstance(out["findings"], list)
    assert out["findings"]


def test_do_check_broken_fixture(broken_ctx):
    out = do_check(data_dir=str(broken_ctx.data_dir), cwd=str(broken_ctx.cwd))
    assert out["overall"] == "FAIL"
    assert any(f["status"] == "FAIL" for f in out["findings"])


def test_do_check_missing_dir_is_report_not_crash(tmp_path):
    out = do_check(data_dir=str(tmp_path / "nonexistent"), cwd=str(tmp_path))
    # a missing data dir is a finding (FAIL), not an exception —
    # the tool boundary only returns {error, detail} for real failures.
    assert "error" not in out or out["overall"] in {"PASS", "WARN", "FAIL"}


def test_do_check_offline_is_per_call_not_global(ctx, monkeypatch):
    """offline=True must not leak through the process env: a concurrent
    online call and any later call keep full probing."""
    import os

    monkeypatch.delenv("DEVIN_DOCTOR_OFFLINE", raising=False)
    out = do_check(
        data_dir=str(ctx.data_dir), cwd=str(ctx.cwd),
        config_dir=str(ctx.config_dir) if ctx.config_dir else None,
        offline=True, now_ms=ctx.now_ms,
    )
    updates = next(
        f for f in out["findings"] if f["check"] == "updates")
    assert "offline" in updates["message"]
    assert "DEVIN_DOCTOR_OFFLINE" not in os.environ


def test_do_capabilities_matches_cli(tmp_path, capsys):
    from devin_doctor.cli import main

    cfg = tmp_path / "cfg"
    rc = main(["capabilities", "--config-dir", str(cfg)])
    expected = json.loads(capsys.readouterr().out)
    out = do_capabilities(config_dir=str(cfg))
    assert rc == 0
    assert out == expected


def test_build_server_registers_both_tools():
    """Both adapters must reach the MCP surface — a silent registration
    miss would leave the skill calling a tool that does not exist."""
    pytest.importorskip("mcp")
    import asyncio
    import inspect

    from devin_doctor.mcp_server import build_server

    server = build_server()
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
    assert {"doctor_check", "doctor_capabilities"} <= names


def test_server_entrypoint_in_pyproject():
    from pathlib import Path

    import tomllib

    pyproject = (
        Path(__file__).parents[1] / "pyproject.toml").read_text(
            encoding="utf-8")
    meta = tomllib.loads(pyproject)
    scripts = meta["project"]["scripts"]
    assert scripts["devin-doctor-mcp"] == "devin_doctor.mcp_server:main"
    assert any(dep.startswith("mcp") for dep in
               meta["project"]["optional-dependencies"]["mcp"])


def _registered_tool_names() -> set[str]:
    """Tools the MCP server registers — derived statically so this test
    runs without the optional ``mcp`` extra installed."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).parents[1] / "src" / "devin_doctor" / "mcp_server.py"
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
    assert _registered_tool_names() == {"doctor_capabilities","doctor_check"}
