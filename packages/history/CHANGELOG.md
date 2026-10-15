# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `devin-history-mcp` keeps the traceback on `.run()` `ImportError`s; the
  missing-extra hint only covers failures while building the app.


### Changed


- Docs: guias de plataforma regenerados; job semanal inclui `--out` obrigatório e usa wrapper `.cmd`.
- README gains the generated `Part of the DEVIN ecosystem` block
  (track/nature/audience/interface rendered from the registry).

- `labeler.yml` is now a thin caller of the shared reusable workflow in `devin-powerups` (`@v1`); PR labeling behavior is unchanged.

- Install section now recommends pypi `uv tool install devin-history` as the primary route, with `pipx`/source installs documented as alternatives.

### Added

- `index.<fmt>` stats block: total sessions, date span, per-project and
  per-format session counts, and user/assistant/tool message totals
  (Markdown table in `index.md`, `stats` object in `index.json`).

- Read-only adapters: `devin_history.mcp_server` MCP server
  (`history_list` + `history_export`, `devin-history-mcp` entry point,
  `mcp` extra), Devin skill and `adapters/` plugin root.

### Fixed

- `history_list` / `do_list` default `limit` now matches the CLI:
  unset means every session (was an implicit cap of 20).

### Changed

- `llms.txt` no longer states a hard-coded ecosystem size; the registry owns the count.

## [0.1.0] - 2026-09-29

### Added

- `devin-history` CLI with three subcommands, `--json` on all:
  - `export` — one Markdown note or JSON file per session + `index.<fmt>`;
    idempotent via the `last_activity` marker; `--all` / `--dry-run`.
  - `audit` — groupings by status / task type / project / month, anomaly
    detection (empty, orphan, long-running sessions), optional `--csv`.
  - `list` — quick session table, `--limit`.
- `src/devin_history/` library: `export.py`, `audit.py`, `format.py`,
  `messages.py`, `times.py`, `paths.py`; `cli.py` is a thin wrapper.
- Store access via `devin-internals-spec` v0.2.0 — schema detection with
  fail-loud behavior on unknown versions; all reads are `mode=ro`.
- Platform auto-detection of `sessions.db` (Windows `%APPDATA%`, macOS,
  Linux XDG).
- Tolerant `chat_message` decoding (`role`/`content`, `role`/`text`,
  block lists; `agent`→`assistant`) for the unstable payload format.
- Fixture-based test suite (44 tests) using
  `devin_internals.fixtures.create_sessions_db` — no binary fixtures.
- `docs/SPEC.md` (canonical spec), real READMEs (EN/PT-BR), STATUS.md.

### Notes

- Ported from the orchestrator workspace scripts in `legacy/`
  (`devin-history-export.py`, `audit_sessions.py`); GUI `acp-messages`
  export is deferred to M2.
