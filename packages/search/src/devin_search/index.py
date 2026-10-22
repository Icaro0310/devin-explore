"""Build and incrementally update the local FTS5 search index.

The index (``search.db``) is the only file devin-search writes; the Devin
stores are opened read-only through ``devin_internals.parsers``, and the
non-Devin surfaces (copilot-cli, codex-cli, vscode-chat —
SESSION-SURFACES.md §6.3–§6.5) are read-only per the shared read
contract. Documents are stored in a single FTS5 table with the
searchable text indexed and the metadata columns ``UNINDEXED``
(filterable but not tokenized).

Incrementality is watermark-based: each source table/file keeps the maximum
row id already indexed in ``index_meta``, so re-runs only scan new rows. If
a source shrinks below its watermark (rows pruned upstream) the source's
documents are dropped and rebuilt from scratch — watermarks cannot detect
mid-table deletions otherwise.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from devin_internals.parsers import AcpMessagesStore, SessionsStore

from devin_search.extract import (
    extract_chat_message,
    extract_payload,
    extract_strings,
    extract_tool_name,
    normalize_role,
    parse_payload,
)

INDEX_SCHEMA_VERSION = 4

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
  tool UNINDEXED,
  tokenize = 'unicode61'
);
"""

WM_NODES = "wm:devin-cli:message_nodes"
WM_PROMPTS = "wm:devin-cli:prompt_history"
WM_CONTENT = "wm:devin-cli:content"
SOURCE_DEVIN_CLI = "devin-cli"
SOURCE_DEVIN_CHAT = "devin-chat"
SOURCE_COPILOT = "copilot-cli"
SOURCE_CODEX = "codex-cli"
SOURCE_VSCODE_CHAT = "vscode-chat"


@dataclass
class IndexStats:
    """What one :func:`build_index` run did."""

    index_path: Path
    message_nodes: int = 0
    prompt_history: int = 0
    tool_calls: int = 0
    devin_chat_messages: int = 0
    copilot_messages: int = 0
    codex_messages: int = 0
    vscode_chat_messages: int = 0
    removed: int = 0
    rebuilt: bool = False
    skipped_records: int = 0
    sources: list[str] = field(default_factory=list)

    @property
    def indexed(self) -> int:
        return (
            self.message_nodes
            + self.prompt_history
            + self.tool_calls
            + self.devin_chat_messages
            + self.copilot_messages
            + self.codex_messages
            + self.vscode_chat_messages
        )


def open_index(path: str | Path, *, rebuild: bool = False) -> sqlite3.Connection:
    """Open (creating if needed) the index DB and ensure the schema.

    Returns ``(connection, rebuilt)`` — ``rebuilt`` is true when the index
    was rebuilt, either by explicit request or automatically after a
    schema-version mismatch, so callers can report it honestly."""
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
        con, _ = open_index(path, rebuild=True)
        return con, True
    con.commit()
    return con, rebuild


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
    tool: str | None = None,
) -> None:
    con.execute(
        "INSERT INTO docs(text, session_id, role, ts, project, session_title,"
        " source, ref, tool) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            text,
            session_id,
            role,
            ts,
            project,
            session_title,
            source,
            ref,
            tool,
        ),
    )


def _reset_source(con: sqlite3.Connection, source: str, wm_keys: Iterable[str]) -> int:
    """Drop a source's docs + watermarks; returns rows removed."""
    cur = con.execute("DELETE FROM docs WHERE source = ?", (source,))
    for key in wm_keys:
        con.execute("DELETE FROM index_meta WHERE key = ?", (key,))
    return cur.rowcount


def _devin_cli_content_digest(
    sessions: dict[str, tuple[str, str | None, int]],
    nodes,
    prompts,
    tool_calls,
    wm_nodes: int,
    wm_prompts: int,
    indexed_tool_refs: set[str],
) -> str:
    """Content identity of the rows already covered by the watermarks.

    Row ids are stable positions, not immutability guarantees: a same-id
    UPDATE (or a mid-table DELETE) leaves the position watermark
    untouched, so stale docs would survive forever without a content
    signal. Only the covered prefix is hashed — new rows past the
    watermark take the cheap incremental path, unchanged rows never
    re-index."""
    h = hashlib.sha256()
    covered_sessions: set[str] = set()
    for n in sorted(nodes, key=lambda x: x.row_id):
        if n.row_id > wm_nodes:
            continue
        covered_sessions.add(n.session_id)
        h.update(
            f"n\x00{n.row_id}\x00{n.session_id}\x00{n.chat_message}"
            f"\x00{n.created_at}\x00".encode()
        )
    for p in sorted(prompts, key=lambda x: x.id):
        if p.id > wm_prompts:
            continue
        covered_sessions.add(p.session_id)
        h.update(
            f"p\x00{p.id}\x00{p.content}\x00{p.session_id}"
            f"\x00{p.is_shell}\x00{p.timestamp}\x00".encode()
        )
    for tc in sorted(tool_calls, key=lambda x: (x.session_id, x.tool_call_id)):
        ref = f"tool:{tc.session_id}/{tc.tool_call_id}"
        if ref not in indexed_tool_refs:
            continue
        covered_sessions.add(tc.session_id)
        h.update(
            f"t\x00{tc.session_id}\x00{tc.tool_call_id}"
            f"\x00{tc.tool_call_json}\x00{tc.tool_call_update_json}\x00".encode()
        )
    for sid in sorted(covered_sessions):
        wd, title, created = sessions.get(sid, ("", None, 0))
        h.update(f"s\x00{sid}\x00{wd}\x00{title}\x00{created}\x00".encode())
    return h.hexdigest()


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

    # Drift guards: a source whose covered content changed under
    # unchanged watermarks — an in-place row UPDATE or mid-table DELETE —
    # or that shrank below its watermark invalidates the whole source
    # slice (the store is already fully loaded, so the digest is cheap;
    # re-indexing is the same reset the shrink path has always used).
    max_node = max((n.row_id for n in nodes), default=0)
    max_prompt = max((p.id for p in prompts), default=0)
    wm_nodes = int(_meta_get(con, WM_NODES) or 0)
    wm_prompts = int(_meta_get(con, WM_PROMPTS) or 0)
    indexed_tool_refs = {
        r["ref"]
        for r in con.execute(
            "SELECT ref FROM docs WHERE source = ? AND ref LIKE 'tool:%'",
            (SOURCE_DEVIN_CLI,),
        )
    }
    stored_digest = _meta_get(con, WM_CONTENT)
    pre_digest = _devin_cli_content_digest(
        sessions, nodes, prompts, tool_calls,
        wm_nodes, wm_prompts, indexed_tool_refs,
    )
    if stored_digest is None:
        # Index built by a pre-digest release: the watermarks say docs
        # exist but the covered content was never fingerprinted, so a
        # stale row could persist forever once the digest is recorded.
        # Reset once to rebuild from a known-good state. A fresh index
        # (watermarks still 0) has nothing covered and is exempt.
        changed = bool(wm_nodes or wm_prompts)
    else:
        changed = stored_digest != pre_digest
    shrunk = (wm_nodes and max_node < wm_nodes) or (
        wm_prompts and max_prompt < wm_prompts
    )
    if changed or shrunk:
        stats.removed += _reset_source(
            con, SOURCE_DEVIN_CLI, (WM_NODES, WM_PROMPTS, WM_CONTENT)
        )
        wm_nodes = wm_prompts = 0
        indexed_tool_refs = set()

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
            ts=_epoch_ms(node.created_at) or 0,
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
            ts=_epoch_ms(p.timestamp) or 0,
            project=project,
            session_title=title,
            source=SOURCE_DEVIN_CLI,
            ref=f"prompt:{p.id}",
        )
        stats.prompt_history += 1
    _meta_set(con, WM_PROMPTS, max(wm_prompts, max_prompt))

    # tool_call_state has no auto-increment id — dedup on the ref instead.
    # Only refs that actually received a doc go into `covered_tool_refs`:
    # hashing skipped calls (no extractable text → never indexed) would
    # diverge from the indexed refs every run and force a reset loop.
    inserted_tool_refs = set()
    for tc in tool_calls:
        ref = f"tool:{tc.session_id}/{tc.tool_call_id}"
        if ref in indexed_tool_refs:
            continue
        parts = []
        for raw in (tc.tool_call_json, tc.tool_call_update_json):
            extracted = extract_payload(raw, kind="tool_call")
            if extracted is not None and extracted[1].strip():
                parts.append(extracted[1])
        if not parts:
            continue
        # Merge update then call payloads (call wins on conflict) — the
        # same precedence devin-graph's extract_session uses — and lift
        # the tool name into docs.tool for `query --tool`.
        merged: dict[str, Any] = {}
        for raw in (tc.tool_call_update_json, tc.tool_call_json):
            data = parse_payload(raw)
            if data:
                merged = {**merged, **data}
        project, title, created = sessions.get(tc.session_id, ("", None, 0))
        _insert_doc(
            con,
            text="\n".join(parts),
            session_id=tc.session_id,
            role="tool",
            ts=_epoch_ms(created) or 0,
            project=project,
            session_title=title,
            source=SOURCE_DEVIN_CLI,
            ref=ref,
            tool=extract_tool_name(merged) if merged else None,
        )
        inserted_tool_refs.add(ref)
        stats.tool_calls += 1

    # Record the digest over everything now covered — computed at the
    # *new* watermarks so a run that both reset and picked up fresh rows
    # does not trigger a second spurious reset next time.
    covered_tool_refs = indexed_tool_refs | inserted_tool_refs
    _meta_set(
        con,
        WM_CONTENT,
        _devin_cli_content_digest(
            sessions,
            nodes,
            prompts,
            tool_calls,
            max(wm_nodes, max_node),
            max(wm_prompts, max_prompt),
            covered_tool_refs,
        ),
    )
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


# ---------------------------------------------------------------------------
# Non-Devin surfaces — copilot-cli, codex-cli, vscode-chat
# (devin-internals-spec → docs/SESSION-SURFACES.md §6.3–§6.5).
#
# Shared conventions: one presence marker ``<surface>:file:<path>`` and one
# watermark ``<surface>:wm:<path>`` per source file (the path component is the
# absolute file path — a surface's files live under several roots, so bare
# basenames are not unique). A file that vanishes from the scan loses its
# docs, marker and watermark on the next run, same as devin-chat:file:*.
# ---------------------------------------------------------------------------


def _connect_readonly(path: Path) -> sqlite3.Connection:
    """Open a source SQLite db per spec §3: ``mode=ro`` retried with
    ``immutable=1``; a constructed-but-failed handle is closed first."""
    exc: sqlite3.Error | None = None
    for query in ("mode=ro", "mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?{query}", uri=True)
        except sqlite3.Error as error:
            exc = error
            continue
        try:
            con.row_factory = sqlite3.Row
            con.execute("SELECT 1").fetchone()  # force the lazy open
            return con
        except sqlite3.Error as error:
            exc = error
            con.close()
    raise exc if exc is not None else sqlite3.Error(f"{path}: unreadable")


def _epoch_ms(value: Any) -> int | None:
    """Normalize a source timestamp to epoch ms (``None`` when absent).

    Numbers use the normative ``< 1e12`` discriminator (SESSION-SURFACES
    §6.1): below it the unit is epoch *seconds* (×1000), at/above it is
    already milliseconds. Observed units: copilot ``timestamp`` and
    vscode-chat ``creationDate``/``requests[].timestamp`` are ms (or ISO
    strings); codex ``threads.*_at`` exist as seconds and ms in the wild
    and rollout ``timestamp`` is ISO — the discriminator covers all of
    them. Strings are parsed as ISO 8601.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value <= 0:
            return None
        return int(value * 1000) if value < 1e12 else int(value)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            dt = datetime.fromisoformat(
                text[:-1] + "+00:00" if text.endswith("Z") else text
            )
        except ValueError:
            try:
                return _epoch_ms(float(text))
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    return None


def _read_lines(path: Path) -> list[str] | None:
    """Read a JSONL file's lines; ``None`` when unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _drop_source_file_docs(
    con: sqlite3.Connection, source: str, file_key: str
) -> int:
    """Delete one source file's docs via an exact ``ref`` prefix match.

    ``substr`` (not ``LIKE``) is used deliberately: file keys are real
    paths and may contain ``%``/``_`` LIKE metacharacters."""
    prefix = f"{source}:{file_key}:"
    cur = con.execute(
        "DELETE FROM docs WHERE source = ? AND substr(ref, 1, ?) = ?",
        (source, len(prefix), prefix),
    )
    return cur.rowcount


def _drop_vanished_files(
    con: sqlite3.Connection,
    source: str,
    present: set[str],
    stats: IndexStats,
) -> None:
    """Drop docs/markers of files that disappeared since the last run."""
    known = {
        r["value"]
        for r in con.execute(
            "SELECT value FROM index_meta WHERE key LIKE ?",
            (f"{source}:file:%",),
        )
    }
    for gone in sorted(known - present):
        stats.removed += _drop_source_file_docs(con, source, gone)
        con.execute(
            "DELETE FROM index_meta WHERE key IN (?, ?, ?)",
            (
                f"{source}:file:{gone}",
                f"{source}:wm:{gone}",
                f"{source}:head:{gone}",
            ),
        )


def _yaml_scalars(text: str) -> dict[str, str]:
    """Top-level ``key: value`` scalars of a flat YAML file.

    Nested blocks, lists and comments are ignored — copilot's
    ``workspace.yaml`` is flat by contract (spec §6.3)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):[ \t]*(.*)$", line)
        if not m:
            continue
        value = m.group(2).strip()
        if not value or value in ("|", ">", "~", "null"):
            continue
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            try:
                value = str(json.loads(value))
            except (json.JSONDecodeError, RecursionError):
                value = value[1:-1]
        elif value.startswith("'") and value.endswith("'") and len(value) >= 2:
            value = value[1:-1].replace("''", "'")
        else:
            value = re.sub(r"\s+#.*$", "", value).strip()
        if value:
            out[m.group(1)] = value
    return out


def _normalize_ws_seed(path: str) -> str:
    """Workspace seed normalization (spec §3): trim, strip trailing
    separators — the same treatment the browser applies before hashing."""
    seed = path.strip()
    while len(seed) > 1 and seed.endswith(("/", "\\")):
        seed = seed[:-1]
    return seed


# --- copilot-cli (§6.3) ------------------------------------------------------

_COPILOT_STATE_DIRS = ("session-state", "history-session-state")
_COPILOT_MESSAGE_ROLES = {
    "user.message": "user",
    "assistant.message": "assistant",
    "system.message": "system",
}
# Structural keys inside a message event's ``data`` that are metadata or
# opaque wire blobs (``reasoningOpaque``/``encryptedContent`` are
# ciphertext, not text) — skipped so ids/timestamps/models don't pollute
# the index. Real stores keep the readable text in ``content`` /
# ``reasoningText`` / ``toolRequests``.
_COPILOT_DATA_SKIP = frozenset(
    {
        "id",
        "parentId",
        "messageId",
        "originatingMessageId",
        "interactionId",
        "turnId",
        "apiCallId",
        "toolCallId",
        "callId",
        "sessionId",
        "timestamp",
        "model",
        "producer",
        "type",
        "rte",
        "reasoningOpaque",
        "encryptedContent",
        "reasoningBlocks",
        "signature",
    }
)


def _copilot_files(roots: Iterable[Path]) -> list[tuple[Path, Path]]:
    """``(root, events-file)`` pairs across the copilot roots, in scan
    order.

    Layouts coexist (§6.3): ``<state>/<uuid>/events.jsonl`` (primary) and
    ``<state>/<uuid>.jsonl`` (flat), for both ``session-state`` and the
    legacy ``history-session-state`` dir."""
    files: list[tuple[Path, Path]] = []
    seen: set[str] = set()
    for root in roots:
        for state_dir in _COPILOT_STATE_DIRS:
            base = root / state_dir
            if not base.is_dir():
                continue
            try:
                entries = sorted(base.iterdir())
            except OSError:
                continue
            for entry in entries:
                path: Path | None = None
                if entry.is_dir():
                    candidate = entry / "events.jsonl"
                    if candidate.is_file():
                        path = candidate
                elif entry.is_file() and entry.name.endswith(".jsonl"):
                    path = entry
                if path is not None and str(path) not in seen:
                    seen.add(str(path))
                    files.append((root, path))
    return files


def _copilot_event_text(data: Any) -> str:
    """Searchable text of a ``*.message`` event's ``data`` payload.

    Observed shapes carry ``content``/``text``/``message`` (string or
    typed block list); anything else falls back to a recursive string
    walk — the tolerant-payload doctrine of SPEC.md §2.5."""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("content", "text", "message", "reasoningText"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, (dict, list)):
                text = extract_strings(value, skip_keys=_COPILOT_DATA_SKIP)
                if text:
                    return text
        return extract_strings(data, skip_keys=_COPILOT_DATA_SKIP)
    return extract_strings(data)


def _index_copilot_dirs(
    con: sqlite3.Connection, roots: Iterable[Path], stats: IndexStats
) -> None:
    stats.sources.append(SOURCE_COPILOT)
    files = _copilot_files(roots)
    present = {str(path) for _, path in files}
    _drop_vanished_files(con, SOURCE_COPILOT, present, stats)

    # Spec §6.3: the same native id seen twice under one root is a silent
    # duplicate; across roots it is a duplicate only when the workspace
    # hash also matches — a different workspace under the same id is its
    # own session. In every case the first readable copy wins.
    seen_sessions: set[tuple[str, str]] = set()
    seen_native_in_root: set[tuple[str, str]] = set()
    for root, path in files:
        if path.name == "events.jsonl":
            native_id = path.parent.name
            yaml_path = path.parent / "workspace.yaml"
        else:
            native_id = path.stem
            yaml_path = None
        ws_seed = ""
        title = None
        if yaml_path is not None and yaml_path.is_file():
            try:
                ws = _yaml_scalars(
                    yaml_path.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                ws = {}
            ws_seed = ws.get("git_root") or ws.get("cwd") or ""
            # session_title only from an explicit user rename — spec §2
            # marks auto-derived titles as content, not metadata.
            if ws.get("user_named", "").lower() in ("true", "yes", "1"):
                title = ws.get("name") or None
        ws_hash = (
            hashlib.sha256(_normalize_ws_seed(ws_seed).encode()).hexdigest()[:16]
            if ws_seed
            else None
        )
        dedup_key = (native_id, ws_hash) if ws_hash else None
        root_key = (str(root), native_id)
        if root_key in seen_native_in_root or (
            dedup_key is not None and dedup_key in seen_sessions
        ):
            # A higher-priority copy already claimed this session — docs
            # indexed from *this* file (when it was the only copy) are
            # stale duplicates, and its watermark must reset so it can
            # take over cleanly if the winner ever disappears.
            loser_key = str(path)
            stats.removed += _drop_source_file_docs(
                con, SOURCE_COPILOT, loser_key
            )
            con.execute(
                "DELETE FROM index_meta WHERE key IN (?, ?)",
                (
                    f"{SOURCE_COPILOT}:wm:{loser_key}",
                    f"{SOURCE_COPILOT}:head:{loser_key}",
                ),
            )
            continue
        lines = _read_lines(path)
        if lines is None:
            continue  # unreadable — another root's copy may still index
        seen_native_in_root.add(root_key)
        if dedup_key is not None:
            seen_sessions.add(dedup_key)

        file_key = str(path)
        wm_key = f"{SOURCE_COPILOT}:wm:{file_key}"
        head_key = f"{SOURCE_COPILOT}:head:{file_key}"
        wm = int(_meta_get(con, wm_key) or 0)
        head = _meta_get(con, head_key)
        # The watermark covers lines[:wm] — digest that prefix so an
        # in-place edit (same length or longer) cannot hide behind it.
        head_now = hashlib.sha256(
            "\n".join(lines[:wm]).encode("utf-8")
        ).hexdigest()
        # head is None for files indexed by a pre-digest release — the
        # covered prefix was never fingerprinted, so re-index once rather
        # than trusting docs we cannot vouch for.
        if wm and (len(lines) < wm or head != head_now):
            # Lines below the watermark were lost or edited in place —
            # docs derived from them are stale; re-index the whole file.
            stats.removed += _drop_source_file_docs(con, SOURCE_COPILOT, file_key)
            wm = 0
        # The watermark is the position just past the last *well-formed*
        # record — never past a malformed tail. A truncated trailing line
        # stays uncovered so the writer completing it is picked up next
        # run; malformed mid-file lines are permanently behind the next
        # good record and never re-parsed.
        last_complete = wm
        for i in range(wm, len(lines)):
            line = lines[i].strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                stats.skipped_records += 1  # malformed — may complete later
                continue
            last_complete = i + 1  # well-formed record — safe to cover
            if not isinstance(event, dict):
                stats.skipped_records += 1
                continue
            etype = event.get("type")
            if not isinstance(etype, str):
                stats.skipped_records += 1  # array/object type — unhashable
                continue
            role = _COPILOT_MESSAGE_ROLES.get(etype)
            if role is None:
                continue  # non-message event type — metadata only
            text = _copilot_event_text(event.get("data"))
            if not text.strip():
                continue
            _insert_doc(
                con,
                text=text,
                session_id=native_id,
                role=role,
                ts=_epoch_ms(event.get("timestamp")) or 0,
                project=ws_seed,
                session_title=title,
                source=SOURCE_COPILOT,
                ref=f"{SOURCE_COPILOT}:{file_key}:{i}",
            )
            stats.copilot_messages += 1
        _meta_set(con, wm_key, last_complete)
        _meta_set(
            con,
            head_key,
            hashlib.sha256(
                "\n".join(lines[:last_complete]).encode("utf-8")
            ).hexdigest(),
        )
        _meta_set(con, f"{SOURCE_COPILOT}:file:{file_key}", file_key)


# --- codex-cli (§6.4) --------------------------------------------------------

_CODEX_STATE_RE = re.compile(r"^state_(\d+)\.sqlite$")
_CODEX_ROLL_OUT_DEPTH = 8
# Text-bearing ``threads`` columns. Spec §3's "metadata columns only"
# rule governs the browser's SessionMeta; for the search index these
# fields *are* the content, so they are selected and indexed here.
_CODEX_THREAD_TEXT_FIELDS = (
    "first_user_message",
    "preview",
    "title",
    "name",
    "summary",
    "description",
    "goal",
    "task",
    "prompt",
)
_CODEX_CONTENT_KEYS = (
    "message",
    "text",
    "content",
    "arguments",
    "output",
    "summary",
    "input",
    "command",
    "action",
)
_CODEX_PAYLOAD_ROLES = {
    "user_message": "user",
    "agent_message": "assistant",
    "agent_reasoning": "assistant",
    "reasoning": "assistant",
    "function_call": "tool",
    "function_call_output": "tool",
    "custom_tool_call": "tool",
    "custom_tool_call_output": "tool",
    "local_shell_call": "tool",
    "exec_command_begin": "tool",
    "exec_command_output": "tool",
}
_CODEX_PAYLOAD_SKIP = frozenset(
    {"type", "id", "call_id", "timestamp", "role", "status", "encrypted_content"}
)


def _codex_record_text(payload: dict[str, Any]) -> str:
    """Text of one ``response_item``/``event_msg`` payload — known
    content keys first, recursive walk minus structural keys last.

    Exec-style records carry *both* ``command`` and ``output``; they are
    concatenated (command first) so the invoked command stays searchable
    alongside its result — not swallowed by the first-key-wins rule."""
    parts: list[str] = []
    for key in ("command", "output"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
        elif isinstance(value, list) and all(
            isinstance(v, str) for v in value
        ):
            # argv-style command lists — one searchable line.
            text = " ".join(value).strip()
            if text:
                parts.append(text)
        elif isinstance(value, (dict, list)):
            text = extract_strings(value, skip_keys=_CODEX_PAYLOAD_SKIP)
            if text:
                parts.append(text)
    if parts:
        return "\n".join(parts)
    for key in _CODEX_CONTENT_KEYS:
        if key in ("command", "output"):
            continue  # already handled above
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, (dict, list)):
            text = extract_strings(value, skip_keys=_CODEX_PAYLOAD_SKIP)
            if text:
                return text
    return extract_strings(payload, skip_keys=_CODEX_PAYLOAD_SKIP)


def _codex_record_role(record_type: str, payload: dict[str, Any]) -> str:
    role = payload.get("role")
    if isinstance(role, str) and role:
        return normalize_role(role)
    ptype = payload.get("type")
    if isinstance(ptype, str) and ptype in _CODEX_PAYLOAD_ROLES:
        return _CODEX_PAYLOAD_ROLES[ptype]
    return normalize_role(ptype or record_type)


def _walk_rollouts(base: Path, out: list[Path], depth: int = 0) -> None:
    if depth > _CODEX_ROLL_OUT_DEPTH:
        return
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir():
            _walk_rollouts(entry, out, depth + 1)
        elif entry.is_file() and entry.name.startswith("rollout-") and (
            entry.name.endswith(".jsonl")
        ):
            out.append(entry)


def _index_codex_state_db(
    con: sqlite3.Connection, path: Path, stats: IndexStats
) -> None:
    """Index the text fields of ``threads`` rows in ``state_<N>.sqlite``.

    ``threads`` has no dependable auto-increment key (``id`` is a TEXT
    primary key) and rows are *mutable*: incrementality is ref-keyed
    upsert — ``codex-cli:<file>:<thread-id>/<field>`` re-inserts when the
    stored text changed — plus stale-ref cleanup, so a thread deleted
    upstream (or a field emptied) loses its docs next run."""
    file_key = str(path)
    try:
        db = _connect_readonly(path)
    except sqlite3.Error:
        return
    try:
        tables = {
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "threads" not in tables:
            return
        rows = db.execute("SELECT * FROM threads").fetchall()
    except sqlite3.Error:
        return
    finally:
        db.close()
    prefix = f"{SOURCE_CODEX}:{file_key}:"
    indexed = {
        r["ref"]: (r["text"], r["ts"], r["project"])
        for r in con.execute(
            "SELECT ref, text, ts, project FROM docs WHERE source = ?"
            " AND substr(ref, 1, ?) = ?",
            (SOURCE_CODEX, len(prefix), prefix),
        )
    }
    seen_refs: set[str] = set()
    for row in rows:
        keys = set(row.keys())
        tid = row["id"] if "id" in keys else None
        if not isinstance(tid, str) or not tid:
            continue
        created = (
            _epoch_ms(row["created_at"]) if "created_at" in keys else None
        ) or 0
        cwd = (
            row["cwd"]
            if "cwd" in keys and isinstance(row["cwd"], str)
            else ""
        )
        for field_name in _CODEX_THREAD_TEXT_FIELDS:
            if field_name not in keys:
                continue
            value = row[field_name]
            if not isinstance(value, str) or not value.strip():
                continue
            ref = f"{prefix}{tid}/{field_name}"
            seen_refs.add(ref)
            if indexed.get(ref) == (value, created, cwd):
                continue  # unchanged — nothing to do
            if ref in indexed:
                stats.removed += con.execute(
                    "DELETE FROM docs WHERE source = ? AND ref = ?",
                    (SOURCE_CODEX, ref),
                ).rowcount
            _insert_doc(
                con,
                text=value,
                session_id=tid,
                # These fields are auto-derived from user content
                # (spec §6.4) — "user" is the honest role for all of them.
                role="user",
                ts=created,
                project=cwd,
                session_title=None,
                source=SOURCE_CODEX,
                ref=ref,
            )
            stats.codex_messages += 1
    # Refs indexed under this file but absent from the current table —
    # a deleted thread or an emptied field. Same presence rule as the
    # file markers, applied at row granularity.
    for stale in sorted(set(indexed) - seen_refs):
        stats.removed += con.execute(
            "DELETE FROM docs WHERE source = ? AND ref = ?",
            (SOURCE_CODEX, stale),
        ).rowcount
    _meta_set(con, f"{SOURCE_CODEX}:file:{file_key}", file_key)


def _index_codex_rollout(
    con: sqlite3.Connection, path: Path, stats: IndexStats
) -> None:
    """Index ``response_item``/``event_msg`` records of one rollout file."""
    file_key = str(path)
    lines = _read_lines(path)
    if lines is None:
        return
    # session_meta records (first line by convention) carry the session
    # id, cwd and fork status — re-scanned every run so appended lines
    # still attach to the right session. Only session_meta lines are
    # parsed in this pass.
    session_id = ""
    cwd = ""
    started = 0
    forked = False
    for line in lines:
        if '"session_meta"' not in line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        if record.get("type") != "session_meta" or not isinstance(payload, dict):
            continue
        if payload.get("forked_from_id"):
            forked = True  # forks are ignored entirely (spec §6.4)
        session_id = payload.get("id") or session_id
        git = payload.get("git")
        git_root = (
            (git.get("root") or git.get("repository_root"))
            if isinstance(git, dict)
            else None
        )
        cwd = cwd or payload.get("cwd") or git_root or ""
        started = (
            _epoch_ms(payload.get("timestamp"))
            or _epoch_ms(record.get("timestamp"))
            or started
        )
    if not session_id:
        session_id = path.stem  # rollout-<ts>-<uuid> — filename identity
    wm_key = f"{SOURCE_CODEX}:wm:{file_key}"
    wm = int(_meta_get(con, wm_key) or 0)
    if wm and len(lines) < wm:
        stats.removed += _drop_source_file_docs(con, SOURCE_CODEX, file_key)
        wm = 0
    # Same truncated-tail rule as copilot: the watermark covers only
    # well-formed records, so a completed tail line is re-read next run.
    last_complete = wm
    if not forked:
        for i in range(wm, len(lines)):
            line = lines[i].strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                stats.skipped_records += 1
                continue
            last_complete = i + 1
            if not isinstance(record, dict):
                stats.skipped_records += 1
                continue
            payload = record.get("payload")
            if record.get("type") not in ("response_item", "event_msg") or (
                not isinstance(payload, dict)
            ):
                continue
            text = _codex_record_text(payload)
            if not text.strip():
                continue
            _insert_doc(
                con,
                text=text,
                session_id=session_id,
                role=_codex_record_role(record["type"], payload),
                ts=_epoch_ms(record.get("timestamp") or payload.get("timestamp"))
                or started,
                project=cwd,
                session_title=None,
                source=SOURCE_CODEX,
                ref=f"{SOURCE_CODEX}:{file_key}:{i}",
            )
            stats.codex_messages += 1
    else:
        # Forks are never indexed — and if the fork meta arrived after
        # docs were already written, they are dropped here.
        stats.removed += _drop_source_file_docs(con, SOURCE_CODEX, file_key)
        last_complete = len(lines)
    _meta_set(con, wm_key, last_complete)
    _meta_set(con, f"{SOURCE_CODEX}:file:{file_key}", file_key)


def _index_codex_root(
    con: sqlite3.Connection, root: Path, stats: IndexStats
) -> None:
    stats.sources.append(SOURCE_CODEX)
    # Spec §6.4 precedence: state_<N>.sqlite — largest N wins — plus
    # rollout JSONL under sessions/, archived_sessions/ and
    # $CODEX_SESSIONS_DIR. A rollout whose id is also in threads is NOT
    # skipped here: the threads row holds derived fields, the rollout
    # holds the real messages — different docs, both indexed.
    state: Path | None = None
    best_n = -1
    try:
        entries = sorted(root.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        match = _CODEX_STATE_RE.match(entry.name)
        if entry.is_file() and match and int(match.group(1)) > best_n:
            best_n, state = int(match.group(1)), entry

    rollouts: list[Path] = []
    for base in (root / "sessions", root / "archived_sessions"):
        _walk_rollouts(base, rollouts)
    extra = os.environ.get("CODEX_SESSIONS_DIR", "").strip()
    if extra:
        _walk_rollouts(Path(extra), rollouts)
    rollouts = sorted(rollouts)

    present = {str(path) for path in rollouts}
    if state is not None:
        present.add(str(state))
    _drop_vanished_files(con, SOURCE_CODEX, present, stats)

    if state is not None:
        _index_codex_state_db(con, state, stats)
    for path in rollouts:
        _index_codex_rollout(con, path, stats)


# --- vscode-chat (§6.5) ------------------------------------------------------

_VSCODE_GLOBAL_DIRS = ("emptyWindowChatSessions", "transferredChatSessions")
# Doc keys that mark a recognizable VS Code chat session (§6.5).
_VSCODE_KNOWN_KEYS = frozenset(
    {
        "requests",
        "customTitle",
        "title",
        "creationDate",
        "lastMessageDate",
        "version",
        "requesterUsername",
        "responderUsername",
        "sessionId",
        "initialLocation",
        "isImported",
        "agent",
    }
)
# Mutation-log path components that must never be followed (parity with
# the TS parser's prototype-pollution guard).
_VSCODE_UNSAFE_KEYS = frozenset({"__proto__", "prototype", "constructor"})
# Documented readable-content keys of a chat request (§6.5 shape:
# ``requests[].message.{text,parts[]}`` and the ``requests[].response[]``
# part objects' ``value``/``content``). Only these are indexed — request
# ids, agent/extension metadata, model info and timings inside a request
# are metadata, not searchable content.
_VSCODE_CONTENT_KEYS = ("text", "value", "content", "parts")


def _vscode_field_text(node: Any) -> str:
    """Text under a vscode-chat content field: strings pass through and
    containers are walked through the content keys only, so metadata
    (agent ids, ranges, extension info) never reaches the index.

    Iterative — a chat doc can nest ``content`` hundreds of levels deep
    and a recursive walk would RecursionError, aborting the whole index
    run (devin-review on #82). Joining is associative over the flat leaf
    sequence, so traversal order matches the old recursive walk: lists
    in order, dicts in ``_VSCODE_CONTENT_KEYS`` order, empties dropped."""
    parts: list[str] = []
    stack: list[Any] = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, str):
            if cur:
                parts.append(cur)
        elif isinstance(cur, list):
            stack.extend(reversed(cur))
        elif isinstance(cur, dict):
            for key in reversed(_VSCODE_CONTENT_KEYS):
                child = cur.get(key)
                if child is not None:
                    stack.append(child)
    return "\n".join(parts)


def _vscode_chat_files(
    user_dirs: Iterable[Path],
) -> list[tuple[Path, str, str | None]]:
    """``(file, session_id, workspace_dir_name|None)`` per chat store.

    ``.jsonl`` mutation logs win over same-id ``.json`` snapshots (§6.5).
    The workspace dir name is VS Code's stable workspace hash — the only
    workspace identity the store offers, used verbatim for ``project``;
    globalStorage sessions get ``None`` (a legitimate hashless identity).
    """
    out: list[tuple[Path, str, str | None]] = []

    def collect(chat_dir: Path, workspace: str | None) -> None:
        if not chat_dir.is_dir():
            return
        try:
            entries = sorted(chat_dir.iterdir())
        except OSError:
            return
        by_id: dict[str, Path] = {}
        for entry in entries:
            if not entry.is_file():
                continue
            if entry.name.endswith(".jsonl"):
                sid = entry.name[: -len(".jsonl")]
            elif entry.name.endswith(".json"):
                sid = entry.name[: -len(".json")]
            else:
                continue
            if sid and (sid not in by_id or entry.name.endswith(".jsonl")):
                by_id[sid] = entry
        for sid in sorted(by_id):
            out.append((by_id[sid], sid, workspace))

    for user_dir in user_dirs:
        for name in _VSCODE_GLOBAL_DIRS:
            collect(user_dir / "globalStorage" / name, None)
        ws_root = user_dir / "workspaceStorage"
        if ws_root.is_dir():
            try:
                ws_entries = sorted(ws_root.iterdir())
            except OSError:
                ws_entries = []
            for entry in ws_entries:
                if entry.is_dir():
                    collect(entry / "chatSessions", entry.name)
    return out


def _vscode_container(root: Any, keys: list[Any]) -> Any:
    """The container at ``keys`` in ``root``, creating dicts/lists along
    the way (port of the TS mutation replay's ``walk``)."""
    cur = root
    for i, key in enumerate(keys):
        child_is_list = i + 1 < len(keys) and (
            isinstance(keys[i + 1], int) and not isinstance(keys[i + 1], bool)
        )
        if isinstance(cur, dict):
            nxt = cur.get(key)
            if not isinstance(nxt, (dict, list)):
                nxt = [] if child_is_list else {}
                cur[key] = nxt
        elif isinstance(cur, list):
            if not isinstance(key, int) or isinstance(key, bool) or key < 0:
                return None
            while len(cur) <= key:
                cur.append(None)
            nxt = cur[key]
            if not isinstance(nxt, (dict, list)):
                nxt = [] if child_is_list else {}
                cur[key] = nxt
        else:
            return None
        cur = nxt
    return cur


def _vscode_rebuild(text: str) -> tuple[dict[str, Any], int]:
    """Rebuild a session document: a ``.json`` snapshot, or a ``.jsonl``
    mutation log replayed in order (kind 0 replace · 1 set · 2 append,
    truncating to ``i`` first · 3 delete; unknown kinds skipped).

    Returns ``(doc, bad_lines)`` — malformed lines are skipped and
    counted, matching the truncated-tail/partial-parse contract."""
    trimmed = text.strip()
    if trimmed.startswith("{"):
        try:
            whole = json.loads(trimmed)
        except (json.JSONDecodeError, RecursionError):
            whole = None
        if isinstance(whole, dict) and not isinstance(whole.get("kind"), int):
            return whole, 0
    state: dict[str, Any] = {}
    bad_lines = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except (json.JSONDecodeError, RecursionError):
            bad_lines += 1  # truncated tail or malformed mid-line
            continue
        if not isinstance(record, dict):
            continue
        raw_keys = record.get("k")
        keys = (
            [
                p
                for p in raw_keys
                if isinstance(p, (str, int)) and not isinstance(p, bool)
            ]
            if isinstance(raw_keys, list)
            else []
        )
        if any(isinstance(p, str) and p in _VSCODE_UNSAFE_KEYS for p in keys):
            continue
        last = keys[-1] if keys else None
        kind = record.get("kind")
        if kind == 0:
            if isinstance(record.get("v"), dict):
                state = record["v"]
        elif kind == 1:
            if not keys:
                if isinstance(record.get("v"), dict):
                    state = record["v"]
            else:
                parent = _vscode_container(state, keys[:-1])
                if isinstance(parent, dict):
                    parent[last] = record.get("v")
                elif isinstance(parent, list):
                    if not isinstance(last, int) or isinstance(last, bool) or (
                        last < 0
                    ):
                        continue
                    while len(parent) <= last:
                        parent.append(None)
                    parent[last] = record.get("v")
        elif kind == 2:
            if not keys:
                continue
            parent = _vscode_container(state, keys[:-1])
            if not isinstance(parent, (dict, list)):
                continue
            arr = parent.get(last) if isinstance(parent, dict) else None
            if isinstance(parent, list):
                if not isinstance(last, int) or isinstance(last, bool) or last < 0:
                    continue
                while len(parent) <= last:
                    parent.append(None)
                arr = parent[last]
            if not isinstance(arr, list):
                arr = []
                parent[last] = arr
            value = record.get("v")
            items = (
                value
                if isinstance(value, list)
                else ([] if "v" not in record else [value])
            )
            idx = record.get("i")
            if (
                isinstance(idx, int)
                and not isinstance(idx, bool)
                and 0 <= idx <= len(arr)
            ):
                arr[idx:] = items
            else:
                arr.extend(items)
        elif kind == 3:
            if not keys:
                continue
            parent = _vscode_container(state, keys[:-1])
            if isinstance(parent, list) and isinstance(last, int) and (
                not isinstance(last, bool) and 0 <= last < len(parent)
            ):
                parent.pop(last)
            elif isinstance(parent, dict):
                parent.pop(last, None)
        # unknown kinds — counted by the browser, silently skipped here
    return state, bad_lines


def _index_vscode_chat_dirs(
    con: sqlite3.Connection, user_dirs: Iterable[Path], stats: IndexStats
) -> None:
    stats.sources.append(SOURCE_VSCODE_CHAT)
    files = _vscode_chat_files(user_dirs)
    present = {str(path) for path, _, _ in files}
    _drop_vanished_files(con, SOURCE_VSCODE_CHAT, present, stats)

    for path, session_id, workspace in files:
        file_key = str(path)
        wm_key = f"{SOURCE_VSCODE_CHAT}:wm:{file_key}"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # vscode-chat has no intrinsic row position: .json snapshots are
        # rewritten whole and .jsonl mutation logs rewrite earlier state.
        # The watermark is therefore a *content* signal — sha256 of the
        # file — not a size: an equal-size edit must re-index (the other
        # surfaces watermark a position/id, which a replace-log lacks).
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if _meta_get(con, wm_key) == digest:
            _meta_set(con, f"{SOURCE_VSCODE_CHAT}:file:{file_key}", file_key)
            continue
        doc, bad_lines = _vscode_rebuild(text)
        stats.skipped_records += bad_lines
        stats.removed += _drop_source_file_docs(con, SOURCE_VSCODE_CHAT, file_key)
        if not (_VSCODE_KNOWN_KEYS & set(doc)):
            # Present but not a recognizable session document — the
            # watermark still moves so it is not reparsed every run.
            _meta_set(con, wm_key, digest)
            _meta_set(con, f"{SOURCE_VSCODE_CHAT}:file:{file_key}", file_key)
            continue
        created = _epoch_ms(doc.get("creationDate"))
        requests = doc.get("requests")
        if isinstance(requests, list):
            for i, request in enumerate(requests):
                if not isinstance(request, dict):
                    continue
                ts = _epoch_ms(request.get("timestamp")) or created or 0
                message = _vscode_field_text(request.get("message"))
                if message.strip():
                    _insert_doc(
                        con,
                        text=message,
                        session_id=session_id,
                        role="user",
                        ts=ts,
                        project=workspace or "",
                        session_title=None,
                        source=SOURCE_VSCODE_CHAT,
                        ref=f"{SOURCE_VSCODE_CHAT}:{file_key}:{i}:req",
                    )
                    stats.vscode_chat_messages += 1
                response = _vscode_field_text(request.get("response"))
                if response.strip():
                    _insert_doc(
                        con,
                        text=response,
                        session_id=session_id,
                        role="assistant",
                        ts=_epoch_ms(request.get("responseTimestamp")) or ts,
                        project=workspace or "",
                        session_title=None,
                        source=SOURCE_VSCODE_CHAT,
                        ref=f"{SOURCE_VSCODE_CHAT}:{file_key}:{i}:resp",
                    )
                    stats.vscode_chat_messages += 1
        _meta_set(con, wm_key, digest)
        _meta_set(con, f"{SOURCE_VSCODE_CHAT}:file:{file_key}", file_key)


def build_index(
    index_path: str | Path,
    *,
    sessions_db: str | Path | None = None,
    acp_dir: str | Path | None = None,
    copilot_dirs: Iterable[str | Path] | None = None,
    codex_dir: str | Path | None = None,
    vscode_chat_dirs: Iterable[str | Path] | None = None,
    rebuild: bool = False,
) -> IndexStats:
    """Index the given stores into ``index_path`` (incrementally).

    ``copilot_dirs``/``vscode_chat_dirs`` take several roots because the
    spec merges every existing candidate (§6.3 roots, §6.5 variants);
    ``codex_dir`` is a single root (§6.4 picks one home).

    Every non-``None`` argument is *managed*: a store path that is absent
    on disk means the source vanished since the last run and its stale
    docs are swept (``stats.removed``). ``None`` means "not managed this
    run" — the CLI's ``--no-*`` flags — and preserves existing docs."""
    index_path = Path(index_path).expanduser()
    stats = IndexStats(index_path=index_path, rebuilt=bool(rebuild))
    con, auto_rebuilt = open_index(index_path, rebuild=rebuild)
    stats.rebuilt = stats.rebuilt or auto_rebuilt
    try:
        sessions: dict[str, tuple[str, str | None, int]] = {}
        if sessions_db is not None:
            sessions_db = Path(sessions_db)
            if sessions_db.is_file():
                sessions = _index_sessions_db(con, sessions_db, stats)
            else:
                stats.removed += _reset_source(
                    con, SOURCE_DEVIN_CLI, (WM_NODES, WM_PROMPTS, WM_CONTENT)
                )
        if acp_dir is not None:
            acp_dir = Path(acp_dir)
            if acp_dir.is_dir():
                _index_acp_dir(con, acp_dir, stats, sessions)
            else:
                _drop_vanished_files(con, SOURCE_DEVIN_CHAT, set(), stats)
        if copilot_dirs is not None:
            copilot_roots = [Path(d).expanduser() for d in copilot_dirs]
            if any(r.is_dir() for r in copilot_roots):
                _index_copilot_dirs(con, copilot_roots, stats)
            else:
                # Managed-but-gone: files under the configured roots
                # vanished upstream — skipping the collector silently
                # would leave stale hits forever.
                _drop_vanished_files(con, SOURCE_COPILOT, set(), stats)
        if codex_dir is not None:
            if Path(codex_dir).is_dir():
                _index_codex_root(con, Path(codex_dir), stats)
            else:
                _drop_vanished_files(con, SOURCE_CODEX, set(), stats)
        if vscode_chat_dirs is not None:
            vscode_dirs = [Path(d).expanduser() for d in vscode_chat_dirs]
            if any(d.is_dir() for d in vscode_dirs):
                _index_vscode_chat_dirs(con, vscode_dirs, stats)
            else:
                _drop_vanished_files(con, SOURCE_VSCODE_CHAT, set(), stats)
        con.commit()
    finally:
        con.close()
    return stats


def index_doc_count(index_path: str | Path) -> int:
    """Total documents currently in the index."""
    con, _ = open_index(index_path)
    try:
        return con.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    finally:
        con.close()
