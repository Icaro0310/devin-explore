"""Check 10 — deps-internals: silently stale internal deps inside ``.venv``.

A repo venv keeps working long after it stopped reflecting the checkout:
``__editable__.devin_*.pth`` installs point at ``packages/*/src`` dirs
that may since have moved or been absorbed, and non-editable ``devin_*``
wheels installed once keep shadowing the source tree at whatever version
they froze at. Nothing fails loudly — imports silently resolve to stale
or phantom code. This check audits the checkout's own venv, fail-loud,
one finding per issue.

Scope: ``ctx.cwd`` is the repo checkout; only ``<cwd>/.venv`` is probed
(the uv/venv convention every check in this repo assumes — a bare
``venv/`` dir is deliberately ignored, and documented here rather than
guessed at). No ``.venv`` → the check reports 'not detected', same
convention as health/disk on a machine without Devin stores — a checkout
without a venv is a valid environment, never a failure.
``ctx.packages`` does not narrow the scan: the venv is checkout-global,
so every internal dep inside it is compared whenever the venv exists.

site-packages resolves via ``lib/python*/site-packages`` (``lib64`` on
distro layouts) and ``Lib/site-packages`` (Windows); when several dirs
match — a lib64 mirror or a ``pythonX.Y`` dir left behind by an
interpreter upgrade — ``pyvenv.cfg``'s ``version`` picks the active one
and sorted order only breaks the remaining ties.

Verdicts:

- FAIL — an editable ``.pth`` target does not exist (dangling install:
  the package moved or was absorbed; the venv imports nothing — or a
  stale copy left behind);
- WARN — an editable target resolves *outside* the checkout (the venv is
  bound to a different workspace), a ``.pth`` is unreadable or carries
  no resolvable target, or a non-editable ``devin_*`` dist's installed
  version differs from the same-named ``packages/*/pyproject.toml``
  version (stale wheel shadowing fresh source). A ``devin_*`` dist with
  no same-named package in the checkout is foreign tooling and ignored;
- PASS — every editable target resolves inside the checkout and each
  matched dist's version agrees with its pyproject.

Editable installs are recognized via ``direct_url.json``
``dir_info.editable`` plus the ``__editable__*`` .pth set, so the
dist-info half never double-counts them. Packages are never imported —
names and versions come from ``*.dist-info`` metadata files only.

Read-only: venv/site-packages introspection only — nothing is
installed, removed or rewritten.
"""

from __future__ import annotations

import ast
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib
from packaging.version import InvalidVersion, Version

from devin_doctor.model import Context, Finding, Status

CHECK_ID = "deps-internals"

_VENV_DIRNAME = ".venv"
# POSIX venvs lay out lib/pythonX.Y/site-packages (lib64 on some distro
# layouts); Windows venvs use Lib/site-packages.
_SITE_PATTERNS = (
    "lib/python*/site-packages",
    "lib64/python*/site-packages",
    "Lib/site-packages",
)
_EDITABLE_PTH_GLOB = "__editable__*.pth"
_EDITABLE_PTH_PREFIX = "__editable__."
_DIST_INFO_SUFFIX = ".dist-info"
# `import __editable___devin_foo_1_0_0_finder` — setuptools' finder-mode
# editable installs carry an import line instead of a path line; the real
# targets live as quoted absolute paths in the companion *_finder.py.
_IMPORT_LINE_RE = re.compile(r"^import\s+([A-Za-z_][\w.]*)")
_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")
# devin_* / devin-* installed distributions are 'internal'.
_INTERNAL_DIST_PREFIX = "devin_"


def _norm(name: str | None) -> str:
    return (name or "").replace("-", "_").lower()


# --- checkout package scope (same shapes as publish/path-check) --------


def _child_dirs(root: Path) -> list[Path]:
    try:
        return sorted(
            d for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    except OSError:
        return []


def _package_dirs(cwd: Path) -> list[Path]:
    packages_root = cwd / "packages"
    if packages_root.is_dir():
        return _child_dirs(packages_root)
    if cwd.name == "packages":
        children = _child_dirs(cwd)
        if children:
            return children
    if (cwd / "pyproject.toml").is_file() or (cwd / "src").is_dir():
        return [cwd]
    return []


def _label(cwd: Path, pkg: Path) -> str:
    try:
        rel = pkg.relative_to(cwd).as_posix()
    except ValueError:
        return pkg.name
    return pkg.name if rel == "." else rel


def _load_pyproject(pkg: Path) -> dict[str, Any] | None:
    path = pkg / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError):
        return None


def _project_name(meta: dict[str, Any] | None) -> str | None:
    if not isinstance(meta, dict):
        return None
    project = meta.get("project")
    if isinstance(project, dict) and isinstance(project.get("name"), str):
        return project["name"]
    return None


def _project_version(meta: dict[str, Any] | None) -> str | None:
    """Literal ``[project].version`` — a dynamic/absent one cannot be
    compared against and is reported as unverifiable, not skipped."""
    if not isinstance(meta, dict):
        return None
    project = meta.get("project")
    if isinstance(project, dict) and isinstance(project.get("version"), str):
        return project["version"] or None
    return None


def _same_version(a: str, b: str) -> bool | None:
    """PEP 440 equality — installed metadata normalizes ``1.0.0-rc1``
    to ``1.0.0rc1`` while pyproject keeps the literal spelling, and
    ``1.0`` equals ``1.0.0``. ``None`` when either side is not a valid
    PEP 440 version: approximating equality there invents drift that
    does not exist, so the pair reports as unverifiable instead."""
    if a == b:
        return True
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return None


# --- venv / site-packages resolution (injectable) -----------------------


def _venv_version(venv: Path) -> str | None:
    """``X.Y`` of the interpreter the venv actually runs, from
    ``pyvenv.cfg`` — the site-packages dir to audit must match it, or a
    stale pre-upgrade ``pythonX.Y`` dir silently wins over the active one."""
    try:
        text = (venv / "pyvenv.cfg").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "version":
            parts = value.strip().split(".")
            return ".".join(parts[:2]) if len(parts) >= 2 else None
    return None


def _site_packages(venv: Path) -> Path | None:
    """The venv's site-packages dir, or ``None`` when it has none.

    Every matching ``_SITE_PATTERNS`` layout is collected. A venv whose
    interpreter was upgraded keeps the old ``pythonX.Y`` dir beside the
    active one, so several matches resolve via ``pyvenv.cfg``'s version;
    the first in sorted order is only the final tie-break — deterministic,
    and identical single-interpreter venvs agree on it.
    """
    matches: list[Path] = []
    for pattern in _SITE_PATTERNS:
        matches += [p for p in venv.glob(pattern) if p.is_dir()]
    if not matches:
        return None
    if len(matches) > 1:
        xy = _venv_version(venv)
        if xy:
            preferred = [p for p in matches if p.parent.name == f"python{xy}"]
            if preferred:
                return min(preferred)
    return min(matches)


def _pth_label(pth: Path) -> str:
    """``__editable__.devin_foo-1.0.0.pth`` → ``devin_foo-1.0.0`` — the
    dist name+version as the installer wrote it."""
    name = pth.name
    if name.startswith(_EDITABLE_PTH_PREFIX) and name.endswith(".pth"):
        return name[len(_EDITABLE_PTH_PREFIX):-len(".pth")]
    return name


def _finder_targets(finder: Path) -> list[str]:
    """Absolute paths quoted inside a ``__editable___*_finder.py`` — its
    MAPPING values are the dirs the editable install puts on sys.path.

    Finder files are Python source: ``MAPPING``/``NAMESPACES`` values are
    string literals, so a Windows path is spelled ``'C:\\\\work\\\\repo'``
    and must be decoded via ``ast.literal_eval`` — probing the doubled-
    backslash spelling would flag a live source dir as missing."""
    try:
        text = finder.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    targets: list[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in tree.body:
            if not (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(t, ast.Name) and t.id in ("MAPPING", "NAMESPACES")
                    for t in node.targets
                )
            ):
                continue
            seg = ast.get_source_segment(text, node.value) or ""
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError, MemoryError):
                value = None
            if "\\\\" not in seg or value is None:
                # No real escapes to decode (or an unparsable literal):
                # quoted text is verbatim path text — decoding single-
                # backslash pseudo-escapes like \r or \t would mangle it.
                entries = [m.group(1) for m in _QUOTED_RE.finditer(seg)]
            elif isinstance(value, dict):
                entries = value.values()
            else:
                continue
            for entry in entries:
                for path in [entry] if isinstance(entry, str) else entry:
                    if isinstance(path, str) and os.path.isabs(path):
                        targets.append(path)
    if not targets:  # nonstandard finder — decode bare literals instead
        for m in _QUOTED_RE.finditer(text):
            decoded = m.group(1)
            if "\\\\" in m.group(0):
                try:
                    value = ast.literal_eval(m.group(0))
                    if isinstance(value, str):
                        decoded = value
                except (ValueError, TypeError, SyntaxError, MemoryError):
                    pass
            if os.path.isabs(decoded):
                targets.append(decoded)
    return targets


def _pth_targets(pth: Path, site_dir: Path) -> list[str] | None:
    """The path(s) an editable ``.pth`` puts on sys.path.

    Path-style editable installs (uv, setuptools compat mode) carry the
    target on the first meaningful line; setuptools' finder mode carries
    an ``import __editable___*_finder`` line instead — then the targets
    are the absolute paths quoted in the companion module. Every
    meaningful line is considered, so mixed files still verify. ``None``
    means the .pth could not be read; an empty list means it was read
    but no target resolved.
    """
    try:
        text = pth.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    targets: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _IMPORT_LINE_RE.match(line)
        if m:
            targets += _finder_targets(site_dir / f"{m.group(1)}.py")
        else:
            targets.append(line)
    return targets


# --- installed distributions (injectable; never imported) ---------------


@dataclass(frozen=True)
class _Dist:
    """One ``*.dist-info`` under site-packages."""

    name: str
    version: str | None
    editable: bool


def _dist_from_dirname(base: str) -> tuple[str | None, str | None]:
    """``devin_foo-1.0.0`` → ``('devin_foo', '1.0.0')`` — normalized dist
    names never contain ``-``, so the last dash splits off the version."""
    name, sep, version = base.rpartition("-")
    if not sep or not name:
        return (base or None), None
    return name, version


def _read_dist_metadata(dist_info: Path) -> tuple[str | None, str | None]:
    """(Name, Version) from the dist-info's METADATA headers — the
    header block ends at the first blank line; a missing or unreadable
    file yields ``(None, None)`` and the dir name is parsed instead."""
    try:
        text = (dist_info / "METADATA").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None, None
    name = version = None
    for line in text.splitlines():
        if not line.strip():
            break
        if line.startswith("Name:"):
            name = line[len("Name:"):].strip()
        elif line.startswith("Version:"):
            version = line[len("Version:"):].strip()
    return name, version


def _is_editable_install(dist_info: Path, editable_bases: set[str]) -> bool:
    """``direct_url.json`` ``dir_info.editable`` (how pip/uv mark
    editable installs) or a same-named ``__editable__`` .pth — either
    means the dist-info belongs to a live editable the .pth half already
    checks, so it must not also count as a shadowing wheel."""
    try:
        data = json.loads(
            (dist_info / "direct_url.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        dir_info = data.get("dir_info")
        if isinstance(dir_info, dict) and dir_info.get("editable") is True:
            return True
    return dist_info.name[: -len(_DIST_INFO_SUFFIX)] in editable_bases


def _installed_dists(site_dir: Path) -> list[_Dist]:
    editable_bases = {
        _pth_label(p) for p in site_dir.glob(_EDITABLE_PTH_GLOB)
    }
    out: list[_Dist] = []
    for d in sorted(site_dir.glob(f"*{_DIST_INFO_SUFFIX}")):
        if not d.is_dir():
            continue
        name, version = _read_dist_metadata(d)
        if name is None or version is None:
            dn, dv = _dist_from_dirname(d.name[: -len(_DIST_INFO_SUFFIX)])
            name = name or dn
            version = version or dv
        if not name:
            continue
        out.append(_Dist(
            name=name,
            version=version,
            editable=_is_editable_install(d, editable_bases),
        ))
    return out


# --- findings ------------------------------------------------------------


def _inside(child: str, root: str) -> bool:
    """``child`` (normcase + realpath) lives under ``root`` — ValueError
    covers Windows drive boundaries."""
    try:
        return os.path.commonpath((child, root)) == root
    except ValueError:
        return False


def _editable_findings(
    site_dir: Path,
    checkout_root: str,
    pth_targets: Callable[[Path, Path], list[str] | None],
) -> tuple[list[Finding], int]:
    """One finding per broken/foreign editable target; returns the
    findings plus the count of fully healthy editable installs."""
    findings: list[Finding] = []
    healthy = 0
    for pth in sorted(site_dir.glob(_EDITABLE_PTH_GLOB)):
        label = _pth_label(pth)
        targets = pth_targets(pth, site_dir)
        if targets is None:
            findings.append(Finding(
                CHECK_ID, Status.WARN,
                f"editable '{label}' ({pth.name}) is unreadable — "
                "target unverified",
                fix="Reinstall the editable package or recreate .venv.",
            ))
            continue
        if not targets:
            findings.append(Finding(
                CHECK_ID, Status.WARN,
                f"editable '{label}' carries no resolvable target — "
                "unverified",
                fix="Reinstall the editable package or recreate .venv.",
            ))
            continue
        broken = False
        for raw_target in targets:
            target = (
                Path(raw_target)
                if os.path.isabs(raw_target)
                else site_dir / raw_target  # site.addpackage semantics
            )
            if not target.exists():
                findings.append(Finding(
                    CHECK_ID, Status.FAIL,
                    f"editable '{label}' points at '{raw_target}' which "
                    "no longer exists — the package moved or was "
                    "absorbed; the venv imports stale or phantom code",
                    fix="Reinstall the editable from its new location "
                    "(uv sync / pip install -e) or recreate .venv.",
                ))
                broken = True
                continue
            resolved = os.path.normcase(os.path.realpath(target))
            if not _inside(resolved, checkout_root):
                findings.append(Finding(
                    CHECK_ID, Status.WARN,
                    f"editable '{label}' points at '{raw_target}' "
                    "outside this checkout — the venv is bound to a "
                    "different workspace",
                    fix="Recreate .venv in this checkout so editables "
                    "resolve against it.",
                ))
                broken = True
        if not broken:
            healthy += 1
    return findings, healthy


def _dist_findings(
    dists: list[_Dist],
    projects: dict[str, tuple[str, str | None, str]],
) -> tuple[list[Finding], int]:
    """Version comparison per non-editable ``devin_*`` dist that has a
    same-named checkout package; returns findings plus the count of
    version-matched dists."""
    findings: list[Finding] = []
    matched = 0
    for dist in dists:
        if dist.editable:
            continue  # the .pth half owns editable installs
        if not _norm(dist.name).startswith(_INTERNAL_DIST_PREFIX):
            continue
        project = projects.get(_norm(dist.name))
        if project is None:
            continue  # foreign tool — not this repo's problem
        proj_name, proj_version, proj_label = project
        same = (
            _same_version(dist.version, proj_version)
            if dist.version and proj_version
            else None
        )
        if same is True:
            matched += 1
            continue
        if same is False:
            findings.append(Finding(
                CHECK_ID, Status.WARN,
                f"'{proj_name}' is installed in .venv at {dist.version} "
                f"but {proj_label} declares {proj_version} — a stale "
                "wheel shadows fresh source",
                fix="Reinstall the package into .venv (uv sync / "
                "pip install) or remove the wheel so the editable "
                "install wins.",
            ))
            continue
        findings.append(Finding(
            CHECK_ID, Status.WARN,
            f"'{proj_name}' version is unverifiable — installed "
            f"{dist.version or '(unknown)'} vs a declared "
            f"{proj_version or '(none)'} in {proj_label}",
        ))
    return findings, matched


def run(
    ctx: Context,
    *,
    site_packages: Callable[[Path], Path | None] = _site_packages,
    pth_targets: Callable[[Path, Path], list[str] | None] = _pth_targets,
    installed_dists: Callable[[Path], list[_Dist]] = _installed_dists,
) -> list[Finding]:
    venv = ctx.cwd / _VENV_DIRNAME
    if not venv.is_dir():
        # No repo venv — a checkout without one is a valid environment:
        # the check reports 'not detected', like health/disk without a
        # Devin store.
        return []
    site_dir = site_packages(venv)
    if site_dir is None:
        return [Finding(
            CHECK_ID, Status.WARN,
            f"{venv} has no lib/python*/site-packages — internal deps "
            "unverified",
            fix="The venv looks broken — recreate it (uv sync / "
            "python -m venv .venv).",
        )]
    checkout_root = os.path.normcase(os.path.realpath(ctx.cwd))

    # Checkout project names → (display name, version, label): the
    # 'same-named package' side of the dist comparison.
    projects: dict[str, tuple[str, str | None, str]] = {}
    for pkg in _package_dirs(ctx.cwd):
        meta = _load_pyproject(pkg)
        name = _project_name(meta)
        if name:
            projects.setdefault(_norm(name), (
                name, _project_version(meta), _label(ctx.cwd, pkg),
            ))

    findings, healthy = _editable_findings(
        site_dir, checkout_root, pth_targets
    )
    dist_findings, matched = _dist_findings(installed_dists(site_dir), projects)
    findings += dist_findings
    if healthy:
        findings.append(Finding(
            CHECK_ID, Status.PASS,
            f"{healthy} editable install(s) in .venv resolve inside the "
            "checkout",
        ))
    if matched:
        findings.append(Finding(
            CHECK_ID, Status.PASS,
            f"{matched} installed devin_* dist(s) match the checkout's "
            "pyproject versions",
        ))
    if not findings:
        findings.append(Finding(
            CHECK_ID, Status.PASS,
            f"no editable installs or internal (devin_*) dists under "
            f"{venv} — nothing internal to verify",
        ))
    return findings
