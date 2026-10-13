"""Persistent knowledge-graph store: a local SQLite ``graph.db``.

Layout: ``nodes`` / ``edges`` keyed on composite ids (``"kind:key"``), an
``extracted_sessions`` ledger for incremental re-extract, and a ``meta`` table
recording which source DB the graph was built from.

Incremental rule: a session is re-extracted only when its
``last_activity_at`` marker changed (or it is new); sessions deleted from the
source are removed; shared nodes (project/file/tool) left with no incident
edges are pruned. Nothing is ever written to the source store — the caller
hands in a read-only ``SessionsStore``.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from devin_graph import identity
from devin_graph.extract import Extraction, extract_session

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  id    TEXT PRIMARY KEY,
  kind  TEXT NOT NULL,
  key   TEXT NOT NULL,
  owner TEXT,
  attrs TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE TABLE IF NOT EXISTS edges (
  kind  TEXT NOT NULL,
  src   TEXT NOT NULL,
  dst   TEXT NOT NULL,
  owner TEXT,
  attrs TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (kind, src, dst)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS extracted_sessions (
  session_id       TEXT PRIMARY KEY,
  last_activity_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

# Node kinds owned by a single session (safe to delete on re-extract).
_OWNED_KINDS = ("session", "tool_call")


@dataclass(frozen=True)
class StoredNode:
    id: str  # "kind:key"
    kind: str
    key: str
    attrs: dict


@dataclass(frozen=True)
class StoredEdge:
    kind: str
    src: str  # "kind:key"
    dst: str
    attrs: dict
    owner: str | None = None  # session_id that produced the edge


@dataclass
class BuildResult:
    extracted: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped: int = 0


def _node_id(kind: str, key: str) -> str:
    return f"{kind}:{key}"


class GraphStore:
    """Read/write handle on a ``graph.db`` file (created on demand).

    ``readonly=True`` opens the file through SQLite's ``mode=ro`` URI and
    skips both the mkdir and the schema DDL — a query must never create,
    repair or otherwise mutate the store it reads; an existing db with an
    incomplete schema surfaces SQL errors instead of being silently
    upgraded.
    """

    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self.path = Path(path).expanduser()
        if readonly:
            self._con = sqlite3.connect(
                f"file:{self.path}?mode=ro", uri=True)
            self._con.execute("PRAGMA query_only = ON")
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._con = sqlite3.connect(self.path)
            self._con.executescript(_SCHEMA)
        self._con.row_factory = sqlite3.Row

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._con.close()

    def __enter__(self) -> GraphStore:  # noqa: PYI034 - Self needs py3.11, floor is 3.10
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- read API ------------------------------------------------------------

    def nodes(self, kind: str | None = None) -> list[StoredNode]:
        sql = "SELECT id, kind, key, attrs FROM nodes"
        args: tuple = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            args = (kind,)
        return [
            StoredNode(r["id"], r["kind"], r["key"], json.loads(r["attrs"]))
            for r in self._con.execute(sql + " ORDER BY id", args)
        ]

    def edges(self, kind: str | None = None) -> list[StoredEdge]:
        sql = "SELECT kind, src, dst, owner, attrs FROM edges"
        args: tuple = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            args = (kind,)
        return [
            StoredEdge(r["kind"], r["src"], r["dst"],
                       json.loads(r["attrs"]), r["owner"])
            for r in self._con.execute(sql + " ORDER BY kind, src, dst", args)
        ]

    def node(self, node_id: str) -> StoredNode | None:
        row = self._con.execute(
            "SELECT id, kind, key, attrs FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
        if row is None:
            return None
        return StoredNode(row["id"], row["kind"], row["key"],
                          json.loads(row["attrs"]))

    def out_edges(self, node_id: str) -> list[StoredEdge]:
        return [
            StoredEdge(r["kind"], r["src"], r["dst"],
                       json.loads(r["attrs"]), r["owner"])
            for r in self._con.execute(
                "SELECT kind, src, dst, owner, attrs FROM edges WHERE src = ?"
                " ORDER BY kind, dst",
                (node_id,),
            )
        ]

    def in_edges(self, node_id: str) -> list[StoredEdge]:
        return [
            StoredEdge(r["kind"], r["src"], r["dst"],
                       json.loads(r["attrs"]), r["owner"])
            for r in self._con.execute(
                "SELECT kind, src, dst, owner, attrs FROM edges WHERE dst = ?"
                " ORDER BY kind, src",
                (node_id,),
            )
        ]

    # -- build ---------------------------------------------------------------

    def build(self, sessions_store,
              extras: list[tuple[str, Extraction]] | None = None
              ) -> BuildResult:
        """Incrementally (re)extract every session in ``sessions_store``.

        ``extras`` are ``(owner, Extraction)`` pairs merged under their own
        owner key (e.g. ``"vscdb"`` for GUI coverage) so rebuilds replace
        them atomically without touching session-owned rows.
        """
        result = BuildResult()
        seen = {
            r["session_id"]: r["last_activity_at"]
            for r in self._con.execute(
                "SELECT session_id, last_activity_at FROM extracted_sessions")
        }
        current_ids = set()
        sessions = sessions_store.sessions()
        with self._con:
            for s in sessions:
                current_ids.add(s.id)
                if seen.get(s.id) == s.last_activity_at:
                    result.skipped += 1
                    continue
                self._delete_owned(s.id)
                self._insert(extract_session(
                    s, sessions_store.tool_call_state(s.id)), owner=s.id)
                self._con.execute(
                    "INSERT OR REPLACE INTO extracted_sessions"
                    " (session_id, last_activity_at) VALUES (?, ?)",
                    (s.id, s.last_activity_at),
                )
                result.extracted.append(s.id)

            for sid in sorted(set(seen) - current_ids):
                self._delete_owned(sid)
                self._con.execute(
                    "DELETE FROM extracted_sessions WHERE session_id = ?",
                    (sid,))
                result.removed.append(sid)

            for owner, ex in extras or []:
                self._delete_owned(owner)
                self._insert(ex, owner=owner)
            self._prune_orphans()
            prov = identity.provenance()
            for key, value in (
                ("source_db", str(sessions_store.path)),
                ("schema_version",
                 str(sessions_store.schema_info["schema_version"])),
                ("machine_id", prov["machine_id"]),
                ("profile", prov["profile"]),
            ):
                self._con.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                    (key, value))
        result.extracted.sort()
        return result

    def _delete_owned(self, session_id: str) -> None:
        self._con.execute("DELETE FROM edges WHERE owner = ?", (session_id,))
        self._con.execute("DELETE FROM nodes WHERE owner = ?", (session_id,))

    def _insert(self, ex: Extraction, owner: str) -> None:
        for n in ex.merged().nodes:
            node_owner = owner if n.kind in _OWNED_KINDS else None
            self._con.execute(
                "INSERT OR REPLACE INTO nodes(id, kind, key, owner, attrs)"
                " VALUES (?, ?, ?, ?, ?)",
                (_node_id(n.kind, n.key), n.kind, n.key, node_owner,
                 json.dumps(n.attrs, sort_keys=True)),
            )
        for e in ex.edges:
            self._con.execute(
                "INSERT OR REPLACE INTO edges(kind, src, dst, owner, attrs)"
                " VALUES (?, ?, ?, ?, ?)",
                (e.kind, _node_id(*e.src), _node_id(*e.dst), owner,
                 json.dumps(e.attrs, sort_keys=True)),
            )

    def _prune_orphans(self) -> None:
        """Drop shared nodes (no owner) left with no incident edges."""
        self._con.execute(
            "DELETE FROM nodes WHERE owner IS NULL"
            " AND id NOT IN (SELECT src FROM edges)"
            " AND id NOT IN (SELECT dst FROM edges)"
        )

    # -- export --------------------------------------------------------------

    def export(self) -> dict:
        """Deterministic D3-friendly dump: ``{meta, nodes, edges}``."""
        meta = {
            r["key"]: r["value"]
            for r in self._con.execute("SELECT key, value FROM meta")
        }
        schema_version = meta.get("schema_version")
        if schema_version is not None:
            meta["schema_version"] = int(schema_version)
        return {
            "meta": meta,
            "nodes": [
                {"id": n.id, "kind": n.kind, "key": n.key, **n.attrs}
                for n in self.nodes()
            ],
            "edges": [
                {"kind": e.kind, "source": e.src, "target": e.dst, **e.attrs}
                for e in self.edges()
            ],
        }
