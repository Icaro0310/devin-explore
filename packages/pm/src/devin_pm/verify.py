"""``devin-pm verify`` — cross-check tracked projects against the hub registry.

``devin-pm`` knows a *project* for every ``working_directory`` seen in
``sessions.db``; the ecosystem hub (``devin-powerups/registry.json``) is the
authoritative catalog of repositories. ``verify`` diffs the two:

- **unknown_to_pm** — registry entries with no matching project
  (informational: the repo exists but no session ran in it);
- **missing_from_registry** — projects pm tracks that the registry does not
  list. Entries are flagged ``relevant`` when they look ecosystem-shaped
  (``devin-*`` name or a checkout under the hub's parent directory); only
  relevant misses count as drift;
- **field_drift** — for matched pairs, ``name``/``description`` read from the
  checkout's ``pyproject.toml`` and ``url`` read from the checkout's git
  ``origin`` remote compared against the registry values. Fields pm cannot
  observe (no pyproject, no remote) are skipped, never guessed.

Everything is local and read-only: ``sessions.db``, ``registry.json``,
``pyproject.toml`` and ``.git/config``. No network calls, ever.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from devin_pm.projects import Project

CANDIDATE_REGISTRIES = (
    # task convention: sibling hub checkout relative to cwd
    Path("../devin-powerups/registry.json"),
)


class RegistryError(ValueError):
    """The hub registry could not be read or has an unexpected shape."""


def default_registry() -> Path | None:
    """Locate the hub ``registry.json`` without an explicit ``--registry``.

    Resolution order: ``../devin-powerups/registry.json`` relative to the
    current working directory, then the checkout that is a sibling of this
    package's own repository (covers running from inside the repo clone).
    Returns ``None`` when neither exists.
    """
    for candidate in CANDIDATE_REGISTRIES:
        if candidate.is_file():
            return candidate
    # <pkg>/src/devin_pm/verify.py -> parents[3] is the ecosystem root
    sibling = (
        Path(__file__).resolve().parents[3]
        / "devin-powerups"
        / "registry.json"
    )
    if sibling.is_file():
        return sibling
    return None


def load_hub_registry(path: str | Path) -> dict[str, dict]:
    """Return ``{name: entry}`` from a devin-powerups ``registry.json``."""
    path = Path(path)
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot load {path}: {exc}") from exc
    repositories = registry.get("repositories")
    if not isinstance(repositories, list):
        raise RegistryError(f"{path}: 'repositories' must be a list")
    entries: dict[str, dict] = {}
    for entry in repositories:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise RegistryError(
                f"{path}: repository entries must be objects with a 'name'"
            )
        entries[entry["name"]] = entry
    return entries


# ---------------------------------------------------------------------------
# local manifest reads (pyproject.toml + .git/config), all best-effort
# ---------------------------------------------------------------------------

def read_pyproject(working_directory: str | Path) -> dict[str, str]:
    """Extract ``[project]`` ``name``/``description`` — tiny TOML subset.

    Kept dependency-free for Python 3.10 (no ``tomllib``); only simple
    ``key = "value"`` lines inside the ``[project]`` table are read.
    Missing/unreadable files return ``{}``.
    """
    try:
        text = (Path(working_directory) / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeDecodeError):
        return {}
    section = re.search(
        r"(?ms)^\[project\]\s*\n(.*?)(?=^\s*\[|\Z)", text
    )
    if not section:
        return {}
    body = section.group(1)
    found: dict[str, str] = {}
    for key in ("name", "description"):
        match = re.search(
            rf"(?m)^\s*{key}\s*=\s*[\"']([^\"'\n]+)[\"']", body
        )
        if match:
            found[key] = match.group(1).strip()
    return found


def _git_config_path(working_directory: Path) -> Path | None:
    """Locate the effective ``config`` file for a checkout.

    Handles plain ``.git/config`` and ``.git`` pointer files (linked
    worktrees/submodules) via ``commondir``.
    """
    dotgit = working_directory / ".git"
    try:
        if dotgit.is_dir():
            return dotgit / "config"
        if dotgit.is_file():
            line = dotgit.read_text(encoding="utf-8").strip()
            if not line.startswith("gitdir:"):
                return None
            gitdir = Path(line.split(":", 1)[1].strip())
            if not gitdir.is_absolute():
                gitdir = (working_directory / gitdir).resolve()
            commondir = gitdir / "commondir"
            if commondir.is_file():
                base = gitdir / commondir.read_text(encoding="utf-8").strip()
                return base.resolve() / "config"
            return gitdir / "config"
    except OSError:
        return None
    return None


def read_git_remote(working_directory: str | Path) -> str | None:
    """Return the ``origin`` remote URL from ``.git/config``, or ``None``."""
    config = _git_config_path(Path(working_directory))
    if config is None:
        return None
    try:
        text = config.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    origin = re.search(
        r'(?ms)^\[remote\s+"origin"\]\s*\n(.*?)(?=^\s*\[|\Z)', text
    )
    if not origin:
        return None
    url = re.search(r"(?m)^\s*url\s*=\s*(\S+)\s*$", origin.group(1))
    return url.group(1) if url else None


def normalize_repo_url(url: str | None) -> str | None:
    """Canonical ``https://github.com/owner/repo`` compare form (lowercase).

    Accepts ``https://``, ``git@host:owner/repo`` and ``ssh://git@host/...``
    remotes, strips a trailing ``.git`` and any trailing slash. Anything
    unrecognized is returned lowercased and stripped.
    """
    if not url:
        return None
    cleaned = url.strip().rstrip("/")
    ssh = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+)$", cleaned)
    if ssh:
        cleaned = f"https://{ssh.group(1)}/{ssh.group(2)}"
    cleaned = cleaned.removesuffix(".git")
    return cleaned.lower()


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------

def _resolve_local_dir(registry_path: Path, local_dir: str) -> Path | None:
    """Resolve a registry ``local_dir`` (relative to the hub or its parent)."""
    for base in (registry_path.parent, registry_path.parent.parent):
        candidate = (base / local_dir).resolve()
        if candidate.is_dir():
            return candidate
    return (registry_path.parent / local_dir).resolve()


def verify(projects: Iterable[Project], registry_path: str | Path) -> dict:
    """Diff pm-tracked ``projects`` against the hub registry (pure data).

    Returns the report dict; raises ``FileNotFoundError`` when the registry
    does not exist and ``RegistryError`` when it cannot be parsed.
    """
    registry_path = Path(registry_path)
    entries = load_hub_registry(registry_path)
    ecosystem_root = registry_path.resolve().parent.parent

    manifests: dict[str, dict] = {}
    project_by_name: dict[str, Project] = {}
    for project in projects:
        project_by_name[project.name] = project
        manifests[project.name] = {
            "working_directory": project.working_directory,
            **read_pyproject(project.working_directory),
            "url": read_git_remote(project.working_directory),
        }

    pm_names = set(project_by_name)
    matched: dict[str, str] = {}  # registry name -> pm project name

    # Match pass 1: identical names (registry name == working-dir basename).
    for name in entries:
        if name in pm_names:
            matched[name] = name

    # Match pass 2: registry ``local_dir`` resolves to a tracked working dir.
    wd_lookup = {
        str(Path(p.working_directory).resolve()): p.name
        for p in project_by_name.values()
    }
    for name, entry in entries.items():
        if name in matched or not entry.get("local_dir"):
            continue
        resolved = _resolve_local_dir(registry_path, entry["local_dir"])
        pm_name = wd_lookup.get(str(resolved))
        if pm_name:
            matched[name] = pm_name

    unknown_to_pm = [
        {
            "name": name,
            "kind": entry.get("kind"),
            "visibility": entry.get("visibility"),
        }
        for name, entry in sorted(entries.items())
        if name not in matched
    ]

    matched_pm = set(matched.values())
    missing_from_registry = []
    for name in sorted(pm_names - matched_pm):
        project = project_by_name[name]
        # ``working_directory`` may come from another platform (a sessions.db
        # synced from Windows stores ``C:/...`` rows). Only paths that are
        # absolute *on this platform* can sit under the ecosystem root —
        # resolving a foreign relative-shaped path would anchor it under cwd
        # and produce false "relevant" hits.
        raw_wd = Path(project.working_directory)
        relevant = name.startswith("devin-") or (
            raw_wd.is_absolute()
            and raw_wd.resolve().is_relative_to(ecosystem_root)
        )
        missing_from_registry.append(
            {
                "name": name,
                "working_directory": project.working_directory,
                "sessions": project.session_count,
                "relevant": bool(relevant),
            }
        )

    field_drift = []
    for reg_name, pm_name in sorted(matched.items()):
        entry = entries[reg_name]
        manifest = manifests[pm_name]

        # name: the checkout's declared project name vs the registry name
        declared = manifest.get("name")
        if declared and declared != reg_name:
            field_drift.append(
                {
                    "name": reg_name,
                    "field": "name",
                    "pm": declared,
                    "registry": reg_name,
                    "working_directory": manifest["working_directory"],
                }
            )
        for field in ("description", "url"):
            pm_value = manifest.get(field)
            reg_value = entry.get(field)
            if pm_value is None or reg_value is None:
                continue
            if field == "url":
                differ = normalize_repo_url(pm_value) != normalize_repo_url(
                    reg_value
                )
            else:
                differ = pm_value != reg_value
            if differ:
                field_drift.append(
                    {
                        "name": reg_name,
                        "field": field,
                        "pm": pm_value,
                        "registry": reg_value,
                        "working_directory": manifest["working_directory"],
                    }
                )

    relevant_missing = [m for m in missing_from_registry if m["relevant"]]
    return {
        "registry": str(registry_path),
        "ecosystem_root": str(ecosystem_root),
        "projects": len(project_by_name),
        "registry_entries": len(entries),
        "matched": sorted(matched),
        "unknown_to_pm": unknown_to_pm,
        "missing_from_registry": missing_from_registry,
        "field_drift": field_drift,
        "drift": bool(field_drift or relevant_missing),
    }


def projects_from_pm_registry(path: str | Path) -> list[Project]:
    """Rehydrate projects from a ``devin-pm registry --out`` document.

    Lets ``verify`` run against a saved pm registry instead of opening
    ``sessions.db`` — useful where the DB is unavailable but a registry
    export exists.
    """
    path = Path(path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot load {path}: {exc}") from exc
    items = doc.get("projects")
    if not isinstance(items, list):
        raise RegistryError(f"{path}: 'projects' must be a list")
    projects = []
    for item in items:
        if not isinstance(item, dict) or "name" not in item:
            raise RegistryError(
                f"{path}: project entries must be objects with a 'name'"
            )
        projects.append(
            Project(
                name=item["name"],
                working_directory=item.get("working_directory", item["name"]),
                sessions=(),
            )
        )
    return projects


def fix_hint(item: dict, section: str) -> str:
    """One-line remediation for a divergence — the ``--fix-hints`` layer.

    ``section`` is ``unknown_to_pm`` / ``missing_from_registry`` /
    ``field_drift``. The registry is authoritative, so the expected value
    is always the registry side; the action either fixes the checkout to
    match or updates the registry when the local value is the intended
    one. Pure data — the CLI may also inject it as ``item["hint"]`` for
    ``--json`` consumers.
    """
    if section == "unknown_to_pm":
        return (
            "informational only — no pm sessions tracked yet; run a "
            "session in the checkout to track it"
        )
    if section == "missing_from_registry":
        if not item.get("relevant"):
            return "non-ecosystem — no registry entry expected; no action"
        return (
            f"expected a registry entry for {item['name']!r} — add it to "
            "registry.json repositories[] (local_dir relative to the hub "
            "works for sibling checkouts), or move the checkout out of "
            "the ecosystem"
        )
    # field_drift: expected value is the registry side
    field = item["field"]
    expected = item["registry"]
    wd = item.get("working_directory")
    pyproject = f"{wd}/pyproject.toml" if wd else "pyproject.toml"
    if field == "name":
        return (
            f"expected {expected!r} — set [project].name in {pyproject} "
            "to match (or update the registry entry if the rename is "
            "intended)"
        )
    if field == "url":
        git = f"git -C {wd} " if wd else "git "
        return (
            f"expected {expected!r} — run `{git}remote set-url origin "
            f"{expected}` (or fix the registry url if origin moved)"
        )
    return (
        f"expected {expected!r} — update {field} in {pyproject} (or "
        "edit the registry entry if pm's value is newer)"
    )


def add_fix_hints(report: dict) -> dict:
    """Inject ``item[\"hint\"]`` into every divergence (for ``--json``)."""
    for section in (
        "unknown_to_pm",
        "missing_from_registry",
        "field_drift",
    ):
        for item in report[section]:
            item["hint"] = fix_hint(item, section)
    return report


def render_text(report: dict, hints: bool = False) -> str:
    """Human-readable verify report (ASCII only — cp1252-safe).

    ``hints=True`` appends a ``hint:`` line under every divergence —
    the ``--fix-hints`` layer over the same report data.
    """
    lines = [
        "devin-pm verify",
        f"  registry: {report['registry']} ({report['registry_entries']} entries)",
        f"  projects: {report['projects']} tracked",
        "",
    ]
    lines.append(
        f"in registry but unknown to pm ({len(report['unknown_to_pm'])}):"
    )
    if report["unknown_to_pm"]:
        for item in report["unknown_to_pm"]:
            detail = "/".join(
                str(item[k]) for k in ("kind", "visibility") if item.get(k)
            )
            lines.append(f"  - {item['name']}" + (f" ({detail})" if detail else ""))
            if hints:
                lines.append(f"    hint: {fix_hint(item, 'unknown_to_pm')}")
    else:
        lines.append("  (none)")
    lines.append(
        "tracked by pm but missing from registry "
        f"({len(report['missing_from_registry'])}):"
    )
    if report["missing_from_registry"]:
        for item in report["missing_from_registry"]:
            flag = "" if item["relevant"] else " [non-ecosystem]"
            lines.append(
                f"  - {item['name']} ({item['working_directory']}){flag}"
            )
            if hints:
                lines.append(
                    "    hint: "
                    f"{fix_hint(item, 'missing_from_registry')}"
                )
    else:
        lines.append("  (none)")
    lines.append(f"field drift ({len(report['field_drift'])}):")
    if report["field_drift"]:
        for item in report["field_drift"]:
            lines.append(
                f"  - {item['name']}.{item['field']}: "
                f"pm={item['pm']!r} registry={item['registry']!r}"
            )
            if hints:
                lines.append(f"    hint: {fix_hint(item, 'field_drift')}")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("result: " + ("DRIFT" if report["drift"] else "OK"))
    return "\n".join(lines)
