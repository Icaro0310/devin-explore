# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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


### Added

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
