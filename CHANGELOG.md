# Changelog

## Unreleased

- **Feature** — `devin-graph orphans` + `devin-graph blame-session`:
  graph diagnostics. `orphans` lists nodes with no incident edges —
  report-only, never deletes — flagging the subset the next `build`
  would prune. `blame-session <id>` (unique prefix accepted, ambiguous
  exits 2 with candidates) reports what a session produced — owned
  nodes + edges — and touched: resolved files, tools, projects, commits
  and the extraction-ledger timestamp. `StoredNode` gains `owner`;
  `GraphStore` gains `extracted_at`. Both read via `mode=ro`.
- **Feature** — `devin-search stats`: index health read straight from
  the index (opened `mode=ro`, never creates/mutates; missing index
  exits 2). Reports total/per-source/per-role doc counts, newest doc
  timestamps, devin-cli + per-file watermark positions, and
  reconciliation counters — `build_index` now persists `run:*` and
  `total:*` keys in `index_meta` (indexed/removed/swept/skipped per run,
  cumulative totals that reset on `--rebuild`). `IndexStats` gains
  `swept_files`; `index --json` and the stats line surface it.
- **Feature** — `devin-doctor drift`: a bounded, privacy-safe local
  snapshot for drift diagnosis. Four sections — tool versions vs
  expected floors (plus installed `devin-*` dists vs declared pyproject
  versions, `--package`-scopable), state-file presence + sha256 digest
  prefixes (contents never emitted), scope-dir/PATH health, and curated
  env key *names* (values never collected). Caps: ≤100 acp dbs digested,
  >64 MiB files stat-only, PATH/env listings bounded. File expectations
  follow the anchor doctrine (`sessions.db` only required on CLI
  installs, `state.vscdb` only on Desktop). Exit `1` on detected drift;
  `--json` emits the whole snapshot.
- **CI** — repin the shared `pr-approve` gate to `devin-powerups`
  `@v1.9`: self-cancel fix (a successful approval no longer renders as
  a failed check), fresh-head recheck and paginated dedup.
- **CI** — `auto-approve` now calls the shared `pr-approve` reusable
  workflow at v1.6 (was v1.1) and also triggers on
  `pull_request_review_comment[created]`, so fix-commit replies
  re-evaluate the gate on the same head; the v1.6 gate adds cross-repo
  fix anchors and gate-eligible concurrency.
- **Feature** — `devin-doctor` gains an `automation` check for silently
  broken scheduled tasks: per-surface enumeration (user crontab,
  `systemd --user` units/timers including unloaded unit files, Windows
  Task Scheduler), FAIL on referenced paths that no longer exist or that
  point into registry-absorbed repo checkouts, and FAIL on tasks removed
  since the last run via a persisted snapshot
  (`<state-dir>/automation-snapshot.json`, resolved to the platform
  `devin-doctor` dir so a `--data-dir` pointed at a repo checkout can
  never write the baseline inside it). Unavailable surfaces carry their
  recorded
  tasks forward instead of reading as empty; removal FAILs persist until
  the task returns or its snapshot entry is deleted by hand; credential-
  shaped arguments are redacted to `***` before output and storage.
- **Fix** — the `automation` snapshot no longer lives under the
  inspected `--data-dir` (where a run pointed at a repo checkout wrote
  `doctor/automation-snapshot.json` into it — absolute paths and cron
  commands were swept into a public commit by `git add -A`). Doctor's
  own state now resolves to a dedicated `devin-doctor` dir
  (`$XDG_DATA_HOME` / `%LOCALAPPDATA%` / Application Support,
  `DEVIN_DOCTOR_STATE_DIR` to override), and a `.gitignore` written
  beside the snapshot keeps the baseline out of `git add -A` even when
  the override points inside a checkout.
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
