"""Ranked full-text search over the index built by :mod:`devin_search.index`.

Queries go through FTS5's ``bm25()`` ranking; ``snippet()`` produces the
highlighted context shown in results. User input is escaped to an implicit
AND of quoted tokens — it is never interpolated raw into ``MATCH``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .index import INDEX_SCHEMA_VERSION

_SNIP_OPEN = "«"
_SNIP_CLOSE = "»"
_SNIP_ELLIPSIS = "…"
_SNIP_TOKENS = 24


class StaleIndexError(RuntimeError):
    """The index exists but was built by an older index format."""


@dataclass(frozen=True)
class Hit:
    session_id: str
    role: str
    ts: int
    project: str
    session_title: str | None
    source: str
    ref: str
    snippet: str
    rank: float
    # Indexed tool name for tool-call docs (the ``query --tool`` filter's
    # ground truth); ``None`` for docs that carry no tool name.
    tool: str | None = None
    # Optional: path of the devin-history export note for this session
    # (filled by devin_search.history.attach_history_notes).
    history_note: str | None = None


def to_fts_query(term: str) -> str:
    """Escape a free-text term into a safe FTS5 query.

    Each whitespace-separated token becomes a quoted phrase; FTS5 ANDs them,
    so ``kubectl delete`` matches docs containing both words anywhere —
    punctuation and FTS5 operators in the input are neutralized.
    """
    tokens = [t for t in term.split() if t.strip()]
    quoted = ['"' + t.replace('"', '""') + '"' for t in tokens]
    return " ".join(quoted) if quoted else '""'


def search(
    index_path: str | Path,
    term: str,
    *,
    role: str | None = None,
    project: str | None = None,
    since: int | None = None,
    tool: str | None = None,
    limit: int = 20,
) -> list[Hit]:
    """Search ``index_path`` for ``term``; returns BM25-ranked hits.

    ``role`` is an exact match on the normalized role; ``project`` is a
    substring match on the session working directory; ``since`` keeps hits
    with ``ts >= since`` (epoch milliseconds); ``tool`` is an exact match
    on the tool name indexed from tool-call payloads.
    """
    index_path = Path(index_path).expanduser()
    if not index_path.exists():
        raise FileNotFoundError(
            f"{index_path}: no index — run `devin-search index` first"
        )
    con = sqlite3.connect(index_path)
    try:
        row = con.execute(
            "SELECT value FROM index_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.Error:
        row = None
    version = int(row[0]) if row else 0
    if version != INDEX_SCHEMA_VERSION:
        con.close()
        raise StaleIndexError(
            f"{index_path}: index schema v{version} is stale "
            f"(need v{INDEX_SCHEMA_VERSION}) — run `devin-search index` to rebuild"
        )
    con.row_factory = sqlite3.Row
    try:
        sql = (
            "SELECT session_id, role, ts, project, session_title, source, ref,"
            " tool,"
            f" snippet(docs, 0, '{_SNIP_OPEN}', '{_SNIP_CLOSE}',"
            f" '{_SNIP_ELLIPSIS}', {_SNIP_TOKENS}) AS snip,"
            " bm25(docs) AS rank"
            " FROM docs WHERE docs MATCH ?"
        )
        args: list = [to_fts_query(term)]
        if role is not None:
            sql += " AND role = ?"
            args.append(role)
        if project is not None:
            sql += " AND project LIKE ?"
            args.append(f"%{project}%")
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        if tool is not None:
            sql += " AND tool = ?"
            args.append(tool)
        sql += " ORDER BY rank LIMIT ?"
        args.append(limit)
        return [
            Hit(
                session_id=r["session_id"],
                role=r["role"],
                ts=r["ts"],
                project=r["project"],
                session_title=r["session_title"],
                source=r["source"],
                ref=r["ref"],
                snippet=r["snip"],
                rank=r["rank"],
                tool=r["tool"],
            )
            for r in con.execute(sql, args)
        ]
    finally:
        con.close()


def parse_since(value: str) -> int:
    """``--since`` value → epoch ms. Accepts ``YYYY-MM-DD`` or epoch ms."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    from datetime import datetime, timezone

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)
