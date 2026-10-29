"""``devin-doctor drift`` — bounded, privacy-safe drift snapshot.

The command's contract is twofold: explicit per-check output plus a hard
privacy bound (no file contents, no env values — digests and key names
only). Every probe seam is injectable so tests stay hermetic.
"""

import hashlib
import json
import os

from devin_doctor import drift
from devin_doctor.cli import main
from devin_doctor.model import Context, Status


def _fake_env(**overrides):
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/x",
        "DEVIN_DATA_DIR": "/secret/data",
        "XDG_CONFIG_HOME": "/secret/config",
        "AWS_SECRET_ACCESS_KEY": "AKIA-SECRET",
    }
    env.update(overrides)
    return env


def _which_present(cmd):
    return f"/stub/bin/{cmd}"


def _version_ok(_exe):
    return "9.9.9"


def _no_dists():
    return {}


def _collect(ctx, **kw):
    kw.setdefault("which", _which_present)
    kw.setdefault("version_of", _version_ok)
    kw.setdefault("installed_dists", _no_dists)
    kw.setdefault("environ", _fake_env())
    return drift.collect(ctx, **kw)


def test_collect_healthy_snapshot(ctx):
    report = _collect(ctx)
    snap = report.snapshot
    assert snap["command"] == "drift"
    assert set(snap["sections"]) == {"tools", "packages", "files", "paths"}
    assert snap["bounds"]["max_digest_bytes"] > 0
    # every expected file of the fixture data dir shows up with a digest
    files = {e.get("relpath"): e for e in snap["sections"]["files"]}
    for rel in ("cli/sessions.db", "credentials.toml"):
        assert files[rel]["status"] == "present"
        assert files[rel]["sha256_16"]
    assert drift.exit_code(report) == 0
    assert all(f.status is not Status.FAIL for f in report.findings)


def test_digest_matches_real_sha256(ctx):
    report = _collect(ctx)
    files = {e.get("relpath"): e for e in report.snapshot["sections"]["files"]}
    real = hashlib.sha256(
        (ctx.data_dir / "credentials.toml").read_bytes()
    ).hexdigest()[:16]
    assert files["credentials.toml"]["sha256_16"] == real


def test_no_file_contents_leak(ctx, tmp_path):
    secret = "SUPERSECRET-TOKEN-VALUE-12345"
    (ctx.data_dir / "credentials.toml").write_text(
        f'token = "{secret}"\n', encoding="utf-8"
    )
    (tmp_path / ".devin").mkdir(exist_ok=True)
    (tmp_path / ".devin" / "config.json").write_text(
        '{"key": "' + secret + '"}', encoding="utf-8"
    )
    report = _collect(ctx)
    blob = drift.render_json(report) + drift.render_text(report)
    assert secret not in blob
    # and env values never leak either
    assert "AKIA-SECRET" not in blob
    assert "/secret/data" not in blob


def test_env_key_names_only(ctx):
    report = _collect(ctx, environ=_fake_env(DEVIN_OFFLINE="1"))
    keys = report.snapshot["sections"]["paths"]["env_keys"]
    assert "DEVIN_DATA_DIR" in keys and "XDG_CONFIG_HOME" in keys
    assert "AWS_SECRET_ACCESS_KEY" not in keys  # not a curated name
    assert "HOME" not in keys


def test_missing_sessions_db_is_drift(ctx):
    # the cli/ dir + credentials.toml remain → the CLI anchor says
    # sessions.db should exist; its absence is real drift.
    (ctx.data_dir / "cli" / "sessions.db").unlink()
    report = _collect(ctx)
    fails = [f for f in report.findings if f.status is Status.FAIL]
    assert any("sessions.db" in f.message for f in fails)
    assert drift.exit_code(report) == 1


def test_missing_credentials_warns_not_fails(ctx):
    # config-check doctrine: a missing credentials.toml is WARN —
    # the install is degraded, not unrecognisable.
    (ctx.data_dir / "credentials.toml").unlink()
    report = _collect(ctx)
    warns = [f for f in report.findings if f.status is Status.WARN]
    assert any("credentials.toml" in f.message for f in warns)
    files = {e.get("relpath"): e for e in report.snapshot["sections"]["files"]}
    assert files["credentials.toml"]["status"] == "missing"
    assert drift.exit_code(report) == 0


def test_no_devin_install_is_not_detected(tmp_path):
    ctx = Context(data_dir=tmp_path / "nope", cwd=tmp_path)
    report = _collect(ctx, environ={"PATH": "/usr/bin"})
    files = report.snapshot["sections"]["files"]
    assert files[0]["status"] == "not-detected"
    # no install anchors → no expected files → nothing is drift;
    # every tool resolves via the injected stub.
    assert drift.exit_code(report) == 0


def test_cli_only_install_does_not_expect_vscdb(tmp_path):
    # cli/ + credentials.toml with no User/ tree: a Desktop file
    # must not be reported missing on a CLI-only machine.
    data = tmp_path / "devin"
    (data / "cli").mkdir(parents=True)
    (data / "cli" / "sessions.db").write_bytes(b"")
    ctx = Context(data_dir=data, cwd=tmp_path)
    report = _collect(ctx)
    files = report.snapshot["sections"]["files"]
    assert not any(
        "state.vscdb" in e.get("relpath", "") for e in files
    )
    assert drift.exit_code(report) == 0


def test_tool_missing_and_below_floor(ctx):
    def which(cmd):
        return None if cmd in ("node", "git") else f"/stub/bin/{cmd}"

    def version_of(_exe):
        return "3.9.0"  # below the 3.10 floor for python3

    report = _collect(ctx, which=which, version_of=version_of)
    tools = {t["name"]: t for t in report.snapshot["sections"]["tools"]}
    assert tools["git"]["status"] == "missing"
    assert tools["node"]["status"] == "missing"
    assert tools["python3"]["status"] == "below-floor"
    fails = [f.message for f in report.findings if f.status is Status.FAIL]
    assert any("'git'" in m for m in fails)
    assert any("below" in m and "python3" in m for m in fails)
    warns = [f.message for f in report.findings if f.status is Status.WARN]
    assert any("'node'" in m and "optional" in m for m in warns)
    assert drift.exit_code(report) == 1


def test_unverifiable_tool_warns(ctx):
    report = _collect(ctx, version_of=lambda _exe: None)
    tools = {t["name"]: t for t in report.snapshot["sections"]["tools"]}
    assert tools["python3"]["status"] == "unverified"
    assert tools["git"]["status"] == "ok"  # no floor → version unknown ok
    assert drift.exit_code(report) == 0


def test_package_version_mismatch(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    pkg = repo / "packages" / "foo"
    pkg.mkdir(parents=True)
    (pkg / "pyproject.toml").write_text(
        '[project]\nname = "devin-foo"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    ctx = Context(data_dir=tmp_path / "nope", cwd=repo)
    report = _collect(
        ctx,
        installed_dists=lambda: {"devin_foo": "1.2.0"},
    )
    pkgs = report.snapshot["sections"]["packages"]
    assert pkgs[0]["status"] == "version-mismatch"
    assert pkgs[0]["installed"] == "1.2.0" and pkgs[0]["declared"] == "1.2.3"

    report = _collect(ctx, installed_dists=lambda: {"devin_foo": "1.2.3"})
    assert report.snapshot["sections"]["packages"][0]["status"] == "ok"

    report = _collect(ctx, installed_dists=dict)
    assert (
        report.snapshot["sections"]["packages"][0]["status"]
        == "not-installed"
    )


def test_package_scoped_flag(tmp_path):
    repo = tmp_path / "repo"
    for name in ("foo", "bar"):
        pkg = repo / "packages" / name
        pkg.mkdir(parents=True)
        (pkg / "pyproject.toml").write_text(
            f'[project]\nname = "devin-{name}"\nversion = "1.0.0"\n',
            encoding="utf-8",
        )
    ctx = Context(data_dir=tmp_path / "nope", cwd=repo, packages=("foo",))
    report = _collect(ctx, installed_dists=lambda: {"devin_foo": "1.0.0"})
    pkgs = report.snapshot["sections"]["packages"]
    assert [p["name"] for p in pkgs] == ["devin-foo"]


def test_dead_path_entries_warn(ctx, tmp_path):
    report = _collect(
        ctx,
        environ=_fake_env(
            PATH=f"{tmp_path}/gone{os.pathsep}{tmp_path}/also-gone"),
    )
    pinfo = report.snapshot["sections"]["paths"]["path"]
    assert len(pinfo["missing"]) == 2
    assert any(
        f.status is Status.WARN and "PATH" in f.message
        for f in report.findings
    )


def test_missing_cwd_fails(tmp_path):
    ctx = Context(
        data_dir=tmp_path / "nope", cwd=tmp_path / "no-such-cwd"
    )
    report = _collect(ctx, environ={"PATH": "/usr/bin"})
    assert any(
        f.status is Status.FAIL and "cwd" in f.message
        for f in report.findings
    )
    assert drift.exit_code(report) == 1


def test_cli_drift_text(ctx, capsys):
    main(
        ["drift", "--data-dir", str(ctx.data_dir), "--cwd", str(ctx.cwd)]
    )
    out = capsys.readouterr().out
    assert "bounded snapshot" in out
    assert "tools" in out and "files" in out and "paths" in out
    assert "overall:" in out


def test_cli_drift_json(ctx, capsys):
    rc = main(
        [
            "drift",
            "--data-dir",
            str(ctx.data_dir),
            "--cwd",
            str(ctx.cwd),
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)
    assert data["command"] == "drift"
    assert data["overall"] in ("PASS", "WARN", "FAIL")
    assert set(data["summary"]) == {"pass", "warn", "fail"}
    assert data["sections"]["files"]
    assert "privacy" in data
    # fixture data dir is healthy; this box's tools decide the exit —
    # the only guarantee is it matches the FAIL count.
    assert rc == (1 if data["summary"]["fail"] else 0)


def test_cli_drift_missing_file_exit_1(ctx, capsys):
    (ctx.data_dir / "cli" / "sessions.db").unlink()
    rc = main(
        ["drift", "--data-dir", str(ctx.data_dir), "--cwd", str(ctx.cwd)]
    )
    assert rc == 1
    assert "sessions.db" in capsys.readouterr().out
