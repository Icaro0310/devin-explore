# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Opt-in check `integrations` (`--integrations`, alias `--ci`):
  "half-configured CI" detection for the repo under `--cwd`. Resolves
  `owner/name` from `git remote get-url origin` and compares `gh api`
  views — Actions secret *names* (values are never exposed) vs
  `.github/workflows` file names. A provisioned secret without its
  matching workflow (or vice-versa) is `FAIL half-configured`; both is
  `PASS`, neither is `WARN`, `git`/`gh` failures are `WARN unavailable`.
  Driven by a rule table (`required_secret` + `workflow_name_substrings`
  + `label`) so new integrations can be added; ships with the
  `TS_AUTHKEY` / `fallback`·`ai-review` rule.
- Opt-in check `live` (`--live`): connectivity probes that authenticate
  instead of checking a file exists — Slack `auth.test` (token from
  `SLACK_BOT_TOKEN`/`SLACK_USER_TOKEN` or the first `*/gateways/.env`
  under `~`/`$DEVIN_ECOSYSTEM_ROOT`; the token is never printed) and
  Ollama `GET /api/tags` (`OLLAMA_HOST`, default
  `http://localhost:11434`), each bounded to ~8s. A configured endpoint
  that fails is `FAIL`; nothing configured is a `WARN` skip.
- Both opt-in checks honor `--offline`/`DEVIN_DOCTOR_OFFLINE=1` and stay
  out of the default run (and the pre-commit gate) — they only run when
  their flag is passed to `check`/`report`/`plan`.

## [0.3.1] - 2026-10-17

### Fixed

- A locked (`SQLITE_BUSY`) `sessions.db` now reports `WARN unreadable`
  at the `readable` rung instead of `FAIL store-corrupt` — a busy file
  is not a damaged file, and the rung contract reserves `corrupt` for
  content damage.

## [0.3.0] - 2026-10-16

### Fixed

- `devin-doctor-mcp` keeps the traceback on `.run()` `ImportError`s; the
  missing-extra hint only covers failures while building the app.

- Surface ladder verdicts now follow SURFACE-HEALTH.md exactly — diver-
  gences fixed while closing the install-states corpus loop:
  - A wrong-shaped but openable acp-messages `.db` (no `meta`+`messages`
    tables) is `schema` `WARN schema-unknown`, not `readable` `FAIL` —
    the file parsed, so it is not corrupt.
  - A `sessions.db` with a missing/empty `refinery_schema_history`
    ledger is `schema` `WARN schema-unknown`, not `FAIL` — only a
    damaged file is corrupt.
  - `devin-cli` schemas below the supported floor emit code
    `schema-old`; above the known range `schema-unknown`.
  - A `codex-cli` root holding a `state_*` db whose suffix is not
    numeric is `schema-unknown`, not silently skipped.
  - `copilot-cli`/`vscode-chat` roots without any store container are
    `present` `WARN store-missing`; containers with no artifacts are
    `sane` `WARN store-empty` — the two codes were previously collapsed
    into one "installed but unused" WARN.
  - Zero-byte artifacts report `sane` `WARN empty-store` on every
    surface (the sane rung owns emptiness — `readable` does not run on
    a zero-byte file).
  - Absent surfaces emit the canonical `present PASS` /
    `note: not-installed` finding instead of staying silent; the report
    still groups them under `not detected`.


### Added

- Agent-environment surface health: `check`/`report`/`plan` now run the
  same four-rung ladder (present → readable → schema → sane) once per
  agent session surface on the machine — `devin-cli`
  (`cli/sessions.db`), `devin-chat` (`acp-messages/*.db` +
  `state.vscdb`), `copilot-cli` (`session-state`/`history-session-state`
  event logs), `codex-cli` (`state_<N>.sqlite` + rollout JSONL) and
  `vscode-chat` (`chatSessions/*.json{,l}`). An absent surface emits the
  canonical `present PASS` / `not-installed` finding and groups under
  `not detected` — an uninstalled agent is a valid environment, never a
  failure. Privacy doctrine is strict:
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
- Surface findings carry optional `rung` / `code` / `note` fields in
  `--json` — the SURFACE-HEALTH §2 ladder metadata (`present` /
  `readable` / `schema` / `sane` plus the §5 codebook). Non-surface
  findings keep the four-key shape.
- `tests/install-states/` vendors the install-states corpus verbatim
  from devin-internals-spec (spec PR #46): 32 home-surrogate fixtures +
  `install-states-manifest.json` declaring per-fixture
  `expected_findings`. `tests/test_install_states.py` replays every one
  through the real surface checks and diffs the produced
  `{rung, status, code, note}` tuples against the manifest —
  `unmodeled_states` (`locked-or-unreadable`) is honored and asserted.

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
