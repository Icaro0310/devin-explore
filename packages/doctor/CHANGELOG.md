# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `publish-readiness` — a `packages/*` member without `pyproject.toml`
  no longer fails when the dir carries no Python-package signals (npm
  bridges, VS Code extensions, docs dirs). Missing pyproject still
  fails for dirs that look like in-progress Python packages
  (`setup.py`/`setup.cfg`, top-level `.py`, or `.py` under `src/`).

### Added

- `toolchain` — missing test/build tools named per-OS with concrete
  `fix:` hints (the 2026-10-02 env-missing incident class). Probes
  `python3`/`python`/`py`, `pip3`/`pip`, `python -m venv` plus
  `import ensurepip` (the Debian `python3-venv` split a PATH lookup
  alone cannot see — a venv that builds without pip FAILs as
  env-missing), `node` (with the `nodejs` alias), `npm` and `git`.
  Hints target the detected package manager: apt/dnf/pacman on Linux
  via `/etc/os-release` ID/ID_LIKE, brew on macOS, winget on Windows,
  generic when the distro is unrecognized. Present-but-degraded pieces
  WARN (pip importable only via `python -m pip`, unanswerable probes);
  healthy pieces PASS. Read-only — `shutil.which` plus `--help`/
  `--version` subprocess calls under 10 s timeouts.

- `deps-internals` — silently stale internal deps inside `<cwd>/.venv`.
  Editable installs (`__editable__*.pth`, both the plain-path and the
  setuptools `*_finder.py` form) are FAILed when their target no longer
  exists — the repo moved or absorbed the package — and WARNed when the
  target resolves outside the checkout (a venv bound to a different
  workspace) or cannot be read. Non-editable `devin_*` dists (read from
  `*.dist-info` metadata, never imported) are compared against the
  same-named `packages/*/pyproject.toml` version — PEP 440-aware, so
  `1.0.0-rc1` matches `1.0.0rc1` — and a drift is a WARN (stale wheel
  shadowing fresh source); dists with no checkout match are foreign
  tooling and ignored. `direct_url.json` `dir_info.editable` plus the
  `__editable__*` .pth set keep editable dist-infos out of the wheel
  comparison. site-packages resolves via `lib/python*/site-packages`,
  `lib64/…` and `Lib/site-packages` (Windows), first sorted match.
  `.venv` only — a checkout without one reports 'not detected'.
  Read-only — venv/site-packages introspection only.

- `path-check` — declared console scripts vs PATH reachability. For each
  package dir in scope (`--cwd` pointing at a `packages/` fan-out root or
  at a single package dir) the check reads `[project.scripts]` /
  `[project.gui-scripts]`, unions in entry points (`console_scripts` +
  `gui_scripts` groups) a same-named installed distribution provides,
  and probes `shutil.which`: an unreachable command is a FAIL naming it
  (with 'installed but not linked' vs 'not installed in this
  interpreter' — the metadata index cannot see isolated uv tool / pipx
  environments, so the wording says so); a command resolving through
  another distribution's entry point is a WARN only when the resolved
  executable actually lives in this interpreter's scripts dirs — a hit
  elsewhere reports ambiguous 'foreign executable' ownership instead of
  blaming the wrong dist. A command declared by more than one package
  WARNs as a collision even when a provider is reachable, PATH hits are
  deduplicated by real path (a repeated or symlink-aliased PATH entry is
  not a second executable), and on Windows the probe mirrors
  `shutil.which` probing the current directory first under `PATHEXT`.
  Read-only — PATH probing and `importlib.metadata` introspection only.

- `automation` check — scheduled automation that silently broke. The
  check enumerates the user crontab, `systemd --user` services/timers
  (loaded units plus installed unit files, so unloaded timers are still
  seen) and Windows Task Scheduler (`schtasks`), extracts the
  script/binary each task references and FAILs on: referenced paths that
  no longer exist, tasks removed since the previous run (tracked via a
  JSON snapshot at `<data-dir>/doctor/automation-snapshot.json` — the
  tool's own state file, the only thing it writes), and targets inside
  repo checkouts the ecosystem registry (`devin-powerups/registry.json`)
  marks as absorbed/archived. Unavailable surfaces carry their history
  forward instead of reading as empty; removal FAILs persist until the
  task returns or its snapshot entry is deleted by hand;
  credential-shaped fragments are redacted to `***` before findings and
  snapshot writes. Motivated by a real incident: a cron line vanished
  for ~3.5 days before anyone noticed.
- New `publish-readiness` check: verifies a package dir can ship through
  the repo's PyPI fan-out (`pypi-publish.yml`). Per package under
  `--cwd`'s `packages/` (or a single package dir passed as `--cwd`) it
  fails loud on: missing/unparseable `pyproject.toml`, `[project]`
  without a literal `name`/`version` (`dynamic = ["version"]` cannot
  stand in — the fan-out's verify job compares a built wheel against the
  field), flat (non-`src/`) layout, `__version__` in
  `src/<mod>/__init__.py` diverging from `[project].version`, and
  console-script entry points that resolve to nothing under `src/`. The
  wheel build is exercised for real via `python -m build --wheel
  --no-isolation` against a temp-dir copy — the package dir is never
  written to — whenever `build`/`setuptools` are importable (`--offline`
  included; it needs no network), and the produced wheel is opened:
  declared import/script modules must ship and the packaged
  `__version__` must match `[project].version`. When no verification can
  run — missing tooling or a local backend older than the declared
  `requires` floor — the build reports WARN `unverified`, never a
  rubber-stamp PASS. `DEVIN_DOCTOR_NO_WHEEL_BUILD=1` opts out of the
  build leg (PEP 517 hooks execute the checkout's declared backend).
  `pypi-publish.yml` coverage tracks `resolve` arms — a package listed
  only in dispatch options/tag patterns would die at publish with
  'unknown package' — and validates the arm's `dir=`/`mod=` outputs;
  coverage findings are WARN-only (not every package publishes). New
  `--package NAME` flag (repeatable, also `package` on the MCP
  `doctor_check` tool) narrows the fan-out by dir or project name.

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
