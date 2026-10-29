"""``devin-doctor drift`` — a bounded, privacy-safe snapshot for drift
diagnosis.

Where ``check`` evaluates health against the documented ideal, ``drift``
captures *what this machine actually looks like right now* so two runs (or
two machines) can be diffed when behavior silently diverged: tool versions
installed vs expected, config/state files present vs expected (recorded as
digests — never contents), PATH sanity, and which environment keys are set.

Privacy contract — enforced by construction, not by filtering afterwards:

- **no file contents are ever emitted** — files are ``stat()``-ed and,
  when small enough, hashed to a sha256 prefix; the digest is the only
  content-derived datum;
- **no env values** — only curated env var *names* (``DEVIN_*``,
  ``XDG_*``, ``COPILOT_*``, ``CODEX_*``, ``APPDATA``/``LOCALAPPDATA``)
  are reported;
- **bounded everywhere** — file count, per-file hash size, PATH entries
  and env keys are all capped; a capped collection says so in the
  snapshot instead of silently truncating;
- **read-only** — nothing is written, configured or installed.

Verdicts: a missing/unreadable expected file, a missing tool, a version
below its floor, or a scoped dir that cannot be read is drift (FAIL);
degraded-but-present states (empty files, stale wheels, dead PATH
entries) are WARN. Exit code is 1 when any check FAILs — the same
convention as ``devin-doctor check``.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tomllib
from packaging.version import InvalidVersion, Version

import devin_doctor
from devin_doctor.model import Context, Finding, Status, platform_name
from devin_doctor.paths import (
    cli_anchor_present,
    credentials_path,
    desktop_anchor_present,
    human_size,
    locate_stores,
    session_locks_dir,
    sessions_db_path,
    user_config_path,
    user_mcp_config_path,
)

COMMAND_ID = "drift"

# --- bounds (the 'bounded' half of the contract) ----------------------------
_MAX_ACP_FILES = 100          # acp-messages/*.db files inspected at most
_MAX_DIGEST_BYTES = 64 * 1024**2   # files bigger than this are stat-only
_MAX_PATH_ENTRIES = 50        # PATH dirs inspected at most
_MAX_ENV_KEYS = 100           # env names reported at most
_PROBE_TIMEOUT = 10

# Tools expected on a working dev machine. ``floor`` is the minimum
# accepted version — python carries the ecosystem's ``requires-python``
# floor (>=3.10); the rest only need to exist and answer ``--version``.
# ``required=False`` marks nice-to-have tools whose absence is WARN.
_TOOLS: tuple[tuple[str, tuple[int, ...] | None, bool], ...] = (
    ("python3", (3, 10), True),
    ("python", (3, 10), False),
    ("pip3", None, True),
    ("node", None, False),
    ("npm", None, False),
    ("git", None, True),
    ("uv", None, False),
    ("devin", None, False),
)

# Project config files the snapshot tracks under ``<cwd>/.devin/``.
_DEVIN_DIR_FILES = (
    "config.json",
    "config.local.json",
    "mcp_config.json",
    "mcp_config.local.json",
    "hooks.v1.json",
)

# Env names worth reporting — names only, values never leave the process.
_ENV_PREFIXES = ("DEVIN_", "XDG_", "COPILOT_", "CODEX_")
_ENV_NAMES = frozenset({"APPDATA", "LOCALAPPDATA"})

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

_INTERNAL_DIST_PREFIXES = ("devin-", "devin_")


@dataclass
class DriftReport:
    """The snapshot plus the findings that flagged drift.

    ``snapshot`` is the structured record (JSON-safe) — file entries are
    presence/size/mtime/digest rows, never content; ``findings`` carries
    the same PASS/WARN/FAIL verdicts the ``check`` command emits, so the
    exit-code story is identical.
    """

    findings: list[Finding] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)


def _norm(name: str | None) -> str:
    return (name or "").replace("-", "_").lower()


# --- environment probing (injectable) --------------------------------------


def _which(cmd: str) -> str | None:
    import shutil

    return shutil.which(cmd)


def _probe(argv: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    """Probe seam (toolchain's contract): ``None`` when the binary cannot
    be executed or times out — 'unverified', never 'missing'."""
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


def _version_of(executable: str) -> str | None:
    """``X.Y[.Z]`` parsed from ``<exe> --version`` output — the only datum
    kept; the raw output is discarded."""
    res = _probe([executable, "--version"])
    if res is None or res.returncode != 0:
        return None
    m = _VERSION_RE.search(f"{res.stdout or ''}\n{res.stderr or ''}")
    return m.group(0) if m else None


def _version_tuple(version: str) -> tuple[int, ...] | None:
    try:
        v = Version(version)
    except InvalidVersion:
        return None
    return v.release


def _below_floor(version: str | None, floor: tuple[int, ...]) -> bool | None:
    """``True`` when ``version`` parses and is below ``floor``; ``None``
    when the version is unverifiable (drift is never invented)."""
    if version is None:
        return None
    parsed = _version_tuple(version)
    if parsed is None:
        return None
    return parsed < floor


# --- section: tools ---------------------------------------------------------


def _tool_findings(
    which: Callable[[str], str | None],
    version_of: Callable[[str], str | None],
) -> tuple[list[Finding], list[dict[str, Any]]]:
    findings: list[Finding] = []
    entries: list[dict[str, Any]] = []
    for name, floor, required in _TOOLS:
        resolved = which(name)
        if resolved is None:
            findings.append(
                Finding(
                    COMMAND_ID,
                    Status.FAIL if required else Status.WARN,
                    f"tool '{name}' is not on PATH"
                    + ("" if required else " (optional)"),
                    fix=(
                        "Install it or fix PATH — see the toolchain check "
                        "for package-manager hints."
                    ),
                )
            )
            entries.append(
                {"name": name, "status": "missing", "expected": True}
            )
            continue
        version = version_of(resolved)
        if floor is not None:
            below = _below_floor(version, floor)
            if below is True:
                findings.append(
                    Finding(
                        COMMAND_ID,
                        Status.FAIL,
                        f"'{name}' {version or '?'} at {resolved} is below "
                        f"the expected floor {'.'.join(map(str, floor))}",
                        fix="Upgrade the interpreter/tool or repoint PATH "
                        "at a newer install.",
                    )
                )
                entries.append(
                    {
                        "name": name,
                        "status": "below-floor",
                        "path": resolved,
                        "version": version,
                        "floor": ".".join(map(str, floor)),
                    }
                )
                continue
            if below is None and version is None:
                findings.append(
                    Finding(
                        COMMAND_ID,
                        Status.WARN,
                        f"'{name}' at {resolved} did not answer "
                        "'--version' — unverified",
                    )
                )
                entries.append(
                    {
                        "name": name,
                        "status": "unverified",
                        "path": resolved,
                        "version": None,
                    }
                )
                continue
        floor_s = (
            f" (>= {'.'.join(map(str, floor))})" if floor is not None else ""
        )
        findings.append(
            Finding(
                COMMAND_ID,
                Status.PASS,
                f"'{name}' {version or 'version unknown'} at "
                f"{resolved}{floor_s}",
            )
        )
        entries.append(
            {
                "name": name,
                "status": "ok",
                "path": resolved,
                "version": version,
            }
        )
    return findings, entries


# --- section: packages (installed devin-* dists vs declared versions) -------


def _child_dirs(root: Path) -> list[Path]:
    try:
        return sorted(
            d for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    except OSError:
        return []


def _package_dirs(cwd: Path) -> list[Path]:
    """Same scoping shapes as path-check/deps-internals: a ``packages/``
    fan-out, the ``packages`` dir itself, or a single package dir."""
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


def _load_pyproject(pkg: Path) -> dict[str, Any] | None:
    path = pkg / "pyproject.toml"
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError):
        return None


def _project_meta(meta: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if not isinstance(meta, dict):
        return None, None
    project = meta.get("project")
    if not isinstance(project, dict):
        return None, None
    name = project.get("name")
    version = project.get("version")
    return (
        name if isinstance(name, str) else None,
        version if isinstance(version, str) else None,
    )


def _label(cwd: Path, pkg: Path) -> str:
    try:
        rel = pkg.relative_to(cwd).as_posix()
    except ValueError:
        return pkg.name
    return pkg.name if rel == "." else rel


def _installed_dists() -> dict[str, str]:
    """``normalized name → version`` of installed ``devin_*`` dists in the
    current interpreter (metadata only — nothing is imported)."""
    out: dict[str, str] = {}
    try:
        dists = importlib.metadata.distributions()
    except Exception:  # noqa: BLE001 — metadata quirks must not kill doctor
        return out
    for dist in dists:
        try:
            name = dist.metadata.get("Name")
        except Exception:  # noqa: BLE001 — oddball dist metadata
            name = None
        if not name or not _norm(name).startswith("devin_"):
            continue
        try:
            version = dist.version
        except Exception:  # noqa: BLE001
            version = ""
        out[_norm(name)] = version
    return out


def _same_version(a: str, b: str) -> bool | None:
    """PEP 440 equality (deps-internals' rule): ``None`` when either side
    is unverifiable — approximating would invent drift that isn't there."""
    if a == b:
        return True
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return None


def _package_findings(
    ctx: Context,
    installed: dict[str, str],
) -> tuple[list[Finding], list[dict[str, Any]]]:
    """Compare each declared package's pyproject version with the dist
    installed in this interpreter. Foreign ``devin_*`` dists (no
    same-named package in scope) are ignored — same doctrine as
    deps-internals."""
    findings: list[Finding] = []
    entries: list[dict[str, Any]] = []
    dirs = _package_dirs(ctx.cwd)
    if not dirs:
        return findings, entries
    wanted = set(getattr(ctx, "packages", ()) or ())
    declared: dict[str, tuple[str, str | None, str]] = {}
    for pkg in dirs:
        name, version = _project_meta(_load_pyproject(pkg))
        if name is None:
            continue
        if wanted and not ({pkg.name, name} & wanted):
            continue
        declared[_norm(name)] = (name, version, _label(ctx.cwd, pkg))
    for key in sorted(declared):
        name, version, label = declared[key]
        installed_v = installed.get(key)
        if installed_v is None:
            findings.append(
                Finding(
                    COMMAND_ID,
                    Status.WARN,
                    f"'{name}' ({label}) declares {version or '?'} but is "
                    "not installed in this interpreter",
                    fix="uv sync / pip install the package, or point the "
                    "run at the venv that has it.",
                )
            )
            entries.append(
                {
                    "name": name,
                    "status": "not-installed",
                    "declared": version,
                    "installed": None,
                    "package": label,
                }
            )
            continue
        same = _same_version(installed_v, version or "")
        if same is True:
            findings.append(
                Finding(
                    COMMAND_ID,
                    Status.PASS,
                    f"'{name}' {installed_v} installed == declared "
                    f"({label})",
                )
            )
            status = "ok"
        elif same is False:
            findings.append(
                Finding(
                    COMMAND_ID,
                    Status.WARN,
                    f"'{name}' installed at {installed_v} but {label} "
                    f"declares {version} — a stale install shadows the "
                    "checkout",
                    fix="Reinstall (uv sync / pip install) so the tool "
                    "matches the checkout.",
                )
            )
            status = "version-mismatch"
        else:
            findings.append(
                Finding(
                    COMMAND_ID,
                    Status.WARN,
                    f"'{name}' version unverifiable — installed "
                    f"{installed_v or '(unknown)'} vs declared "
                    f"{version or '(none)'} ({label})",
                )
            )
            status = "unverifiable"
        entries.append(
            {
                "name": name,
                "status": status,
                "declared": version,
                "installed": installed_v,
                "package": label,
            }
        )
    return findings, entries


# --- section: files (presence + digests, never contents) --------------------


def _digest(path: Path, size: int) -> str | None:
    """sha256 prefix of a file, or ``None`` — contents never leave the
    digest. Files over ``_MAX_DIGEST_BYTES`` are stat-only."""
    if size > _MAX_DIGEST_BYTES:
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _iso(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def _file_entry(path: Path) -> tuple[dict[str, Any], Status, str]:
    """Stat+digest one file; ``(entry, status, detail)``."""
    try:
        st = path.stat()
    except OSError as exc:
        return (
            {"path": str(path), "status": "unreadable", "error": str(exc)},
            Status.FAIL,
            f"unreadable ({exc.strerror or exc})",
        )
    entry: dict[str, Any] = {
        "path": str(path),
        "status": "present",
        "size_bytes": st.st_size,
        "mtime": _iso(st.st_mtime),
    }
    if st.st_size > _MAX_DIGEST_BYTES:
        entry["sha256_16"] = None
        entry["digest_note"] = "skipped-large"
        return entry, Status.PASS, (
            f"present ({human_size(st.st_size)}, digest skipped — "
            f">{human_size(_MAX_DIGEST_BYTES)})"
        )
    try:
        entry["sha256_16"] = _digest(path, st.st_size)
    except OSError as exc:
        entry["status"] = "unreadable"
        entry["error"] = str(exc)
        return (
            entry,
            Status.FAIL,
            f"stat'd but not readable ({exc.strerror or exc})",
        )
    status = Status.WARN if st.st_size == 0 else Status.PASS
    note = (
        f"present ({human_size(st.st_size)}, "
        f"sha256:{entry['sha256_16']})"
    )
    if st.st_size == 0:
        note += " — empty file"
    return entry, status, note


def _expected_file(
    path: Path,
    rel: str,
    missing_status: Status,
    findings: list[Finding],
    entries: list[dict[str, Any]],
    fix: str,
) -> None:
    """One file the detected install shape says should exist."""
    if not path.exists():
        findings.append(
            Finding(
                COMMAND_ID,
                missing_status,
                f"expected file missing: {rel} ({path})",
                fix=fix,
            )
        )
        entries.append(
            {
                "path": str(path),
                "relpath": rel,
                "status": "missing",
                "expected": True,
            }
        )
        return
    entry, status, note = _file_entry(path)
    entry["relpath"] = rel
    entry["expected"] = True
    findings.append(Finding(COMMAND_ID, status, f"{rel}: {note}"))
    entries.append(entry)


def _optional_file(
    path: Path,
    rel: str,
    findings: list[Finding],
    entries: list[dict[str, Any]],
) -> None:
    """A file reported when present; absence is never drift."""
    if not path.exists():
        return
    entry, status, note = _file_entry(path)
    entry["relpath"] = rel
    entry["expected"] = False
    findings.append(Finding(COMMAND_ID, status, f"{rel}: {note}"))
    entries.append(entry)


def _files_findings(ctx: Context) -> tuple[list[Finding], list[dict[str, Any]]]:
    """Presence + digest rows for the install's state files.

    Expectations follow the anchor doctrine shared with ``check``: a
    missing ``cli/sessions.db`` only FAILs when the tree shows a CLI
    install (``cli/`` dir or ``credentials.toml``), a missing
    ``state.vscdb`` only when Desktop artifacts exist — on a CLI-only
    machine the file legitimately never existed. ``config.json`` /
    ``mcp_config.json`` / ``.devin/*`` are optional: reported when
    present, silent when not.
    """
    findings: list[Finding] = []
    entries: list[dict[str, Any]] = []
    stores = locate_stores(ctx.data_dir, ctx.config_dir)
    config_root = ctx.config_dir or ctx.data_dir
    cli_anchor = cli_anchor_present(ctx.data_dir)
    desktop_anchor = desktop_anchor_present(stores)

    if not cli_anchor and not desktop_anchor:
        findings.append(
            Finding(
                COMMAND_ID,
                Status.PASS,
                f"no Devin install anchors under {ctx.data_dir} — "
                "nothing to snapshot (not detected)",
            )
        )
        entries.append({"path": str(ctx.data_dir), "status": "not-detected"})
    else:
        if cli_anchor:
            _expected_file(
                sessions_db_path(ctx.data_dir),
                "cli/sessions.db",
                Status.FAIL,
                findings,
                entries,
                "The CLI session store vanished while other CLI "
                "artifacts remain — check for a pruned data dir.",
            )
            _expected_file(
                credentials_path(ctx.data_dir),
                "credentials.toml",
                Status.WARN,
                findings,
                entries,
                "Re-authenticate if CLI access is needed.",
            )
            locks = session_locks_dir(ctx.data_dir)
            entries.append(
                {
                    "path": str(locks),
                    "status": "present" if locks.is_dir() else "missing",
                    "expected": False,
                }
            )
        else:
            findings.append(
                Finding(
                    COMMAND_ID,
                    Status.PASS,
                    "CLI install not detected — sessions.db / "
                    "credentials.toml not expected",
                )
            )
        if desktop_anchor:
            _expected_file(
                stores.state_vscdb,
                "User/globalStorage/state.vscdb",
                Status.FAIL,
                findings,
                entries,
                "Desktop artifacts remain but state.vscdb vanished — "
                "the GUI profile was likely pruned.",
            )
        else:
            findings.append(
                Finding(
                    COMMAND_ID,
                    Status.PASS,
                    "Desktop install not detected — state.vscdb not "
                    "expected",
                )
            )
        _optional_file(
            user_config_path(config_root), "config.json", findings, entries
        )
        _optional_file(
            user_mcp_config_path(config_root),
            "mcp_config.json",
            findings,
            entries,
        )
        acp_all = list(stores.acp_dbs)
        acp_files = acp_all[:_MAX_ACP_FILES]
        if stores.acp_dir.is_dir():
            findings.append(
                Finding(
                    COMMAND_ID,
                    Status.PASS,
                    f"acp-messages: {len(acp_files)} db file(s) digested"
                    + (
                        f" (capped — {len(acp_all)} present)"
                        if len(acp_all) > _MAX_ACP_FILES
                        else ""
                    ),
                )
            )
        for path in acp_files:
            entry, status, note = _file_entry(path)
            entry["relpath"] = f"User/acp-messages/{path.name}"
            entry["expected"] = False
            if status is not Status.PASS:
                findings.append(
                    Finding(
                        COMMAND_ID,
                        status,
                        f"acp-messages/{path.name}: {note}",
                    )
                )
            entries.append(entry)

    devin_dir = ctx.cwd / ".devin"
    for name in _DEVIN_DIR_FILES:
        _optional_file(
            devin_dir / name, f".devin/{name}", findings, entries
        )
    return findings, entries


# --- section: paths + env key names ----------------------------------------


def _path_findings(
    ctx: Context,
    environ: dict[str, str],
) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    scope: dict[str, Any] = {}

    for label, root in (
        ("data_dir", ctx.data_dir),
        ("config_dir", ctx.config_dir),
        ("cwd", ctx.cwd),
    ):
        if root is None:
            scope[label] = {"path": None, "status": "unset"}
            continue
        ok = root.is_dir()
        status = Status.PASS if ok else Status.FAIL
        # A missing data/config dir is 'not detected' elsewhere in the
        # report; a missing --cwd is a user error and always FAILs.
        if label != "cwd" and not ok:
            status = Status.PASS
            scope[label] = {"path": str(root), "status": "not-detected"}
            continue
        findings.append(
            Finding(
                COMMAND_ID,
                status,
                f"{label} {'is a readable dir' if ok else 'does not exist'}: "
                f"{root}",
            )
        )
        scope[label] = {
            "path": str(root),
            "status": "ok" if ok else "missing",
        }

    raw_path = environ.get("PATH", "")
    segments = [seg for seg in raw_path.split(os.pathsep)]
    seen: set[str] = set()
    missing: list[str] = []
    dupes = 0
    inspected = 0
    for seg in segments:
        if not seg or seg in seen:
            dupes += 1 if seg else 0
            continue
        seen.add(seg)
        if inspected >= _MAX_PATH_ENTRIES:
            continue
        inspected += 1
        if not Path(seg).is_dir():
            missing.append(seg)
    scope["path"] = {
        "entries": len(segments),
        "inspected": inspected,
        "missing": missing,
        "duplicates": dupes,
    }
    if missing:
        findings.append(
            Finding(
                COMMAND_ID,
                Status.WARN,
                f"{len(missing)} PATH entr{'y' if len(missing) == 1 else 'ies'}"
                f" point at dirs that do not exist: "
                f"{', '.join(missing[:3])}{' …' if len(missing) > 3 else ''}",
                fix="Stale PATH entries — clean up the shell profile.",
            )
        )
    else:
        findings.append(
            Finding(
                COMMAND_ID,
                Status.PASS,
                f"PATH healthy — {inspected} entr"
                f"{'y' if inspected == 1 else 'ies'} inspected, "
                f"{dupes} duplicate(s)",
            )
        )

    names = sorted(
        k
        for k in environ
        if k.startswith(_ENV_PREFIXES) or k in _ENV_NAMES
    )
    capped = len(names) > _MAX_ENV_KEYS
    scope["env_keys"] = names[:_MAX_ENV_KEYS]
    scope["env_keys_capped"] = capped
    findings.append(
        Finding(
            COMMAND_ID,
            Status.PASS,
            f"{len(names)} env key name(s) observed"
            f"{' (capped)' if capped else ''} — values never collected",
        )
    )
    return findings, scope


# --- collect + render --------------------------------------------------------


def collect(
    ctx: Context,
    *,
    which: Callable[[str], str | None] | None = None,
    version_of: Callable[[str], str | None] | None = None,
    installed_dists: Callable[[], dict[str, str]] | None = None,
    environ: dict[str, str] | None = None,
) -> DriftReport:
    """Assemble the bounded drift snapshot for ``ctx``.

    Probe seams (``which``/``version_of``/``installed_dists``/``environ``)
    are injectable so tests never touch the real PATH/env/interpreter.
    """
    which = which or _which
    version_of = version_of or _version_of
    installed_dists = installed_dists or _installed_dists
    env = dict(os.environ if environ is None else environ)

    report = DriftReport()
    sections: dict[str, Any] = {}

    tools_findings, tool_entries = _tool_findings(which, version_of)
    report.findings += tools_findings
    sections["tools"] = tool_entries

    pkg_findings, pkg_entries = _package_findings(ctx, installed_dists())
    report.findings += pkg_findings
    sections["packages"] = pkg_entries

    file_findings, file_entries = _files_findings(ctx)
    report.findings += file_findings
    sections["files"] = file_entries

    path_findings, scope = _path_findings(ctx, env)
    report.findings += path_findings
    sections["paths"] = scope

    report.snapshot = {
        "tool": "devin-doctor",
        "version": devin_doctor.__version__,
        "command": COMMAND_ID,
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "platform": platform_name(),
        "python": sys.version.split()[0],
        "data_dir": str(ctx.data_dir),
        "config_dir": str(ctx.config_dir) if ctx.config_dir else None,
        "cwd": str(ctx.cwd),
        "bounds": {
            "max_acp_files": _MAX_ACP_FILES,
            "max_digest_bytes": _MAX_DIGEST_BYTES,
            "max_path_entries": _MAX_PATH_ENTRIES,
            "max_env_keys": _MAX_ENV_KEYS,
        },
        "privacy": {
            "file_contents": "never collected — sha256 digests only",
            "env_values": "never collected — key names only",
        },
        "sections": sections,
    }
    return report


def _counts(report: DriftReport) -> dict[str, int]:
    return {
        s.value.lower(): sum(1 for f in report.findings if f.status is s)
        for s in Status
    }


def _overall(report: DriftReport) -> Status:
    if any(f.status is Status.FAIL for f in report.findings):
        return Status.FAIL
    if any(f.status is Status.WARN for f in report.findings):
        return Status.WARN
    return Status.PASS


def exit_code(report: DriftReport) -> int:
    """1 on detected drift — the same FAIL-means-nonzero convention as
    ``devin-doctor check``."""
    return 1 if _overall(report) is Status.FAIL else 0


def render_text(report: DriftReport) -> str:
    sections = report.snapshot["sections"]
    lines = [
        (
            "devin-doctor drift — bounded snapshot "
            "(no file contents or env values collected)"
        ),
        "",
    ]
    groups = (
        ("tools", "tools — installed versions vs expected"),
        ("packages", "packages — installed devin-* dists vs declared"),
        ("files", "files — presence, size, sha256 digests (no contents)"),
        ("paths", "paths — scope dirs, PATH health, env key names"),
    )
    for key, title in groups:
        lines.append(title)
        entries = sections.get(key)
        if key == "paths":
            for label in ("data_dir", "config_dir", "cwd"):
                info = entries.get(label)
                if not info or info.get("status") == "unset":
                    continue
                lines.append(
                    f"      {label}: {info['status']} — {info['path']}"
                )
            pinfo = entries["path"]
            if pinfo["missing"]:
                lines.append(
                    f"      PATH: {len(pinfo['missing'])} dead entr"
                    f"{'y' if len(pinfo['missing']) == 1 else 'ies'} of "
                    f"{pinfo['entries']} ({', '.join(pinfo['missing'][:3])})"
                )
            else:
                lines.append(
                    f"      PATH: {pinfo['inspected']} entr"
                    f"{'y' if pinfo['inspected'] == 1 else 'ies'} ok, "
                    f"{pinfo['duplicates']} duplicate(s)"
                )
            env = entries.get("env_keys", [])
            shown = ", ".join(env[:12])
            lines.append(
                f"      env keys ({len(env)}): {shown}"
                f"{' …' if len(env) > 12 else ''}"
            )
        elif not entries:
            lines.append("      (nothing in scope)")
        else:
            for e in entries:
                if key == "tools":
                    detail = (
                        f"{e['name']}: {e['status']}"
                        + (f" {e['version']}" if e.get("version") else "")
                        + (f" at {e['path']}" if e.get("path") else "")
                    )
                elif key == "packages":
                    detail = (
                        f"{e['name']}: {e['status']} (declared "
                        f"{e.get('declared') or '?'} / installed "
                        f"{e.get('installed') or '—'})"
                    )
                else:  # files
                    bits = [e["status"]]
                    if e.get("size_bytes") is not None:
                        bits.append(human_size(e["size_bytes"]))
                    if e.get("sha256_16"):
                        bits.append(f"sha256:{e['sha256_16']}")
                    if e.get("digest_note"):
                        bits.append(e["digest_note"])
                    detail = (
                        f"{e.get('relpath') or e['path']}: {', '.join(bits)}"
                    )
                lines.append(f"      {detail}")
        lines.append("")
    lines.append("findings")
    for f in report.findings:
        lines.append(f"  {f.status.value:<4}  {f.message}")
        if f.fix and f.status is not Status.PASS:
            lines.append(f"        fix: {f.fix}")
    counts = _counts(report)
    lines.append("")
    lines.append(
        f"overall: {_overall(report).value} ({counts['pass']} PASS, "
        f"{counts['warn']} WARN, {counts['fail']} FAIL) — exit "
        f"{exit_code(report)} on drift"
    )
    return "\n".join(lines)


def render_json(report: DriftReport) -> str:
    counts = _counts(report)
    payload = {
        **report.snapshot,
        "overall": _overall(report).value,
        "summary": counts,
        "findings": [f.as_dict() for f in report.findings],
    }
    return json.dumps(payload, indent=2)
