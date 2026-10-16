# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `devin-doctor-mcp` keeps the traceback on `.run()` `ImportError`s; the
  missing-extra hint only covers failures while building the app.


### Added

- Agent-environment surface health: `check`/`report`/`plan` now run the
  same four-rung ladder (present → readable → schema → sane) once per
  agent session surface on the machine — `devin-cli`
  (`cli/sessions.db`), `devin-chat` (`acp-messages/*.db` +
  `state.vscdb`), `copilot-cli` (`session-state`/`history-session-state`
  event logs), `codex-cli` (`state_<N>.sqlite` + rollout JSONL) and
  `vscode-chat` (`chatSessions/*.json{,l}`). An absent surface emits no
  findings and groups under `not detected` — an uninstalled agent is a
  valid environment, never a failure. Privacy doctrine is strict:
  headers/metadata only — counts, sizes, paths, statuses; never message
  content.
- `--surface NAME` (repeatable, `check`/`report`/`plan` and the MCP
  `doctor_check` tool) scopes the surface ladder; the Devin-environment
  checks always run unscoped. The report gains a `surfaces` section
  (one line per surface) and `--json` gains a `surfaces` object
  (`detected`/`not_detected`).
- `check`/`report`/`plan` gain `--offline`, equivalent to
  `DEVIN_DOCTOR_OFFLINE=1`: the `updates` check skips its manifest fetch
  and the run stays fully local. Backs the repo's new `devin-doctor-gate`
  pre-commit hook (`devin-doctor check --offline`).

### Changed

- The old `stores`/`schema` checks re-home under the `devin-cli` surface
  (`checks/surfaces/`); the report groups remaining checks
  (`health-of-data`, `config`, `hooks-windows`, `disk`, `updates`) under
  `devin environment`. With no Devin stores at all these now report
  not-detected/PASS rather than hard `FAIL`s, so the tool stays useful
  on machines that only run other agents.
- Devin schema verdicts soften: an out-of-range or unknown
  `sessions.db` schema is `WARN schema-unknown` (counts still run)
  instead of a `FAIL` verdict, matching the per-surface doctrine.

- `disk` check: the data-dir walk is now bounded (250k files / 45s) and
  single-stat per file via `os.scandir` — multi-million-file data dirs
  (Cache/blob_storage) no longer stall `check` for minutes. Capped scans
  report honest lower bounds (`≥`, `(scan capped)`): they can prove WARN
  but never PASS, so under-threshold capped results are WARN.
- Docs: guias de plataforma regenerados (Zscaler com CA por ferramenta, exceções de rede nomeadas).
- README gains the generated `Part of the DEVIN ecosystem` block
  (track/nature/audience/interface rendered from the registry).

- `labeler.yml` is now a thin caller of the shared reusable workflow in `devin-powerups` (`@v1`); PR labeling behavior is unchanged.

- Install section now recommends pypi `uv tool install devin-doctor` as the primary route, with `pipx`/source installs documented as alternatives.

- `llms.txt` no longer states a hard-coded ecosystem size; the registry owns the count.

## [0.1.0] - 2026-09-29

### Added

- Read-only adapters: `devin_doctor.mcp_server` MCP server
  (`doctor_check` + `doctor_capabilities`, `devin-doctor-mcp` entry
  point, `mcp` extra), Devin skill and `adapters/` plugin root. The
  `offline` flag is scoped per call through `Context.offline` — the MCP
  server never mutates `DEVIN_DOCTOR_OFFLINE`, so concurrent calls
  cannot interfere with each other's update check.

- `devin-doctor check` — five read-only checks over a Devin data dir:
  `stores` (presence/size/row counts), `schema` (version + layout detection
  via `devin-internals-spec`), `health-of-data` (empty sessions, orphan
  `message_nodes`, stale sessions, `SQLITE_BUSY` locks), `config`
  (masked `credentials.toml`, `.devin/` hooks/MCP JSONC sanity) and `disk`
  (footprint, largest stores, acp-messages accumulation). PASS/WARN/FAIL +
  `fix:` suggestions; exit 1 on any FAIL.
- `devin-doctor report --md` — markdown report for pasting into issues.
- `--json` output contract, `--data-dir`, `--cwd`, `--stale-days` options.
- Test suite (53 tests) running entirely on `devin_internals.fixtures`
  synthetic trees — no real session data required.

### Changed

- README/README.pt-BR filled with real content; `docs/SPEC.md` written;
  `SECURITY.md`/`STATUS.md` updated.
