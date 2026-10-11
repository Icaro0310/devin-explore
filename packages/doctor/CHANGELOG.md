# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed


- Docs: guias de plataforma regenerados (Zscaler com CA por ferramenta, exceções de rede nomeadas).
- README gains the generated `Part of the DEVIN ecosystem` block
  (track/nature/audience/interface rendered from the registry).

- `labeler.yml` is now a thin caller of the shared reusable workflow in `devin-powerups` (`@v1`); PR labeling behavior is unchanged.

- Install section now recommends pypi `uv tool install devin-doctor` as the primary route, with `pipx`/source installs documented as alternatives.

- `llms.txt` no longer states a hard-coded ecosystem size; the registry owns the count.

## [0.1.0] - 2026-09-29

### Added

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
