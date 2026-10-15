# SPEC — devin-search

Full-text search over every Devin session — find a command, an error
message, a file path or a decision across months of history in under a
second. A local SQLite FTS5 index (`search.db`) is built incrementally
from `sessions.db` and `acp-messages/*.db`; queries are BM25-ranked with
role, project and date filters.

**Unofficial community project.** Devin's local stores are private
internals; this tool depends on
[`devin-internals-spec`](https://github.com/Icaro0310/devin-internals-spec)
for schema detection and typed parsers instead of guessing SQL.

## 1. Scope

In scope (M1):

- Sources: CLI `sessions.db` (`message_nodes`, `prompt_history`,
  `tool_call_state`) and GUI `User/acp-messages/*.db` (`messages`).
- One FTS5 index file (`search.db`) — the **only** file devin-search
  writes.
- Incremental indexing via per-source watermarks (only new rows scanned
  on re-runs).
- `devin-search query` — BM25 ranking, highlighted snippets, `--role` /
  `--project` / `--since` / `--limit` filters, `--json`.

Out of scope (see §6):

- Semantic/embedding search, TUI/browser result UI, Obsidian links, PyPI.

## 2. Invariants

1. **Read-only sources.** `sessions.db` and `acp-messages/*.db` are opened
   through `devin_internals.parsers` (`file:...?mode=ro`); tests assert
   the source file hashes are unchanged after indexing.
2. **Fail loud on schema drift.** Unknown `sessions.db` schema versions
   raise `SchemaError`; the CLI exits 2 with the detector's message.
3. **Index is disposable.** `search.db` carries an `index_meta`
   schema-version stamp; a mismatch rebuilds from scratch rather than
   misreading a stale layout. `--rebuild` forces the same path.
4. **Safe queries.** User input is escaped into quoted FTS5 tokens
   (`to_fts_query`) — operators like `OR`, `NEAR` or stray quotes in the
   term can never alter query semantics.
5. **Tolerant payloads.** `chat_message`, `tool_call_*_json` and acp
   `payload` are *unstable* (SCHEMA.md): extraction accepts
   `role`/`content`/`text` shapes and block lists, falls back to a
   recursive string walk, and returns `None` on undecodable blobs.

## 3. Index layout (`search.db`)

```sql
CREATE TABLE index_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE VIRTUAL TABLE docs USING fts5(
  text,                                        -- searchable content
  session_id UNINDEXED,  role UNINDEXED,       -- filters
  ts UNINDEXED,                                -- epoch ms
  project UNINDEXED,     session_title UNINDEXED,
  source UNINDEXED,      ref UNINDEXED         -- 'devin-cli'|'devin-chat' + rowid
);
```

`source` uses the canonical session-surface vocabulary
(`devin-internals-spec` → `docs/SESSION-SURFACES.md`): `devin-cli` for
`cli/sessions.db` rows, `devin-chat` for `User/acp-messages/*.db` rows.

`ref` links each hit back to its source row: `node:<row_id>`,
`prompt:<id>`, `tool:<session_id>/<tool_call_id>`,
`devin-chat:<file>:<position>`.

### Watermarks (`index_meta`)

| key | value |
|---|---|
| `schema_version` | index format version (currently `2`) |
| `wm:devin-cli:message_nodes` | max `row_id` indexed |
| `wm:devin-cli:prompt_history` | max `id` indexed |
| `devin-chat:wm:<file>` | max `position` indexed per acp file |
| `devin-chat:file:<file>` | presence marker — used to detect deleted files |

`tool_call_state` has no auto-increment id, so incrementality there is
ref-dedup (`ref NOT IN docs`). If a source's max id is ever *below* its
watermark the upstream store was pruned/rebuilt: that source's docs are
dropped and re-indexed — watermarks cannot see mid-table deletions.

## 4. Modules (`src/devin_search/`)

| module | role |
|---|---|
| `cli.py` | thin argparse wrapper; exit codes; `--json` plumbing |
| `index.py` | `build_index()` — FTS5 build + incremental watermarks, `IndexStats` |
| `query.py` | `search()` → `Hit` list; `to_fts_query()`, `parse_since()` |
| `fmt.py` | pure emitters: hits table, JSON dicts, stats line |
| `extract.py` | tolerant payload → `(role, text)` extraction |
| `paths.py` | platform defaults for stores and the index file |

## 5. CLI contract

```
devin-search index [--sessions-db PATH] [--acp-dir DIR]
                   [--index PATH] [--rebuild] [--json]
devin-search query "<term>" [--role R] [--project P] [--since DATE]
                   [--limit N] [--index PATH] [--json]
```

- Defaults auto-detect `%APPDATA%/devin/cli/sessions.db` and
  `%APPDATA%/devin/User/acp-messages` (macOS/Linux equivalents);
  `--index` defaults to `<app-dir>/devin-search/search.db`.
- `index` prints a one-line stats summary (or JSON); exit 0.
- `query` prints a fixed-width table (`WHEN · ROLE · PROJECT · SESSION ·
  SNIPPET`, matches wrapped in `«»`) or `--json`; exit 0 with hits, 1 on
  no hits, 2 on usage/store errors.
- `term` is tokenized and ANDed; `--since` accepts `YYYY-MM-DD` or epoch
  ms; `--project` is a substring match on the working directory.

### Roles

Normalized: `user`, `assistant` (aliases: `agent`, `thought`), `tool`
(`tool_call`, `tool_result`), `system`, `shell` (prompt_history with
`is_shell=1`), `unknown`.

## 6. Testing

- Fixtures generated at test time via `devin_internals.fixtures`
  (`create_sessions_db`, `create_acp_messages_db`) plus test helpers that
  inject sessions/acp files with searchable content — no committed
  binaries.
- Required green: build + incremental (no re-index, new rows only), acp
  file removal cleanup, BM25 ordering, each filter, JSON shapes, source
  stores byte-identical (sha256), FTS-operator injection safety.

## 7. M2 queue

- TUI/browser result viewer; open-in-Devin session links.
- Obsidian link output for hits.
- Optional semantic search (opt-in embeddings sidecar).
- PyPI publish (`pipx install devin-search`).
