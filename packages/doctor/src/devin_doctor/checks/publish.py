"""Check 8 — publish-readiness: can a ``packages/<name>`` dir ship to PyPI?

The repo's PyPI fan-out (``.github/workflows/pypi-publish.yml``) resolves a
tag/dispatch name to ``packages/<name>`` + module, then gates the upload on
the wheel building with ``__version__ == [project].version``. This check
replays that contract locally, per package, fail-loud — every missing or
mismatched item is named in its own finding:

- ``pyproject.toml`` parses and ``[project]`` carries ``name``/``version``;
- ``src/`` layout holds the import package (flat modules are named);
- ``src/<mod>/__init__.py`` ``__version__`` equals ``[project].version``;
- every declared ``[project.scripts]`` entry point resolves under ``src/``;
- the wheel actually builds — ``python -m build --wheel --no-isolation``
  run against a *copy* in a temp dir (the package dir is never written
  to). When ``build``/``setuptools`` are absent — or ``--offline`` /
  ``DEVIN_DOCTOR_OFFLINE=1`` pins the run local — a static-equivalent
  pass over ``[build-system]`` stands in;
- ``pypi-publish.yml``'s ``resolve`` case covers the package (WARN-only:
  not every package under ``packages/`` publishes).

Scope: ``ctx.cwd`` is the fan-out root when it holds ``packages/`` (every
child dir is a candidate) or the container dir named ``packages`` itself;
a lone dir with a ``pyproject.toml`` is checked as a single package.
``ctx.packages`` (``--package NAME``) narrows the fan-out by dir or
project name. Nothing package-shaped → one PASS 'nothing to verify'
finding (the ``config`` check's convention — a machine without a repo
checkout is a valid environment).
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import tomllib

from devin_doctor.model import Context, Finding, Status

CHECK_ID = "publish-readiness"

_BUILD_TIMEOUT_S = 240
# Bounds the upward hunt for .github/workflows/pypi-publish.yml — enough
# for packages/<name> (2 levels) plus symlinked/nested worktree layouts.
_MAX_UPWARD = 6

_VERSION_RE = re.compile(r"^__version__\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE)
_ARM_RE = re.compile(
    r"^\s*[A-Za-z0-9_-]+\)\s+echo\s+\"dir=packages/([^\"]+)\"", re.MULTILINE
)
_OPTIONS_BLOCK_RE = re.compile(r"options:\s*\n((?:\s*-\s*\S+\s*\n)+)")
_IGNORE_ON_COPY = shutil.ignore_patterns(
    ".git", "dist", "build", "__pycache__", "*.egg-info",
    ".venv", "*.pyc", ".mypy_cache", ".pytest_cache", ".ruff_cache",
)


def _child_dirs(root: Path) -> list[Path]:
    try:
        return sorted(
            d for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    except OSError:
        return []


def _package_dirs(cwd: Path) -> list[Path]:
    """Package dirs in scope, sorted.

    ``cwd/packages`` exists → every non-hidden child is a candidate (a
    missing ``pyproject.toml`` inside is itself a finding). A ``cwd``
    named ``packages`` → same child scan. Else ``cwd`` with a
    ``pyproject.toml`` — or a bare ``src/`` dir, the mark of an
    in-progress package — → the single package. Anything else → empty.
    """
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
    """Finding prefix — ``packages/<name>`` in fan-out mode, the bare
    dir name otherwise. ``as_posix`` keeps it stable across OSes."""
    try:
        rel = pkg.relative_to(cwd).as_posix()
    except ValueError:
        return pkg.name
    return pkg.name if rel == "." else rel


def _load_pyproject(pkg: Path) -> tuple[dict[str, Any] | None, str | None]:
    """(parsed TOML, error). ``error`` is a human-ready message."""
    path = pkg / "pyproject.toml"
    if not path.is_file():
        return None, "no pyproject.toml — nothing publishable here"
    try:
        return tomllib.loads(path.read_text(encoding="utf-8")), None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
        return None, f"pyproject.toml does not parse: {exc}"


def _project_name(meta: dict[str, Any] | None) -> str | None:
    if not isinstance(meta, dict):
        return None
    project = meta.get("project")
    if isinstance(project, dict) and isinstance(project.get("name"), str):
        return project["name"]
    return None


def _src_packages(src: Path) -> list[str]:
    """Import-package dirs under ``src/`` (dir + ``__init__.py``)."""
    if not src.is_dir():
        return []
    try:
        return sorted(
            d.name for d in src.iterdir()
            if d.is_dir() and (d / "__init__.py").is_file()
        )
    except OSError:
        return []


def _module_name(project_name: str | None, src_pkgs: list[str]) -> str | None:
    """The module expected to hold ``__version__``: the name-derived
    package (``devin-doctor`` → ``devin_doctor``) when it exists under
    ``src/``, else the only src package when unambiguous."""
    derived = project_name.replace("-", "_").lower() if project_name else None
    if derived and derived in src_pkgs:
        return derived
    if len(src_pkgs) == 1:
        return src_pkgs[0]
    return derived


def _module_file(src: Path, module: str) -> Path | None:
    """The file backing dotted ``module`` under ``src/``, if any."""
    rel = module.replace(".", "/")
    for candidate in (src / f"{rel}.py", src / rel / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _attr_declared(module_file: Path, attr: str) -> bool:
    try:
        text = module_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(
        re.search(rf"^(?:def|class)\s+{re.escape(attr)}\b", text, re.MULTILINE)
        or re.search(rf"^{re.escape(attr)}\s*=", text, re.MULTILINE)
    )


def _find_publish_workflow(start: Path) -> Path | None:
    """Nearest ``.github/workflows/pypi-publish.yml`` at or above
    ``start`` (bounded — a package dir is 2 levels below the root)."""
    current = start
    for _ in range(_MAX_UPWARD + 1):
        candidate = current / ".github" / "workflows" / "pypi-publish.yml"
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent
    return None


def _publish_coverage(workflow: Path) -> set[str] | None:
    """Package names the fan-out resolves: ``dir=packages/<name>`` arms
    plus the ``workflow_dispatch`` ``options`` list. ``None`` when the
    file cannot be read."""
    try:
        text = workflow.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    covered = set(_ARM_RE.findall(text))
    block = _OPTIONS_BLOCK_RE.search(text)
    if block:
        covered.update(
            re.findall(r"^\s*-\s*([A-Za-z0-9_-]+)\s*$", block.group(1), re.MULTILINE)
        )
    return covered


def _build_available() -> bool:
    """``python -m build --no-isolation`` can run: the frontend and the
    setuptools backend (the repo's declared backend) are both importable.
    No network is needed — isolation is off."""
    return (
        importlib.util.find_spec("build") is not None
        and importlib.util.find_spec("setuptools") is not None
    )


def _build_wheel(pkg: Path) -> str | None:
    """Build the wheel from a temp-dir *copy* of ``pkg``. Returns an
    error string, or ``None`` when a ``.whl`` lands. Read-only against
    the package dir — only the copy is built."""
    with tempfile.TemporaryDirectory(prefix="devin-doctor-build-") as tmp:
        work = Path(tmp) / "pkg"
        outdir = Path(tmp) / "dist"
        try:
            shutil.copytree(pkg, work, ignore=_IGNORE_ON_COPY)
        except OSError as exc:
            return f"could not stage a build copy: {exc}"
        try:
            proc = subprocess.run(
                [
                    sys.executable, "-m", "build", "--wheel",
                    "--no-isolation", "--outdir", str(outdir), str(work),
                ],
                capture_output=True, text=True, check=False,
                timeout=_BUILD_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return f"python -m build exceeded {_BUILD_TIMEOUT_S}s"
        except OSError as exc:
            return f"python -m build could not start: {exc}"
        wheels = list(outdir.glob("*.whl")) if outdir.is_dir() else []
        if proc.returncode == 0 and wheels:
            return None
        for stream in (proc.stderr, proc.stdout):
            lines = (stream or "").strip().splitlines()
            if lines:
                return "\n".join(lines[-5:])
        return f"exit {proc.returncode}, no wheel produced"


def _metadata_findings(label: str, meta: dict[str, Any] | None, err: str | None):
    """pyproject parse + [project] name/version gate."""
    if err is not None:
        return [Finding(
            CHECK_ID, Status.FAIL, f"{label}: {err}",
            fix="Restore a valid pyproject.toml — the fan-out builds "
            "from it.",
        )], None
    project = meta.get("project")
    if not isinstance(project, dict):
        return [Finding(
            CHECK_ID, Status.FAIL,
            f"{label}: pyproject.toml has no [project] table",
            fix="The PyPI fan-out needs [project] metadata.",
        )], None
    missing = [
        key for key in ("name", "version")
        if not isinstance(project.get(key), str) or not project.get(key)
    ]
    dynamic = project.get("dynamic")
    if "version" in missing and isinstance(dynamic, list) and (
        "version" in dynamic
    ):
        missing.remove("version")
    if missing:
        return [Finding(
            CHECK_ID, Status.FAIL,
            f"{label}: [project] is missing {', '.join(missing)}",
            fix="Add the field(s) — the publish gate reads name/version "
            "from pyproject.toml.",
        )], project
    return [Finding(
        CHECK_ID, Status.PASS,
        f"{label}: {project['name']} "
        + (project.get("version") or "(dynamic version)"),
    )], project


def _layout_findings(
    label: str, pkg: Path, src_pkgs: list[str], mod: str | None
) -> list[Finding]:
    src = pkg / "src"
    if not src.is_dir():
        flat = []
        if mod and (pkg / f"{mod}.py").is_file():
            flat.append(f"{mod}.py")
        if mod and (pkg / mod / "__init__.py").is_file():
            flat.append(f"{mod}/")
        detail = f" — flat module found: {', '.join(flat)}" if flat else ""
        return [Finding(
            CHECK_ID, Status.FAIL,
            f"{label}: no src/ layout{detail}",
            fix="Move the import package under src/<module>/ — the "
            "fan-out builds a src-layout wheel.",
        )]
    if not src_pkgs:
        return [Finding(
            CHECK_ID, Status.FAIL,
            f"{label}: src/ exists but holds no package "
            "(no */__init__.py)",
            fix="Add the import package under src/<module>/.",
        )]
    return [Finding(
        CHECK_ID, Status.PASS,
        f"{label}: src layout ok (src/{src_pkgs[0]}"
        + (", …" if len(src_pkgs) > 1 else "") + ")",
    )]


def _version_findings(
    label: str,
    pkg: Path,
    project: dict[str, Any] | None,
    mod: str | None,
) -> list[Finding]:
    if project is None:
        return []
    version = project.get("version")
    dynamic = project.get("dynamic")
    if not isinstance(version, str) or not version:
        if isinstance(dynamic, list) and "version" in dynamic:
            return [Finding(
                CHECK_ID, Status.WARN,
                f"{label}: version is dynamic — __version__ sync cannot "
                "be verified statically",
            )]
        return []  # the metadata finding already named it
    if mod is None:
        return [Finding(
            CHECK_ID, Status.FAIL,
            f"{label}: cannot resolve the import module for "
            f"'{project.get('name')}' under src/",
            fix="Name the src/ package after the project "
            "(devin-x → devin_x) or keep exactly one.",
        )]
    init = pkg / "src" / mod / "__init__.py"
    if not init.is_file():
        return [Finding(
            CHECK_ID, Status.FAIL,
            f"{label}: src/{mod}/__init__.py missing — __version__ gate "
            "has nothing to read",
        )]
    try:
        text = init.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [Finding(
            CHECK_ID, Status.FAIL,
            f"{label}: src/{mod}/__init__.py unreadable: {exc}",
        )]
    match = _VERSION_RE.search(text)
    if match is None:
        return [Finding(
            CHECK_ID, Status.FAIL,
            f"{label}: src/{mod}/__init__.py has no __version__",
            fix=f'Add __version__ = "{version}" — the wheel gate '
            "compares it to [project].version.",
        )]
    if match.group(1) != version:
        return [Finding(
            CHECK_ID, Status.FAIL,
            f"{label}: __version__ '{match.group(1)}' != pyproject "
            f"version '{version}'",
            fix="Bump both together — the verify-wheel-version gate "
            "blocks this release.",
        )]
    return [Finding(
        CHECK_ID, Status.PASS,
        f"{label}: __version__ == [project].version ({version})",
    )]


def _scripts_findings(
    label: str, pkg: Path, project: dict[str, Any] | None
) -> list[Finding]:
    if project is None:
        return []
    scripts = project.get("scripts")
    if scripts is None:
        return [Finding(
            CHECK_ID, Status.PASS,
            f"{label}: no [project.scripts] — library-only package",
        )]
    if not isinstance(scripts, dict) or not scripts:
        return [Finding(
            CHECK_ID, Status.WARN,
            f"{label}: [project.scripts] is empty or not a table",
        )]
    src = pkg / "src"
    findings: list[Finding] = []
    unverifiable: list[str] = []
    for command, target in scripts.items():
        module, sep, attr = str(target).partition(":")
        if not sep or not module or not attr:
            findings.append(Finding(
                CHECK_ID, Status.FAIL,
                f"{label}: script '{command} = {target}' is not "
                "'module:attr'",
                fix="Entry points must read 'module.path:callable'.",
            ))
            continue
        module_file = _module_file(src, module) if src.is_dir() else None
        if module_file is None:
            findings.append(Finding(
                CHECK_ID, Status.FAIL,
                f"{label}: script '{command}' → '{module}' resolves to "
                "nothing under src/",
                fix="Fix the entry point or add the module — the "
                "console script would be dead on install.",
            ))
            continue
        if not _attr_declared(module_file, attr):
            unverifiable.append(f"{command} ({module}:{attr})")
    if unverifiable:
        findings.append(Finding(
            CHECK_ID, Status.WARN,
            f"{label}: could not verify the callable for "
            f"{', '.join(unverifiable)} — module exists, attribute not "
            "found statically",
        ))
    if not findings:
        findings.append(Finding(
            CHECK_ID, Status.PASS,
            f"{label}: {len(scripts)} console script(s) resolve "
            f"({', '.join(sorted(scripts))})",
        ))
    return findings


def _build_findings(
    label: str,
    pkg: Path,
    meta: dict[str, Any] | None,
    offline: bool,
    build_probe: Callable[[], bool],
    builder: Callable[[Path], str | None],
) -> list[Finding]:
    if not isinstance(meta, dict):
        return []
    bs = meta.get("build-system")
    bs_ok = (
        isinstance(bs, dict)
        and bool(bs.get("requires"))
        and isinstance(bs.get("build-backend"), str)
    )
    if not bs_ok:
        return [Finding(
            CHECK_ID, Status.FAIL,
            f"{label}: [build-system] missing or incomplete — needs "
            "requires + build-backend",
            fix='Add e.g. requires = ["setuptools>=68"], '
            'build-backend = "setuptools.build_meta".',
        )]
    if offline or not build_probe():
        reason = (
            "offline mode" if offline
            else "'build'/'setuptools' not importable"
        )
        return [Finding(
            CHECK_ID, Status.PASS,
            f"{label}: build metadata ok — real wheel build skipped "
            f"({reason})",
        )]
    error = builder(pkg)
    if error is not None:
        return [Finding(
            CHECK_ID, Status.FAIL,
            f"{label}: wheel build failed — {error}",
            fix="Run `python -m build --wheel` in the package dir for "
            "the full log.",
        )]
    return [Finding(
        CHECK_ID, Status.PASS,
        f"{label}: wheel builds (python -m build --wheel, "
        "staged copy)",
    )]


def _coverage_findings(
    label: str, pkg: Path, project_name: str | None
) -> list[Finding]:
    workflow = _find_publish_workflow(pkg)
    if workflow is None:
        return [Finding(
            CHECK_ID, Status.PASS,
            f"{label}: no pypi-publish.yml above this dir — fan-out "
            "coverage not applicable",
        )]
    covered = _publish_coverage(workflow)
    if covered is None:
        return [Finding(
            CHECK_ID, Status.WARN,
            f"{label}: pypi-publish.yml unreadable — cannot verify "
            "fan-out coverage",
        )]
    candidates = {pkg.name}
    if project_name:
        candidates.add(project_name)
        if project_name.startswith("devin-"):
            candidates.add(project_name.removeprefix("devin-"))
    hit = candidates & covered
    if hit:
        return [Finding(
            CHECK_ID, Status.PASS,
            f"{label}: pypi-publish.yml covers '{min(hit)}'",
        )]
    if not covered:
        return [Finding(
            CHECK_ID, Status.WARN,
            f"{label}: pypi-publish.yml found but no resolve case or "
            "dispatch options parsed",
        )]
    return [Finding(
        CHECK_ID, Status.WARN,
        f"{label}: '{pkg.name}' not covered by pypi-publish.yml — "
        "tags like '<name>-v*' will not publish it",
        fix=f"Add a '{pkg.name})' case to the resolve step, a "
        f"'{pkg.name}-v*' tag pattern and a dispatch option.",
    )]


def _check_package(
    ctx: Context,
    pkg: Path,
    meta: dict[str, Any] | None,
    err: str | None,
    *,
    offline: bool,
    build_probe: Callable[[], bool],
    builder: Callable[[Path], str | None],
) -> list[Finding]:
    label = _label(ctx.cwd, pkg)
    findings, project = _metadata_findings(label, meta, err)
    if project is None:
        return findings
    src_pkgs = _src_packages(pkg / "src")
    mod = _module_name(project.get("name"), src_pkgs)
    findings += _layout_findings(label, pkg, src_pkgs, mod)
    findings += _version_findings(label, pkg, project, mod)
    findings += _scripts_findings(label, pkg, project)
    findings += _build_findings(
        label, pkg, meta, offline, build_probe, builder
    )
    findings += _coverage_findings(label, pkg, project.get("name"))
    return findings


def run(
    ctx: Context,
    *,
    build_probe: Callable[[], bool] = _build_available,
    builder: Callable[[Path], str | None] = _build_wheel,
) -> list[Finding]:
    dirs = _package_dirs(ctx.cwd)
    if not dirs:
        if ctx.packages:
            return [
                Finding(
                    CHECK_ID, Status.WARN,
                    f"--package '{name}' matched nothing under "
                    f"{ctx.cwd} — no packages/ fan-out or package dir",
                )
                for name in ctx.packages
            ]
        # Same convention as `config`: an environment with nothing in
        # scope reports PASS, not silence.
        return [Finding(
            CHECK_ID, Status.PASS,
            f"no packages/ fan-out or package dir under {ctx.cwd} — "
            "nothing to verify",
        )]

    wanted = set(ctx.packages)
    unmatched = set(wanted)
    findings: list[Finding] = []
    offline = ctx.offline or os.environ.get("DEVIN_DOCTOR_OFFLINE") == "1"
    for pkg in dirs:
        meta, err = _load_pyproject(pkg)
        name = _project_name(meta)
        matched = not wanted or bool({pkg.name, name} & wanted)
        if not matched:
            continue
        unmatched -= {pkg.name} | ({name} if name else set())
        findings += _check_package(
            ctx, pkg, meta, err,
            offline=offline, build_probe=build_probe, builder=builder,
        )
    for name in sorted(unmatched):
        findings.append(Finding(
            CHECK_ID, Status.WARN,
            f"--package '{name}' matched no packages/ dir or project "
            f"name under {ctx.cwd}",
        ))
    return findings
