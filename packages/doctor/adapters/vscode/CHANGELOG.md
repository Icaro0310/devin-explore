# Changelog

## [0.1.0]

### Added

- Status bar verdict (PASS / WARN / FAIL) with per-severity counts, click to re-run.
- `devin-doctor: Run health check`, `Open full report`, and `Show remediation plan` commands backed by `devin-doctor check --json`, `report --md`, and `plan`.
- Settings: `devinDoctor.executable`, `checkOnStartup`, `extraArgs`.
- Warning notification on failing checks.
