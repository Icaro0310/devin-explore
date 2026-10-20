"""path-check: declared console scripts vs PATH reachability.

Package trees are synthetic tmp dirs; PATH reachability, the installed
entry-point index and dist versions are all injected — nothing real is
installed or probed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from devin_doctor import doctor
from devin_doctor.checks import path_check
from devin_doctor.cli import main
from devin_doctor.model import Context, Status


def _ctx(cwd: Path) -> Context:
    return Context(data_dir=cwd, cwd=cwd)


def write_pkg(
    pkg: Path,
    *,
    name: str = "devin-foo",
    scripts: dict[str, str] | None = None,
    gui_scripts: dict[str, str] | None = None,
) -> Path:
    """Minimal package dir — path-check only reads pyproject scripts."""
    if scripts is None:
        mod = name.replace("-", "_")
        scripts = {name: f"{mod}.cli:main"}
    pkg.mkdir(parents=True)
    lines = ["[project]", f'name = "{name}"', 'version = "1.0.0"']
    if scripts:
        lines += ["", "[project.scripts]"]
        lines += [f'{k} = "{v}"' for k, v in scripts.items()]
    if gui_scripts:
        lines += ["", "[project.gui-scripts]"]
        lines += [f'{k} = "{v}"' for k, v in gui_scripts.items()]
    (pkg / "pyproject.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return pkg


def write_repo(root: Path, names=("foo", "bar")) -> Path:
    for name in names:
        write_pkg(root / "packages" / name, name=f"devin-{name}")
    return root


def _fake_which(mapping: dict[str, str]):
    return lambda cmd: mapping.get(cmd)


def _fake_hits(mapping: dict[str, list[str]]):
    return lambda cmd: mapping.get(cmd, [])


def _status(findings, status):
    return [f for f in findings if f.status is status]


# --- happy / missing paths ---------------------------------------------------


def test_reachable_script_passes(tmp_path):
    pkg = write_pkg(tmp_path / "mypkg")
    findings = path_check.run(
        _ctx(pkg),
        which=_fake_which({"devin-foo": "/x/bin/devin-foo"}),
        hits=_fake_hits({"devin-foo": ["/x/bin/devin-foo"]}),
        index=lambda: {"devin-foo": "devin-foo"},
        dist_version=lambda n: "1.0.0",
    )
    assert [f.status for f in findings] == [Status.PASS]
    assert "on PATH" in findings[0].message


def test_missing_script_fails_naming_it(tmp_path):
    pkg = write_pkg(tmp_path / "mypkg")
    findings = path_check.run(
        _ctx(pkg),
        which=_fake_which({}),
        hits=_fake_hits({}),
        index=dict,
        dist_version=lambda n: None,
    )
    assert [f.status for f in findings] == [Status.FAIL]
    assert "'devin-foo'" in findings[0].message
    assert "not on PATH" in findings[0].message
    assert "not installed" in findings[0].message


def test_installed_but_off_path_fails(tmp_path):
    """The dist provides the entry point yet PATH misses it — the
    'installed but unreachable' case the check exists for."""
    pkg = write_pkg(tmp_path / "mypkg")
    findings = path_check.run(
        _ctx(pkg),
        which=_fake_which({}),
        hits=_fake_hits({}),
        index=lambda: {"devin-foo": "devin-foo"},
        dist_version=lambda n: "1.0.0",
    )
    assert [f.status for f in findings] == [Status.FAIL]
    assert "provided by installed 'devin-foo'" in findings[0].message
    assert "not on PATH" in findings[0].message


def test_installed_without_script_link_fails(tmp_path):
    """Package installed, but the dist never exposed the script — the
    message says the link is missing rather than 'not installed'."""
    pkg = write_pkg(tmp_path / "mypkg")
    findings = path_check.run(
        _ctx(pkg),
        which=_fake_which({}),
        hits=_fake_hits({}),
        index=dict,
        dist_version=lambda n: "1.0.0",
    )
    assert [f.status for f in findings] == [Status.FAIL]
    assert "installed but its script is not linked" in findings[0].message


# --- shadowing ----------------------------------------------------------------


def test_shadowed_by_other_distribution_warns(tmp_path):
    pkg = write_pkg(tmp_path / "mypkg")
    findings = path_check.run(
        _ctx(pkg),
        which=_fake_which({"devin-foo": "/x/bin/devin-foo"}),
        hits=_fake_hits({"devin-foo": ["/x/bin/devin-foo"]}),
        index=lambda: {"devin-foo": "other-tool"},
        dist_version=lambda n: "1.0.0",
    )
    assert [f.status for f in findings] == [Status.WARN]
    assert "other-tool" in findings[0].message
    assert "shadow" in findings[0].message


def test_extra_path_hits_warn_shadowing(tmp_path):
    """A second same-named executable deeper on PATH is shadowed — named
    even though the command resolves."""
    pkg = write_pkg(tmp_path / "mypkg")
    findings = path_check.run(
        _ctx(pkg),
        which=_fake_which({"devin-foo": "/a/bin/devin-foo"}),
        hits=_fake_hits(
            {"devin-foo": ["/a/bin/devin-foo", "/b/bin/devin-foo"]}
        ),
        index=lambda: {"devin-foo": "devin-foo"},
        dist_version=lambda n: "1.0.0",
    )
    assert [f.status for f in findings] == [Status.WARN]
    assert "shadowing" in findings[0].message
    assert "devin-foo" in findings[0].message


def test_uv_tool_style_no_provider_still_passes(tmp_path):
    """uv tool / pipx shims resolve on PATH but no dist in *this*
    interpreter claims them — reachable is reachable: PASS, not WARN."""
    pkg = write_pkg(tmp_path / "mypkg")
    findings = path_check.run(
        _ctx(pkg),
        which=_fake_which({"devin-foo": "/x/bin/devin-foo"}),
        hits=_fake_hits({"devin-foo": ["/x/bin/devin-foo"]}),
        index=dict,
        dist_version=lambda n: None,
    )
    assert [f.status for f in findings] == [Status.PASS]


# --- scope and discovery -------------------------------------------------------


def test_fanout_checks_every_package(tmp_path):
    write_repo(tmp_path)
    findings = path_check.run(
        _ctx(tmp_path),
        which=_fake_which({
            "devin-foo": "/x/bin/devin-foo",
            "devin-bar": "/x/bin/devin-bar",
        }),
        hits=_fake_hits({
            "devin-foo": ["/x/bin/devin-foo"],
            "devin-bar": ["/x/bin/devin-bar"],
        }),
        index=dict,
        dist_version=lambda n: None,
    )
    assert all(f.status is Status.PASS for f in findings)
    assert {f.message.split("'")[1] for f in findings} == {
        "devin-foo", "devin-bar",
    }


def test_gui_scripts_are_checked_too(tmp_path):
    pkg = write_pkg(
        tmp_path / "guipkg",
        gui_scripts={"devin-foo-gui": "devin_foo.gui:main"},
    )
    findings = path_check.run(
        _ctx(pkg),
        which=_fake_which({}),
        hits=_fake_hits({}),
        index=dict,
        dist_version=lambda n: None,
    )
    assert {f.status for f in findings} == {Status.FAIL}
    assert {f.message.split("'")[1] for f in findings} == {
        "devin-foo", "devin-foo-gui",
    }


def test_installed_dist_scripts_union_in(tmp_path):
    """An installed dist exposes a script the pyproject doesn't declare —
    it is still probed (the 'given installed package' half)."""
    pkg = write_pkg(tmp_path / "mypkg")
    findings = path_check.run(
        _ctx(pkg),
        which=_fake_which({"devin-foo": "/x/bin/devin-foo"}),
        hits=_fake_hits({"devin-foo": ["/x/bin/devin-foo"]}),
        index=lambda: {
            "devin-foo": "devin-foo",
            "devin-foo-extra": "devin-foo",
        },
        dist_version=lambda n: "1.0.0",
    )
    names = {f.message.split("'")[1] for f in findings}
    assert names == {"devin-foo", "devin-foo-extra"}
    assert any(f.status is Status.FAIL for f in findings)


def test_no_scripts_declared_passes(tmp_path):
    pkg = write_pkg(tmp_path / "libpkg", scripts={})
    findings = path_check.run(_ctx(pkg))
    assert [f.status for f in findings] == [Status.PASS]
    assert "no console scripts" in findings[0].message


def test_nothing_package_shaped_passes(tmp_path):
    findings = path_check.run(_ctx(tmp_path))
    assert [f.status for f in findings] == [Status.PASS]
    assert "nothing to check" in findings[0].message


def test_package_scope_filter_via_context(tmp_path):
    """ctx.packages narrows the fan-out — getattr keeps the check working
    whether or not the Context carries the field yet."""
    write_repo(tmp_path)
    ctx = _ctx(tmp_path)
    ctx.packages = ("foo",)  # settable attr — field lands with --package
    findings = path_check.run(
        ctx,
        which=_fake_which({"devin-foo": "/x/bin/devin-foo"}),
        hits=_fake_hits({"devin-foo": ["/x/bin/devin-foo"]}),
        index=dict,
        dist_version=lambda n: None,
    )
    assert {f.message.split("'")[1] for f in findings} == {"devin-foo"}


def test_package_scope_unknown_name_warns(tmp_path):
    write_repo(tmp_path)
    ctx = _ctx(tmp_path)
    ctx.packages = ("ghost",)
    findings = path_check.run(
        ctx,
        which=_fake_which({}),
        hits=_fake_hits({}),
        index=dict,
        dist_version=lambda n: None,
    )
    assert any(
        "ghost" in f.message and f.status is Status.WARN
        for f in findings
    )
    assert not any(f.status is Status.FAIL for f in findings)


def test_unparseable_pyproject_contributes_nothing(tmp_path):
    pkg = tmp_path / "bad"
    pkg.mkdir()
    (pkg / "pyproject.toml").write_text("not = = toml\n")
    findings = path_check.run(_ctx(pkg))
    # dir is package-shaped but declares nothing → nothing probed
    assert [f.status for f in findings] == [Status.PASS]
    assert "no console scripts" in findings[0].message


# --- wiring / CLI --------------------------------------------------------------


def test_run_all_includes_check(ctx):
    report = doctor.run_all(ctx)
    assert "path-check" in report.check_status()


def test_cli_missing_script_exit_1(tmp_path, capsys):
    pkg = write_pkg(tmp_path / "mypkg")
    rc = main([
        "check", "--data-dir", str(tmp_path), "--cwd", str(pkg),
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert "path-check" in out
    assert "devin-foo" in out


def test_cli_reachable_script_exit_0(tmp_path, monkeypatch, capsys):
    """A real PATH lookup: tmp bin dir on PATH, script named portably."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "devin-foo"
    script.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(script, 0o755)  # shutil.which requires X_OK on POSIX
    (bindir / "devin-foo.cmd").write_text("@exit /b 0\r\n")
    monkeypatch.setenv("PATH", str(bindir))
    pkg = write_pkg(tmp_path / "mypkg")
    rc = main([
        "check", "--data-dir", str(tmp_path), "--cwd", str(pkg),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "path-check" in out
    assert "on PATH" in out


def test_cli_json_names_the_command(tmp_path, capsys):
    pkg = write_pkg(tmp_path / "mypkg")
    rc = main([
        "check", "--data-dir", str(tmp_path), "--cwd", str(pkg),
        "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    mine = [
        f for f in payload["findings"] if f["check"] == "path-check"
    ]
    assert mine and any("devin-foo" in f["message"] for f in mine)


def test_every_failure_names_its_command(tmp_path):
    """Regression: two broken scripts → two FAILs, each naming its own
    command — no merged/silent failure."""
    pkg = write_pkg(
        tmp_path / "multi",
        scripts={
            "alpha": "devin_foo.cli:a",
            "beta": "devin_foo.cli:b",
            "gamma": "devin_foo.cli:c",
        },
    )
    findings = path_check.run(
        _ctx(pkg),
        which=_fake_which({"beta": "/x/bin/beta"}),
        hits=_fake_hits({"beta": ["/x/bin/beta"]}),
        index=dict,
        dist_version=lambda n: None,
    )
    fails = _status(findings, Status.FAIL)
    assert {f.message.split("'")[1] for f in fails} == {"alpha", "gamma"}
    passes = _status(findings, Status.PASS)
    assert {f.message.split("'")[1] for f in passes} == {"beta"}
