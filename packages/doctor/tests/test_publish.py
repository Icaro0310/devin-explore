"""publish-readiness check: pyproject metadata, src layout, version sync,
console-script resolvability, wheel build and pypi-publish.yml coverage.

All package trees are synthetic tmp dirs; the wheel build is injected (or
runs against a temp-dir copy in the one real-build test), so the package
dir under test is never written to.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from devin_doctor import doctor
from devin_doctor.checks import publish
from devin_doctor.cli import main
from devin_doctor.model import Context, Status


@pytest.fixture(autouse=True)
def _builds_allowed(monkeypatch):
    """conftest pins DEVIN_DOCTOR_OFFLINE=1 for the updates check; the
    publish check reads the same flag for its real-build leg, so tests
    that inject a builder must see the env as online."""
    monkeypatch.delenv("DEVIN_DOCTOR_OFFLINE", raising=False)


def _ctx(cwd: Path, packages: tuple[str, ...] = ()) -> Context:
    return Context(data_dir=cwd, cwd=cwd, packages=packages)


def write_pkg(
    pkg: Path,
    *,
    name: str = "devin-foo",
    version: str | None = "1.2.3",
    init_version: str | None = "1.2.3",
    module: str | None = None,
    scripts: dict[str, str] | None = None,
    src_layout: bool = True,
    build_system: bool = True,
) -> Path:
    """A synthetic src-layout package: pyproject + src/<mod>/{__init__,cli}."""
    if scripts is None:
        mod = module or name.replace("-", "_")
        scripts = {name: f"{mod}.cli:main"}
    pkg.mkdir(parents=True)
    lines: list[str] = []
    if build_system:
        lines += [
            "[build-system]",
            'requires = ["setuptools>=68"]',
            'build-backend = "setuptools.build_meta"',
            "",
        ]
    lines += ["[project]", f'name = "{name}"']
    if version is not None:
        lines.append(f'version = "{version}"')
    if scripts:
        lines += ["", "[project.scripts]"]
        lines += [f'{k} = "{v}"' for k, v in scripts.items()]
    (pkg / "pyproject.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    if src_layout:
        mod_dir = pkg / "src" / (module or name.replace("-", "_"))
        mod_dir.mkdir(parents=True)
        init = "" if init_version is None else f'__version__ = "{init_version}"\n'
        (mod_dir / "__init__.py").write_text(init, encoding="utf-8")
        (mod_dir / "cli.py").write_text(
            "def main():\n    return 0\n", encoding="utf-8"
        )
    return pkg


_WORKFLOW = """\
name: publish
on:
  push:
    tags: ['foo-v*', 'bar-v*']
  workflow_dispatch:
    inputs:
      package:
        type: choice
        options:
          - foo
          - bar
jobs:
  resolve:
    steps:
      - run: |
          case "$base" in
            foo)  echo "dir=packages/foo"  >> "$GITHUB_OUTPUT"; echo "mod=devin_foo" ;;
            bar)  echo "dir=packages/bar"  >> "$GITHUB_OUTPUT"; echo "mod=devin_bar" ;;
          esac
"""


def write_repo(root: Path, names=("foo", "bar")) -> Path:
    """A mini fan-out repo: packages/<name>/ + the publish workflow."""
    for name in names:
        write_pkg(root / "packages" / name, name=f"devin-{name}")
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "pypi-publish.yml").write_text(_WORKFLOW, encoding="utf-8")
    return root


def _ok(_pkg: Path) -> None:
    return None


def _statuses(findings):
    return [f.status for f in findings]


# --- happy paths -----------------------------------------------------------


def test_single_package_all_pass(tmp_path):
    pkg = write_pkg(tmp_path / "mypkg")
    findings = publish.run(_ctx(pkg), build_probe=lambda: True, builder=_ok)
    assert findings, "expected findings"
    assert all(f.status is Status.PASS for f in findings)
    assert all(f.check == "publish-readiness" for f in findings)
    messages = "\n".join(f.message for f in findings)
    assert "devin-foo" in messages
    assert "__version__ == [project].version" in messages
    assert "console script" in messages
    assert "wheel builds" in messages


def test_fanout_repo_root(tmp_path):
    write_repo(tmp_path)
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, builder=_ok
    )
    assert all(f.status is Status.PASS for f in findings)
    labels = {f.message.split(":")[0] for f in findings}
    assert "packages/foo" in labels
    assert "packages/bar" in labels
    assert any("covers 'foo'" in f.message for f in findings)


def test_nothing_package_shaped_is_pass(tmp_path):
    findings = publish.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.PASS]
    assert "nothing to verify" in findings[0].message


def test_cwd_can_be_the_packages_container(tmp_path):
    write_repo(tmp_path)
    findings = publish.run(
        _ctx(tmp_path / "packages"),
        build_probe=lambda: True,
        builder=_ok,
    )
    assert all(f.status is Status.PASS for f in findings)
    assert {f.message.split(":")[0] for f in findings} == {"foo", "bar"}


# --- pyproject metadata ----------------------------------------------------


def test_missing_pyproject_fails(tmp_path):
    pkg = tmp_path / "noproj"
    (pkg / "src").mkdir(parents=True)  # package-shaped, no pyproject
    findings = publish.run(_ctx(pkg))
    assert _statuses(findings) == [Status.FAIL]
    assert "no pyproject.toml" in findings[0].message


def test_missing_pyproject_under_packages_fails(tmp_path):
    write_repo(tmp_path)
    (tmp_path / "packages" / "noproj").mkdir()
    findings = publish.run(_ctx(tmp_path))
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "packages/noproj" in f.message and "no pyproject.toml" in f.message
        for f in fails
    )


def test_invalid_toml_fails(tmp_path):
    pkg = tmp_path / "bad"
    pkg.mkdir()
    (pkg / "pyproject.toml").write_text("this is = = not toml\n")
    findings = publish.run(_ctx(pkg))
    assert _statuses(findings) == [Status.FAIL]
    assert "does not parse" in findings[0].message


def test_missing_name_and_version_fails(tmp_path):
    pkg = tmp_path / "noname"
    pkg.mkdir()
    (pkg / "pyproject.toml").write_text(
        '[project]\ndescription = "x"\n'
    )
    findings = publish.run(_ctx(pkg))
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "missing name" in f.message and "version" in f.message
        for f in fails
    ), findings


def test_missing_version_only_fails(tmp_path):
    pkg = write_pkg(tmp_path / "nover", version=None)
    findings = publish.run(_ctx(pkg))
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any("missing version" in f.message for f in fails)


# --- src layout / version sync ----------------------------------------------


def test_no_src_layout_names_flat_module(tmp_path):
    pkg = write_pkg(tmp_path / "flat", src_layout=False)
    (pkg / "devin_foo.py").write_text('__version__ = "1.2.3"\n')
    findings = publish.run(_ctx(pkg))
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any("no src/ layout" in f.message for f in fails)
    assert any("devin_foo.py" in f.message for f in fails)


def test_src_without_package_fails(tmp_path):
    pkg = write_pkg(tmp_path / "emptysrc")
    for p in (pkg / "src" / "devin_foo").iterdir():
        p.unlink()
    (pkg / "src" / "devin_foo").rmdir()
    (pkg / "src" / "notes.txt").write_text("not a package")
    findings = publish.run(_ctx(pkg))
    assert any(
        "holds no package" in f.message and f.status is Status.FAIL
        for f in findings
    )


def test_version_mismatch_fails_naming_both(tmp_path):
    pkg = write_pkg(tmp_path / "skew", version="1.2.3", init_version="9.9.9")
    findings = publish.run(_ctx(pkg))
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "9.9.9" in f.message and "1.2.3" in f.message for f in fails
    )


def test_missing_dunder_version_fails(tmp_path):
    pkg = write_pkg(tmp_path / "noversion", init_version=None)
    findings = publish.run(_ctx(pkg))
    assert any(
        "no __version__" in f.message and f.status is Status.FAIL
        for f in findings
    )


# --- console scripts --------------------------------------------------------


def test_script_module_missing_fails(tmp_path):
    pkg = write_pkg(
        tmp_path / "badscript",
        scripts={"devin-foo": "devin_foo.nope:main"},
    )
    findings = publish.run(_ctx(pkg))
    assert any(
        "'devin-foo'" in f.message and "devin_foo.nope" in f.message
        and f.status is Status.FAIL
        for f in findings
    )


def test_script_not_module_attr_fails(tmp_path):
    pkg = write_pkg(
        tmp_path / "badfmt", scripts={"devin-foo": "devin_foo.cli"}
    )
    findings = publish.run(_ctx(pkg))
    assert any(
        "'module:attr'" in f.message and f.status is Status.FAIL
        for f in findings
    )


def test_script_attr_not_found_warns(tmp_path):
    pkg = write_pkg(
        tmp_path / "noattr", scripts={"devin-foo": "devin_foo.cli:run"}
    )
    findings = publish.run(_ctx(pkg))
    assert any(
        "devin_foo.cli:run" in f.message and f.status is Status.WARN
        for f in findings
    )
    assert not any(f.status is Status.FAIL for f in findings)


def test_no_scripts_is_library_pass(tmp_path):
    pkg = write_pkg(tmp_path / "lib", scripts={})
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, builder=_ok
    )
    assert all(f.status is Status.PASS for f in findings)
    assert any("library-only" in f.message for f in findings)


# --- wheel build ------------------------------------------------------------


def test_build_failure_fails_with_detail(tmp_path):
    pkg = write_pkg(tmp_path / "brokenbuild")
    findings = publish.run(
        _ctx(pkg),
        build_probe=lambda: True,
        builder=lambda p: "setuptools died",
    )
    assert any(
        "wheel build failed" in f.message
        and "setuptools died" in f.message
        and f.status is Status.FAIL
        for f in findings
    )


def test_build_static_when_tooling_absent(tmp_path):
    pkg = write_pkg(tmp_path / "static")
    findings = publish.run(_ctx(pkg), build_probe=lambda: False)
    assert all(f.status is Status.PASS for f in findings)
    assert any("skipped" in f.message for f in findings)


def test_build_skipped_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVIN_DOCTOR_OFFLINE", "1")
    called = []

    def spy(_pkg):
        called.append(1)

    pkg = write_pkg(tmp_path / "offline")
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, builder=spy
    )
    assert called == []  # offline never spawns the builder
    assert any("offline mode" in f.message for f in findings)


def test_missing_build_system_fails(tmp_path):
    pkg = write_pkg(tmp_path / "nobs", build_system=False)
    findings = publish.run(_ctx(pkg), build_probe=lambda: False)
    assert any(
        "[build-system]" in f.message and f.status is Status.FAIL
        for f in findings
    )


# --- pypi-publish.yml fan-out coverage ---------------------------------------


def test_coverage_hit_passes(tmp_path):
    write_repo(tmp_path)
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, builder=_ok
    )
    assert any(
        "covers 'foo'" in f.message and f.status is Status.PASS
        for f in findings
    )


def test_coverage_miss_warns(tmp_path):
    root = write_repo(tmp_path)
    write_pkg(root / "packages" / "newpkg", name="devin-newpkg")
    findings = publish.run(
        _ctx(root), build_probe=lambda: True, builder=_ok
    )
    mine = [f for f in findings if f.message.startswith("packages/newpkg")]
    assert any(
        "not covered" in f.message and f.status is Status.WARN
        for f in mine
    )


def test_coverage_not_applicable_passes(tmp_path):
    # no .github/workflows/pypi-publish.yml above tmp_path
    pkg = write_pkg(tmp_path / "lone")
    findings = publish.run(_ctx(pkg), build_probe=lambda: False)
    assert any(
        "not applicable" in f.message and f.status is Status.PASS
        for f in findings
    )


# --- --package scoping ------------------------------------------------------


def test_package_scope_selects_one(tmp_path):
    write_repo(tmp_path)
    findings = publish.run(
        _ctx(tmp_path, packages=("foo",)),
        build_probe=lambda: True,
        builder=_ok,
    )
    assert findings
    assert {f.message.split(":")[0] for f in findings} == {"packages/foo"}


def test_package_scope_matches_project_name(tmp_path):
    write_repo(tmp_path)
    findings = publish.run(
        _ctx(tmp_path, packages=("devin-bar",)),
        build_probe=lambda: True,
        builder=_ok,
    )
    assert {f.message.split(":")[0] for f in findings} == {"packages/bar"}


def test_package_scope_unknown_name_warns(tmp_path):
    write_repo(tmp_path)
    findings = publish.run(
        _ctx(tmp_path, packages=("ghost",)),
        build_probe=lambda: True,
        builder=_ok,
    )
    assert _statuses(findings) == [Status.WARN]
    assert "ghost" in findings[0].message


def test_package_scope_on_empty_dir_warns(tmp_path):
    findings = publish.run(_ctx(tmp_path, packages=("ghost",)))
    assert _statuses(findings) == [Status.WARN]


# --- read-only guarantee -----------------------------------------------------


def _tree(pkg: Path) -> dict[str, str]:
    return {
        str(p.relative_to(pkg).as_posix()): p.read_bytes().decode(
            "utf-8", errors="replace"
        )
        for p in sorted(pkg.rglob("*"))
        if p.is_file()
    }


def test_check_is_readonly_against_package_dir(tmp_path):
    pkg = write_pkg(tmp_path / "frozen")
    before = _tree(pkg)
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, builder=_ok
    )
    assert _tree(pkg) == before
    assert not (pkg / "dist").exists()
    assert findings


# --- wiring / CLI ------------------------------------------------------------


def test_run_all_includes_check(ctx, tmp_path):
    # ctx.cwd is a bare tmp dir → the scope fallback PASS line
    report = doctor.run_all(ctx)
    assert "publish-readiness" in report.check_status()


def test_cli_single_package_exit_0(tmp_path, capsys):
    pkg = write_pkg(tmp_path / "mypkg")
    rc = main([
        "check", "--data-dir", str(tmp_path), "--cwd", str(pkg),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "publish-readiness" in out


def test_cli_broken_package_exit_1(tmp_path, capsys):
    pkg = write_pkg(tmp_path / "skew", version="1.2.3", init_version="9.9.9")
    rc = main([
        "check", "--data-dir", str(tmp_path), "--cwd", str(pkg),
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert "9.9.9" in out


def test_cli_json_lists_findings(tmp_path, capsys):
    pkg = write_pkg(tmp_path / "mypkg")
    rc = main([
        "check", "--data-dir", str(tmp_path), "--cwd", str(pkg),
        "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    mine = [
        f for f in payload["findings"]
        if f["check"] == "publish-readiness"
    ]
    assert mine and all(f["status"] == "PASS" for f in mine)


def test_cli_package_flag_scopes(tmp_path, capsys):
    write_repo(tmp_path)
    rc = main([
        "check", "--data-dir", str(tmp_path), "--cwd", str(tmp_path),
        "--package", "foo", "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    mine = [
        f for f in payload["findings"]
        if f["check"] == "publish-readiness"
    ]
    assert mine
    assert all(f["message"].startswith("packages/foo") for f in mine)


# --- real wheel build (only where the tooling exists) ------------------------


_BUILD_TOOLS = (
    importlib.util.find_spec("build") is not None
    and importlib.util.find_spec("setuptools") is not None
)


@pytest.mark.skipif(
    not _BUILD_TOOLS, reason="python -m build + setuptools not installed"
)
def test_real_wheel_build_stages_a_copy(tmp_path):
    """End-to-end: the real builder copies the package to a temp dir and
    produces a wheel without touching the source tree."""
    pkg = write_pkg(tmp_path / "realpkg")
    before = _tree(pkg)
    findings = publish.run(_ctx(pkg), build_probe=lambda: True)
    assert any(
        "wheel builds" in f.message and f.status is Status.PASS
        for f in findings
    ), [f.message for f in findings]
    assert _tree(pkg) == before  # read-only against the package dir
    assert not (pkg / "dist").exists()


def test_builder_called_with_package_dir(tmp_path):
    """Regression: the builder receives the package dir, not cwd — the
    copy step is what keeps the repo read-only."""
    pkg = write_pkg(tmp_path / "seen")
    seen = []
    findings = publish.run(
        _ctx(pkg),
        build_probe=lambda: True,
        builder=lambda p: seen.append(p) or None,
    )
    assert seen == [pkg]
    assert findings
