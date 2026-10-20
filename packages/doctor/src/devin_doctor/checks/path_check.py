"""Check 9 — path-check: declared console scripts vs PATH reachability.

A package can build cleanly while its console scripts never reach the
user: the install landed in a venv whose bin dir is not on ``PATH``, a
hand-made shim shadows the real entry point, or two PATH entries carry
the same command name. This check compares the ``[project.scripts]`` /
``[project.gui-scripts]`` entry points declared by the packages in scope
against what ``shutil.which`` actually finds — fail-loud, one finding
per unreachable or shadowed command.

Scope mirrors ``publish-readiness``: ``ctx.cwd`` is the fan-out root
when it holds ``packages/`` (or is itself named ``packages``), else a
single package dir. ``ctx.packages`` — when the attribute exists (the
``--package`` flag) — narrows the fan-out by dir or project name;
``getattr`` keeps this check independent of the flag's rollout. Scripts
a same-named *installed* distribution provides are unioned in, so an
installed package whose pyproject isn't in front of us still gets its
entry points probed.

Verdicts per command:

- FAIL — not on ``PATH`` at all (named explicitly, with whether a
  distribution claims it or the package is even installed);
- WARN — reachable but a *different* installed distribution provides
  the same entry point, a foreign executable (uv tool / pipx / other
  prefix) shadows it, more than one package declares the same command,
  or further same-named executables sit deeper on ``PATH``;
- PASS — ``which()`` resolves it.

Read-only: PATH probing and ``importlib.metadata`` introspection only.
"""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import sys
import sysconfig
from collections.abc import Callable
from pathlib import Path
from typing import Any

import tomllib

from devin_doctor.model import Context, Finding, Status

CHECK_ID = "path-check"


def _norm(name: str | None) -> str:
    return (name or "").replace("-", "_").lower()


# --- scope (same shapes as publish-readiness) -------------------------------


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


def _declared_scripts(meta: dict[str, Any] | None) -> dict[str, str]:
    """``[project.scripts]`` + ``[project.gui-scripts]`` name → target."""
    if not isinstance(meta, dict):
        return {}
    project = meta.get("project")
    if not isinstance(project, dict):
        return {}
    out: dict[str, str] = {}
    for group in ("scripts", "gui-scripts"):
        table = project.get(group)
        if isinstance(table, dict):
            out.update({str(k): str(v) for k, v in table.items()})
    return out


# --- environment probing (injectable) ---------------------------------------


def _console_script_index() -> dict[str, str]:
    """Installed script name → providing distribution name, unioning the
    ``console_scripts`` and ``gui_scripts`` entry-point groups (current
    interpreter's environment only — uv/pipx shims correctly report no
    provider)."""
    try:
        eps = [
            *importlib.metadata.entry_points(group="console_scripts"),
            *importlib.metadata.entry_points(group="gui_scripts"),
        ]
    except Exception:  # noqa: BLE001 — metadata quirks must not kill doctor
        return {}
    out: dict[str, str] = {}
    for ep in eps:
        name = None
        dist = getattr(ep, "dist", None)
        if dist is not None:
            try:
                name = dist.metadata["Name"]
            except Exception:  # noqa: BLE001 — oddball dist metadata
                name = None
        out.setdefault(ep.name, name or "?")
    return out


def _dist_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _current_scripts_dirs() -> frozenset[str]:
    """Real paths of this interpreter's script dirs (every sysconfig
    scheme's ``scripts`` plus the interpreter's own dir) — the only
    places a provider from the entry-point index can own an executable.
    Normalized via ``normcase`` + ``realpath`` for comparison."""
    dirs: set[str] = set()
    for scheme in sysconfig.get_scheme_names():
        try:
            raw = sysconfig.get_path("scripts", scheme=scheme)
        except (KeyError, ValueError):
            continue
        if raw:
            dirs.add(os.path.normcase(os.path.realpath(raw)))
    if sys.executable:
        dirs.add(os.path.normcase(
            os.path.realpath(os.path.dirname(sys.executable))
        ))
    return frozenset(dirs)


def _path_hits(command: str) -> list[str]:
    """Every executable named ``command`` on PATH, in resolution order —
    hits past the first are shadowed. Honors ``PATHEXT`` on Windows and
    mirrors ``shutil.which`` probing CWD first there. Hits are deduped
    by real path so a repeated (or symlink-aliased) PATH entry never
    counts as a competing executable."""
    path_env = os.environ.get("PATH", "")
    extensions = [""]
    directories = path_env.split(os.pathsep)
    if os.name == "nt":
        extensions = [
            e.lower() for e in
            os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
        ]
        if not os.environ.get("NoDefaultCurrentDirectoryInExePath"):
            directories.insert(0, os.curdir)
    hits: list[str] = []
    seen: set[str] = set()
    for directory in directories:
        if not directory:
            continue
        base = Path(directory) / command
        for ext in extensions:
            candidate = (
                base
                if not ext or command.lower().endswith(ext)
                else Path(str(base) + ext)
            )
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    real = os.path.normcase(os.path.realpath(candidate))
                    if real not in seen:
                        seen.add(real)
                        hits.append(str(candidate))
                    break
            except OSError:
                continue
    return hits


# --- findings ----------------------------------------------------------------


def _command_finding(
    cmd: str,
    label: str,
    canonical: str | None,
    which: Callable[[str], str | None],
    hits: Callable[[str], list[str]],
    provider: str | None,
    dist_version: Callable[[str], str | None],
    scripts_dirs: frozenset[str],
) -> Finding:
    resolved = which(cmd)
    if resolved is None:
        if provider is not None:
            return Finding(
                CHECK_ID, Status.FAIL,
                f"'{cmd}' is provided by installed '{provider}' but is "
                "not on PATH",
                fix="Add the environment's bin dir to PATH, or "
                "reinstall the package.",
            )
        hint = ""
        if canonical:
            version = dist_version(canonical)
            hint = (
                f" — '{canonical}' is not installed in this interpreter "
                "(an isolated uv tool / pipx install may exist off-PATH)"
                if version is None
                else f" — '{canonical}' {version} is installed but its "
                "script is not linked"
            )
        return Finding(
            CHECK_ID, Status.FAIL,
            f"'{cmd}' declared by {label} is not on PATH{hint}",
            fix="Install the package (uv tool install / pipx install) "
            "or fix PATH.",
        )
    if provider is not None and canonical is not None and (
        _norm(provider) != _norm(canonical)
    ):
        # The entry-point index describes this interpreter only — blame
        # 'provider' for the resolved executable just when that exe
        # actually lives in one of its scripts dirs; a hit elsewhere
        # (uv tool, pipx, another prefix) is a foreign shim.
        owner_dir = os.path.normcase(
            os.path.realpath(os.path.dirname(resolved))
        )
        if owner_dir in scripts_dirs:
            return Finding(
                CHECK_ID, Status.WARN,
                f"'{cmd}' resolves to {resolved} — provided by "
                f"'{provider}', not '{canonical}' (shadowed)",
                fix="Check which install should own this command.",
            )
        return Finding(
            CHECK_ID, Status.WARN,
            f"'{cmd}' resolves to {resolved} — a foreign executable "
            f"shadowing '{canonical}'; '{provider}' claims the entry "
            "point in this interpreter but does not own that path",
            fix="Check which install should own this command.",
        )
    extra = hits(cmd)[1:]
    if extra:
        shown = ", ".join(extra[:3])
        return Finding(
            CHECK_ID, Status.WARN,
            f"'{cmd}' resolves to {resolved} but "
            f"{len(extra)} more executable(s) sit on PATH: {shown} — "
            "shadowing",
            fix="Disambiguate PATH order or remove the stale shim.",
        )
    return Finding(
        CHECK_ID, Status.PASS,
        f"'{cmd}' on PATH at {resolved}",
    )


def run(
    ctx: Context,
    *,
    which: Callable[[str], str | None] = shutil.which,
    hits: Callable[[str], list[str]] = _path_hits,
    index: Callable[[], dict[str, str]] = _console_script_index,
    dist_version: Callable[[str], str | None] = _dist_version,
    scripts_dirs: Callable[[], frozenset[str]] = _current_scripts_dirs,
) -> list[Finding]:
    dirs = _package_dirs(ctx.cwd)
    if not dirs:
        # Same convention as publish-readiness/config: nothing in scope
        # is a PASS note, not silence.
        return [Finding(
            CHECK_ID, Status.PASS,
            f"no packages/ fan-out or package dir under {ctx.cwd} — "
            "nothing to check",
        )]

    wanted = set(getattr(ctx, "packages", ()) or ())
    unmatched = set(wanted)
    expected: dict[str, tuple[str, str | None]] = {}  # cmd → (label, project)
    declarers: dict[str, list[str]] = {}  # cmd → every declarer, in order
    projects: dict[str, str] = {}  # normalized project name → display name
    for pkg in dirs:
        meta = _load_pyproject(pkg)
        name = _project_name(meta)
        if wanted and not ({pkg.name, name} & wanted):
            continue
        unmatched -= {pkg.name} | ({name} if name else set())
        label = _label(ctx.cwd, pkg)
        for cmd in _declared_scripts(meta):
            declarers.setdefault(cmd, []).append(label)
            expected.setdefault(cmd, (label, name))
        if name:
            projects[_norm(name)] = name

    idx = index()
    # Union in entry points a same-named installed dist provides — the
    # "given installed package" half of the contract. A provider other
    # than the declaring project counts as an extra declarer.
    for cmd, provider in idx.items():
        if _norm(provider) not in projects:
            continue
        existing = expected.get(cmd)
        expected.setdefault(cmd, (provider, provider))
        if existing is not None and _norm(existing[1]) != _norm(provider):
            declarers.setdefault(cmd, []).append(provider)

    findings: list[Finding] = [
        Finding(
            CHECK_ID, Status.WARN,
            f"--package '{name}' matched no packages/ dir or project "
            f"name under {ctx.cwd}",
        )
        for name in sorted(unmatched)
    ]
    if not expected:
        findings.append(Finding(
            CHECK_ID, Status.PASS,
            f"{len(dirs)} package dir(s) declare no console scripts — "
            "nothing to probe",
        ))
        return findings
    sdirs = scripts_dirs()
    for cmd in sorted(expected):
        label, canonical = expected[cmd]
        owners = declarers.get(cmd, [])
        if len(owners) > 1:
            findings.append(Finding(
                CHECK_ID, Status.WARN,
                f"'{cmd}' is declared by {len(owners)} package(s): "
                f"{', '.join(owners)} — same-name scripts race on PATH",
                fix="Keep a single declarer or rename the command.",
            ))
        findings.append(_command_finding(
            cmd, label, canonical, which, hits,
            idx.get(cmd), dist_version, sdirs,
        ))
    return findings
