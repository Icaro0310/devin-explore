"""Ecosystem registry discovery — ``devin-powerups/registry.json``.

The registry is the ecosystem's machine-readable map of repositories:
each ``repositories[]`` entry carries a ``name`` and a GitHub ``url``.
Checks that operate at ecosystem scope (the ``automation`` absorbed-repo
sub-check, the ``pr-ready-not-merged`` PR scan) share the same locate +
parse helpers so a registry layout change lands in one place.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from devin_doctor.model import Context

_GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/?$"
)


def locate_registry(ctx: Context, home: Path) -> Path | None:
    """Find ``devin-powerups/registry.json`` under known workspace roots:
    ``ctx.cwd`` and its ancestors, ``~`` and ``~/devin``, plus one level
    of container dirs below them (e.g. ``~/devin/devin-ecosystem/``)."""
    candidates: list[Path] = []
    for root in (ctx.cwd, *ctx.cwd.parents):
        candidates.append(root / "devin-powerups" / "registry.json")
    candidates.append(home / "devin-powerups" / "registry.json")
    candidates.append(home / "devin" / "devin-powerups" / "registry.json")
    for base in (home, home / "devin", ctx.cwd):
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and child.name != "devin-powerups":
                candidates.append(child / "devin-powerups" / "registry.json")
    for path in dict.fromkeys(candidates):
        if path.is_file():
            return path
    return None


def github_slug(url: Any) -> str | None:
    """``owner/name`` from a registry ``url`` — only plain
    ``https://github.com/owner/repo`` forms count (``.git`` suffix
    tolerated); anything else returns ``None``."""
    if not isinstance(url, str):
        return None
    text = url.strip().rstrip("/").removesuffix(".git")
    match = _GITHUB_URL_RE.match(text)
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def registry_slugs(path: Path) -> list[str]:
    """Sorted unique ``owner/name`` slugs for every registry repository
    that points at github.com. Products absorbed into a monorepo share
    the container's URL, so dedup matters — several ``name`` entries map
    to one GitHub repo. Unreadable/malformed registries yield ``[]``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    repos = data.get("repositories") if isinstance(data, dict) else None
    if not isinstance(repos, list):
        return []
    slugs = {
        slug
        for repo in repos
        if isinstance(repo, dict)
        for slug in [github_slug(repo.get("url"))]
        if slug is not None
    }
    return sorted(slugs)
