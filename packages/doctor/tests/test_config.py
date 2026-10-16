"""config check: credentials.toml presence (masked) and .devin/
hooks/mcp config sanity."""

from devin_doctor.checks import config
from devin_doctor.model import Context, Status


def _finding(findings, needle):
    return next(f for f in findings if needle in f.message)


def test_healthy_credentials_masked(ctx):
    f = _finding(config.run(ctx), "credentials.toml")
    assert f.status is Status.PASS
    assert "masked" in f.message
    # value from conftest must never leak into output
    for finding in config.run(ctx):
        assert "fixture-token" not in finding.message
        assert not finding.fix or "fixture-token" not in finding.fix


def test_missing_credentials_warns(broken_ctx):
    f = _finding(config.run(broken_ctx), "credentials.toml")
    assert f.status is Status.WARN
    assert f.fix


def test_invalid_credentials_fails(ctx):
    (ctx.data_dir / "credentials.toml").write_text("[broken\n", "utf-8")
    f = _finding(config.run(ctx), "credentials.toml")
    assert f.status is Status.FAIL


def test_desktop_only_skips_cli_credentials(tmp_path):
    """UI config root without a CLI data dir → no false 'sign in'
    warning; user-level UI config is still checked (Devin Review #4)."""
    ui_root = tmp_path / "Devin"
    (ui_root / "User").mkdir(parents=True)
    ctx = Context(
        data_dir=tmp_path / "no-cli-data",
        cwd=tmp_path,
        config_dir=ui_root,
    )
    findings = config.run(ctx)
    assert all("credentials.toml" not in f.message for f in findings)
    f = _finding(findings, "config.json")
    assert f.status is Status.PASS


def test_cli_only_skips_ui_config_but_checks_credentials(tmp_path):
    """CLI data dir without a UI config root → credentials checked, no
    Desktop config expectation."""
    root = tmp_path / "devin"
    root.mkdir()
    (root / "credentials.toml").write_text('[auth]\ntoken = "x"\n')
    ctx = Context(
        data_dir=root, cwd=tmp_path, config_dir=tmp_path / "no-ui"
    )
    findings = config.run(ctx)
    assert _finding(findings, "credentials.toml").status is Status.PASS


def test_no_devin_dir_passes(ctx):
    f = _finding(config.run(ctx), ".devin")
    assert f.status is Status.PASS


def test_valid_hooks_file_passes(ctx):
    devin_dir = ctx.cwd / ".devin"
    devin_dir.mkdir()
    (devin_dir / "hooks.v1.json").write_text(
        '// hooks are fine\n{"PreToolUse": [{"matcher": "exec",'
        ' "hooks": [{"type": "command", "command": "./check.sh"}]}]}',
        "utf-8",
    )
    f = _finding(config.run(ctx), "hooks.v1.json")
    assert f.status is Status.PASS


def test_invalid_json_in_hooks_file_fails(ctx):
    devin_dir = ctx.cwd / ".devin"
    devin_dir.mkdir()
    (devin_dir / "hooks.v1.json").write_text("{not json", "utf-8")
    f = _finding(config.run(ctx), "hooks.v1.json")
    assert f.status is Status.FAIL


def test_unknown_hook_event_warns(ctx):
    devin_dir = ctx.cwd / ".devin"
    devin_dir.mkdir()
    (devin_dir / "hooks.v1.json").write_text(
        '{"NoSuchEvent": [{"hooks": []}]}', "utf-8"
    )
    f = _finding(config.run(ctx), "hooks.v1.json")
    assert f.status is Status.WARN
    assert "NoSuchEvent" in f.message


def test_malformed_hooks_structure_fails(ctx):
    devin_dir = ctx.cwd / ".devin"
    devin_dir.mkdir()
    (devin_dir / "hooks.v1.json").write_text(
        '{"PreToolUse": "nope"}', "utf-8"
    )
    f = _finding(config.run(ctx), "hooks.v1.json")
    assert f.status is Status.FAIL


def test_mcp_config_ok(ctx):
    devin_dir = ctx.cwd / ".devin"
    devin_dir.mkdir()
    (devin_dir / "mcp_config.json").write_text(
        '{"mcpServers": {"x": {"command": "srv"}}}', "utf-8"
    )
    f = _finding(config.run(ctx), ".devin/mcp_config.json")
    assert f.status is Status.PASS


def test_mcp_config_missing_servers_key_warns(ctx):
    devin_dir = ctx.cwd / ".devin"
    devin_dir.mkdir()
    (devin_dir / "mcp_config.json").write_text('{"oops": {}}', "utf-8")
    f = _finding(config.run(ctx), ".devin/mcp_config.json")
    assert f.status is Status.WARN


def test_legacy_mcpservers_in_config_warns(ctx):
    devin_dir = ctx.cwd / ".devin"
    devin_dir.mkdir()
    (devin_dir / "config.json").write_text(
        '{"mcpServers": {"x": {"command": "srv"}}}', "utf-8"
    )
    f = _finding(config.run(ctx), ".devin/config.json")
    assert f.status is Status.WARN
    assert "mcp_config" in f.message or "mcpServers" in f.message


def test_user_config_in_data_dir_invalid_fails(ctx):
    (ctx.data_dir / "config.json").write_text("{bad json", "utf-8")
    f = _finding(config.run(ctx), "config.json")
    assert f.status is Status.FAIL


def test_child_repo_devin_dir_checked(tmp_path, data_dir):
    repo = tmp_path / "child-repo"
    (repo / ".devin").mkdir(parents=True)
    (repo / ".devin" / "hooks.v1.json").write_text("{broken", "utf-8")
    ctx = Context(data_dir=data_dir, cwd=tmp_path)
    f = _finding(config.run(ctx), "child-repo")
    assert f.status is Status.FAIL


def test_desktop_only_install_skips_credentials_check(tmp_path):
    """Only the UI config dir present → no CLI data root, so the
    credentials check must not warn about a missing credentials.toml
    (devin-review on #85)."""
    (tmp_path / "Devin" / "User").mkdir(parents=True)
    ctx = Context(
        data_dir=tmp_path / "devin",  # absent CLI data root
        config_dir=tmp_path / "Devin",
        cwd=tmp_path,
    )
    findings = config.run(ctx)
    assert all("credentials.toml" not in f.message for f in findings)


def test_desktop_only_shared_root_skips_credentials(tmp_path):
    """data_dir exists but holds only Desktop anchors (``User/``) — no
    ``cli/`` or credentials file — so the credentials check must not
    warn. Mirrors the case-insensitive unified-root situation."""
    root = tmp_path / "shared"
    (root / "User" / "acp-messages").mkdir(parents=True)
    ctx = Context(data_dir=root, config_dir=root, cwd=tmp_path)
    findings = config.run(ctx)
    assert all("credentials.toml" not in f.message for f in findings)


def test_cli_only_install_skips_ui_config(tmp_path, data_dir):
    """A CLI-only data root still checks credentials but does not force
    UI config findings on an absent config_dir."""
    ctx = Context(data_dir=data_dir, cwd=tmp_path)  # config_dir=None
    findings = config.run(ctx)
    assert any("credentials.toml" in f.message for f in findings)
