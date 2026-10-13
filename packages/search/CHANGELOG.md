# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
