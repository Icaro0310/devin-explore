"""Pure emitters — terminal table and JSON shapes. No logic, no I/O."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timezone

from devin_search.index import IndexStats
from devin_search.query import Hit


def fmt_ts(ts_ms: int) -> str:
    if not ts_ms:
        return "-"
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M"
    )


def _flatten(text: str, width: int) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= width:
        return flat
    return flat[: width - 1] + "…"


def _project_label(project: str) -> str:
    tail = project.rstrip("/\\").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return tail or project or "-"


def hits_table(hits: Iterable[Hit]) -> str:
    """Fixed-width table: when · role · project · session · snippet."""
    hits = list(hits)
    if not hits:
        return "no hits"
    header = (
        f"{'WHEN':<16} {'ROLE':<10} {'PROJECT':<16} {'SESSION':<10} SNIPPET"
    )
    lines = [header, "-" * len(header) + "-----"]
    for h in hits:
        lines.append(
            f"{fmt_ts(h.ts):<16} {h.role:<10} {_project_label(h.project):<16}"
            f" {h.session_id[:8]:<10} {_flatten(h.snippet, 60)}"
        )
        if h.history_note:
            lines.append(
                f"{'':<16} {'':<10} {'':<16} {'':<10} note: {h.history_note}"
            )
    return "\n".join(lines)


def hit_to_dict(h: Hit) -> dict:
    return {
        "session_id": h.session_id,
        "role": h.role,
        "ts": h.ts,
        "project": h.project,
        "session_title": h.session_title,
        "source": h.source,
        "ref": h.ref,
        "snippet": h.snippet,
        "rank": h.rank,
        "tool": h.tool,
        "history_note": h.history_note,
    }


def hits_to_dicts(hits: Iterable[Hit]) -> list[dict]:
    return [hit_to_dict(h) for h in hits]


def stats_to_dict(stats: IndexStats) -> dict:
    return {
        "index": str(stats.index_path),
        "rebuilt": stats.rebuilt,
        "indexed": stats.indexed,
        "removed": stats.removed,
        "sources": stats.sources,
        "skipped_records": stats.skipped_records,
        "by_source": {
            "message_nodes": stats.message_nodes,
            "prompt_history": stats.prompt_history,
            "tool_calls": stats.tool_calls,
            "devin_chat_messages": stats.devin_chat_messages,
            "copilot_messages": stats.copilot_messages,
            "codex_messages": stats.codex_messages,
            "vscode_chat_messages": stats.vscode_chat_messages,
        },
    }


def stats_line(stats: IndexStats) -> str:
    verb = "rebuilt" if stats.rebuilt else "indexed"
    return (
        f"{verb}: +{stats.indexed} docs"
        f" (nodes {stats.message_nodes} · prompts {stats.prompt_history}"
        f" · tools {stats.tool_calls} · devin-chat {stats.devin_chat_messages}"
        f" · copilot {stats.copilot_messages} · codex {stats.codex_messages}"
        f" · vscode-chat {stats.vscode_chat_messages})"
        f" · removed {stats.removed} → {stats.index_path}"
    )
