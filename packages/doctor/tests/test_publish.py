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
    requires: str = "setuptools>=68",
    dynamic: list[str] | None = None,
    gui_scripts: dict[str, str] | None = None,
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
            f'requires = ["{requires}"]',
            'build-backend = "setuptools.build_meta"',
            "",
        ]
    lines += ["[project]", f'name = "{name}"']
    if version is not None:
        lines.append(f'version = "{version}"')
    if dynamic:
        items = ", ".join(f'"{d}"' for d in dynamic)
        lines.append(f"dynamic = [{items}]")
    if scripts:
        lines += ["", "[project.scripts]"]
        lines += [f'{k} = "{v}"' for k, v in scripts.items()]
    if gui_scripts:
        lines += ["", "[project.gui-scripts]"]
        lines += [f'{k} = "{v}"' for k, v in gui_scripts.items()]
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


def _ok(_pkg: Path, _expect: publish._BuildExpect) -> None:
    return None


def _statuses(findings):
    return [f.status for f in findings]


# --- happy paths -----------------------------------------------------------


def test_single_package_all_pass(tmp_path):
    pkg = write_pkg(tmp_path / "mypkg")
    findings = publish.run(_ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok)
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
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert all(f.status is Status.PASS for f in findings)
    labels = {f.message.split(":")[0] for f in findings}
    assert "packages/foo" in labels
    assert "packages/bar" in labels
    assert any("covers 'foo'" in f.message for f in findings)


def test_nothing_package_shaped_is_pass(tmp_path):
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert _statuses(findings) == [Status.PASS]
    assert "nothing to verify" in findings[0].message


def test_cwd_can_be_the_packages_container(tmp_path):
    write_repo(tmp_path)
    findings = publish.run(
        _ctx(tmp_path / "packages"),
        build_probe=lambda: True, dist_version=lambda _n: None,
        builder=_ok,
    )
    assert all(f.status is Status.PASS for f in findings)
    assert {f.message.split(":")[0] for f in findings} == {"foo", "bar"}


# --- pyproject metadata ----------------------------------------------------


def test_missing_pyproject_fails(tmp_path):
    pkg = tmp_path / "noproj"
    # An in-progress Python package: src/<mod>/__init__.py but no
    # pyproject — the check must still FAIL it.
    mod = pkg / "src" / "noproj"
    mod.mkdir(parents=True)
    (mod / "__init__.py").write_text("", encoding="utf-8")
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert _statuses(findings) == [Status.FAIL]
    assert "no pyproject.toml" in findings[0].message


def test_missing_pyproject_under_packages_fails(tmp_path):
    write_repo(tmp_path)
    noproj = tmp_path / "packages" / "noproj"
    (noproj / "src" / "noproj").mkdir(parents=True)
    (noproj / "src" / "noproj" / "__init__.py").write_text("", encoding="utf-8")
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "packages/noproj" in f.message and "no pyproject.toml" in f.message
        for f in fails
    )


# --- non-Python packages/* members are skipped -----------------------------


def test_nonpython_member_skipped(tmp_path):
    """An npm/VS-Code-style member (package.json + src/*.ts, no .py) is
    not a PyPI candidate — PASS note, never a pyproject FAIL."""
    write_repo(tmp_path)
    bridge = tmp_path / "packages" / "bridge"
    (bridge / "src").mkdir(parents=True)
    (bridge / "src" / "index.ts").write_text("export {}\n", encoding="utf-8")
    (bridge / "package.json").write_text("{}\n", encoding="utf-8")
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert not any("packages/bridge" in f.message for f in fails)
    assert any(
        "packages/bridge" in f.message and "not a Python package" in f.message
        for f in findings
    )


def test_empty_member_dir_skipped(tmp_path):
    """An empty packages/ dir carries no publish intent either."""
    write_repo(tmp_path)
    (tmp_path / "packages" / "empty").mkdir()
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert not any(f.status is Status.FAIL for f in findings)


def test_setup_py_signals_still_fails_without_pyproject(tmp_path):
    """A setup.py legacy package can't ship through the pyproject-only
    fan-out — the missing-pyproject FAIL still applies."""
    write_repo(tmp_path)
    legacy = tmp_path / "packages" / "legacy"
    legacy.mkdir()
    (legacy / "setup.py").write_text("from setuptools import setup\nsetup()\n", encoding="utf-8")
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "packages/legacy" in f.message and "no pyproject.toml" in f.message
        for f in fails
    )


def test_flat_layout_member_still_fails(tmp_path):
    """``packages/tool/tool/__init__.py`` with no project metadata is a
    flat-layout package in progress — FAIL, not skipped."""
    write_repo(tmp_path)
    mod = tmp_path / "packages" / "tool" / "tool"
    mod.mkdir(parents=True)
    (mod / "__init__.py").write_text("", encoding="utf-8")
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "packages/tool" in f.message and "no pyproject.toml" in f.message
        for f in fails
    )


def test_nested_namespace_member_still_fails(tmp_path):
    """``src/acme/tools/__init__.py`` — a deeper src nesting — still
    counts as Python intent."""
    write_repo(tmp_path)
    ns = tmp_path / "packages" / "ns" / "src" / "acme" / "tools"
    ns.mkdir(parents=True)
    (ns / "__init__.py").write_text("", encoding="utf-8")
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "packages/ns" in f.message and "no pyproject.toml" in f.message
        for f in fails
    )


def test_dangling_pyproject_symlink_fails(tmp_path):
    """A broken ``pyproject.toml`` symlink is publish intent gone wrong
    — the metadata leg reports it instead of the skip hiding it."""
    write_repo(tmp_path)
    broken = tmp_path / "packages" / "broken"
    broken.mkdir()
    (broken / "pyproject.toml").symlink_to(broken / "gone.toml")
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any("packages/broken" in f.message for f in fails)


def test_publish_target_never_skipped(tmp_path):
    """A resolve arm mapping ``dir=packages/<name>`` makes the member a
    publish target: gutted of Python files it must FAIL, not skip."""
    write_repo(tmp_path)
    gutted = tmp_path / "packages" / "foo"
    for child in list(gutted.iterdir()):
        if child.is_dir():
            import shutil
            shutil.rmtree(child)
        else:
            child.unlink()
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "packages/foo" in f.message and "no pyproject.toml" in f.message
        for f in fails
    )


def test_helper_script_next_to_package_json_skips(tmp_path):
    """A lone ``clean.py`` beside ``package.json`` is npm tooling, not a
    single-module distribution — the member stays skipped."""
    write_repo(tmp_path)
    member = tmp_path / "packages" / "npm-tools"
    member.mkdir()
    (member / "package.json").write_text("{}\n", encoding="utf-8")
    (member / "clean.py").write_text(
        "import shutil\nshutil.rmtree('dist', ignore_errors=True)\n",
        encoding="utf-8",
    )
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert not any("packages/npm-tools" in f.message for f in fails)
    assert any(
        "packages/npm-tools" in f.message
        and "not a Python package" in f.message
        for f in findings
    )


def test_lone_module_without_manifest_still_fails(tmp_path):
    """A lone top-level ``.py`` with no foreign manifest is a
    single-module flat distribution in progress — FAIL, not skipped."""
    write_repo(tmp_path)
    solo = tmp_path / "packages" / "solo"
    solo.mkdir()
    (solo / "solo.py").write_text(
        "__version__ = '0.1'\n", encoding="utf-8"
    )
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "packages/solo" in f.message and "no pyproject.toml" in f.message
        for f in fails
    )


def test_import_package_dir_beats_foreign_manifest(tmp_path):
    """``package.json`` only mutes loose root scripts — a real
    import-package dir still marks the member Python."""
    write_repo(tmp_path)
    hybrid = tmp_path / "packages" / "hybrid"
    (hybrid / "hybrid").mkdir(parents=True)
    (hybrid / "package.json").write_text("{}\n", encoding="utf-8")
    (hybrid / "hybrid" / "__init__.py").write_text("", encoding="utf-8")
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "packages/hybrid" in f.message and "no pyproject.toml" in f.message
        for f in fails
    )


def test_invalid_toml_fails(tmp_path):
    pkg = tmp_path / "bad"
    pkg.mkdir()
    (pkg / "pyproject.toml").write_text("this is = = not toml\n")
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert _statuses(findings) == [Status.FAIL]
    assert "does not parse" in findings[0].message


def test_missing_name_and_version_fails(tmp_path):
    pkg = tmp_path / "noname"
    pkg.mkdir()
    (pkg / "pyproject.toml").write_text(
        '[project]\ndescription = "x"\n'
    )
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "missing name" in f.message and "version" in f.message
        for f in fails
    ), findings


def test_missing_version_only_fails(tmp_path):
    pkg = write_pkg(tmp_path / "nover", version=None)
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any("missing version" in f.message for f in fails)


def test_dynamic_version_does_not_exempt_fails(tmp_path):
    """Regression (review): dynamic = ["version"] used to waive the
    missing literal version. This fan-out's verify gate compares a built
    wheel against [project].version — absent means FAIL either way."""
    pkg = write_pkg(tmp_path / "dynver", version=None, dynamic=["version"])
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any("missing version" in f.message for f in fails), findings
    # and no stray WARN pretending the dynamic version was checked
    assert not any(
        "dynamic" in f.message and f.status is Status.WARN
        for f in findings
    )


# --- src layout / version sync ----------------------------------------------


def test_no_src_layout_names_flat_module(tmp_path):
    pkg = write_pkg(tmp_path / "flat", src_layout=False)
    (pkg / "devin_foo.py").write_text('__version__ = "1.2.3"\n')
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any("no src/ layout" in f.message for f in fails)
    assert any("devin_foo.py" in f.message for f in fails)


def test_src_without_package_fails(tmp_path):
    pkg = write_pkg(tmp_path / "emptysrc")
    for p in (pkg / "src" / "devin_foo").iterdir():
        p.unlink()
    (pkg / "src" / "devin_foo").rmdir()
    (pkg / "src" / "notes.txt").write_text("not a package")
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert any(
        "holds no package" in f.message and f.status is Status.FAIL
        for f in findings
    )


def test_version_mismatch_fails_naming_both(tmp_path):
    pkg = write_pkg(tmp_path / "skew", version="1.2.3", init_version="9.9.9")
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "9.9.9" in f.message and "1.2.3" in f.message for f in fails
    )


def test_missing_dunder_version_fails(tmp_path):
    pkg = write_pkg(tmp_path / "noversion", init_version=None)
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
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
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert any(
        "'devin-foo'" in f.message and "devin_foo.nope" in f.message
        and f.status is Status.FAIL
        for f in findings
    )


def test_script_not_module_attr_fails(tmp_path):
    pkg = write_pkg(
        tmp_path / "badfmt", scripts={"devin-foo": "devin_foo.cli"}
    )
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert any(
        "'module:attr'" in f.message and f.status is Status.FAIL
        for f in findings
    )


def test_script_attr_not_found_warns(tmp_path):
    pkg = write_pkg(
        tmp_path / "noattr", scripts={"devin-foo": "devin_foo.cli:run"}
    )
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert any(
        "devin_foo.cli:run" in f.message and f.status is Status.WARN
        for f in findings
    )
    assert not any(f.status is Status.FAIL for f in findings)


def test_no_scripts_is_library_pass(tmp_path):
    pkg = write_pkg(tmp_path / "lib", scripts={})
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert all(f.status is Status.PASS for f in findings)
    assert any("library-only" in f.message for f in findings)


# --- wheel build ------------------------------------------------------------


def test_build_failure_fails_with_detail(tmp_path):
    pkg = write_pkg(tmp_path / "brokenbuild")
    findings = publish.run(
        _ctx(pkg),
        build_probe=lambda: True, dist_version=lambda _n: None,
        builder=lambda _p, _e: "setuptools died",
    )
    assert any(
        "wheel build failed" in f.message
        and "setuptools died" in f.message
        and f.status is Status.FAIL
        for f in findings
    )


def test_build_unverified_when_tooling_absent(tmp_path):
    """No local build tooling → WARN 'unverified', never a PASS that
    pretends the wheel was exercised."""
    pkg = write_pkg(tmp_path / "static")
    findings = publish.run(_ctx(pkg), build_probe=lambda: False)
    assert any(
        "unverified" in f.message and f.status is Status.WARN
        for f in findings
    )
    assert not any("wheel builds" in f.message for f in findings)


def test_build_runs_offline_when_tooling_exists(tmp_path, monkeypatch):
    """Regression (review): offline mode used to skip the build leg,
    letting broken wheels PASS the offline gate. --no-isolation needs
    no network, so offline must still build."""
    monkeypatch.setenv("DEVIN_DOCTOR_OFFLINE", "1")
    called = []

    def spy(_pkg, _expect):
        called.append(1)
        return "boom"

    pkg = write_pkg(tmp_path / "offline")
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=spy
    )
    assert called == [1]
    assert any(
        "wheel build failed" in f.message and f.status is Status.FAIL
        for f in findings
    )


def test_no_wheel_build_env_optout(tmp_path, monkeypatch):
    """DEVIN_DOCTOR_NO_WHEEL_BUILD=1 documents the trust boundary —
    PEP 517 hooks execute the checkout's backend — and reports WARN."""
    monkeypatch.setenv("DEVIN_DOCTOR_NO_WHEEL_BUILD", "1")
    called = []

    def spy(_pkg, _expect):
        called.append(1)

    pkg = write_pkg(tmp_path / "optout")
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=spy
    )
    assert called == []
    assert any(
        "unverified" in f.message
        and "DEVIN_DOCTOR_NO_WHEEL_BUILD" in f.message
        and f.status is Status.WARN
        for f in findings
    )


def test_old_local_backend_warns_unverified(tmp_path):
    """A local setuptools older than the declared requires floor would
    fail the no-isolation build for environment reasons — report the
    env problem, not a package FAIL."""
    pkg = write_pkg(tmp_path / "oldback", requires="setuptools>=68")
    findings = publish.run(
        _ctx(pkg),
        build_probe=lambda: True,
        builder=lambda _p, _e: None,
        dist_version=lambda _name: "61.0",
    )
    assert any(
        "unverified" in f.message
        and "setuptools 61.0" in f.message
        and f.status is Status.WARN
        for f in findings
    ), [f.message for f in findings]


def test_missing_build_system_fails(tmp_path):
    pkg = write_pkg(tmp_path / "nobs", build_system=False)
    findings = publish.run(_ctx(pkg), build_probe=lambda: False)
    assert any(
        "[build-system]" in f.message and f.status is Status.FAIL
        for f in findings
    )


# --- wheel artifact contents -------------------------------------------------


def _fake_wheel(path: Path, members: dict[str, str]) -> Path:
    import zipfile

    with zipfile.ZipFile(path, "w") as zf:
        for arc, body in members.items():
            zf.writestr(arc, body)
    return path


def test_wheel_missing_module_fails(tmp_path):
    """Regression (review): a wheel that built but shipped without the
    declared import module used to PASS — existence was all we checked."""
    wheel = _fake_wheel(
        tmp_path / "devin_foo-1.2.3-py3-none-any.whl",
        {"other/__init__.py": ""},
    )
    err = publish._wheel_errors(
        wheel, publish._BuildExpect(modules=("devin_foo",))
    )
    assert err is not None and "devin_foo" in err


def test_wheel_version_mismatch_fails(tmp_path):
    wheel = _fake_wheel(
        tmp_path / "devin_foo-9.9.9-py3-none-any.whl",
        {"devin_foo/__init__.py": '__version__ = "9.9.9"\n'},
    )
    err = publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo",),
            init_module="devin_foo",
            version="1.2.3",
        ),
    )
    assert err is not None and "9.9.9" in err and "1.2.3" in err


def test_wheel_missing_script_module_fails(tmp_path):
    wheel = _fake_wheel(
        tmp_path / "devin_foo-1.2.3-py3-none-any.whl",
        {"devin_foo/__init__.py": '__version__ = "1.2.3"\n'},
    )
    err = publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo", "devin_foo.cli"),
            init_module="devin_foo",
            version="1.2.3",
        ),
    )
    assert err is not None and "devin_foo.cli" in err


def test_wheel_ok_returns_none(tmp_path):
    wheel = _fake_wheel(
        tmp_path / "devin_foo-1.2.3-py3-none-any.whl",
        {
            "devin_foo/__init__.py": '__version__ = "1.2.3"\n',
            "devin_foo/cli.py": "def main():\n    return 0\n",
            "devin_foo-1.2.3.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: devin-foo\nVersion: 1.2.3\n"
            ),
            "devin_foo-1.2.3.dist-info/entry_points.txt": (
                "[console_scripts]\ndevin-foo = devin_foo.cli:main\n"
            ),
        },
    )
    assert publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo", "devin_foo.cli"),
            init_module="devin_foo",
            version="1.2.3",
            dist_name="devin-foo",
            scripts=(
                ("console_scripts", "devin-foo", "devin_foo.cli:main"),
            ),
        ),
    ) is None


def test_wheel_entry_point_whitespace_in_attr_fails(tmp_path):
    """Regression (re-review): whitespace INSIDE the callable is a
    broken target, not an alternate spelling — ``cli:m ain`` can never
    resolve ``main``. Only spacing around the colon normalizes."""
    wheel = _fake_wheel(
        tmp_path / "w.whl",
        {
            "devin_foo/__init__.py": '__version__ = "1.2.3"\n',
            "devin_foo/cli.py": "def main():\n    return 0\n",
            "devin_foo-1.2.3.dist-info/METADATA": (
                "Name: devin-foo\nVersion: 1.2.3\n"
            ),
            "devin_foo-1.2.3.dist-info/entry_points.txt": (
                "[console_scripts]\ndevin-foo = devin_foo.cli:m ain\n"
            ),
        },
    )
    err = publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo", "devin_foo.cli"),
            init_module="devin_foo",
            version="1.2.3",
            dist_name="devin-foo",
            scripts=(
                ("console_scripts", "devin-foo", "devin_foo.cli:main"),
            ),
        ),
    )
    assert err is not None and "devin-foo" in err and "points to" in err


def test_wheel_entry_point_spaced_colon_passes(tmp_path):
    """Spacing around the colon is legal entry-point syntax and must
    still normalize away."""
    wheel = _fake_wheel(
        tmp_path / "w.whl",
        {
            "devin_foo/__init__.py": '__version__ = "1.2.3"\n',
            "devin_foo/cli.py": "def main():\n    return 0\n",
            "devin_foo-1.2.3.dist-info/METADATA": (
                "Name: devin-foo\nVersion: 1.2.3\n"
            ),
            "devin_foo-1.2.3.dist-info/entry_points.txt": (
                "[console_scripts]\ndevin-foo = devin_foo.cli : main\n"
            ),
        },
    )
    assert publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo", "devin_foo.cli"),
            init_module="devin_foo",
            version="1.2.3",
            dist_name="devin-foo",
            scripts=(
                ("console_scripts", "devin-foo", "devin_foo.cli:main"),
            ),
        ),
    ) is None


def test_wheel_version_exact_compare_matches_gate(tmp_path):
    """Regression (re-review): the publish gate asserts
    ``__version__ == [project].version`` literally — '1.0' vs '1.0.0'
    are PEP 440-equal but gate-unequal, so readiness must FAIL too."""
    wheel = _fake_wheel(
        tmp_path / "w.whl",
        {
            "devin_foo/__init__.py": '__version__ = "1.0"\n',
            "devin_foo-1.0.0.dist-info/METADATA": (
                "Name: devin-foo\nVersion: 1.0.0\n"
            ),
        },
    )
    err = publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo",),
            init_module="devin_foo",
            version="1.0.0",
            dist_name="devin-foo",
        ),
    )
    assert err is not None and "__version__" in err


def test_wheel_entry_point_wrong_group_fails(tmp_path):
    """Regression (re-review): a console script landing in [gui_scripts]
    means no console launcher is installed — name-alone matching missed
    it."""
    wheel = _fake_wheel(
        tmp_path / "w.whl",
        {
            "devin_foo/__init__.py": '__version__ = "1.2.3"\n',
            "devin_foo/cli.py": "def main():\n    return 0\n",
            "devin_foo-1.2.3.dist-info/METADATA": (
                "Name: devin-foo\nVersion: 1.2.3\n"
            ),
            "devin_foo-1.2.3.dist-info/entry_points.txt": (
                "[gui_scripts]\ndevin-foo = devin_foo.cli:main\n"
            ),
        },
    )
    err = publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo", "devin_foo.cli"),
            init_module="devin_foo",
            version="1.2.3",
            dist_name="devin-foo",
            scripts=(
                ("console_scripts", "devin-foo", "devin_foo.cli:main"),
            ),
        ),
    )
    assert err is not None and "gui_scripts" in err


def test_wheel_entry_point_wrong_target_fails(tmp_path):
    """Same name, different callable — the installed command runs the
    wrong function."""
    wheel = _fake_wheel(
        tmp_path / "w.whl",
        {
            "devin_foo/__init__.py": '__version__ = "1.2.3"\n',
            "devin_foo/cli.py": "def main():\n    return 0\n",
            "devin_foo-1.2.3.dist-info/METADATA": (
                "Name: devin-foo\nVersion: 1.2.3\n"
            ),
            "devin_foo-1.2.3.dist-info/entry_points.txt": (
                "[console_scripts]\ndevin-foo = devin_foo.cli:other\n"
            ),
        },
    )
    err = publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo", "devin_foo.cli"),
            init_module="devin_foo",
            version="1.2.3",
            dist_name="devin-foo",
            scripts=(
                ("console_scripts", "devin-foo", "devin_foo.cli:main"),
            ),
        ),
    )
    assert err is not None and "cli:other" in err


def test_wheel_metadata_missing_name_header_fails(tmp_path):
    """Regression (re-review): a METADATA file without a Name header
    used to skip the comparison and pass."""
    wheel = _fake_wheel(
        tmp_path / "w.whl",
        {
            "devin_foo/__init__.py": '__version__ = "1.2.3"\n',
            "devin_foo-1.2.3.dist-info/METADATA": (
                "Metadata-Version: 2.1\nVersion: 1.2.3\n"
            ),
        },
    )
    err = publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo",),
            init_module="devin_foo",
            version="1.2.3",
            dist_name="devin-foo",
        ),
    )
    assert err is not None and "Name" in err


def test_wheel_metadata_missing_version_header_fails(tmp_path):
    wheel = _fake_wheel(
        tmp_path / "w.whl",
        {
            "devin_foo/__init__.py": '__version__ = "1.2.3"\n',
            "devin_foo-1.2.3.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: devin-foo\n"
            ),
        },
    )
    err = publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo",),
            init_module="devin_foo",
            version="1.2.3",
            dist_name="devin-foo",
        ),
    )
    assert err is not None and "Version" in err


def test_wheel_prerelease_version_passes(tmp_path):
    """Regression (re-review): '1.0.0-rc1' in pyproject is the same
    release as normalized '1.0.0rc1' in METADATA — PEP 440, not raw
    string equality."""
    wheel = _fake_wheel(
        tmp_path / "w.whl",
        {
            "devin_foo/__init__.py": '__version__ = "1.0.0-rc1"\n',
            "devin_foo-1.0.0rc1.dist-info/METADATA": (
                "Name: devin-foo\nVersion: 1.0.0rc1\n"
            ),
        },
    )
    assert publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo",),
            init_module="devin_foo",
            version="1.0.0-rc1",
            dist_name="devin-foo",
        ),
    ) is None


def test_gui_script_dead_callable_fails(tmp_path):
    """Regression (re-review): gui-scripts targets get the same
    module+attr verification as console scripts."""
    pkg = write_pkg(
        tmp_path / "guimod",
        scripts={"devin-foo": "devin_foo.cli:main"},
        gui_scripts={"devin-foo-gui": "devin_foo.nope:main"},
    )
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert any(
        "'devin-foo-gui'" in f.message
        and "devin_foo.nope" in f.message
        and f.status is Status.FAIL
        for f in findings
    ), [f.message for f in findings]


def test_gui_script_missing_attr_warns(tmp_path):
    pkg = write_pkg(
        tmp_path / "guiattr",
        scripts={"devin-foo": "devin_foo.cli:main"},
        gui_scripts={"devin-foo-gui": "devin_foo.cli:run"},
    )
    findings = publish.run(
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert any(
        "devin_foo.cli:run" in f.message and f.status is Status.WARN
        for f in findings
    )
    assert not any(f.status is Status.FAIL for f in findings)


def test_wheel_metadata_name_mismatch_fails(tmp_path):
    """Regression (re-review): dist metadata was unchecked — a wheel
    naming a different project passed."""
    wheel = _fake_wheel(
        tmp_path / "w.whl",
        {
            "devin_foo/__init__.py": '__version__ = "1.2.3"\n',
            "devin_bar-1.2.3.dist-info/METADATA": (
                "Name: devin-bar\nVersion: 1.2.3\n"
            ),
        },
    )
    err = publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo",),
            init_module="devin_foo",
            version="1.2.3",
            dist_name="devin-foo",
        ),
    )
    assert err is not None and "devin-bar" in err and "devin-foo" in err


def test_wheel_metadata_version_mismatch_fails(tmp_path):
    wheel = _fake_wheel(
        tmp_path / "w.whl",
        {
            "devin_foo/__init__.py": '__version__ = "1.2.3"\n',
            "devin_foo-9.9.9.dist-info/METADATA": (
                "Name: devin-foo\nVersion: 9.9.9\n"
            ),
        },
    )
    err = publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo",),
            init_module="devin_foo",
            version="1.2.3",
            dist_name="devin-foo",
        ),
    )
    assert err is not None and "9.9.9" in err and "1.2.3" in err


def test_wheel_missing_dist_info_fails(tmp_path):
    wheel = _fake_wheel(
        tmp_path / "w.whl",
        {"devin_foo/__init__.py": '__version__ = "1.2.3"\n'},
    )
    err = publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo",),
            init_module="devin_foo",
            version="1.2.3",
            dist_name="devin-foo",
        ),
    )
    assert err is not None and "METADATA" in err


def test_wheel_entry_point_missing_fails(tmp_path):
    wheel = _fake_wheel(
        tmp_path / "w.whl",
        {
            "devin_foo/__init__.py": '__version__ = "1.2.3"\n',
            "devin_foo/cli.py": "def main():\n    return 0\n",
            "devin_foo-1.2.3.dist-info/METADATA": (
                "Name: devin-foo\nVersion: 1.2.3\n"
            ),
            "devin_foo-1.2.3.dist-info/entry_points.txt": "",
        },
    )
    err = publish._wheel_errors(
        wheel,
        publish._BuildExpect(
            modules=("devin_foo", "devin_foo.cli"),
            init_module="devin_foo",
            version="1.2.3",
            dist_name="devin-foo",
            scripts=(
                ("console_scripts", "devin-foo", "devin_foo.cli:main"),
            ),
        ),
    )
    assert err is not None and "entry_points" in err and "devin-foo" in err


# --- pypi-publish.yml fan-out coverage ---------------------------------------


def test_coverage_hit_passes(tmp_path):
    write_repo(tmp_path)
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert any(
        "covers 'foo'" in f.message and f.status is Status.PASS
        for f in findings
    )


def test_coverage_miss_warns(tmp_path):
    root = write_repo(tmp_path)
    write_pkg(root / "packages" / "newpkg", name="devin-newpkg")
    findings = publish.run(
        _ctx(root), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    mine = [f for f in findings if f.message.startswith("packages/newpkg")]
    assert any(
        "not covered" in f.message and f.status is Status.WARN
        for f in mine
    )


def test_dispatch_option_without_arm_warns(tmp_path):
    """Regression (review): coverage used to union dispatch options with
    resolve arms, so a package listed in options but with no arm read as
    covered — publishing it would exit 'unknown package'."""
    root = tmp_path
    write_pkg(root / "packages" / "foo", name="devin-foo")
    write_pkg(root / "packages" / "ghost", name="devin-ghost")
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "pypi-publish.yml").write_text(
        """name: publish
on:
  push:
    tags: ['foo-v*', 'ghost-v*']
  workflow_dispatch:
    inputs:
      package:
        type: choice
        options:
          - foo
          - ghost
jobs:
  resolve:
    steps:
      - run: |
          case "$base" in
            foo)  echo "dir=packages/foo" >> "$GITHUB_OUTPUT"; echo "mod=devin_foo" ;;
          esac
""",
        encoding="utf-8",
    )
    findings = publish.run(
        _ctx(root), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    mine = [f for f in findings if f.message.startswith("packages/ghost")]
    assert any(
        "no resolve arm" in f.message and f.status is Status.WARN
        for f in mine
    ), [f.message for f in mine]
    assert not any(
        "resolve covers 'ghost'" in f.message for f in mine
    )


def test_arm_wrong_module_warns(tmp_path):
    """Regression (review): an arm mapping the right dir but the wrong
    mod makes the verify job check a module that isn't the package's."""
    root = tmp_path
    write_pkg(root / "packages" / "foo", name="devin-foo")
    wf = root / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "pypi-publish.yml").write_text(
        """name: publish
jobs:
  resolve:
    steps:
      - run: |
          case "$base" in
            foo)  echo "dir=packages/foo" >> "$GITHUB_OUTPUT"; echo "mod=devin_wrong" ;;
          esac
""",
        encoding="utf-8",
    )
    findings = publish.run(
        _ctx(root), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    mine = [f for f in findings if f.message.startswith("packages/foo")]
    assert any(
        "mod=devin_wrong" in f.message
        and "devin_foo" in f.message
        and f.status is Status.WARN
        for f in mine
    ), [f.message for f in mine]


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
        build_probe=lambda: True, dist_version=lambda _n: None,
        builder=_ok,
    )
    assert findings
    assert {f.message.split(":")[0] for f in findings} == {"packages/foo"}


def test_package_scope_matches_project_name(tmp_path):
    write_repo(tmp_path)
    findings = publish.run(
        _ctx(tmp_path, packages=("devin-bar",)),
        build_probe=lambda: True, dist_version=lambda _n: None,
        builder=_ok,
    )
    assert {f.message.split(":")[0] for f in findings} == {"packages/bar"}


def test_package_scope_unknown_name_warns(tmp_path):
    write_repo(tmp_path)
    findings = publish.run(
        _ctx(tmp_path, packages=("ghost",)),
        build_probe=lambda: True, dist_version=lambda _n: None,
        builder=_ok,
    )
    assert _statuses(findings) == [Status.WARN]
    assert "ghost" in findings[0].message


def test_package_scope_on_empty_dir_warns(tmp_path):
    findings = publish.run(
        _ctx(tmp_path, packages=("ghost",)),
        build_probe=lambda: True, dist_version=lambda _n: None,
        builder=_ok,
    )
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
        _ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    assert _tree(pkg) == before
    assert not (pkg / "dist").exists()
    assert findings


# --- wiring / CLI ------------------------------------------------------------


def test_run_all_includes_check(ctx, tmp_path):
    # ctx.cwd is a bare tmp dir → the scope fallback PASS line
    report = doctor.run_all(ctx)
    assert "publish-readiness" in report.check_status()


def _devin_foo_on_path(tmp_path: Path, monkeypatch) -> None:
    """Put a `devin-foo` executable on PATH so `path-check` stays green.

    The shared `check` command runs every devin-environment check, so a
    synthetic package with a declared `devin-foo` console script would
    otherwise fail `path-check` (not installed) inside publish tests.
    """
    bindir = tmp_path / "path-bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "devin-foo"
    script.write_text("#!/bin/sh\nexit 0\n")
    import os

    os.chmod(script, 0o755)  # shutil.which requires X_OK on POSIX
    (bindir / "devin-foo.cmd").write_text("@exit /b 0\r\n")
    monkeypatch.setenv("PATH", str(bindir))


def test_cli_single_package_exit_0(tmp_path, monkeypatch, capsys):
    _devin_foo_on_path(tmp_path, monkeypatch)
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


def test_cli_json_lists_findings(tmp_path, monkeypatch, capsys):
    _devin_foo_on_path(tmp_path, monkeypatch)
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
    # the build leg is PASS where build+setuptools exist, WARN
    # 'unverified' elsewhere — never FAIL on this healthy package
    assert mine and all(f["status"] != "FAIL" for f in mine)
    assert any(f["status"] == "PASS" for f in mine)


def test_cli_package_flag_scopes(tmp_path, monkeypatch, capsys):
    _devin_foo_on_path(tmp_path, monkeypatch)
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
    findings = publish.run(_ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None)
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
        build_probe=lambda: True, dist_version=lambda _n: None,
        builder=lambda p, _e: seen.append(p) or None,
    )
    assert seen == [pkg]
    assert findings


@pytest.mark.skipif(
    not _BUILD_TOOLS, reason="python -m build + setuptools not installed"
)
def test_real_wheel_missing_declared_module_fails(tmp_path):
    """End-to-end regression: the wheel builds fine but ships without a
    declared script module — the artifact check must FAIL it."""
    pkg = write_pkg(
        tmp_path / "halfbaked",
        scripts={"devin-foo": "devin_foo.cli:main",
                 "devin-foo-alt": "devin_foo.missing:main"},
    )
    findings = publish.run(_ctx(pkg), build_probe=lambda: True, dist_version=lambda _n: None)
    assert any(
        "does not contain" in f.message
        and "devin_foo.missing" in f.message
        and f.status is Status.FAIL
        for f in findings
    ), [f.message for f in findings]


def test_hybrid_member_module_still_checked(tmp_path):
    """``widget/widget.py`` beside ``package.json`` is a hybrid single-
    module distribution — the JS manifest does not hide it."""
    write_repo(tmp_path)
    widget = tmp_path / "packages" / "widget"
    widget.mkdir()
    (widget / "package.json").write_text("{}\n", encoding="utf-8")
    (widget / "widget.py").write_text(
        "__version__ = '0.1'\n", encoding="utf-8"
    )
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "packages/widget" in f.message and "no pyproject.toml" in f.message
        for f in fails
    )


def test_broken_manifest_does_not_prove_foreign(tmp_path):
    """An unparseable ``package.json`` proves nothing — a loose ``.py``
    beside it is still a Python signal, not npm tooling."""
    write_repo(tmp_path)
    member = tmp_path / "packages" / "npm-tools"
    member.mkdir()
    (member / "package.json").write_text("{oops\n", encoding="utf-8")
    (member / "clean.py").write_text(
        "import shutil\n", encoding="utf-8"
    )
    findings = publish.run(
        _ctx(tmp_path), build_probe=lambda: True, dist_version=lambda _n: None, builder=_ok
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert any(
        "packages/npm-tools" in f.message and "no pyproject.toml" in f.message
        for f in fails
    )
