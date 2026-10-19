"""CLI contract: `check`/`report`, --json schema, --md, exit codes."""

import json
import re

from devin_doctor import doctor
from devin_doctor.cli import main


def test_check_healthy_exit_0(ctx, capsys):
    rc = main(
        ["check", "--data-dir", str(ctx.data_dir), "--cwd", str(ctx.cwd)]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "PASS" in out
    for check_id in (
        "devin-cli",
        "devin-chat",
        "health-of-data",
        "config",
        "hooks-windows",
        "disk",
        "automation",
    ):
        assert check_id in out


def test_check_broken_exit_1(broken_ctx, capsys):
    rc = main(
        [
            "check",
            "--data-dir",
            str(broken_ctx.data_dir),
            "--cwd",
            str(broken_ctx.cwd),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
    assert "fix:" in out


def test_check_json_schema(ctx, capsys):
    rc = main(
        [
            "check",
            "--data-dir",
            str(ctx.data_dir),
            "--cwd",
            str(ctx.cwd),
            "--stale-days",
            "3650",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "devin-doctor"
    assert payload["overall"] == "PASS"
    assert isinstance(payload["findings"], list)
    assert payload["findings"], "expected findings"
    for f in payload["findings"]:
        # base contract + optional SURFACE-HEALTH §2 fields on surface
        # findings (rung/code/note — emitted only when set)
        assert {"check", "status", "message", "fix"} <= set(f) <= {
            "check", "status", "message", "fix", "rung", "code", "note",
        }
        assert f["status"] in ("PASS", "WARN", "FAIL")
    # every surface emits its canonical present finding — absent ones
    # report PASS/not-installed instead of staying out of the map
    assert set(payload["checks"]) == {
        "devin-cli",
        "devin-chat",
        "copilot-cli",
        "codex-cli",
        "vscode-chat",
        "health-of-data",
        "config",
        "hooks-windows",
        "disk",
        "updates",
        "automation",
    }
    # Extension contract: the VS Code adapter gates on a parseable version
    # and renders all three summary counters.
    assert isinstance(payload["version"], str)
    assert re.match(r"\d+\.\d+\.\d+", payload["version"])
    assert set(payload["summary"]) == {"pass", "warn", "fail"}
    assert all(isinstance(v, int) for v in payload["summary"].values())
    assert payload["summary"]["fail"] == 0


def test_check_json_broken_overall_fail(broken_ctx, capsys):
    rc = main(
        [
            "check",
            "--data-dir",
            str(broken_ctx.data_dir),
            "--cwd",
            str(broken_ctx.cwd),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert payload["overall"] == "FAIL"
    assert payload["summary"]["fail"] > 0


def test_report_md(ctx, capsys):
    rc = main(
        [
            "report",
            "--data-dir",
            str(ctx.data_dir),
            "--cwd",
            str(ctx.cwd),
            "--md",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "# devin-doctor report" in out
    assert "| check |" in out
    assert "credentials.toml" in out
    assert "fixture-token" not in out  # masked, always


def test_missing_data_dir_is_not_detected(tmp_path, capsys):
    """A missing data dir means 'no Devin detected' — absence is a valid
    environment, not a failure (exit 0)."""
    rc = main(
        ["check", "--data-dir", str(tmp_path / "nope"), "--cwd", str(tmp_path)]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "not detected" in out


def test_surface_scope_filters_surface_checks(ctx, capsys):
    """--surface keeps one surface's ladder plus the devin-environment
    checks; other surfaces never run (not even 'not detected')."""
    rc = main(
        [
            "check",
            "--data-dir",
            str(ctx.data_dir),
            "--cwd",
            str(ctx.cwd),
            "--surface",
            "devin-cli",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "devin-cli — PASS" in out
    assert "devin-chat" not in out
    assert "not detected" not in out  # only the scoped surface ran
    for check_id in (
        "health-of-data", "config", "disk", "updates", "automation",
    ):
        assert check_id in out  # devin checks always run


def test_surface_scope_rejects_unknown_name(ctx, capsys):
    import pytest

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "check",
                "--data-dir",
                str(ctx.data_dir),
                "--cwd",
                str(ctx.cwd),
                "--surface",
                "not-a-surface",
            ]
        )
    assert exc.value.code == 2  # argparse choices rejection
    assert "invalid choice" in capsys.readouterr().err


def test_a_crashing_check_becomes_fail_finding(ctx, monkeypatch):
    from devin_doctor.checks import disk as disk_mod

    def boom(_ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(disk_mod, "run", boom)
    report = doctor.run_all(ctx)
    crash = next(f for f in report.findings if "crashed" in f.message)
    assert crash.check == "disk"
    assert doctor.exit_code(report) == 1


def test_render_json_roundtrip(ctx):
    report = doctor.run_all(ctx)
    payload = json.loads(doctor.render_json(report, ctx))
    assert payload["overall"] in ("PASS", "WARN", "FAIL")
