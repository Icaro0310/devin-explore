"""Export ``sessions.db`` to one file per session plus an index.

Idempotent: the session's ``last_activity`` marker is embedded in each note's
frontmatter (md) or top-level field (json); a rerun skips files whose marker
already matches. Never writes to the source database — the caller hands in a
read-only ``SessionsStore``.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from devin_history import identity, vscdb
from devin_history.format import (
    SURFACE_DEVIN_CHAT,
    SURFACE_DEVIN_CLI,
    evidence_ref,
    project_name,
    render_gui_index_json,
    render_gui_session_md,
    render_index_json,
    render_index_md,
    render_session_md,
    session_to_dict,
)
from devin_history.messages import parse_nodes
from devin_history.times import fmt_ts

MIN_USEFUL_NODES = 2
FORMATS = ("md", "json")

_MARKER_MD = re.compile(r"^last_activity:\s*(\d+)", re.MULTILINE)
_ANCHOR_MD = re.compile(r"^ref:\s*\S", re.MULTILINE)
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class IndexEntry:
    date: str
    filename: str
    title: str
    project: str
    session_id: str = ""
    surface: str = ""
    native_id: str = ""
    ref: str = ""
    user_msgs: int = 0
    assistant_msgs: int = 0
    tool_msgs: int = 0


@dataclass
class ExportResult:
    out_dir: Path
    format: str
    written: list[str] = field(default_factory=list)
    skipped_unchanged: list[str] = field(default_factory=list)
    skipped_empty: list[str] = field(default_factory=list)
    index_entries: list[IndexEntry] = field(default_factory=list)


@dataclass(frozen=True)
class GuiIndexEntry:
    date: str
    filename: str
    slug: str
    title: str
    backend: str
    project: str
    workspace_id: str | None
    last_updated: int | float | None
    last_accessed: int | float | None
    surface: str = ""
    native_id: str = ""
    ref: str = ""


@dataclass
class GuiExportResult:
    out_dir: Path
    written: list[str] = field(default_factory=list)
    skipped_unchanged: list[str] = field(default_factory=list)
    index_entries: list[GuiIndexEntry] = field(default_factory=list)


def _exported_marker(path: Path, fmt: str) -> int | None:
    """Stored ``last_activity`` of a previously written note, if readable.

    ``-1`` when the file is unreadable or lacks the marker — and also when
    it predates the SESSION-EVIDENCE anchors, so an incremental re-export
    heals pre-anchor notes instead of skipping them forever.
    """
    if not path.exists():
        return None
    try:
        if fmt == "md":
            head = path.read_text(encoding="utf-8")[:800]
            fm_end = head.find("\n---", 4)
            frontmatter = head[:fm_end] if fm_end != -1 else head
            m = _MARKER_MD.search(frontmatter)
            if not m or not _ANCHOR_MD.search(frontmatter):
                return -1
            return int(m.group(1))
        data = json.loads(path.read_text(encoding="utf-8"))
        if "ref" not in data:
            return -1
        return int(data.get("last_activity", -1))
    except (OSError, ValueError):
        return -1


def _write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.write_text(text, encoding="utf-8")
    return True


def export_sessions(
    store,
    out_dir: str | Path,
    *,
    fmt: str = "md",
    force: bool = False,
    dry_run: bool = False,
    only_session: str | None = None,
    since: int | None = None,
    cursor=None,
) -> ExportResult:
    """Export every useful session in ``store`` to ``out_dir``.

    ``fmt`` is ``md`` (Obsidian notes) or ``json`` (searchable dump). Sessions
    with no user messages or fewer than ``MIN_USEFUL_NODES`` nodes are skipped.
    ``only_session`` restricts the export to one session id (or unique prefix)
    — the SessionEnd-hook incremental mode (HI-2); the index is still rebuilt
    from all sessions so it stays complete.

    ``since`` (epoch milliseconds) pre-filters to sessions whose
    ``last_activity_at`` is newer — the watermark leg of the incrementality
    spec. ``cursor`` is a
    ``devin_internals.cursor.Cursor``; when given without an explicit
    ``since``, its ``last_activity_at`` value is the baseline and, unless
    ``dry_run``, it commits the max activity seen after the run — a second
    ``--incremental`` invocation therefore exports only what changed. The
    per-file marker skip above stays as the idempotent safety net.
    """
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r} (expected one of {FORMATS})")
    out_dir = Path(out_dir)
    result = ExportResult(out_dir=out_dir, format=fmt)
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    prov = identity.provenance()
    sessions = sorted(store.sessions(), key=lambda s: s.created_at)
    # Positions in the FULL ordered listing back the ephemeral anchor
    # (<surface>:ephemeral:<index>) — taking them after --session-id/--
    # since filtering would renumber workspace-less sessions and collapse
    # distinct notes onto the same ephemeral ref.
    positions = {s.id: i for i, s in enumerate(sessions)}
    if only_session is not None:
        matches = [s for s in sessions
                   if s.id == only_session or s.id.startswith(only_session)]
        if not matches:
            raise ValueError(f"no session matches {only_session!r}")
        if len(matches) > 1:
            raise ValueError(
                f"{only_session!r} matches {len(matches)} sessions — "
                "pass a longer prefix")
        sessions = matches
    effective_since = since
    if effective_since is None and cursor is not None:
        effective_since = cursor.get("last_activity_at", 0) or 0
    if effective_since:
        sessions = [s for s in sessions
                    if int(s.last_activity_at or 0) > effective_since]
    for s in sessions:
        nodes = store.message_nodes(s.id)
        messages = parse_nodes(nodes)
        user_msgs = sum(1 for m in messages if m.role == "user")
        if user_msgs == 0 or len(nodes) < MIN_USEFUL_NODES:
            result.skipped_empty.append(s.id)
            continue

        ref = evidence_ref(
            SURFACE_DEVIN_CLI, s.id, s.working_directory,
            ephemeral_index=positions[s.id],
        )
        date = fmt_ts(s.created_at, "%Y-%m-%d")
        filename = f"{date}_{s.id}.{fmt}"
        fpath = out_dir / filename

        if not force and _exported_marker(fpath, fmt) == s.last_activity_at:
            result.skipped_unchanged.append(s.id)
        else:
            if fmt == "md":
                text = render_session_md(s, messages, prov, ref=ref)
            else:
                text = json.dumps(
                    session_to_dict(
                        s, messages, store.tool_call_state(s.id), prov,
                        ref=ref),
                    ensure_ascii=False,
                    indent=2,
                ) + "\n"
            if not dry_run:
                _write_if_changed(fpath, text)
            result.written.append(filename)

        counts = Counter(m.role for m in messages)
        result.index_entries.append(
            IndexEntry(
                date=date,
                filename=filename,
                session_id=s.id,
                surface=SURFACE_DEVIN_CLI,
                native_id=s.id,
                ref=ref,
                title=(s.title or "Untitled").strip(),
                project=project_name(s.working_directory),
                user_msgs=counts["user"],
                assistant_msgs=counts["assistant"],
                tool_msgs=counts["tool"],
            )
        )

    if not dry_run:
        if only_session is not None:
            _merge_index_entry(out_dir, fmt, result.index_entries, prov)
        else:
            index = (
                render_index_md(result.index_entries)
                if fmt == "md"
                else render_index_json(result.index_entries, prov)
            )
            _write_if_changed(out_dir / f"index.{fmt}", index)

    if cursor is not None and not dry_run:
        watermark = max(
            (int(s.last_activity_at or 0) for s in store.sessions()),
            default=int(effective_since or 0),
        )
        cursor.set("last_activity_at", watermark)
        cursor.commit()

    return result


def _merge_index_entry(out_dir: Path, fmt: str,
                       new_entries: list, prov) -> None:
    """Single-session export: merge the new entry into the existing index.

    ``index.json`` merges by ``session_id`` (it carries one); for markdown
    indexes we append a line only when the index file already exists —
    regenerating the full markdown index from one session would drop the
    rest, so we leave a complete re-export to rebuild it.
    """
    if fmt == "json":
        ipath = out_dir / "index.json"
        existing = []
        if ipath.is_file():
            try:
                existing = json.loads(
                    ipath.read_text(encoding="utf-8")).get("sessions", [])
            except (OSError, ValueError, AttributeError):
                existing = []
        seen = {e.session_id for e in new_entries}
        merged_entries = [
            IndexEntry(
                date=e.get("date", ""), filename=e.get("file", ""),
                title=e.get("title", ""), project=e.get("project", ""),
                session_id=e.get("session_id", ""),
                surface=e.get("surface", ""),
                native_id=e.get("native_id", ""),
                ref=e.get("ref", ""),
                user_msgs=e.get("user_msgs", 0),
                assistant_msgs=e.get("assistant_msgs", 0),
                tool_msgs=e.get("tool_msgs", 0))
            for e in existing if e.get("session_id") not in seen
        ]
        merged_entries.extend(new_entries)
        merged_entries.sort(key=lambda e: (e.date, e.filename))
        _write_if_changed(ipath, render_index_json(merged_entries, prov))
    elif (out_dir / "index.md").is_file() and new_entries:
        for e in new_entries:
            line = (f"- [{e.date} — {e.title}]({e.filename}) "
                    f"({e.project})\n")
            with open(out_dir / "index.md", "a", encoding="utf-8") as fh:
                fh.write(line)



def _safe_name(slug: str) -> str:
    return _UNSAFE_NAME.sub("-", slug).strip("-.") or "session"


def export_gui_sessions(
    store,
    out_dir: str | Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> GuiExportResult:
    """Export GUI session metadata from ``state.vscdb`` to ``out_dir``.

    One Markdown note per ``windsurfSpace.sessionWorkspace/*`` binding plus a
    root ``index.json``. Idempotent via the ``last_activity`` frontmatter
    marker (= the binding's ``lastUpdated``); ``--all``/``--dry-run`` mirror
    :func:`export_sessions`. Never writes to the source database — the caller
    hands in a read-only ``StateVscdbStore``.
    """
    out_dir = Path(out_dir)
    result = GuiExportResult(out_dir=out_dir)
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    prov = identity.provenance()
    sessions = sorted(
        vscdb.gui_sessions(store),
        key=lambda g: (g.last_updated or g.last_accessed or 0, g.slug),
    )
    used_names: set[str] = set()
    for i, gs in enumerate(sessions):
        marker = int(gs.last_updated or gs.last_accessed or 0)
        ref = evidence_ref(
            SURFACE_DEVIN_CHAT, gs.slug, gs.workspace_id,
            ephemeral_index=i,
        )
        date = (
            fmt_ts(gs.last_updated or gs.last_accessed, "%Y-%m-%d")
            or "undated"
        )
        filename = f"{date}_{_safe_name(gs.slug)}.md"
        if filename in used_names:  # same slug under another backend
            filename = f"{date}_{_safe_name(gs.backend)}-{_safe_name(gs.slug)}.md"
        used_names.add(filename)
        fpath = out_dir / filename

        if not force and _exported_marker(fpath, "md") == marker:
            result.skipped_unchanged.append(gs.slug)
        else:
            if not dry_run:
                _write_if_changed(
                    fpath, render_gui_session_md(gs, prov, ref=ref))
            result.written.append(filename)

        result.index_entries.append(
            GuiIndexEntry(
                date=date,
                filename=filename,
                slug=gs.slug,
                title=(gs.label or gs.slug).strip(),
                backend=gs.backend,
                project=project_name(gs.workspace_id),
                workspace_id=gs.workspace_id,
                last_updated=gs.last_updated,
                last_accessed=gs.last_accessed,
                surface=SURFACE_DEVIN_CHAT,
                native_id=gs.slug,
                ref=ref,
            )
        )

    if not dry_run:
        _write_if_changed(
            out_dir / "index.json",
            render_gui_index_json(result.index_entries, prov),
        )

    return result
