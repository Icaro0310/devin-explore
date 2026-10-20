"""deps-internals: silently stale internal deps inside <cwd>/.venv.

Checkouts and venvs are synthetic tmp trees — real .pth / dist-info /
pyproject files, never a real interpreter or installed package. The
site-packages resolver, the .pth target reader and the dist index are
all injectable too.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from devin_doctor import doctor
from devin_doctor.checks import deps_internals
from devin_doctor.cli import main
from devin_doctor.model import Context, Status


def _ctx(cwd: Path) -> Context:
    return Context(data_dir=cwd, cwd=cwd)


def write_pkg(
    root: Path,
    name: str,
    *,
    project: str | None = None,
    version: str | None = "1.2.3",
) -> Path:
    """packages/<name> carrying just the pyproject fields deps-internals
    matches on — [project] name + (optional) version."""
    pkg = root / "packages" / name
    pkg.mkdir(parents=True)
    lines = ["[project]", f'name = "{project or f"devin-{name}"}"']
    if version is not None:
        lines.append(f'version = "{version}"')
    (pkg / "pyproject.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return pkg


def write_venv(root: Path, lib: str = "lib", pyver: str = "python3.12") -> Path:
    """<root>/.venv/<lib>/<pyver>/site-packages — a synthetic venv."""
    site_dir = root / ".venv" / lib / pyver / "site-packages"
    site_dir.mkdir(parents=True)
    return site_dir


def write_publishable_pkg(pkg: Path, name: str = "devin-foo") -> Path:
    """A fully publish-ready package — only needed by CLI tests, where
    the shared ``check`` command also runs publish-readiness and
    path-check on whatever ``packages/*`` contains."""
    pkg.mkdir(parents=True)
    mod = name.replace("-", "_")
    (pkg / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=68"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        "[project]\n"
        f'name = "{name}"\n'
        'version = "1.2.3"\n\n'
        "[project.scripts]\n"
        f'{name} = "{mod}.cli:main"\n',
        encoding="utf-8",
    )
    mod_dir = pkg / "src" / mod
    mod_dir.mkdir(parents=True)
    (mod_dir / "__init__.py").write_text(
        '__version__ = "1.2.3"\n', encoding="utf-8"
    )
    (mod_dir / "cli.py").write_text(
        "def main():\n    return 0\n", encoding="utf-8"
    )
    return pkg


def _devin_foo_on_path(tmp_path: Path, monkeypatch) -> None:
    """Put a `devin-foo` executable on PATH so `path-check` stays green
    when a CLI test's fixture declares the script (test_publish's
    helper, same shape)."""
    bindir = tmp_path / "path-bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "devin-foo"
    script.write_text("#!/bin/sh\nexit 0\n")
    os.chmod(script, 0o755)  # shutil.which requires X_OK on POSIX
    (bindir / "devin-foo.cmd").write_text("@exit /b 0\r\n")
    monkeypatch.setenv("PATH", str(bindir))


def write_pth(site_dir: Path, name: str, body: str) -> Path:
    """An ``__editable__.<name>.pth`` file — ``name`` carries the
    name-version label ('devin_foo-1.2.3'), ``body`` its content."""
    pth = site_dir / f"__editable__.{name}.pth"
    pth.write_text(body, encoding="utf-8")
    return pth


def write_dist_info(
    site_dir: Path,
    name: str,
    version: str,
    *,
    editable: bool = False,
    metadata: bool = True,
) -> Path:
    """A ``<name>-<version>.dist-info`` dir — METADATA unless disabled,
    ``direct_url.json`` with the editable marker when asked."""
    dist_info = site_dir / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir()
    if metadata:
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            encoding="utf-8",
        )
    if editable:
        (dist_info / "direct_url.json").write_text(
            '{"url":"file:///x/src","dir_info":{"editable":true}}',
            encoding="utf-8",
        )
    return dist_info


def _statuses(findings):
    return [f.status for f in findings]


# --- scope / detection -----------------------------------------------------


def test_no_venv_is_not_detected(tmp_path):
    """No .venv → no findings; the renderer groups the check under
    'not detected', like health/disk on a machine without Devin."""
    write_pkg(tmp_path, "foo")
    assert deps_internals.run(_ctx(tmp_path)) == []


def test_venv_without_site_packages_warns(tmp_path):
    """A .venv that exists but has no site-packages cannot be inspected
    — WARN 'unverified', never a rubber-stamp PASS."""
    (tmp_path / ".venv").mkdir()
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.WARN]
    assert "site-packages" in findings[0].message
    assert "unverified" in findings[0].message


def test_venv_without_internal_dists_passes(tmp_path):
    """A venv holding only third-party dists has nothing internal to
    verify — PASS note, not silence."""
    site_dir = write_venv(tmp_path)
    write_dist_info(site_dir, "requests", "2.31.0")
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.PASS]
    assert "nothing internal" in findings[0].message


# --- editable installs -----------------------------------------------------


def test_healthy_editable_passes(tmp_path):
    write_pkg(tmp_path, "foo")
    src = tmp_path / "packages" / "foo" / "src"
    src.mkdir()
    site_dir = write_venv(tmp_path)
    write_pth(site_dir, "devin_foo-1.2.3", f"{src}\n")
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.PASS]
    assert "resolve inside the checkout" in findings[0].message


def test_dangling_editable_fails(tmp_path):
    """The real-world failure class: the repo moved/absorbed the package
    and the .pth still points at the ghost path — FAIL naming both."""
    write_pkg(tmp_path, "foo")
    site_dir = write_venv(tmp_path)
    ghost = tmp_path / "packages" / "foo" / "src"  # never created
    write_pth(site_dir, "devin_foo-1.2.3", f"{ghost}\n")
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.FAIL]
    assert "devin_foo-1.2.3" in findings[0].message
    assert str(ghost) in findings[0].message
    assert "no longer exists" in findings[0].message


def test_foreign_editable_target_warns(tmp_path):
    """Target exists but resolves outside the checkout — the venv is
    bound to a different workspace."""
    foreign = tmp_path / "other-worktree" / "packages" / "foo" / "src"
    foreign.mkdir(parents=True)
    repo = tmp_path / "repo"
    write_pkg(repo, "foo")
    site_dir = write_venv(repo)
    write_pth(site_dir, "devin_foo-1.2.3", f"{foreign}\n")
    findings = deps_internals.run(_ctx(repo))
    assert _statuses(findings) == [Status.WARN]
    assert "devin_foo-1.2.3" in findings[0].message
    assert "outside this checkout" in findings[0].message


def test_symlinked_target_outside_checkout_warns(tmp_path):
    """A symlinked pth target is judged on its real path — a link out of
    the checkout is foreign even though the link itself sits inside."""
    repo = tmp_path / "repo"
    write_pkg(repo, "foo")
    site_dir = write_venv(repo)
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    link = repo / "src-link"
    try:
        link.symlink_to(foreign, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    write_pth(site_dir, "devin_foo-1.2.3", f"{link}\n")
    findings = deps_internals.run(_ctx(repo))
    assert _statuses(findings) == [Status.WARN]
    assert "outside this checkout" in findings[0].message


def test_unreadable_pth_warns_unverified(tmp_path):
    """An __editable__ entry that cannot be read as a file reports WARN
    'unverified' — never silently healthy."""
    site_dir = write_venv(tmp_path)
    (site_dir / "__editable__.devin_foo-1.2.3.pth").mkdir()
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.WARN]
    assert "devin_foo-1.2.3" in findings[0].message
    assert "unverified" in findings[0].message


def test_finder_style_pth_resolves_finder_module(tmp_path):
    """setuptools' finder mode: the .pth carries an import line and the
    targets are the absolute paths inside the *_finder.py MAPPING."""
    write_pkg(tmp_path, "foo")
    src = tmp_path / "packages" / "foo" / "src" / "devin_foo"
    src.mkdir(parents=True)
    site_dir = write_venv(tmp_path)
    write_pth(
        site_dir, "devin_foo-1.2.3",
        "import __editable___devin_foo_1_2_3_finder\n",
    )
    (site_dir / "__editable___devin_foo_1_2_3_finder.py").write_text(
        f"MAPPING = {{'devin_foo': '{src}'}}\n", encoding="utf-8"
    )
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.PASS]


def test_finder_style_pth_dangling_fails(tmp_path):
    """Finder-mode targets get the same existence verdict — a ghost
    MAPPING path is a dangling editable."""
    site_dir = write_venv(tmp_path)
    ghost = tmp_path / "gone" / "src"
    write_pth(
        site_dir, "devin_foo-1.2.3",
        "import __editable___devin_foo_1_2_3_finder\n",
    )
    (site_dir / "__editable___devin_foo_1_2_3_finder.py").write_text(
        f"MAPPING = {{'devin_foo': '{ghost}'}}\n", encoding="utf-8"
    )
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.FAIL]
    assert str(ghost) in findings[0].message


def test_finder_import_without_module_warns(tmp_path):
    """An import line whose companion finder file is missing leaves the
    editable unverifiable — WARN, not a guessed PASS."""
    site_dir = write_venv(tmp_path)
    write_pth(
        site_dir, "devin_foo-1.2.3",
        "import __editable___devin_foo_1_2_3_finder\n",
    )
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.WARN]
    assert "no resolvable target" in findings[0].message


def test_mixed_editables_fail_and_pass_summary(tmp_path):
    """One finding per issue: the dangling pth FAILs while the healthy
    one still earns its PASS summary — no merged verdict."""
    write_pkg(tmp_path, "foo")
    src = tmp_path / "packages" / "foo" / "src"
    src.mkdir()
    site_dir = write_venv(tmp_path)
    write_pth(site_dir, "devin_foo-1.2.3", f"{src}\n")
    write_pth(site_dir, "devin_bar-0.1.0", f"{tmp_path / 'gone'}\n")
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.FAIL, Status.PASS]
    assert "devin_bar-0.1.0" in findings[0].message
    assert "1 editable install(s)" in findings[1].message


# --- installed devin_* dists -----------------------------------------------


def test_version_drift_warns(tmp_path):
    """A non-editable devin_* wheel older than the checkout's pyproject
    shadows fresh source — WARN naming dist and both versions."""
    write_pkg(tmp_path, "foo", version="1.2.3")
    site_dir = write_venv(tmp_path)
    write_dist_info(site_dir, "devin-foo", "0.9.0")
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.WARN]
    msg = findings[0].message
    assert "devin-foo" in msg
    assert "0.9.0" in msg and "1.2.3" in msg
    assert "packages/foo" in msg
    assert "stale" in msg


def test_matching_version_passes(tmp_path):
    write_pkg(tmp_path, "foo", version="1.2.3")
    site_dir = write_venv(tmp_path)
    write_dist_info(site_dir, "devin-foo", "1.2.3")
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.PASS]
    assert "match the checkout's pyproject versions" in findings[0].message


def test_prerelease_spelling_matches_normalized_metadata(tmp_path):
    """pyproject '1.0.0-rc1' installs as METADATA '1.0.0rc1' — PEP 440
    equal, not a drift."""
    write_pkg(tmp_path, "foo", version="1.0.0-rc1")
    site_dir = write_venv(tmp_path)
    write_dist_info(site_dir, "devin-foo", "1.0.0rc1")
    findings = deps_internals.run(_ctx(tmp_path))
    assert all(f.status is Status.PASS for f in findings)


def test_foreign_devin_dist_is_ignored(tmp_path):
    """A devin_* dist with no same-named package in the checkout is
    foreign tooling — not this repo's problem."""
    site_dir = write_venv(tmp_path)
    write_dist_info(site_dir, "devin-zzz", "9.9.9")
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.PASS]
    assert "nothing internal" in findings[0].message


def test_editable_marked_dist_is_not_version_compared(tmp_path):
    """An editable install's dist-info must not also count as a wheel:
    direct_url.json dir_info.editable excludes it from the dist half."""
    write_pkg(tmp_path, "foo", version="1.2.3")
    src = tmp_path / "packages" / "foo" / "src"
    src.mkdir()
    site_dir = write_venv(tmp_path)
    write_pth(site_dir, "devin_foo-0.9.0", f"{src}\n")
    # stale-versioned dist-info, but it belongs to the live editable
    write_dist_info(site_dir, "devin-foo", "0.9.0", editable=True)
    findings = deps_internals.run(_ctx(tmp_path))
    assert all(
        "stale" not in f.message and "shadows" not in f.message
        for f in findings
    )
    assert all(f.status is Status.PASS for f in findings)


def test_pth_named_dist_is_not_version_compared(tmp_path):
    """Same guard via the .pth set: even without direct_url.json an
    __editable__-named dist-info is not a shadowing wheel."""
    write_pkg(tmp_path, "foo", version="1.2.3")
    src = tmp_path / "packages" / "foo" / "src"
    src.mkdir()
    site_dir = write_venv(tmp_path)
    write_pth(site_dir, "devin_foo-0.9.0", f"{src}\n")
    write_dist_info(site_dir, "devin-foo", "0.9.0")  # no direct_url.json
    findings = deps_internals.run(_ctx(tmp_path))
    assert all(f.status is Status.PASS for f in findings)


def test_dist_without_metadata_uses_dirname(tmp_path):
    """A dist-info lacking METADATA still gets compared — name and
    version fall back to the normalized dir name."""
    write_pkg(tmp_path, "foo", version="1.2.3")
    site_dir = write_venv(tmp_path)
    write_dist_info(site_dir, "devin-foo", "0.9.0", metadata=False)
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.WARN]
    assert "0.9.0" in findings[0].message


def test_unverifiable_version_warns(tmp_path):
    """A declared version is required on both sides — a checkout package
    without a literal version makes the comparison unverifiable (WARN),
    not skipped."""
    write_pkg(tmp_path, "foo", version=None)
    site_dir = write_venv(tmp_path)
    write_dist_info(site_dir, "devin-foo", "0.9.0")
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.WARN]
    assert "unverifiable" in findings[0].message
    assert "0.9.0" in findings[0].message


# --- site-packages resolution ---------------------------------------------


def test_site_packages_picks_first_sorted_match(tmp_path):
    """Multiple python*/site-packages dirs (a lib64 mirror, several
    versions): the first in sorted order wins — deterministic."""
    venv = tmp_path / ".venv"
    older = write_venv(tmp_path, pyver="python3.10")
    write_venv(tmp_path, pyver="python3.9")
    write_venv(tmp_path, lib="lib64", pyver="python3.12")
    resolved = deps_internals._site_packages(venv)
    assert resolved == older  # lib/python3.10 < lib/python3.9 < lib64/…
    assert (venv / "lib" / "python3.10" / "site-packages") == resolved


def test_site_packages_lib64_only(tmp_path):
    site_dir = write_venv(tmp_path, lib="lib64")
    assert deps_internals._site_packages(tmp_path / ".venv") == site_dir


def test_site_packages_windows_lib_layout(tmp_path):
    site_dir = tmp_path / ".venv" / "Lib" / "site-packages"
    site_dir.mkdir(parents=True)
    assert deps_internals._site_packages(tmp_path / ".venv") == site_dir


# --- injected seams ----------------------------------------------------------


def test_installed_dists_seam(tmp_path):
    """The dist index is injectable — no dist-info files needed."""
    write_pkg(tmp_path, "foo", version="1.2.3")
    write_venv(tmp_path)
    findings = deps_internals.run(
        _ctx(tmp_path),
        installed_dists=lambda _s: [
            deps_internals._Dist("devin-foo", "0.9.0", editable=False)
        ],
    )
    assert _statuses(findings) == [Status.WARN]
    assert "0.9.0" in findings[0].message


def test_pth_targets_seam(tmp_path):
    """The .pth target reader is injectable too — the .pth file only has
    to exist for the glob to see it."""
    site_dir = write_venv(tmp_path)
    write_pth(site_dir, "devin_foo-1.2.3", "import whatever\n")
    findings = deps_internals.run(
        _ctx(tmp_path),
        pth_targets=lambda _p, _s: [str(tmp_path / "ghost-src")],
    )
    assert _statuses(findings) == [Status.FAIL]
    assert "ghost-src" in findings[0].message


# --- read-only guarantee -----------------------------------------------------


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root).as_posix()): p.read_bytes().decode(
            "utf-8", errors="replace"
        )
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_check_is_readonly_against_repo_and_venv(tmp_path):
    write_pkg(tmp_path, "foo")
    src = tmp_path / "packages" / "foo" / "src"
    src.mkdir()
    site_dir = write_venv(tmp_path)
    write_pth(site_dir, "devin_foo-1.2.3", f"{src}\n")
    write_dist_info(site_dir, "devin-foo", "0.9.0")
    before = _tree(tmp_path)
    findings = deps_internals.run(_ctx(tmp_path))
    assert _tree(tmp_path) == before
    assert findings


# --- wiring / CLI ------------------------------------------------------------


def test_run_all_includes_check(tmp_path):
    """Registered in DEVIN_CHECKS — visible once a .venv exists under
    ctx.cwd (a bare cwd groups it under 'not detected')."""
    repo = tmp_path / "repo"
    site_dir = write_venv(repo)
    write_pth(site_dir, "devin_foo-1.2.3", f"{repo / 'gone'}\n")
    report = doctor.run_all(_ctx(repo))
    assert "deps-internals" in report.check_status()


def test_run_all_without_venv_is_not_detected(tmp_path):
    """No .venv → the check emits nothing and the devin-environment
    section renders it 'not detected' (never a FAIL)."""
    report = doctor.run_all(_ctx(tmp_path))
    assert "deps-internals" not in report.check_status()
    out = doctor.render_text(report)
    assert any(
        line.startswith("  not detected:") and "deps-internals" in line
        for line in out.splitlines()
    )
    assert not any(
        f.status is Status.FAIL and f.check == "deps-internals"
        for f in report.findings
    )


def test_cli_dangling_editable_exit_1(tmp_path, capsys):
    site_dir = write_venv(tmp_path)
    write_pth(site_dir, "devin_foo-1.2.3", f"{tmp_path / 'gone'}\n")
    rc = main([
        "check", "--data-dir", str(tmp_path), "--cwd", str(tmp_path),
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert "deps-internals" in out
    assert "devin_foo-1.2.3" in out


def test_cli_healthy_venv_exit_0(tmp_path, capsys):
    """A checkout whose cwd is not package-shaped keeps publish/path-
    check at their 'nothing' PASS notes while deps-internals PASSes."""
    src = tmp_path / "pkgsrc" / "devin_foo"  # not under packages/
    src.mkdir(parents=True)
    site_dir = write_venv(tmp_path)
    write_pth(site_dir, "devin_foo-1.2.3", f"{src}\n")
    rc = main([
        "check", "--data-dir", str(tmp_path), "--cwd", str(tmp_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "deps-internals" in out


def test_cli_json_reports_the_dist(tmp_path, monkeypatch, capsys):
    _devin_foo_on_path(tmp_path, monkeypatch)
    write_publishable_pkg(tmp_path / "packages" / "foo")
    site_dir = write_venv(tmp_path)
    write_pth(
        site_dir, "devin_foo-1.2.3",
        f"{tmp_path / 'packages' / 'foo' / 'src'}\n",
    )
    write_dist_info(site_dir, "devin-foo", "0.9.0")
    rc = main([
        "check", "--data-dir", str(tmp_path), "--cwd", str(tmp_path),
        "--json",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    mine = [
        f for f in payload["findings"] if f["check"] == "deps-internals"
    ]
    assert any(
        f["status"] == "WARN" and "0.9.0" in f["message"] for f in mine
    )
    assert any(f["status"] == "PASS" for f in mine)


def test_import_line_pth_keeps_cwd_out_of_message(tmp_path):
    """Sanity guard: an unreadable/absent finder must not be reported as
    'dangling at <path>' — the WARN names the label, no phantom path."""
    site_dir = write_venv(tmp_path)
    write_pth(
        site_dir, "devin_foo-1.2.3",
        "import __editable___devin_foo_1_2_3_finder\n",
    )
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.WARN]
    assert "devin_foo-1.2.3" in findings[0].message


# --- review fixes: active site-packages, finder escapes, PEP 440 ----------


def test_site_packages_prefers_the_pyvenv_cfg_version(tmp_path):
    """A venv upgraded 3.10 → 3.11 keeps both dirs; ``pyvenv.cfg`` says
    which interpreter actually runs, so its site-packages wins over the
    lexical-min stale one."""
    venv = tmp_path / ".venv"
    write_venv(tmp_path, pyver="python3.10")
    active = write_venv(tmp_path, pyver="python3.11")
    (venv / "pyvenv.cfg").write_text(
        "home = /usr/bin\nversion = 3.11.5\n", encoding="utf-8"
    )
    assert deps_internals._site_packages(venv) == active


def test_site_packages_without_pyvenv_cfg_keeps_sorted_order(tmp_path):
    """No version hint (broken/synthetic venv): the deterministic min()
    fallback still applies."""
    venv = tmp_path / ".venv"
    older = write_venv(tmp_path, pyver="python3.10")
    write_venv(tmp_path, pyver="python3.11")
    assert deps_internals._site_packages(venv) == older


def test_finder_decodes_windows_escaped_mapping(tmp_path, monkeypatch):
    """A setuptools finder spells ``C:\\work`` as ``'C:\\\\work'`` —
    regex-only extraction probes the doubled-backslash literal and flags
    a live dir as missing. ``ast.literal_eval`` decodes it first."""
    import ntpath

    finder = tmp_path / "__editable___devin_foo_1_2_3_finder.py"
    finder.write_text(
        "MAPPING = {'devin_foo': 'C:\\\\work\\\\repo\\\\src\\\\devin_foo'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(deps_internals.os.path, "isabs", ntpath.isabs)
    assert deps_internals._finder_targets(finder) == [
        "C:\\work\\repo\\src\\devin_foo"
    ]


def test_finder_still_reads_posix_mapping(tmp_path):
    finder = tmp_path / "__editable___devin_foo_1_2_3_finder.py"
    finder.write_text(
        f"MAPPING = {{'devin_foo': '{tmp_path / 'src' / 'devin_foo'}'}}\n",
        encoding="utf-8",
    )
    assert deps_internals._finder_targets(finder) == [
        str(tmp_path / "src" / "devin_foo")
    ]


def test_same_version_pep440_equivalents():
    """``1.0`` == ``1.0.0`` and ``-rc1`` == ``rc1`` — the review's false-
    drift cases; invalid input is unverifiable, never approximated."""
    assert deps_internals._same_version("1.0", "1.0.0") is True
    assert deps_internals._same_version("1.0.0-rc1", "1.0.0rc1") is True
    assert deps_internals._same_version("1.0", "1.1") is False
    assert deps_internals._same_version("not-a-version", "1.0") is None


def test_dist_findings_equivalent_versions_do_not_warn(tmp_path):
    """The exact review case: wheel 1.0 vs pyproject 1.0.0 must match,
    not emit a stale-wheel WARN."""
    write_pkg(tmp_path, "foo", version="1.0.0")
    site_dir = write_venv(tmp_path)
    write_dist_info(site_dir, "devin-foo", "1.0")
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.PASS]
    assert "match" in findings[0].message


def test_dist_findings_invalid_version_is_unverifiable(tmp_path):
    """An unparseable installed version warns 'unverifiable', never
    'stale wheel' — approximated equality would invent drift."""
    write_pkg(tmp_path, "foo", version="1.2.3")
    site_dir = write_venv(tmp_path)
    write_dist_info(site_dir, "devin-foo", "not-a-version")
    findings = deps_internals.run(_ctx(tmp_path))
    assert _statuses(findings) == [Status.WARN]
    assert "unverifiable" in findings[0].message


def test_finder_keeps_raw_capture_on_invalid_escapes(tmp_path, monkeypatch):
    """A hand-written finder with an unescaped Windows path — 'C:\\\\x'
    inside the literal — is not valid Python, so ast fails and the raw
    quoted capture must be kept instead of dropped."""
    import ntpath

    finder = tmp_path / "__editable___devin_foo_1_2_3_finder.py"
    # Single backslashes: an invalid \\U escape for Python parsers but a
    # plausible hand-edited file on a Windows box.
    finder.write_text(
        "MAPPING = {'devin_foo': 'C:\\work\\repo\\src\\devin_foo'}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(deps_internals.os.path, "isabs", ntpath.isabs)
    assert deps_internals._finder_targets(finder) == [
        "C:\\work\\repo\\src\\devin_foo"
    ]
