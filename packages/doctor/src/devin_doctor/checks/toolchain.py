"""Check 11 — toolchain: missing test/build tools, named per-OS.

The 2026-10-02 incident burned sixteen E2E runs on 'code-fail' triage
before anyone noticed the environment itself was bare: ``pip`` missing,
``python3 -m venv`` broken by Debian's ``python3-venv`` split,
``node``/``npm`` absent. Env-missing and code-fail must be diagnosable
instantly — this check names each missing piece and the package-manager
command that installs it.

Probes — ``shutil.which`` for binaries, plus tiny subprocess calls where
a binary on PATH is ambiguous (the classic case: ``python3`` exists but
``python3 -m venv`` fails without ``python3-venv``):

- ``python3`` / ``python`` / ``py`` — the interpreter pytest and venv
  creation hang off. Presence alone is not enough: each candidate must
  answer ``--version``, so a Microsoft Store alias or broken shim that
  resolves but cannot run falls through to the next name;
- ``pip3`` / ``pip`` — and, when no script is on PATH, ``<python> -m
  pip --version``: a module that answers but exposes no script is
  degraded (WARN), not missing — bare ``pip`` still fails;
- ``<python> -m venv --help`` then ``import ensurepip`` **and** a
  ``pip-*.whl`` lookup — under sysconfig's ``WHEEL_PKG_DIR`` (the
  Debian-family layout) or ensurepip's ``_bundled`` dir — the venv
  module itself, the pip-bootstrap half Debian splits into
  ``python3-venv``, and the partial install whose ensurepip imports
  but ships no wheel to bootstrap from;
- ``node``, ``npm``, ``git`` — an alias-only hit (``nodejs`` without
  ``node``) is WARN, not PASS: the shell still cannot resolve the name
  the build/test scripts actually invoke.

Verdicts:

- FAIL — a required piece is absent (named, with a concrete ``fix:``
  for the detected package manager: apt/dnf/pacman on Linux — chosen
  from ``/etc/os-release`` ID/ID_LIKE — brew on macOS, winget on
  Windows, a generic 'use your package manager' when the distro is
  unrecognized);
- WARN — present but degraded (pip importable via ``python -m pip``
  with no PATH script, or a probe that cannot answer — a timed-out
  probe is 'unverified', never 'missing');
- PASS — the piece resolves and answers.

Unlike the store checks there is no 'not detected' state: the toolchain
matters machine-wide, so a bare system reports FAILs — that loudness is
the point. ``ctx`` is accepted but unused — nothing here is scoped to a
data dir or checkout.

Read-only: PATH probing and ``--help``/``--version`` subprocess calls
only (10 s timeouts, output captured and discarded) — nothing is
installed, written or configured.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from devin_doctor.model import Context, Finding, Status, platform_name

CHECK_ID = "toolchain"

_PROBE_TIMEOUT = 10
_OS_RELEASE = Path("/etc/os-release")

# Interpreter names probed in order — ``python3`` everywhere it exists,
# ``python`` as the venv/Windows spelling, ``py`` the Windows launcher.
_PYTHON_NAMES = ("python3", "python", "py")
_PIP_NAMES = ("pip3", "pip")
_NODE_NAMES = ("node", "nodejs")  # older Debian ships only 'nodejs'

# tool → package manager → concrete install command. Deliberately a
# small mapping — the managers that cover the fleet, not a distro db.
_FIXES: dict[str, dict[str, str]] = {
    "python": {
        "apt": "sudo apt install python3",
        "dnf": "sudo dnf install python3",
        "pacman": "sudo pacman -S python",
        "brew": "brew install python",
        "winget": "winget install -e --id Python.Python.3",
    },
    "pip": {
        "apt": "sudo apt install python3-pip",
        "dnf": "sudo dnf install python3-pip",
        "pacman": "sudo pacman -S python-pip",
        # brew python wheels pip in; Windows pips arrive via ensurepip.
        "brew": "brew install python",
        "winget": "py -m ensurepip",
    },
    "venv": {
        "apt": "sudo apt install python3-venv",
        # dnf/pacman/brew Pythons carry venv+ensurepip in the main
        # package — only Debian-family splits it out.
        "dnf": "sudo dnf install python3",
        "pacman": "sudo pacman -S python",
        "brew": "brew install python",
        "winget": "winget install -e --id Python.Python.3",
    },
    "node": {
        "apt": "sudo apt install nodejs",
        "dnf": "sudo dnf install nodejs",
        "pacman": "sudo pacman -S nodejs npm",
        "brew": "brew install node",
        "winget": "winget install -e --id OpenJS.NodeJS",
    },
    "npm": {
        "apt": "sudo apt install npm",
        "dnf": "sudo dnf install npm",
        "pacman": "sudo pacman -S npm",
        # brew and winget bundle npm inside the node install.
        "brew": "brew install node",
        "winget": "winget install -e --id OpenJS.NodeJS",
    },
    "git": {
        "apt": "sudo apt install git",
        "dnf": "sudo dnf install git",
        "pacman": "sudo pacman -S git",
        "brew": "brew install git",
        "winget": "winget install -e --id Git.Git",
    },
}

# Package spelling for the generic hint when the distro is unknown.
_PKG_NAMES = {
    "python": "python3",
    "pip": "python3-pip",
    "venv": "python3-venv",
    "node": "nodejs",
    "npm": "npm",
    "git": "git",
}


# --- environment probing (injectable) ------------------------------------


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _probe(argv: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    """Probe seam: ``None`` when the binary cannot be executed or the
    call times out — an unanswerable probe is 'unverified', never
    'missing'."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _os_release() -> str:
    try:
        return _OS_RELEASE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _manager(plat: str, os_release: str) -> str | None:
    """The package manager ``fix:`` hints target — apt/dnf/pacman are
    picked from ``/etc/os-release`` ID/ID_LIKE; ``None`` when the distro
    is unrecognized (the hint then names the package generically)."""
    if plat == "windows":
        return "winget"
    if plat == "macos":
        return "brew"
    fields: dict[str, str] = {}
    for line in os_release.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip().strip('"')
    tokens = {fields.get("ID", "").lower()}
    tokens.update(fields.get("ID_LIKE", "").lower().split())
    if tokens & {"debian", "ubuntu"}:
        return "apt"
    if tokens & {"fedora", "rhel", "centos"}:
        return "dnf"
    if tokens & {"arch", "archlinux"}:
        return "pacman"
    return None


def _fix(tool: str, manager: str | None, *, pyexe: str | None = None) -> str:
    if manager is not None:
        command = _FIXES[tool][manager]
        # "py -m ensurepip" presumes the launcher — name the
        # interpreter the probe actually resolved instead.
        if tool == "pip" and manager == "winget" and pyexe is not None:
            command = f"{Path(pyexe).stem} -m ensurepip"
        return f"Install it: {command}."
    return (
        f"Install '{_PKG_NAMES[tool]}' with your package manager "
        "(apt/dnf/pacman/brew/winget)."
    )


# --- per-piece findings ---------------------------------------------------


def _python_finding(
    which: Callable[[str], str | None],
    probe: Callable[
        [Sequence[str]], subprocess.CompletedProcess[str] | None
    ],
    manager: str | None,
) -> tuple[Finding, str | None]:
    """(finding, resolved interpreter path) — the path feeds the
    ``-m venv``/``-m pip`` probes; ``None`` when nothing resolved.

    ``which`` only proves a file exists — a Microsoft Store
    ``python.exe`` alias or a broken shim resolves but cannot run, so
    each candidate must answer ``--version`` before it counts. A broken
    shim falls through to the next name (a working ``py`` launcher
    behind a dead ``python`` still wins)."""
    broken: list[str] = []
    for name in _PYTHON_NAMES:
        resolved = which(name)
        if not resolved:
            continue
        res = probe([resolved, "--version"])
        if res is not None and res.returncode == 0:
            alias = "" if name == "python3" else f" (resolved as '{name}')"
            return (
                Finding(
                    CHECK_ID, Status.PASS,
                    f"python on PATH at {resolved}{alias}",
                ),
                resolved,
            )
        if res is None:
            # Unanswerable probe is 'unverified', never 'missing' —
            # still hand the path downstream so the venv/pip probes
            # can draw their own verdicts.
            return (
                Finding(
                    CHECK_ID, Status.WARN,
                    f"python on PATH at {resolved} but '--version' "
                    "could not be probed",
                ),
                resolved,
            )
        broken.append(name)
    if broken:
        names = "', '".join(broken)
        detail = (
            f"'{names}' resolves on PATH but fails "
            "to run (broken shim or Store alias); "
        )
    else:
        detail = "'python3', 'python' and 'py' are all missing; "
    return (
        Finding(
            CHECK_ID, Status.FAIL,
            f"no working Python interpreter on PATH — {detail}"
            "pytest and venv creation cannot run",
            fix=_fix("python", manager),
        ),
        None,
    )


def _pip_finding(
    which: Callable[[str], str | None],
    probe: Callable[
        [Sequence[str]], subprocess.CompletedProcess[str] | None
    ],
    pyexe: str | None,
    manager: str | None,
) -> Finding:
    for name in _PIP_NAMES:
        resolved = which(name)
        if resolved:
            return Finding(
                CHECK_ID, Status.PASS,
                f"'{name}' on PATH at {resolved}",
            )
    if pyexe is not None:
        res = probe([pyexe, "-m", "pip", "--version"])
        if res is not None and res.returncode == 0:
            return Finding(
                CHECK_ID, Status.WARN,
                "no 'pip'/'pip3' script on PATH but 'python -m pip' "
                "answers — commands invoking bare 'pip' still fail",
                fix=_fix("pip", manager),
            )
        detail = (
            " and 'python -m pip' does not answer"
            if res is not None
            else " and 'python -m pip' could not be probed"
        )
    else:
        detail = " and no interpreter exists to try 'python -m pip'"
    return Finding(
        CHECK_ID, Status.FAIL,
        f"'pip'/'pip3' not on PATH{detail}",
        fix=_fix("pip", manager, pyexe=pyexe),
    )


def _venv_finding(
    probe: Callable[
        [Sequence[str]], subprocess.CompletedProcess[str] | None
    ],
    pyexe: str | None,
    manager: str | None,
) -> Finding | None:
    """``None`` when no interpreter exists — the python FAIL already
    names that; cascading a venv FAIL would double-count one cause."""
    if pyexe is None:
        return None
    res = probe([pyexe, "-m", "venv", "--help"])
    if res is None:
        return Finding(
            CHECK_ID, Status.WARN,
            f"'{pyexe} -m venv --help' did not answer — venv support "
            "unverified",
        )
    if res.returncode != 0:
        return Finding(
            CHECK_ID, Status.FAIL,
            f"'{pyexe} -m venv' fails — the venv module is unavailable "
            "(distros split it into a separate package)",
            fix=_fix("venv", manager),
        )
    ensurepip = probe([pyexe, "-c", "import ensurepip"])
    if ensurepip is None:
        return Finding(
            CHECK_ID, Status.WARN,
            "'import ensurepip' did not answer — pip bootstrap inside "
            "fresh venvs unverified",
        )
    if ensurepip.returncode != 0:
        return Finding(
            CHECK_ID, Status.FAIL,
            "'python -m venv' works but ensurepip is missing — fresh "
            "venvs get no pip (Debian splits this into 'python3-venv')",
            fix=_fix("venv", manager),
        )
    # Importable is not bootstrappable: a partial install can ship
    # ensurepip with no pip wheel, and ``python -m venv`` then dies
    # inside ``_setup_pip``. The lookup mirrors ensurepip's own
    # ``_get_pip_whl_path_ctx``: ``pip-*.whl`` under sysconfig's
    # ``WHEEL_PKG_DIR`` (the Debian-family ``python3-pip-whl``
    # layout, where ``_bundled`` may not exist at all) when set,
    # else ensurepip's ``_bundled`` dir.
    wheels_probe = (
        "import ensurepip, glob, os, sys, sysconfig; "
        "dirs = [sysconfig.get_config_var('WHEEL_PKG_DIR'), "
        "os.path.join(os.path.dirname(ensurepip.__file__), "
        "'_bundled')]; "
        "sys.exit(0 if any(glob.glob(os.path.join(d, 'pip-*.whl')) "
        "for d in dirs if d and os.path.isdir(d)) else 1)"
    )
    wheels = probe([pyexe, "-c", wheels_probe])
    if wheels is None:
        return Finding(
            CHECK_ID, Status.WARN,
            "ensurepip imports but its bundled-wheel probe did not "
            "answer — pip bootstrap inside fresh venvs unverified",
        )
    if wheels.returncode != 0:
        return Finding(
            CHECK_ID, Status.FAIL,
            "ensurepip imports but bundles no pip wheel — fresh venvs "
            "fail during pip bootstrap",
            fix=_fix("venv", manager),
        )
    return Finding(
        CHECK_ID, Status.PASS,
        f"'{pyexe} -m venv' works and ensurepip can bootstrap pip",
    )


def _binary_finding(
    tool: str,
    names: tuple[str, ...],
    which: Callable[[str], str | None],
    manager: str | None,
) -> Finding:
    for name in names:
        resolved = which(name)
        if resolved:
            if name != tool:
                # An alias binary does not make the shell resolve the
                # real name: with only 'nodejs', every 'node ...'
                # invocation in test/build scripts still fails.
                return Finding(
                    CHECK_ID, Status.WARN,
                    f"only '{name}' on PATH at {resolved} — commands "
                    f"invoking '{tool}' still fail",
                    fix=_fix(tool, manager),
                )
            return Finding(
                CHECK_ID, Status.PASS,
                f"'{tool}' on PATH at {resolved}",
            )
    return Finding(
        CHECK_ID, Status.FAIL,
        f"'{tool}' is not on PATH — the build/test toolchain needs it",
        fix=_fix(tool, manager),
    )


def run(
    ctx: Context,
    *,
    which: Callable[[str], str | None] | None = None,
    probe: Callable[
        [Sequence[str]], subprocess.CompletedProcess[str] | None
    ] | None = None,
    plat: str | None = None,
    os_release: str | None = None,
) -> list[Finding]:
    which = which or _which
    probe = probe or _probe
    plat = plat or platform_name()
    if os_release is None:
        os_release = _os_release() if plat == "linux" else ""
    manager = _manager(plat, os_release)

    findings: list[Finding] = []
    py_finding, pyexe = _python_finding(which, probe, manager)
    findings.append(py_finding)
    findings.append(_pip_finding(which, probe, pyexe, manager))
    venv = _venv_finding(probe, pyexe, manager)
    if venv is not None:
        findings.append(venv)
    findings.append(_binary_finding("node", _NODE_NAMES, which, manager))
    findings.append(_binary_finding("npm", ("npm",), which, manager))
    findings.append(_binary_finding("git", ("git",), which, manager))
    return findings
