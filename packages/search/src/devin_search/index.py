"""Build and incrementally update the local FTS5 search index.

The index (``search.db``) is the only file devin-search writes; every Devin
store is opened read-only through ``devin_internals.parsers``. Documents are
stored in a single FTS5 table with the searchable text indexed and the
metadata columns ``UNINDEXED`` (filterable but not tokenized).

Incrementality is watermark-based: each source table/file keeps the maximum
row id already indexed in ``index_meta``, so re-runs only scan new rows. If
a source shrinks below its watermark (rows pruned upstream) the source's
documents are dropped and rebuilt from scratch — watermarks cannot detect
mid-table deletions otherwise.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devin_internals.parsers import AcpMessagesStore, SessionsStore

from devin_search.extract import extract_chat_message, extract_payload

INDEX_SCHEMA_VERSION = 2

_INDEX_DDL = """
CREATE TABLE IF NOT EXISTS index_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
  text,
  session_id UNINDEXED,
  role UNINDEXED,
  ts UNINDEXED,
  project UNINDEXED,
  session_title UNINDEXED,
  source UNINDEXED,
  ref UNINDEXED,
  tokenize = 'unicode61'
);
"""

WM_NODES = "wm:sessions:message_nodes"
WM_PROMPTS = "wm:sessions:prompt_history"
SOURCE_DEVIN_CLI = "devin-cli"
SOURCE_DEVIN_CHAT = "devin-chat"


@dataclass
class IndexStats:
    """What one :func:`build_index` run did."""

    index_path: Path
    message_nodes: int = 0
    prompt_history: int = 0
    tool_calls: int = 0
    devin_chat_messages: int = 0
    removed: int = 0
    rebuilt: bool = False
    sources: list[str] = field(default_factory=list)

    @property
    def indexed(self) -> int:
        return (
            self.message_nodes
            + self.prompt_history
            + self.tool_calls
            + self.devin_chat_messages
        )


def open_index(path: str | Path, *, rebuild: bool = False) -> sqlite3.Connection:
    """Open (creating if needed) the index DB and ensure the schema."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    if rebuild:
        con.executescript(
            "DROP TABLE IF EXISTS docs; DROP TABLE IF EXISTS index_meta;"
        )
    con.executescript(_INDEX_DDL)
    version = _meta_get(con, "schema_version")
    if version is None:
        _meta_set(con, "schema_version", str(INDEX_SCHEMA_VERSION))
    elif int(version) != INDEX_SCHEMA_VERSION:
        # Index format drifted — rebuild rather than misread our own store.
        con.close()
        return open_index(path, rebuild=True)
    con.commit()
    return con


def _meta_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute(
        "SELECT value FROM index_meta WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def _meta_set(con: sqlite3.Connection, key: str, value: Any) -> None:
    con.execute(
        "INSERT OR REPLACE INTO index_meta(key, value) VALUES (?, ?)",
        (key, str(value)),
    )


def _insert_doc(
    con: sqlite3.Connection,
    *,
    text: str,
    session_id: str,
    role: str,
    ts: int,
    project: str,
    session_title: str | None,
    source: str,
    ref: str,
) -> None:
    con.execute(
        "INSERT INTO docs(text, session_id, role, ts, project, session_title,"
        " source, ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (text, session_id, role, ts, project, session_title, source, ref),
    )


def _reset_source(con: sqlite3.Connection, source: str, wm_keys: Iterable[str]) -> int:
    """Drop a source's docs + watermarks; returns rows removed."""
    cur = con.execute("DELETE FROM docs WHERE source = ?", (source,))
    for key in wm_keys:
        con.execute("DELETE FROM index_meta WHERE key = ?", (key,))
    return cur.rowcount


def _index_sessions_db(
    con: sqlite3.Connection, db_path: Path, stats: IndexStats
) -> dict[str, tuple[str, str | None, int]]:
    """Index message_nodes / prompt_history / tool_call_state.

    Returns ``session_id -> (project, title, created_at)`` so other sources
    (devin-chat) can resolve project names.
    """
    stats.sources.append(SOURCE_DEVIN_CLI)
    with SessionsStore(db_path) as store:
        sessions = {
            s.id: (s.working_directory, s.title, s.created_at)
            for s in store.sessions()
        }
        nodes = store.message_nodes()
        prompts = store.prompt_history()
        tool_calls = store.tool_call_state()

    # Drift guard: a shrunk source invalidates its watermark.
    max_node = max((n.row_id for n in nodes), default=0)
    max_prompt = max((p.id for p in prompts), default=0)
    wm_nodes = int(_meta_get(con, WM_NODES) or 0)
    wm_prompts = int(_meta_get(con, WM_PROMPTS) or 0)
    if (wm_nodes and max_node < wm_nodes) or (
        wm_prompts and max_prompt < wm_prompts
    ):
        stats.removed += _reset_source(
            con, SOURCE_DEVIN_CLI, (WM_NODES, WM_PROMPTS)
        )
        wm_nodes = wm_prompts = 0

    for node in nodes:
        if node.row_id <= wm_nodes:
            continue
        extracted = extract_chat_message(node.chat_message)
        if extracted is None or not extracted[1].strip():
            continue
        role, text = extracted
        project, title, _ = sessions.get(node.session_id, ("", None, 0))
        _insert_doc(
            con,
            text=text,
            session_id=node.session_id,
            role=role,
            ts=node.created_at,
            project=project,
            session_title=title,
            source=SOURCE_DEVIN_CLI,
            ref=f"node:{node.row_id}",
        )
        stats.message_nodes += 1
    _meta_set(con, WM_NODES, max(wm_nodes, max_node))

    for p in prompts:
        if p.id <= wm_prompts or not p.content.strip():
            continue
        project, title, _ = sessions.get(p.session_id, ("", None, 0))
        _insert_doc(
            con,
            text=p.content,
            session_id=p.session_id,
            role="shell" if p.is_shell else "user",
            ts=p.timestamp,
            project=project,
            session_title=title,
            source=SOURCE_DEVIN_CLI,
            ref=f"prompt:{p.id}",
        )
        stats.prompt_history += 1
    _meta_set(con, WM_PROMPTS, max(wm_prompts, max_prompt))

    # tool_call_state has no auto-increment id — dedup on the ref instead.
    indexed_refs = {
        r["ref"]
        for r in con.execute(
            "SELECT ref FROM docs WHERE source = ? AND ref LIKE 'tool:%'",
            (SOURCE_DEVIN_CLI,),
        )
    }
    for tc in tool_calls:
        ref = f"tool:{tc.session_id}/{tc.tool_call_id}"
        if ref in indexed_refs:
            continue
        parts = []
        for raw in (tc.tool_call_json, tc.tool_call_update_json):
            extracted = extract_payload(raw, kind="tool_call")
            if extracted is not None and extracted[1].strip():
                parts.append(extracted[1])
        if not parts:
            continue
        project, title, created = sessions.get(tc.session_id, ("", None, 0))
        _insert_doc(
            con,
            text="\n".join(parts),
            session_id=tc.session_id,
            role="tool",
            ts=created,
            project=project,
            session_title=title,
            source=SOURCE_DEVIN_CLI,
            ref=ref,
        )
        stats.tool_calls += 1
    return sessions


def _acp_meta(meta: dict[str, str], needle: str) -> str | None:
    for key, value in meta.items():
        if needle in key:
            return value
    return None


def _index_acp_dir(
    con: sqlite3.Connection,
    acp_dir: Path,
    stats: IndexStats,
    sessions: dict[str, tuple[str, str | None, int]],
) -> None:
    stats.sources.append(SOURCE_DEVIN_CHAT)
    files = sorted(acp_dir.glob("*.db"))
    present = {f.name for f in files}

    # Files that vanished upstream get their docs removed.
    known = {
        r["value"]
        for r in con.execute(
            "SELECT value FROM index_meta WHERE key LIKE 'devin-chat:file:%'"
        )
    }
    for gone in sorted(known - present):
        cur = con.execute(
            "DELETE FROM docs WHERE source = ? AND ref LIKE ?",
            (SOURCE_DEVIN_CHAT, f"devin-chat:{gone}:%"),
        )
        stats.removed += cur.rowcount
        for key in (f"devin-chat:file:{gone}", f"devin-chat:wm:{gone}"):
            con.execute("DELETE FROM index_meta WHERE key = ?", (key,))

    for path in files:
        wm_key = f"devin-chat:wm:{path.name}"
        # positions start at 0 — an absent watermark must be -1
        raw_wm = _meta_get(con, wm_key)
        wm = int(raw_wm) if raw_wm is not None else -1
        try:
            with AcpMessagesStore(path) as store:
                meta = store.meta()
                messages = store.messages()
        except Exception:  # noqa: BLE001, S112
            # Not an acp-messages db (or unreadable) — skip it.
            continue
        if wm and not any(m.position > wm for m in messages):
            _meta_set(con, f"devin-chat:file:{path.name}", path.name)
            continue

        sid = _acp_meta(meta, "session_id") or path.stem
        try:
            base_ts = int(_acp_meta(meta, "created") or 0)
        except ValueError:
            base_ts = 0
        project, title, created = sessions.get(sid, ("", None, 0))
        if not base_ts:
            base_ts = created

        for m in messages:
            if m.position <= wm:
                continue
            extracted = extract_payload(m.payload, kind=m.kind)
            if extracted is None or not extracted[1].strip():
                continue
            role, text = extracted
            _insert_doc(
                con,
                text=text,
                session_id=sid,
                role=role,
                ts=base_ts + m.position,
                project=project,
                session_title=title,
                source=SOURCE_DEVIN_CHAT,
                ref=f"devin-chat:{path.name}:{m.position}",
            )
            stats.devin_chat_messages += 1
        if messages:
            _meta_set(con, wm_key, max(m.position for m in messages))
        _meta_set(con, f"devin-chat:file:{path.name}", path.name)


def build_index(
    index_path: str | Path,
    *,
    sessions_db: str | Path | None = None,
    acp_dir: str | Path | None = None,
    rebuild: bool = False,
) -> IndexStats:
    """Index the given stores into ``index_path`` (incrementally)."""
    index_path = Path(index_path).expanduser()
    stats = IndexStats(index_path=index_path, rebuilt=bool(rebuild))
    con = open_index(index_path, rebuild=rebuild)
    try:
        sessions: dict[str, tuple[str, str | None, int]] = {}
        if sessions_db is not None:
            sessions = _index_sessions_db(con, Path(sessions_db), stats)
        if acp_dir is not None and Path(acp_dir).is_dir():
            _index_acp_dir(con, Path(acp_dir), stats, sessions)
        con.commit()
    finally:
        con.close()
    return stats


def index_doc_count(index_path: str | Path) -> int:
    """Total documents currently in the index."""
    con = open_index(index_path)
    try:
        return con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    finally:
        con.close()
