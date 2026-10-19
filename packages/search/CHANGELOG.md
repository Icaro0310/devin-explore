# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `devin-search query --tool <name>`: exact-match filter on the tool a
  hit came from. `tool_call_state` payloads now lift their tool name
  into a new `docs.tool` column (`UNINDEXED`) — extraction ports
  devin-graph's best-effort heuristic (`name`/`tool_name`/`tool`/
  `kind`/`type`/`title` at top level, then one level inside
  `rawInput`/`raw_input`/`input`/`args`; the call payload wins over the
  update payload). Combines with `--role tool` and the other filters —
  a blank `--tool ""` value means no filter, the same contract the MCP
  adapter applies to `tool=""` — and `--json` hits now carry a `tool`
  field. Index format bumped to v4 — existing indexes rebuild
  automatically on the next `index` run.
- `search_query` MCP tool and `do_query()` accept `tool` for CLI parity;
  the local query log records the filter when set.

## [0.2.1] - 2026-10-17

### Fixed

- Incremental indexing could serve stale content or drop corrections:
  - `devin-cli`: in-place row `UPDATE`s under unchanged row ids and
    mid-table deletes were invisible to the watermarks; a persisted
    content digest now detects covered-content drift and resets the
    source slice. Indexes built before the digest existed (watermarks
    set, no fingerprint) are rebuilt once rather than trusted blindly.
  - `copilot-cli`: in-place `events.jsonl` edits of equal or greater
    length hid behind the line watermark; a covered-prefix fingerprint
    now re-indexes the file. Files indexed before the fingerprint
    existed are re-indexed once on upgrade.
  - Vanished managed sources (`sessions.db` deleted, ACP dir removed,
    copilot/codex roots gone, a specific `--vscode-chat-dir` now absent)
    sweep their docs on the next run instead of resurfacing them forever.
  - Tool calls with no extractable text no longer enter the coverage
    digest — hashing them caused a spurious reset on every run.
- CLI: `index --no-*` for a source no longer validates that source's
  explicit `--*-dir` path — a disabled source never reads it.
- CLI: unreadable or locked `--sessions-db` / index files report a
  controlled `devin-search:` error (exit 2), not a raw traceback.

## [0.2.0] - 2026-10-16

### Fixed

- `docs.ts` for `devin-cli` sources now normalizes epoch **seconds** to
  milliseconds (Linux v17 stores use seconds; SESSION-SURFACES §6.1) —
  previously hits rendered ~1970 and `--since` dropped them. Covers
  `message_nodes`, `prompt_history` and `tool_call_state` insert points.
  `INDEX_SCHEMA_VERSION` bumped to 3 so existing indexes rebuild with
  corrected timestamps.

### Changed

- **Index format v2**: logical source vocabulary renamed to the canonical
  session-surface set (`devin-internals-spec` → `docs/SESSION-SURFACES.md`) —
  `docs.source` values `sessions`→`devin-cli`, `acp`→`devin-chat`; ref
  prefixes `acp:`→`devin-chat:`; `index_meta` keys `wm:sessions:*`→
  `wm:devin-cli:*`, `acp:*`→`devin-chat:*`; stats field `acp_messages`→
  `devin_chat_messages`. Existing indexes auto-rebuild on next `index`
  run; `query` on a stale-version index fails closed with a reindex hint.
- `acp_dir_candidates` on darwin probes `Devin` (capitalized UI config
  dir) instead of lowercase `devin` — case-sensitive volumes were missed.

### Fixed

- `devin-search-mcp` keeps the traceback on `.run()` `ImportError`s; the
  missing-extra hint only covers failures while building the app.
- Multi-source collector correctness (review hardening): JSONL
  watermarks now cover only well-formed records, so a truncated tail
  completed upstream is indexed on the next run (copilot + codex);
  the vscode-chat watermark is a content hash, so equal-size rewrites
  re-index; codex `threads` rows upsert by ref and drop docs for
  deleted threads; a higher-priority copilot root claiming an
  already-indexed session evicts the stale copy; codex exec payloads
  index `command` *and* `output`; vscode-chat extraction is restricted
  to documented content fields so request metadata stays out.


### Added

- **Multi-source collectors**: the index now covers the three non-Devin
  session surfaces from `SESSION-SURFACES.md` — `copilot-cli`
  (`session-state`/`history-session-state` event logs, all layouts),
  `codex-cli` (newest `state_<N>.sqlite` `threads` text fields +
  `sessions/`/`archived_sessions`/`$CODEX_SESSIONS_DIR` rollout JSONL)
  and `vscode-chat` (`chatSessions` `.json` snapshots + `.jsonl`
  mutation-log replay, workspace and global storage). Per-file
  watermarks + presence markers give the same incremental/deletion
  semantics as `devin-chat`; malformed JSONL lines are counted and
  skipped. New `index` flags: `--copilot-dir`/`--no-copilot`,
  `--codex-dir`/`--no-codex`, `--vscode-chat-dir`/`--no-vscode-chat`;
  defaults auto-detect the spec roots. Index format stays at v2 —
  existing indexes gain the new docs on the next run, no rebuild needed.
- JSON payloads now self-report the CLI `version` (query and misses
  outputs), and `--version` prints it — the VS Code adapter uses both
  for precise compatibility classification instead of shape-only checks.
- Read-only adapters: `devin_search.mcp_server` MCP server
  (`search_query`, `devin-search-mcp` entry point, `mcp` extra), Devin
  skill and `adapters/` plugin root.

### Changed

- README gains the generated `Part of the DEVIN ecosystem` block
  (track/nature/audience/interface rendered from the registry).

- `labeler.yml` is now a thin caller of the shared reusable workflow in `devin-powerups` (`@v1`); PR labeling behavior is unchanged.

- Published to PyPI; the README install section prescribes `uv tool install`/`pip install` from the index and the source-only `DIST-STATUS` banner is gone.

- `llms.txt` no longer states a hard-coded ecosystem size; the registry owns the count.

## [0.1.0] - 2026-09-29

### Added

- Initial scaffold from `devin-repo-template`.
- `devin-search index` — builds a local SQLite FTS5 index (`search.db`)
  from `sessions.db` (`message_nodes`, `prompt_history`, `tool_call_state`)
  and `User/acp-messages/*.db`; incremental via per-source watermarks,
  `--rebuild`, `--json`. Source stores are opened read-only through
  `devin-internals-spec` v0.2.0 and unknown schema versions fail loudly.
- `devin-search query <term>` — BM25-ranked hits with highlighted
  snippets, role/project/since/limit filters and `--json`; each hit links
  back to its source row via `ref`.
