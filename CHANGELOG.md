# Changelog

## Unreleased

- **Feature** — `devin-doctor` gains an `automation` check for silently
  broken scheduled tasks: per-surface enumeration (user crontab,
  `systemd --user` units/timers including unloaded unit files, Windows
  Task Scheduler), FAIL on referenced paths that no longer exist or that
  point into registry-absorbed repo checkouts, and FAIL on tasks removed
  since the last run via a persisted snapshot (`<data-dir>/doctor/
  automation-snapshot.json`). Unavailable surfaces carry their recorded
  tasks forward instead of reading as empty; removal FAILs persist until
  the task returns or its snapshot entry is deleted by hand; credential-
  shaped arguments are redacted to `***` before output and storage.
- **Feature** — `devin-history` exports now carry the SESSION-EVIDENCE
  anchor triple (`surface`/`native_id`/`ref`) in Markdown frontmatter and
  JSON, per `devin-internals-spec` `docs/SESSION-EVIDENCE.md`.
- **Release** — `devin-search` bumped to 0.2.0 across pyproject,
  `__version__`, `package.json` and `plugin.json` (multi-source
  collectors + index format v2). The extension's `devin-search-cli`
  compatibility ceiling widened to `>=0.1,<0.3` so it accepts the
  matched 0.2.x CLI (contract test updated).
- **Release** — `devin-doctor` bumped to 0.3.0 across pyproject,
  `__version__`, `package.json` and `plugin.json` (agent-environment
  surface-health ladder). The extension's `devin-doctor-cli`
  compatibility ceiling widened to `>=0.1,<0.4` so it accepts the
  matched 0.3.x CLI (contract test updated).
- **MCP** — every `*-mcp` serve entry point now keeps the traceback for
  startup `ImportError`s from `.run()`; only app-build failures get the
  missing-extra hint (exit 1).

- **Release** — version lockstep for the shared `devin-powerups` release
  gate (tag ↔ pyproject ↔ package.json): `devin-doctor` pyproject,
  `__version__` and `plugin.json` aligned to the already-published 0.2.0
  VS Code extension, and the extension's `devin-doctor-cli` compatibility
  range widened to `>=0.1,<0.3` so it accepts the matched 0.2.x CLI;
  `devin-search` bumped to 0.1.2 across pyproject, package.json,
  `__version__` and `plugin.json`.
- **CI** — `open-pr-bot` fails closed without `PR_BOT_TOKEN` and its
  existing-PR lookup is base-, owner- and case-aware; fork PRs sharing
  the head name no longer block creating the repository's own PR.
- **Fix** — `devin-doctor` updates check fetches the DevKit manifest from its
  monorepo path (`packages/devkit/src/devin_devkit/manifest.json`); the
  pre-monorepo URL always 404d, silently degrading the check to "registry
  unreachable".
- **CI** — `release.yml` gains a `fanout` job calling the reusable
  `devin-powerups` release workflow (wheel gate, `.vsix` artifact for
  the doctor/search adapters). `release` and `fanout` are tag-push-only;
  a manual `workflow_dispatch` exercises the `resolve` job only — the
  end-to-end proof is a real tag push (a fork works since the
  Marketplace/PyPI legs skip without secrets). `packages/search` bumped
  to 0.1.1 to match the already-published `devin-search` extension
  version. The fanout job forwards `OVSX_PAT` explicitly (never
  `inherit`) so the Open VSX publish leg activates without exposing
  unrelated secrets to the reusable workflow.

- **Docs** — corporate-Windows guides: new `devin-doctor` VS Code
  extension section (thin shell running `check --json`/`report --md`/
  `plan`; the editor container stays outside the local-only contract)
  and the Zscaler root export now covers both certificate stores
  (`certmgr.msc`/`certlm.msc`, `Cert:\CurrentUser\Root` +
  `Cert:\LocalMachine\Root`) and yields PEM (Base-64 wizard export, or
  `certutil -encode` after `Export-Certificate`, which writes DER).
- **Pre-commit** — `.pre-commit-hooks.yaml` adds the `devin-doctor-gate`
  hook: `devin-doctor check --offline` runs every registered check on
  each commit, blocking only on FAIL (`verbose` keeps WARN findings
  visible on green runs). `devin-doctor check`/`report`/`plan` gain a
  matching `--offline` flag, equivalent to `DEVIN_DOCTOR_OFFLINE=1`.
- **CI** — `auto-approve` now re-runs the shared gate on
  `pull_request_review[submitted]` and on the `Devin Review` commit
  `status` success, so clean analyses and late reviews no longer strand
  PRs in REVIEW_REQUIRED.
- **Docs** — refreshed the generated `Part of the DEVIN ecosystem` block: journey recuration v2 (six paths, zero repeats, `Local-first ops` label, `devin-bridge` in DevOps).
- **Docs** — ecosystem journey recuration applied (six curated audiences); stale `Path:` lines removed from the devin-doctor eco-block, which is not a registry entry.
- **CI** — Ruff lint job added (`astral-sh/ruff-action`, pinned); `legacy/`
  migration scripts excluded from the shipped lint scope.
- **Publish** — consolidated `pypi-publish.yml` builds and uploads
  `devin-doctor`, `devin-graph`, `devin-history`, `devin-pm` and
  `devin-search` via PyPI Trusted Publishing (OIDC), tag `*-v*` or
  manual dispatch.
- **Fix** — `devin-pm` resolves `sessions.db` across `XDG_DATA_HOME`,
  `XDG_CONFIG_HOME` and `~` candidates, matching the rest of the
  ecosystem.
- **Fix** — `devin-history` timestamps are timezone-aware (`DTZ006`).

## 2026-10 (F4 consolidation)

- Packages consolidated into this repo:
  `devin-doctor` 0.1.0, `devin-graph` 0.1.0, `devin-history` 0.1.0,
  `devin-pm` 0.1.0, `devin-search` 0.1.0.
- Prior per-repo history lives in each package's git history.
