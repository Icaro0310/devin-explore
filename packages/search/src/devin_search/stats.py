"""Index health — what ``devin-search stats`` reports.

Read-only: the index is opened ``mode=ro`` so a stats run can never
create or mutate it. Everything comes from two places — the ``docs``
table (counts, per-source/role breakdowns, newest doc timestamp) and
``index_meta`` (watermark positions, per-file markers, and the
``run:*``/``total:*`` counters ``build_index`` persists after each run).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from devin_search.index import (
    SOURCE_CODEX,
    SOURCE_COPILOT,
    SOURCE_DEVIN_CHAT,
    SOURCE_DEVIN_CLI,
    SOURCE_VSCODE_CHAT,
    WM_CONTENT,
    WM_NODES,
    WM_PROMPTS,
)

_MAX_WM_POSITIONS = 50  # per-source file watermark rows reported at most

# Sources whose incrementality is tracked per file (``:file:`` markers +
# ``:wm:`` positions), as opposed to devin-cli's two table watermarks.
_FILE_SOURCES = (
    SOURCE_DEVIN_CHAT,
    SOURCE_COPILOT,
    SOURCE_CODEX,
    SOURCE_VSCODE_CHAT,
)

_CLI_WM_KEYS = (
    (WM_NODES, "message_nodes"),
    (WM_PROMPTS, "prompt_history"),
    (WM_CONTENT, "content_digest"),
)

_RUN_KEYS = (
    "last_run_at",
    "last_indexed",
    "last_removed",
    "last_swept",
    "last_skipped",
)

_TOTAL_KEYS = ("indexed", "removed", "swept")


@dataclass(frozen=True)
class IndexHealth:
    """Persistent index state — independent of any single run.

    ``run``/``totals`` are empty when the index predates run-stat
    persistence (schema v4 before this feature); callers should report
    "no runs recorded" rather than zeros that mean something else.
    """

    index_path: Path
    schema_version: int | None
    docs: int
    latest_doc_ts: int | None
    by_source: dict[str, dict[str, int | None]]
    by_role: dict[str, int]
    watermarks: dict[str, Any]
    run: dict[str, int] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": str(self.index_path),
            "schema_version": self.schema_version,
            "docs": self.docs,
            "latest_doc_ts": self.latest_doc_ts,
            "by_source": self.by_source,
            "by_role": self.by_role,
            "watermarks": self.watermarks,
            "run": self.run,
            "totals": self.totals,
        }


def _int_or_str(value: str) -> int | str:
    """Watermark positions are ints everywhere except vscode-chat, whose
    watermark is a content sha256 — report it faithfully."""
    try:
        return int(value)
    except ValueError:
        return value


def _watermarks(meta: dict[str, str]) -> dict[str, Any]:
    watermarks: dict[str, Any] = {}
    cli = {
        name: _int_or_str(meta[key])
        for key, name in _CLI_WM_KEYS
        if meta.get(key) is not None
    }
    if cli:
        watermarks[SOURCE_DEVIN_CLI] = cli
    for source in _FILE_SOURCES:
        files = sorted(
            key[len(f"{source}:file:") :]
            for key in meta
            if key.startswith(f"{source}:file:")
        )
        if not files:
            continue
        positions = {
            name: _int_or_str(meta[f"{source}:wm:{name}"])
            for name in files
            if meta.get(f"{source}:wm:{name}") is not None
        }
        entry: dict[str, Any] = {
            "files": len(files),
            "positions": dict(list(positions.items())[:_MAX_WM_POSITIONS]),
        }
        if len(positions) > _MAX_WM_POSITIONS:
            entry["positions_capped"] = True
        watermarks[source] = entry
    return watermarks


def collect(index_path: str | Path) -> IndexHealth:
    """Read the index's persistent health state (``mode=ro``).

    ``FileNotFoundError`` when the index does not exist — the CLI maps
    that to exit 2, same as ``query`` on a missing index."""
    path = Path(index_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"index not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        tables = {
            r["name"]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        meta: dict[str, str] = {}
        if "index_meta" in tables:
            meta = {
                r["key"]: r["value"]
                for r in con.execute("SELECT key, value FROM index_meta")
            }
        docs = 0
        latest_ts: int | None = None
        by_source: dict[str, dict[str, int | None]] = {}
        by_role: dict[str, int] = {}
        if "docs" in tables:
            docs = con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
            latest_ts = con.execute(
                "SELECT MAX(ts) FROM docs"
            ).fetchone()[0]
            by_source = {
                r["source"]: {"docs": r["n"], "latest_ts": r["mx"]}
                for r in con.execute(
                    "SELECT source, COUNT(*) AS n, MAX(ts) AS mx "
                    "FROM docs GROUP BY source ORDER BY n DESC"
                )
            }
            by_role = {
                r["role"]: r["n"]
                for r in con.execute(
                    "SELECT role, COUNT(*) AS n FROM docs "
                    "GROUP BY role ORDER BY n DESC"
                )
            }
    finally:
        con.close()
    schema_raw = meta.get("schema_version")
    return IndexHealth(
        index_path=path,
        schema_version=int(schema_raw) if schema_raw is not None else None,
        docs=docs,
        latest_doc_ts=latest_ts,
        by_source=by_source,
        by_role=by_role,
        watermarks=_watermarks(meta),
        run={
            key: int(meta[f"run:{key}"])
            for key in _RUN_KEYS
            if meta.get(f"run:{key}") is not None
        },
        totals={
            key: int(meta[f"total:{key}"])
            for key in _TOTAL_KEYS
            if meta.get(f"total:{key}") is not None
        },
    )


def _iso(ts_ms: int | None) -> str:
    if not ts_ms:
        return "-"
    return (
        datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _wm_value(value: int | str) -> str:
    # Digest watermarks (vscode-chat) are shown as a prefix — they
    # identify state, they are not meant to be re-verified here.
    if isinstance(value, str) and len(value) > 16:
        return f"{value[:16]}…"
    return str(value)


def render_text(h: IndexHealth) -> str:
    lines = [
        f"index: {h.index_path}"
        + (
            f" (schema v{h.schema_version})"
            if h.schema_version is not None
            else " (uninitialized — no schema version)"
        ),
        f"documents: {h.docs} across {len(h.by_source)} source(s)",
    ]
    for source, info in h.by_source.items():
        lines.append(
            f"  {source:<14} {info['docs']:>5} docs  "
            f"latest {_iso(info['latest_ts'])}"
        )
    if h.by_role:
        roles = " · ".join(f"{role} {n}" for role, n in h.by_role.items())
        lines.append(f"roles: {roles}")
    lines.append("watermarks:")
    if not h.watermarks:
        lines.append("  (none recorded yet)")
    for source, wm in h.watermarks.items():
        if source == SOURCE_DEVIN_CLI:
            parts = " ".join(
                f"{k}={_wm_value(v)}" for k, v in wm.items()
            )
            lines.append(f"  {source}: {parts}")
        else:
            lines.append(
                f"  {source}: {wm['files']} file(s) tracked"
                + (
                    " (positions capped)"
                    if wm.get("positions_capped")
                    else ""
                )
            )
    lines.append("reconciliation:")
    if h.run:
        lines.append(
            f"  last run {_iso(h.run.get('last_run_at'))}: "
            f"indexed {h.run.get('last_indexed', 0)}, "
            f"removed {h.run.get('last_removed', 0)}, "
            f"swept {h.run.get('last_swept', 0)}, "
            f"skipped {h.run.get('last_skipped', 0)}"
        )
    else:
        lines.append("  last run: (no runs recorded)")
    if h.totals:
        lines.append(
            f"  totals: indexed {h.totals.get('indexed', 0)}, "
            f"removed {h.totals.get('removed', 0)}, "
            f"swept {h.totals.get('swept', 0)}"
        )
    return "\n".join(lines)
