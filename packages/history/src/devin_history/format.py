"""Markdown / JSON / CSV emitters — pure functions, no I/O beyond ``write_csv``.

Everything here is deterministic: no timestamps of "now" are embedded, so
re-exporting an unchanged database produces byte-identical output.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from devin_history.messages import ChatMessage
from devin_history.times import duration_minutes, fmt_ts

if TYPE_CHECKING:
    from devin_internals.parsers import Session, ToolCallState

    from devin_history.audit import AuditReport, SessionAudit
    from devin_history.export import GuiIndexEntry, IndexEntry
    from devin_history.vscdb import GuiSession

USER_CAP = 4000
ASSISTANT_CAP = 1500

# SESSION-EVIDENCE anchors (devin-internals-spec → docs/SESSION-EVIDENCE.md
# §1 + SESSION-SURFACES §1/§4): ``surface`` is the closed-set surface of the
# store the session came from, ``native_id`` the store's own session id, and
# ``ref`` the §4 canonical id when a workspace identity exists (else the
# ephemeral ``display_key``, §2). Exported notes carry the same three keys so
# downstream consumers (e.g. the claims-vs-evidence auditor) can bind an
# export back to the canonical session record.
SURFACE_DEVIN_CLI = "devin-cli"    # ``cli/sessions.db``
SURFACE_DEVIN_CHAT = "devin-chat"  # Devin Desktop chat (state.vscdb bindings)


def _enc(component: str) -> str:
    """Encode one canonical-id component: ``%`` first, then ``:`` (§4)."""
    return component.replace("%", "%25").replace(":", "%3A")


def _normalize_ws_seed(path: str) -> str:
    """Workspace-seed normalization (SURFACES §3): trim, strip trailing
    separators — the same treatment devin-search applies before hashing."""
    seed = path.strip()
    while len(seed) > 1 and seed.endswith(("/", "\\")):
        seed = seed[:-1]
    return seed


def evidence_ref(
    surface: str,
    native_id: str,
    workspace_seed: str | None,
    *,
    ephemeral_index: int = 0,
) -> str:
    """SESSION-EVIDENCE ``ref`` for one exported session.

    ``<surface>:<workspace-hash>:native:<id>`` — the SESSION-SURFACES §4
    canonical id — when a workspace identity is derivable; the ephemeral
    ``<surface>:ephemeral:<index>`` display key otherwise (§4: a hashless
    native id never gets a canonical id). The workspace hash is
    ``sha256(normalized seed)[:16]`` — the path itself is never emitted.
    """
    if workspace_seed and workspace_seed.strip():
        ws_hash = hashlib.sha256(
            _normalize_ws_seed(workspace_seed).encode("utf-8")
        ).hexdigest()[:16]
        return f"{surface}:{ws_hash}:native:{_enc(native_id)}"
    return f"{surface}:ephemeral:{ephemeral_index}"


def clip(text: str, cap: int) -> str:
    text = text.strip()
    if len(text) > cap:
        return text[:cap] + f"\n\n*… ({len(text) - cap} chars truncated)*"
    return text


def project_name(working_directory: str | None) -> str:
    return Path(working_directory).name if working_directory else "?"


def _conversation_parts(messages: Iterable[ChatMessage]) -> list[str]:
    parts: list[str] = []
    last_block = None
    for m in messages:
        if m.role not in ("user", "assistant"):
            continue
        content = m.text.strip()
        if not content or content == last_block:
            continue
        last_block = content
        label = "🧑 User" if m.role == "user" else "🤖 Devin"
        parts.append(f"#### {label}\n\n{clip(content, USER_CAP if m.role == 'user' else ASSISTANT_CAP)}")
    return parts


def render_session_md(
    session: Session,
    messages: list[ChatMessage],
    prov: dict[str, str] | None = None,
    *,
    ephemeral_index: int = 0,
) -> str:
    """One Obsidian-ready note for a CLI session."""
    counts = Counter(m.role for m in messages)
    parts = _conversation_parts(messages)
    prov_fm = "" if prov is None else (
        f"\nmachine_id: {prov['machine_id']}\nprofile: {prov['profile']}"
    )
    ref = evidence_ref(
        SURFACE_DEVIN_CLI, session.id, session.working_directory,
        ephemeral_index=ephemeral_index,
    )
    project = project_name(session.working_directory)
    title = (session.title or "Untitled").strip()
    created = fmt_ts(session.created_at)
    body = "\n\n".join(parts) if parts else "_No user/assistant messages — tool calls only._"
    return f"""---
session_id: {session.id}
surface: {SURFACE_DEVIN_CLI}
native_id: {session.id}
ref: {ref}
project: {project}
started: {created}
last_activity: {session.last_activity_at}
user_msgs: {counts['user']}
assistant_msgs: {counts['assistant']}
tool_msgs: {counts['tool']}
tags: [session, devin, history]{prov_fm}
---

# {title}

> Session `{session.id}` · project **{project}** · started {created}
> {counts['user']} user messages · {counts['assistant']} replies · {counts['tool']} tool calls

`{session.working_directory}`

---

## Conversation

{body}

---
"""


def session_to_dict(
    session: Session,
    messages: list[ChatMessage],
    tool_calls: list[ToolCallState],
    prov: dict[str, str] | None = None,
    *,
    ephemeral_index: int = 0,
) -> dict[str, Any]:
    """Searchable JSON shape for one session (full text, no clipping)."""
    return {
        **({"machine_id": prov["machine_id"], "profile": prov["profile"]}
           if prov else {}),
        "session_id": session.id,
        "surface": SURFACE_DEVIN_CLI,
        "native_id": session.id,
        "ref": evidence_ref(
            SURFACE_DEVIN_CLI, session.id, session.working_directory,
            ephemeral_index=ephemeral_index,
        ),
        "title": (session.title or "Untitled").strip(),
        "project": project_name(session.working_directory),
        "working_directory": session.working_directory,
        "backend_type": session.backend_type,
        "model": session.model,
        "agent_mode": session.agent_mode,
        "hidden": session.hidden,
        "created_at": session.created_at,
        "created": fmt_ts(session.created_at),
        "last_activity": session.last_activity_at,
        "last_activity_iso": fmt_ts(session.last_activity_at),
        "messages": [
            {"role": m.role, "thinking": m.thinking, "text": m.text}
            for m in messages
        ],
        "tool_calls": [
            {
                "tool_call_id": tc.tool_call_id,
                "call": _json_or_none(tc.tool_call_json),
                "update": _json_or_none(tc.tool_call_update_json),
            }
            for tc in tool_calls
        ],
    }


def _json_or_none(raw: str | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def index_stats(entries: list[IndexEntry]) -> dict[str, Any]:
    """Aggregate totals for the index stats block (md and json)."""
    projects = Counter(e.project for e in entries)
    formats = Counter(
        Path(e.filename).suffix.lstrip(".") or "?" for e in entries
    )
    dates = sorted(e.date for e in entries)
    return {
        "total": len(entries),
        "date_first": dates[0] if dates else None,
        "date_last": dates[-1] if dates else None,
        "messages": {
            "user": sum(e.user_msgs for e in entries),
            "assistant": sum(e.assistant_msgs for e in entries),
            "tool": sum(e.tool_msgs for e in entries),
        },
        "projects": dict(sorted(projects.items())),
        "formats": dict(sorted(formats.items())),
    }


def render_index_md(entries: list[IndexEntry]) -> str:
    """`index.md` — stats block plus sessions grouped by project."""
    stats = index_stats(entries)
    by_project: dict[str, list[IndexEntry]] = {}
    for e in entries:
        by_project.setdefault(e.project, []).append(e)
    span = (
        f"{stats['date_first']} → {stats['date_last']}"
        if stats["date_first"]
        else "—"
    )
    formats = " · ".join(f"{k}: {v}" for k, v in stats["formats"].items()) or "—"
    msg = stats["messages"]
    lines = [
        "---",
        "tags: [index, sessions, devin]",
        "---",
        "",
        "# Session index",
        "",
        f"Exported from `sessions.db` — {len(entries)} sessions.",
        "",
        "## Stats",
        "",
        f"**Span:** {span} · **Formats:** {formats}",
        "",
        "| Project | Sessions | User | Assistant | Tool |",
        "|---|---|---|---|---|",
    ]
    for project in sorted(stats["projects"], key=lambda p: (-stats["projects"][p], p)):
        sub = [e for e in entries if e.project == project]
        lines.append(
            f"| {project} | {stats['projects'][project]} "
            f"| {sum(e.user_msgs for e in sub)} "
            f"| {sum(e.assistant_msgs for e in sub)} "
            f"| {sum(e.tool_msgs for e in sub)} |"
        )
    lines.append(
        f"| **Total** | **{stats['total']}** | **{msg['user']}** "
        f"| **{msg['assistant']}** | **{msg['tool']}** |"
    )
    for project in sorted(by_project):
        lines.append(f"\n## {project}\n")
        for e in sorted(by_project[project], key=lambda x: x.date):
            lines.append(f"- [[{e.filename[:-3]}|{e.date} — {e.title[:70]}]]")
    return "\n".join(lines) + "\n"


def render_index_json(
    entries: list[IndexEntry], prov: dict[str, str] | None = None
) -> str:
    return json.dumps(
        {**({"provenance": prov} if prov else {}),
         "stats": index_stats(entries),
         "sessions": [
            {"date": e.date, "file": e.filename, "title": e.title,
             "project": e.project, "session_id": e.session_id,
             "user_msgs": e.user_msgs, "assistant_msgs": e.assistant_msgs,
             "tool_msgs": e.tool_msgs}
            for e in entries
        ]},
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def render_gui_session_md(
    session: GuiSession,
    prov: dict[str, str] | None = None,
    *,
    ephemeral_index: int = 0,
) -> str:
    """One Obsidian-ready note for a GUI session (metadata only).

    GUI sessions have no local transcript — the note carries the
    session↔workspace binding stored in ``state.vscdb``.
    """
    prov_fm = "" if prov is None else (
        f"\nmachine_id: {prov['machine_id']}\nprofile: {prov['profile']}"
    )
    accessed_fm = "" if session.last_accessed is None else (
        f"\nlast_accessed: {int(session.last_accessed)}"
    )
    ref = evidence_ref(
        SURFACE_DEVIN_CHAT, session.slug, session.workspace_id,
        ephemeral_index=ephemeral_index,
    )
    project = project_name(session.workspace_id)
    title = (session.label or session.slug).strip()
    updated = fmt_ts(session.last_updated)
    accessed = fmt_ts(session.last_accessed)
    folders = (
        "\n".join(f"  - `{f}`" for f in session.folders)
        if session.folders
        else "  - _none recorded_"
    )
    return f"""---
session_id: {session.slug}
source: gui
surface: {SURFACE_DEVIN_CHAT}
native_id: {session.slug}
ref: {ref}
backend: {session.backend}
project: {project}
last_activity: {int(session.last_updated or 0)}{accessed_fm}
tags: [session, devin, history, gui]{prov_fm}
---

# {title}

> GUI session `{session.slug}` · backend **{session.backend}** · workspace `{session.workspace_id or '—'}`
> lastUpdated {updated or '—'} · lastAccessed {accessed or '—'}

## Workspace

- **Workspace ID:** `{session.workspace_id or '—'}`
- **Folders:**
{folders}

## Metadata

| Field | Value |
|---|---|
| Slug | `{session.slug}` |
| Label | {session.label or '—'} |
| Backend | `{session.backend}` |
| lastUpdated | {session.last_updated if session.last_updated is not None else '—'}{f" ({updated})" if updated else ""} |
| lastAccessed | {session.last_accessed if session.last_accessed is not None else '—'}{f" ({accessed})" if accessed else ""} |
| Space | `{session.space_id or '—'}` |

---

_GUI session metadata from `state.vscdb` — the GUI keeps no local transcript,
so only the session/workspace binding is exportable._
"""


def gui_index_stats(entries: list[GuiIndexEntry]) -> dict[str, Any]:
    """Aggregate totals for the GUI ``index.json`` stats block."""
    projects = Counter(e.project for e in entries)
    backends = Counter(e.backend for e in entries)
    dates = sorted(e.date for e in entries if e.date != "undated")
    return {
        "total": len(entries),
        "date_first": dates[0] if dates else None,
        "date_last": dates[-1] if dates else None,
        "projects": dict(sorted(projects.items())),
        "backends": dict(sorted(backends.items())),
    }


def render_gui_index_json(
    entries: list[GuiIndexEntry], prov: dict[str, str] | None = None
) -> str:
    """``index.json`` for the GUI export — provenance + stats + entries."""
    return json.dumps(
        {**({"provenance": prov} if prov else {}),
         "source": "state.vscdb",
         "stats": gui_index_stats(entries),
         "sessions": [
             {"date": e.date, "file": e.filename, "slug": e.slug,
              "title": e.title, "backend": e.backend, "project": e.project,
              "workspace_id": e.workspace_id,
              "last_updated": e.last_updated,
              "last_accessed": e.last_accessed}
             for e in entries
         ]},
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def sessions_table(sessions: list[Session]) -> str:
    """Fixed-width table for `devin-history list`."""
    header = f"{'ID':<10} {'CREATED':<17} {'DUR':>7} {'MODEL':<14} {'PROJECT':<20} TITLE"
    lines = [header, "-" * len(header)]
    for s in sessions:
        dur = duration_minutes(s.created_at, s.last_activity_at)
        dur_s = f"{dur:.0f}m" if dur < 60 else f"{dur / 60:.1f}h"
        lines.append(
            f"{s.id[:8]:<10} {fmt_ts(s.created_at):<17} {dur_s:>7} "
            f"{(s.model or '?')[:14]:<14} {project_name(s.working_directory)[:20]:<20} "
            f"{(s.title or 'Untitled')[:60]}"
        )
    return "\n".join(lines)


def sessions_to_dicts(sessions: list[Session]) -> list[dict[str, Any]]:
    return [
        {
            "id": s.id,
            "title": (s.title or "Untitled").strip(),
            "project": project_name(s.working_directory),
            "working_directory": s.working_directory,
            "backend_type": s.backend_type,
            "model": s.model,
            "agent_mode": s.agent_mode,
            "hidden": s.hidden,
            "created_at": s.created_at,
            "created": fmt_ts(s.created_at),
            "last_activity": s.last_activity_at,
            "duration_min": round(duration_minutes(s.created_at, s.last_activity_at), 1),
        }
        for s in sessions
    ]


# --------------------------------------------------------------------------
# Audit emitters
# --------------------------------------------------------------------------

AUDIT_CSV_FIELDS = [
    "session_id", "title", "project", "backend", "model", "agent_mode",
    "created", "last_activity", "duration_min", "hidden", "status",
    "task_type", "user_msgs", "assistant_msgs", "tool_calls", "failed_calls",
    "files_touched", "context_tokens_max", "prompt_excerpt",
]


def audit_row_to_dict(r: SessionAudit) -> dict[str, Any]:
    return {
        "session_id": r.id,
        "title": r.title,
        "project": r.project,
        "working_directory": r.working_directory,
        "backend": r.backend,
        "model": r.model,
        "agent_mode": r.agent_mode,
        "created_at": r.created_at,
        "created": fmt_ts(r.created_at),
        "last_activity": r.last_activity_at,
        "duration_min": r.duration_min,
        "hidden": r.hidden,
        "status": r.status,
        "task_type": r.task_type,
        "user_msgs": r.user_msgs,
        "assistant_msgs": r.assistant_msgs,
        "tool_msgs": r.tool_msgs,
        "system_msgs": r.system_msgs,
        "thinking": r.thinking,
        "tool_calls": r.tool_calls,
        "failed_calls": r.failed_calls,
        "tool_kinds": r.tool_kinds,
        "files_touched": r.files_touched,
        "context_tokens_max": r.context_tokens_max,
        "prompt_excerpt": r.prompt_excerpt,
        "fail_samples": r.fail_samples,
    }


def audit_to_dict(report: AuditReport) -> dict[str, Any]:
    return {
        "schema_version": report.schema_version,
        "sessions": [audit_row_to_dict(r) for r in report.rows],
        "anomalies": [
            {"kind": a.kind, "session_id": a.session_id, "detail": a.detail}
            for a in report.anomalies
        ],
        "summary": {
            "total": len(report.rows),
            "by_status": dict(Counter(r.status for r in report.rows)),
            "by_task_type": dict(Counter(r.task_type for r in report.rows)),
            "by_project": dict(Counter(r.project for r in report.rows)),
        },
    }


def write_audit_csv(report: AuditReport, path: str | Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(AUDIT_CSV_FIELDS)
        for r in report.rows:
            w.writerow([
                r.id, r.title, r.project, r.backend, r.model, r.agent_mode,
                fmt_ts(r.created_at, "%Y-%m-%d %H:%M:%S"),
                fmt_ts(r.last_activity_at, "%Y-%m-%d %H:%M:%S"),
                r.duration_min, int(r.hidden), r.status, r.task_type,
                r.user_msgs, r.assistant_msgs, r.tool_calls, r.failed_calls,
                r.files_touched, r.context_tokens_max, r.prompt_excerpt,
            ])


def _fmt_dur(mins: float) -> str:
    return f"{mins:.0f}m" if mins < 60 else f"{mins / 60:.1f}h"


def audit_to_markdown(report: AuditReport) -> str:
    """Compact audit report: summary, grouping tables, anomaly list."""
    rows = report.rows
    L: list[str] = []
    A = L.append
    A("# Session audit\n")
    A(f"Schema v{report.schema_version} · {len(rows)} sessions · "
      "status is **inferred** — `sessions.db` has no final-state column.\n")

    A("## By status\n")
    A("| Status | N |")
    A("|---|---|")
    for k, v in Counter(r.status for r in rows).most_common():
        A(f"| {k} | {v} |")

    A("\n## By task type\n")
    A("| Type | N |")
    A("|---|---|")
    for k, v in Counter(r.task_type for r in rows).most_common():
        A(f"| {k} | {v} |")

    A("\n## By project\n")
    A("| Project | Sessions | Tool calls |")
    A("|---|---|---|")
    for k, v in Counter(r.project for r in rows).most_common():
        A(f"| {k} | {v} | {sum(r.tool_calls for r in rows if r.project == k)} |")

    A("\n## By month\n")
    A("| Month | Sessions | Tool calls | User msgs |")
    A("|---|---|---|---|")
    months = Counter(fmt_ts(r.created_at)[:7] for r in rows)
    for m in sorted(months):
        sub = [r for r in rows if fmt_ts(r.created_at)[:7] == m]
        A(f"| {m} | {len(sub)} | {sum(r.tool_calls for r in sub)} "
          f"| {sum(r.user_msgs for r in sub)} |")

    A("\n## Anomalies\n")
    if not report.anomalies:
        A("_None._")
    for a in report.anomalies:
        A(f"- **{a.kind}** `{a.session_id}` — {a.detail}")

    A("\n## Sessions\n")
    A("| Session | Started | Duration | User msgs | Tool calls | Files | Status | Title |")
    A("|---|---|---|---|---|---|---|---|")
    for r in sorted(rows, key=lambda x: x.created_at):
        A(f"| `{r.id[:8]}` | {fmt_ts(r.created_at)} | {_fmt_dur(r.duration_min)} "
          f"| {r.user_msgs} | {r.tool_calls} ({r.failed_calls}✗) "
          f"| {r.files_touched} | {r.status} | {r.title[:60]} |")
    return "\n".join(L) + "\n"
