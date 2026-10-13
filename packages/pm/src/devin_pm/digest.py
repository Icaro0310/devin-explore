"""Weekly-ish project digest — devin-pm's incrementality surface.

``devin-pm digest`` renders a markdown digest of project/milestone
activity newer than a watermark — the sessions a weekly review cares
about, not the whole history ``report`` already covers. The baseline
comes from ``--since`` (epoch s/ms or ISO-8601), from the shared
``pm-digest`` cursor (``--incremental`` — same contract as
``devin-history export --incremental``), or defaults to the last
``DEFAULT_WINDOW_MS`` (7 days) when neither is given.

Envelope (``devin-powerups/docs/incrementality.md``):

- the report file is written **atomically** (tmp + ``os.replace``);
- the cursor commits only *after* the report is emitted — a crash
  re-digests instead of skipping;
- missing/corrupt cursor state **fails open** to a full-history digest
  (baseline 0);
- ``DEVIN_CURSOR_DIR`` (handled inside ``devin_internals.cursor``)
  relocates the state file for tests/offloaded runs.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from devin_pm.milestones import Milestone, collect_milestones, done_fraction
from devin_pm.projects import Project
from devin_pm.report import (
    _sessions_table,
    _summary_line,
    ms_to_iso,
)

CURSOR_NAME = "pm-digest"
"""Shared-cursor name → ``<dir>/pm-digest-state.json``."""

DEFAULT_WINDOW_MS = 7 * 86_400_000
"""Bare ``digest`` looks back one week — the "weekly-ish" default."""

_EPOCH_RE = re.compile(r"^\d+(\.\d+)?$")
# Epoch ms values sit at ~1e12 today; anything under 1e11 must be seconds.
_EPOCH_SECONDS_CUTOFF = 100_000_000_000


def parse_since(text: str) -> int:
    """``--since`` value → epoch ms.

    Accepts a bare epoch (seconds or milliseconds — values under
    ``1e11`` are read as seconds) or an ISO-8601 date/datetime
    (``2026-10-13``, ``2026-10-13T10:00:00Z``); naive datetimes are UTC.
    Raises :class:`ValueError` on anything else.
    """
    raw = str(text).strip()
    if _EPOCH_RE.match(raw):
        value = float(raw)
        if value < _EPOCH_SECONDS_CUTOFF:
            value *= 1000.0
        return int(value)
    iso = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(
            f"invalid --since {text!r} — expected epoch s/ms or ISO-8601 "
            "(e.g. 2026-10-13 or 2026-10-13T10:00:00Z)"
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def digest_filename(now: datetime | None = None) -> str:
    """``pm-digest-YYYY-Www.md`` — ISO week of ``now`` (UTC)."""
    now = now or datetime.now(tz=timezone.utc)
    return f"pm-digest-{now.strftime('%G-W%V')}.md"


def window_projects(
    projects: Iterable[Project], since_ms: int
) -> list[Project]:
    """Projects restricted to sessions with activity after ``since_ms``.

    Projects with no in-window session drop out entirely; the survivors
    keep name/working_directory but carry only the newer sessions —
    ``last_activity_at``/``status_counts``/``cost`` therefore describe
    the window, not all history. Sorted most-recent first.
    """
    out = []
    for project in projects:
        sessions = tuple(
            s for s in project.sessions
            if int(s.last_activity_at or 0) > since_ms
        )
        if sessions:
            out.append(
                Project(
                    name=project.name,
                    working_directory=project.working_directory,
                    sessions=sessions,
                )
            )
    return sorted(out, key=lambda p: p.last_activity_at, reverse=True)


def _milestone_lines(
    milestones: Iterable[Milestone], in_window_ids: set[str]
) -> list[str]:
    """Checkbox list; session-sourced milestones in-window get a marker."""
    ms = list(milestones)
    if not ms:
        return []
    lines = [""]
    for m in ms:
        mark = " _(this period)_" if m.session_id in in_window_ids else ""
        lines.append(f"- [{'x' if m.done else ' '}] {m.name}{mark}")
    done, total, pct = done_fraction(ms)
    lines += ["", f"Done: {done}/{total} ({pct:.0f}%)"]
    return lines


def render_digest(
    projects: Iterable[Project],
    milestones: Mapping[str, Iterable[Milestone]],
    *,
    since_ms: int,
    now: datetime | None = None,
) -> str:
    """``# devin-pm weekly digest — YYYY-Www`` over in-window projects.

    ``milestones`` maps project name → milestone list; pass the
    *unfiltered* project's milestones so the section reflects current
    state, with ``_(this period)_`` marking entries whose session moved
    inside the window.
    """
    now = now or datetime.now(tz=timezone.utc)
    projects = list(projects)
    lines = [
        f"# devin-pm weekly digest — {now.strftime('%G-W%V')}",
        "",
        (
            f"_{len(projects)} projects with activity since "
            f"{ms_to_iso(since_ms)}, generated "
            f"{now.strftime('%Y-%m-%d')}_"
        ),
    ]
    for project in projects:
        in_window_ids = {s.id for s in project.sessions}
        lines += [
            "",
            f"## {project.name}",
            "",
            _summary_line(project),
            "",
            "### Sessions",
            "",
            *_sessions_table(project),
        ]
        tail = _milestone_lines(
            milestones.get(project.name, ()), in_window_ids
        )
        if tail:
            lines += ["", "### Milestones", *tail]
    return "\n".join(lines) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    """tmp + ``os.replace`` — the envelope's atomic-write rule."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=path.parent, prefix=path.name, suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class DigestResult:
    """Outcome of :func:`run_digest`."""

    since_ms: int
    report: str | None = None
    """Rendered markdown; ``None`` when nothing was new — no file then."""
    out_path: Path | None = None
    """Digest file written (``None`` for ``stdout``/nothing-new)."""
    projects: list[Project] = field(default_factory=list)
    """In-window projects that made it into the report."""
    watermark: int | None = None
    """Committed ``last_activity_at`` (cursor mode only)."""


def _milestones_map(
    active: list[Project],
    originals: Mapping[str, Project],
    milestones: (
        Callable[[Project], list[Milestone]]
        | Mapping[str, Iterable[Milestone]]
        | None
    ),
) -> dict[str, Iterable[Milestone]]:
    """Milestones per active project, collected on the *full* project."""
    if milestones is None:
        return {p.name: collect_milestones(originals[p.name]) for p in active}
    if callable(milestones):
        return {p.name: milestones(originals[p.name]) for p in active}
    return {p.name: milestones.get(p.name, ()) for p in active}


def run_digest(
    projects: Iterable[Project],
    *,
    since_ms: int | None = None,
    cursor=None,
    out_dir: str | Path | None = None,
    stdout: bool = False,
    now: datetime | None = None,
    milestones=None,
) -> DigestResult:
    """Produce the digest: filter → render → write → commit cursor.

    Baseline precedence mirrors ``devin-history export``: an explicit
    ``since_ms`` wins; otherwise ``cursor.get("last_activity_at")`` when
    a cursor is passed (missing/corrupt state → 0, a full digest);
    otherwise the trailing 7-day window.

    When the cursor is given, its watermark advances to the max
    ``last_activity_at`` over *all* sessions — committed only after the
    report is written/returned, never on failure. ``stdout=True``
    returns the report text instead of writing
    ``pm-digest-YYYY-Www.md``; it still commits (the digest was
    produced). A run with no in-window activity produces no report and
    no file.
    """
    now = now or datetime.now(tz=timezone.utc)
    projects = list(projects)

    effective_since = since_ms
    if effective_since is None:
        if cursor is not None:
            try:
                effective_since = int(
                    cursor.get("last_activity_at", 0) or 0
                )
            except (TypeError, ValueError):
                # valid JSON holding a garbage value — fail open too
                effective_since = 0
        else:
            effective_since = (
                int(now.timestamp() * 1000) - DEFAULT_WINDOW_MS
            )

    active = window_projects(projects, effective_since)
    result = DigestResult(since_ms=effective_since, projects=active)

    if active:
        originals = {p.name: p for p in projects}
        report = render_digest(
            active,
            _milestones_map(active, originals, milestones),
            since_ms=effective_since,
            now=now,
        )
        result.report = report
        if not stdout:
            directory = Path(out_dir) if out_dir else Path.cwd()
            path = directory / digest_filename(now)
            _atomic_write(path, report)
            result.out_path = path

    if cursor is not None:
        result.watermark = max(
            (
                int(s.last_activity_at or 0)
                for p in projects
                for s in p.sessions
            ),
            default=effective_since,
        )
        cursor.set("last_activity_at", result.watermark)
        cursor.set("generated_at", int(now.timestamp() * 1000))
        cursor.commit()

    return result
