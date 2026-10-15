# Changelog

## [Unreleased]

### Changed

- CLI compatibility check: `devin-search --version` is probed once per
  session so an argparse `unrecognized arguments` failure is classified
  precisely — a bad `devinSearch.extraArgs` flag now names the setting
  instead of telling the user to upgrade a healthy CLI.

### Security

- Executable resolution hardened for Restricted Mode (shared
  safe-executable resolver): PATH candidates and npm `.cmd` shim entries
  are realpath'd and rejected when they resolve inside any untrusted
  workspace folder (multi-root included).

## [0.1.1]

### Fixed

- `devinSearch.limit` clamped to 1–500 (`-1` meant "unlimited" to SQLite
  and could overflow the 8 MiB output buffer).
- `misses` surfaces the install/config guidance on spawn failure instead
  of dying silently.
- A quick-pick selection resolving after a newer search started no longer
  opens a superseded hit (seq re-checked post-await).
- `misses` renders the CLI's `per_month` breakdown, matching the README.

## [0.1.0]

### Added

- Status bar hit count for the last query, click to search again.
- `devin-search: Search past sessions` backed by
  `devin-search query --json --no-log`, with a quick-pick of hits and a
  markdown detail sheet per hit.
- `devin-search: Show zero-hit query stats` backed by
  `devin-search misses --json` in the output channel.
- Settings: `devinSearch.executable`, `limit`, `extraArgs`.
- Strictly read-only: `index` stays CLI-only and queries never write the
  miss log.
- `searchSeq` guard drops stale results from overlapping searches.
- `devin-search-cli` pin (`>=0.1,<0.2`) plus payload-shape validation with
  loud drift errors.
