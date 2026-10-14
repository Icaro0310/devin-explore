# Changelog

## [0.2.0]

### Security

- Declared `capabilities.untrustedWorkspaces` (`supported: "limited"`)
  with `restrictedConfigurations` covering `devinDoctor.executable`,
  `devinDoctor.extraArgs` and `devinDoctor.checkOnStartup`; executable
  resolution rejects candidates inside an untrusted workspace, and the
  startup check now requires `vscode.workspace.isTrusted`, so a
  user-level `checkOnStartup` can no longer spawn the CLI in a
  restricted folder.

### Added

- CLI compatibility contract: `devin-doctor-cli` range in `package.json`
  enforced at runtime against the payload's `version` — out-of-range CLIs
  get a warning marker and a named compatible range instead of silent drift.
- Payload validation: a `check --json` response missing contracted fields
  fails loudly as version drift.
- `test/cli-contract.test.js` keeps the declared range and the enforced
  range in sync, and pins the rendered JSON shape.

### Fixed

- `npm test` now uses bare `node --test` (discovers all test files).

## [0.1.1]

### Fixed

- Case-insensitive verdict rendering in the status bar.
- `checkSeq` guard drops stale results from overlapping check runs.
- `node --test` wiring for the adapter test suite.

## [0.1.0]

### Added

- Status bar verdict (PASS / WARN / FAIL) with per-severity counts, click to re-run.
- `devin-doctor: Run health check`, `Open full report`, and `Show remediation plan` commands backed by `devin-doctor check --json`, `report --md`, and `plan`.
- Settings: `devinDoctor.executable`, `checkOnStartup`, `extraArgs`.
- Warning notification on failing checks.
