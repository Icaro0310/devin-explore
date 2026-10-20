"""toolchain: missing test/build tools named per-OS with fix: hints.

PATH reachability and interpreter probes are injected — no real binary
is ever looked up or executed. ``which`` fakes map command name → path;
``probe`` fakes decide by argv (``-m venv --help``, ``-m pip
--version``, ``import ensurepip``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from devin_doctor.checks import toolchain
from devin_doctor.model import Context, Status

_ALL_BINS = ("python3", "python", "py", "pip3", "pip",
             "node", "nodejs", "npm", "git")

_DEBIAN = 'ID=debian\n'
_UBUNTU_LIKE = 'ID=ubuntu\nID_LIKE=debian\n'
_FEDORA = 'ID=fedora\n'
_ARCH = 'ID=arch\nID_LIKE=arch\n'
_UNKNOWN_DISTRO = 'ID=nixos\n'


def _ctx(tmp_path: Path) -> Context:
    return Context(data_dir=tmp_path, cwd=tmp_path)


def _which(mapping: dict[str, str]):
    return lambda cmd: mapping.get(cmd)


def _ok_probe(argv):
    return subprocess.CompletedProcess(argv, 0, "", "")


def _probe(rules: dict[tuple[str, ...], int]):
    """rc by argv-tail match — e.g. {("-m", "venv", "--help"): 1}."""
    def fake(argv):
        for tail, rc in rules.items():
            if tuple(argv[-len(tail):]) == tail:
                return subprocess.CompletedProcess(argv, rc, "", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return fake


def _full_path_which() -> dict[str, str]:
    return {name: f"/usr/bin/{name}" for name in _ALL_BINS}


def _by_msg(findings, needle):
    return [f for f in findings if needle in f.message]


# --- happy path -------------------------------------------------------------


def test_all_present_passes(tmp_path):
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(_full_path_which()),
        probe=_ok_probe,
        plat="linux",
        os_release=_DEBIAN,
    )
    assert findings
    assert all(f.status is Status.PASS for f in findings)
    assert all(f.check == "toolchain" for f in findings)


def test_every_piece_reports(tmp_path):
    """python, pip, venv, node, npm, git — one finding each."""
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(_full_path_which()),
        probe=_ok_probe,
        plat="linux",
        os_release=_DEBIAN,
    )
    assert len(findings) == 6


# --- missing pieces ---------------------------------------------------------


def test_missing_pip_fails_with_apt_hint(tmp_path):
    bins = _full_path_which()
    del bins["pip3"], bins["pip"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        # '-m pip --version' also fails — genuinely absent, not degraded.
        probe=_probe({("-m", "pip", "--version"): 1}),
        plat="linux",
        os_release=_DEBIAN,
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert len(fails) == 1
    assert "pip" in fails[0].message
    assert "apt install python3-pip" in (fails[0].fix or "")


def test_pip_module_only_warns(tmp_path):
    """'python -m pip' works but no PATH script — degraded, not missing."""
    bins = _full_path_which()
    del bins["pip3"], bins["pip"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        probe=_ok_probe,
        plat="linux",
        os_release=_DEBIAN,
    )
    warns = [f for f in findings if f.status is Status.WARN]
    assert len(warns) == 1
    assert "python -m pip" in warns[0].message
    assert "apt install python3-pip" in (warns[0].fix or "")


def test_missing_python_venv_fails(tmp_path):
    """python3 exists but the Debian python3-venv split leaves fresh
    venvs without pip — env-missing, FAIL with the apt hint."""
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(_full_path_which()),
        probe=_probe({("-c", "import ensurepip"): 1}),
        plat="linux",
        os_release=_DEBIAN,
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert len(fails) == 1
    assert "ensurepip" in fails[0].message
    assert "apt install python3-venv" in (fails[0].fix or "")


def test_venv_module_itself_missing_fails(tmp_path):
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(_full_path_which()),
        probe=_probe({("-m", "venv", "--help"): 1}),
        plat="linux",
        os_release=_DEBIAN,
    )
    fails = _by_msg(findings, "-m venv")
    assert [f.status for f in fails] == [Status.FAIL]
    assert "apt install python3-venv" in (fails[0].fix or "")


def test_venv_not_probed_without_python(tmp_path):
    """No interpreter → one python FAIL, no cascading venv FAIL."""
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which({"git": "/usr/bin/git"}),
        probe=_ok_probe,
        plat="linux",
        os_release=_DEBIAN,
    )
    # 5 findings: python, pip, node, npm, git — venv folded into python.
    assert len(findings) == 5
    assert not _by_msg(findings, "ensurepip")


def test_missing_node_and_npm_fail(tmp_path):
    bins = _full_path_which()
    del bins["node"], bins["nodejs"], bins["npm"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        probe=_ok_probe,
        plat="linux",
        os_release=_DEBIAN,
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    node = _by_msg(fails, "'node'")
    npm = _by_msg(fails, "'npm'")
    assert len(node) == 1 and len(npm) == 1
    assert "apt install nodejs" in (node[0].fix or "")
    assert "apt install npm" in (npm[0].fix or "")


def test_nodejs_alias_passes(tmp_path):
    """Older Debian exposes only 'nodejs' — still a working node."""
    bins = _full_path_which()
    del bins["node"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        probe=_ok_probe,
        plat="linux",
        os_release=_DEBIAN,
    )
    node = _by_msg(findings, "'node'")
    assert [f.status for f in node] == [Status.PASS]
    assert "nodejs" in node[0].message


def test_missing_git_fails(tmp_path):
    bins = _full_path_which()
    del bins["git"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        probe=_ok_probe,
        plat="linux",
        os_release=_DEBIAN,
    )
    git = _by_msg(findings, "'git'")
    assert [f.status for f in git] == [Status.FAIL]
    assert "apt install git" in (git[0].fix or "")


def test_no_python_fails_naming_it(tmp_path):
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which({}),
        probe=_ok_probe,
        plat="linux",
        os_release=_DEBIAN,
    )
    fails = _by_msg(findings, "Python interpreter")
    assert [f.status for f in fails] == [Status.FAIL]
    assert "apt install python3" in (fails[0].fix or "")


def test_bare_system_names_every_missing_piece(tmp_path):
    """The incident shape: nothing on PATH → every piece a named FAIL
    (venv folds into the python FAIL — no interpreter to probe)."""
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which({}),
        probe=_ok_probe,
        plat="linux",
        os_release=_DEBIAN,
    )
    fails = [f for f in findings if f.status is Status.FAIL]
    assert len(fails) == 5  # python, pip, node, npm, git
    assert not any(f.status is Status.PASS for f in fails)
    assert all(f.fix for f in fails)


# --- per-OS fix hints --------------------------------------------------------


def test_windows_uses_winget(tmp_path):
    bins = _full_path_which()
    del bins["node"], bins["nodejs"], bins["git"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        probe=_ok_probe,
        plat="windows",
    )
    fixes = " ".join(f.fix or "" for f in findings)
    assert "winget install" in fixes
    assert "OpenJS.NodeJS" in fixes
    assert "Git.Git" in fixes
    assert "apt install" not in fixes


def test_macos_uses_brew(tmp_path):
    bins = _full_path_which()
    del bins["pip3"], bins["pip"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        probe=_probe({("-m", "pip", "--version"): 1}),
        plat="macos",
    )
    pip_fail = _by_msg(findings, "pip")[0]
    assert "brew install" in (pip_fail.fix or "")


def test_fedora_uses_dnf(tmp_path):
    bins = _full_path_which()
    del bins["git"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        probe=_ok_probe,
        plat="linux",
        os_release=_FEDORA,
    )
    git = _by_msg(findings, "'git'")[0]
    assert "dnf install git" in (git.fix or "")


def test_arch_uses_pacman(tmp_path):
    bins = _full_path_which()
    del bins["node"], bins["nodejs"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        probe=_ok_probe,
        plat="linux",
        os_release=_ARCH,
    )
    node = _by_msg(findings, "'node'")[0]
    assert "pacman -S nodejs" in (node.fix or "")


def test_ubuntu_via_id_like_gets_apt(tmp_path):
    """ID_LIKE=debian suffices when ID is not literally 'debian'."""
    bins = _full_path_which()
    del bins["git"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        probe=_ok_probe,
        plat="linux",
        os_release=_UBUNTU_LIKE,
    )
    git = _by_msg(findings, "'git'")[0]
    assert "apt install git" in (git.fix or "")


def test_unknown_distro_gets_generic_hint(tmp_path):
    bins = _full_path_which()
    del bins["git"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        probe=_ok_probe,
        plat="linux",
        os_release=_UNKNOWN_DISTRO,
    )
    git = _by_msg(findings, "'git'")[0]
    assert git.status is Status.FAIL
    assert "package manager" in (git.fix or "")
    assert "apt install" not in (git.fix or "")


def test_missing_os_release_file_is_generic(tmp_path):
    bins = _full_path_which()
    del bins["git"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        probe=_ok_probe,
        plat="linux",
        os_release="",
    )
    git = _by_msg(findings, "'git'")[0]
    assert "package manager" in (git.fix or "")


# --- degraded probes ----------------------------------------------------------


def test_unanswerable_venv_probe_warns_not_fails(tmp_path):
    """A timed-out/unexecutable probe is 'unverified' — never read as
    'missing' (same doctrine as automation's unavailable surfaces)."""
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(_full_path_which()),
        probe=lambda argv: None,
        plat="linux",
        os_release=_DEBIAN,
    )
    venv = _by_msg(findings, "unverified")
    assert len(venv) == 1
    assert venv[0].status is Status.WARN
    assert not any(f.status is Status.FAIL for f in findings)


def test_python_fallback_names_the_resolved_alias(tmp_path):
    """Only 'python' (no 'python3') still passes — and says which name
    resolved, since scripts spelling 'python3' would miss."""
    bins = _full_path_which()
    del bins["python3"]
    findings = toolchain.run(
        _ctx(tmp_path),
        which=_which(bins),
        probe=_ok_probe,
        plat="linux",
        os_release=_DEBIAN,
    )
    py = _by_msg(findings, "python on PATH")
    assert [f.status for f in py] == [Status.PASS]
    assert "'python'" in py[0].message


# --- registration / runner ----------------------------------------------------


def test_registered_after_deps_internals():
    from devin_doctor.checks import DEVIN_CHECKS

    ids = [m.CHECK_ID for m in DEVIN_CHECKS]
    assert ids.index("toolchain") == ids.index("deps-internals") + 1


def test_run_all_includes_check(ctx):
    from devin_doctor import doctor

    report = doctor.run_all(ctx)
    # conftest stubs every probe healthy → the check reports, PASS.
    assert report.check_status()["toolchain"] == "PASS"
