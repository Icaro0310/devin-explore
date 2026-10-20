<div align="center">

<img src="assets/banner.svg" alt="devin-doctor" width="100%"/>

<a href="https://github.com/Icaro0310/devin-explore/actions/workflows/ci.yml"><img src="https://github.com/Icaro0310/devin-explore/actions/workflows/ci.yml/badge.svg" alt="CI"/></a>
<a href="https://github.com/Icaro0310/devin-explore/releases"><img src="https://img.shields.io/github/v/release/Icaro0310/devin-explore" alt="GitHub release"/></a>
<a href="https://pypi.org/project/devin-doctor/"><img src="https://img.shields.io/pypi/v/devin-doctor" alt="PyPI"/></a>
<a href="https://scorecard.dev/viewer/?uri=github.com/Icaro0310/devin-explore"><img src="https://api.scorecard.dev/projects/github.com/Icaro0310/devin-explore/badge" alt="OpenSSF Scorecard"/></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"/></a>


<a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+"/></a>
<a href="https://github.com/Icaro0310/devin-explore"><img src="https://img.shields.io/github/stars/Icaro0310/devin-explore" alt="GitHub stars"/></a>
<a href="https://github.com/Icaro0310/devin-explore/commits/main"><img src="https://img.shields.io/github/last-commit/Icaro0310/devin-explore" alt="Last commit"/></a>
<a href="https://github.com/Icaro0310/awesome-devin"><img src="https://img.shields.io/badge/part%20of-devin--*-ecosystem-7c3aed" alt="devin-* ecosystem"/></a>
<a href="https://github.com/Icaro0310/devin-explore/issues"><img src="https://img.shields.io/badge/PRs-welcome-brightgreen" alt="PRs welcome"/></a>
</div>

<!-- DEVIN-ECO:BEGIN -->
> **Part of the [DEVIN ecosystem](https://github.com/Icaro0310/awesome-devin)**  
> Track: Understand · Nature: product  
> For: Local-first ops, End users, Data Scientists  
> Interface: CLI  
> Path: Local-first ops · step 1/5 — before `devin-pm`
<!-- DEVIN-ECO:END -->


# devin-doctor

> **Renamed (Oct 2026):** this repository moved from `Icaro0310/devin-doctor` to `Icaro0310/devin-explore`. The PyPI package and console script stay `devin-doctor`; stars, issues and history are preserved by the redirect.

> **Unofficial community project.** Not affiliated with, endorsed by, or
> sponsored by Cognition AI. "Devin" is a trademark of Cognition AI.

**[Linux](README.linux.md)** · **[Personal Windows](README.windows.md)** · **[Corporate Windows](README.corporate-windows.md)**

Part of the [awesome-devin](https://github.com/Icaro0310/awesome-devin) ecosystem: the curated hub for the devin-* tools.

`devin-doctor` diagnoses a Devin Desktop installation — one command that
checks the local stores, schema versions, config files and disk usage, then
prints a health report with concrete fix suggestions. Read-only, always.

## The problem

Devin keeps a lot of local state: a versioned `sessions.db`, one
`acp-messages/*.db` per GUI session, a `state.vscdb` KV store,
`credentials.toml` and JSONC config files. When any of it breaks or bloats —
a schema the tooling doesn't know, a locked database, an unreadable hooks
file, a 700 MiB sessions store — nothing warns you. You find out indirectly:
missing history, auth errors, disk pressure.

`devin-doctor` is `brew doctor` for that installation.

## Prior art

- `brew doctor` / `flutter doctor` — the genre: one command, many checks,
  PASS/WARN/FAIL with fix suggestions. This project adapts the pattern; it
  does not reinvent it.
- [`devin-internals-spec`](https://github.com/Icaro0310/devin-internals-spec)
  — provides the schema-version detector, the read-only store parsers and
  the fixture generator this project builds on.

## What makes it Devin-native

It understands Devin's actual on-disk layout — the `refinery_schema_history`
migration ledger, the `acp-messages` per-session stores, `state.vscdb`, and
the `.devin/` hooks/MCP config format — things no generic system check can
see. Remove Devin and there is nothing to diagnose.

## Install

Python ≥ 3.10 required; install with `uv` (recommended) or `pipx`.

```bash
uv tool install devin-doctor
```

or with `pipx` (alternative):

```bash
pipx install devin-doctor
```

## Usage

```bash
devin-doctor check                    # diagnose the default data dir
devin-doctor check --data-dir D:\devin-backup
# Use a separate Devin UI config root (e.g. Linux)
devin-doctor check --data-dir ~/.local/share/devin --config-dir ~/.config/Devin
devin-doctor check --json             # machine-readable output
devin-doctor check --surface codex-cli  # scope surface checks (repeatable)
devin-doctor check --cwd . --package doctor  # publish-readiness on one package
devin-doctor report --md              # markdown, for pasting into issues
devin-doctor capabilities             # machine capability profile, JSON
```

Each check prints `PASS`/`WARN`/`FAIL`, a one-line finding and a `fix:`
suggestion. Exit code is `0` unless something `FAIL`s (then `1`).

The report has two sections. **surfaces** runs the same four-rung ladder —
`present → readable → schema → sane` — once per agent session store found
on the machine, following SURFACE-HEALTH.md: a `FAIL` stops the ladder, a
`WARN` never does, and a rung with nothing to evaluate emits nothing. A
surface whose roots don't exist emits the canonical `present PASS` /
`note: not-installed` finding and groups under `not detected`: an
uninstalled agent is a valid environment, never a failure.
`--surface NAME` (repeatable) scopes this section; the
**devin environment** checks always run unscoped — except
`publish-readiness`, which scopes to `packages/*` under `--cwd` (or a
single package dir) and narrows further with `--package NAME`
(repeatable, by dir or project name).

Surface findings carry three optional fields in `--json` — `rung` (the
ladder rung that produced them), `code` (the §5 codebook on WARN/FAIL:
`store-missing`, `store-corrupt`, `schema-old`, `schema-unknown`,
`empty-store`, `store-empty`), and `note` (`not-installed`). Parity with
the spec is enforced by `tests/test_install_states.py`, which replays all
32 fixtures of the vendored `tests/install-states` corpus (from
devin-internals-spec `fixtures/install-states`, spec PR #46) and diffs the
produced `{rung, status, code, note}` tuples against the manifest.

| surface | store | ladder specifics |
|---|---|---|
| `devin-cli` | `<data-dir>/cli/sessions.db` | SQLite `mode=ro` (+`immutable=1` retry); `refinery_schema_history` ledger v15–v17, newer/missing → `schema-unknown` WARN, older → `schema-old` WARN; session/`message_nodes` counts |
| `devin-chat` | `<config>/Devin/User/acp-messages/*.db` + `state.vscdb` | `meta` + `messages` table shape; message-row counts, key counts — never message bodies |
| `copilot-cli` | `$COPILOT_DIR`/`$COPILOT_HOME`/`~/.copilot` | `session-state/<uuid>/events.jsonl` and flat `<uuid>.jsonl`; first event must carry `{id, parentId, timestamp, type, data}` |
| `codex-cli` | `$CODEX_HOME`/`~/.codex/state_<N>.sqlite` | largest `N` wins; requires `threads` table; rollout JSONL under `sessions/`/`archived_sessions/`/`$CODEX_SESSIONS_DIR` can carry a state-db-less install |
| `vscode-chat` | `Code`/`Code - Insiders` user dirs + `~/.vscode-server` | `chatSessions/*.jsonl` mutation logs (kinds 0–3) and `*.json` documents |

**devin environment** — the checks that diagnose the local Devin install
itself. With no Devin stores at all they report not-detected/PASS rather
than hard failures, so the tool is still useful on agent-only machines:

| check | reports |
|---|---|
| `health-of-data` | empty sessions, orphan `message_nodes`, sessions idle > `--stale-days`, `SQLITE_BUSY` locks |
| `config` | `credentials.toml` presence + validity (**values masked**); `.devin/` hooks/MCP config sanity |
| `hooks-windows` | hooks/MCP commands that would pop a visible console window on Windows (`cmd /c` without a hidden wrapper, `powershell` without `-WindowStyle Hidden`, direct `.bat`/`.ps1`, `python.exe` vs `pythonw.exe`) — each with file, hook event and a wrapper fix |
| `disk` | total data-dir size, largest DBs, `acp-messages` accumulation trend |
| `updates` | installed tool versions vs the DevKit manifest (the only check that touches the network — fetches `manifest.json` from GitHub; self-skips when unreachable, or force off with `DEVIN_DOCTOR_OFFLINE=1` / `--offline`) |
| `automation` | scheduled tasks per surface — user crontab, `systemd --user` services/timers (loaded units plus installed unit files, so unloaded timers are still seen), Windows Task Scheduler: referenced paths/binaries that no longer exist (**FAIL**), tasks removed since the last run (**FAIL**, via a snapshot at `<data-dir>/doctor/automation-snapshot.json` — the only file the tool writes, its own state), and targets inside repo checkouts the ecosystem registry marks as absorbed. Surfaces that don't exist on the host report `surface unavailable` — never an error |
| `path-check` | `[project.scripts]`/`[project.gui-scripts]` declared by the packages under `--cwd` (`packages/*` fan-out or one package dir) vs `shutil.which` reachability — each unreachable command is a named **FAIL** (with 'installed but not linked' vs 'not installed in this interpreter' via `importlib.metadata`, which cannot see isolated uv/pipx envs), a command declared by multiple packages **WARN**s as a collision, and a command resolving to another distribution's entry point — verified against this interpreter's scripts dirs, else reported as an ambiguous foreign shim — or sitting behind extra same-named PATH hits (deduped by real path; CWD-first probing on Windows) reports **WARN** shadowed |

The `automation` check's semantics are deliberately conservative:

- **Unavailable ≠ removed.** If a scheduler probe fails (`crontab -l` error, no user systemd bus), that surface's recorded tasks are carried forward — not flagged as removed and not dropped from the baseline.
- **Removal FAILs are sticky.** A task that disappears keeps FAILing every run until it returns or the removal is acknowledged — delete the task's entry inside `<data-dir>/doctor/automation-snapshot.json`, or delete the file to reset the whole baseline.
- **Secrets are scrubbed.** Credential-shaped material (`--*token*`/`--*key*`/`--*pass*`/`--*secret*`/`--*auth*` arguments, sensitive `KEY=value` assignments, `Bearer` tokens, URL userinfo) becomes `***` before anything is stored or printed — a minimal local pattern set, not the full `devin-redact` catalog.
- **Known limits:** Task Scheduler enumeration is capped at 200 tasks (a WARN fires when the listing is truncated) and a task whose `/xml` query fails is counted as unreadable WARN rather than silently healthy; shell parsing is heuristic — command substitution, globs and `$(...)`-generated paths are skipped rather than guessed.
| `publish-readiness` | can a `packages/<name>` dir ship to PyPI — literal `[project]` name/version (`dynamic = ["version"]` cannot stand in), `src/` layout, `__version__` sync, console-script targets, a staged-copy wheel build **whose artifact is opened** (declared modules must ship; wheel `__version__` must match), `pypi-publish.yml` `resolve`-arm coverage including the `mod=` output. The build runs whenever `build`+`setuptools` are importable — `--offline` included — and reports WARN `unverified` when it cannot run; `DEVIN_DOCTOR_NO_WHEEL_BUILD=1` opts out (PEP 517 hooks execute the checkout's backend). Scopes to `packages/*` under `--cwd` (or one package dir); `--package NAME` narrows the fan-out. Mixed fan-outs are expected: a member without `pyproject.toml` and without Python-package signals (`setup.py`/`setup.cfg`, top-level `.py` or an import-package dir, `.py` under `src/`) is **skipped** with a PASS note — npm/VS Code members are not PyPI candidates — unless a `pypi-publish.yml` arm or dispatch/tag reference names it, which keeps the missing-metadata FAIL |
| `deps-internals` | silently stale internal deps inside `<cwd>/.venv` — each `__editable__*.pth` target that no longer exists is a named **FAIL** (the repo moved/absorbed the package), a target resolving outside the checkout is **WARN** (venv bound to another workspace), and a non-editable `devin_*` wheel whose version differs from the same-named `packages/*/pyproject.toml` is **WARN** (stale wheel shadowing fresh source; `devin_*` dists with no checkout match are ignored as foreign tooling, editable-marked dists are never double-counted). No `.venv` → 'not detected', like a machine without Devin stores |

Privacy doctrine is strict across all checks: headers and metadata only —
counts, sizes, paths and statuses, never message content. SQLite stores
open read-only (`mode=ro`, `immutable=1` retry); corrupt files are
`FAIL store-corrupt`, empty ones `WARN empty-store` (installed but
unused), old layouts `WARN schema-old`, unrecognized ones
`WARN schema-unknown`.

### Pre-commit gate

The repo root ships `.pre-commit-hooks.yaml` — reference it from a
consumer's `.pre-commit-config.yaml`:

```yaml
- repo: https://github.com/Icaro0310/devin-explore
  rev: <tag-or-sha>
  hooks: [{ id: devin-doctor-gate }]
```

The gate runs `devin-doctor check --offline` on every commit: read-only,
fully local (no manifest fetch), blocks only on FAIL. WARN findings still
print because the hook is `verbose`. On a large data dir the disk-usage
walk is the slow part — that latency is the check itself, not a hang.

## Capability profile

```bash
devin-doctor capabilities             # JSON profile, exit 0
devin-doctor capabilities --probe-network
```

`capabilities` prints a JSON object — `{"profile": ..., "capabilities":
{...}}` — describing what the machine can do for the ecosystem (scheduler,
daemon, local LLM, containers, comms, proxy, ≥24 GiB RAM, …). The profile is
`corporate` by default (**fail-closed**); `personal` applies only when
explicitly declared via `DEVIN_ECOSYSTEM_PROFILE=corporate|personal` or
`devin-profile.json` (`{"profile": "personal"}`) in the Devin config dir —
the env var wins. All probing is local; `net.outbound`/`net.listener`
stay `"unknown"` unless `--probe-network` is passed, which performs exactly
ONE outbound TCP connect (1.1.1.1:443 or the configured `HTTPS_PROXY`, 2 s
timeout) plus a loopback bind — the only network call this tool can make.

## Works with Devin alone (Devin-only mode)

devin-doctor is a pure local diagnostic: it reads Devin's own data
directories, writes nothing except its own small state file
(`doctor/automation-snapshot.json`, used by the `automation` check's
removed-task detection), and never contacts a network service. No VM, no
Tailscale, no Ollama, no Slack — just Devin Desktop plus Python. On a
restricted or corporate machine it is the safest first tool: install, run
`devin-doctor`, read the report.

## Platform support

Tested on Windows and Linux. On Linux, session data defaults to
`$XDG_DATA_HOME/devin` (normally `~/.local/share/devin`) and UI stores default
to `$XDG_CONFIG_HOME/Devin` (normally `~/.config/Devin`). Windows stores use
`%APPDATA%\devin` and `%APPDATA%\Devin`. Use `--data-dir` and `--config-dir`
when the stores are elsewhere. macOS paths exist but are not verified.


### `devin-doctor plan` — remediation plan, still read-only

`plan` runs the same checks but emits a numbered remediation plan for every
WARN/FAIL finding (the check's `fix` suggestion) instead of a verdict —
nothing is executed; execution stays a human decision or another tool's job.
`--json` gives `{"overall", "steps": [{check, status, finding, fix}]}`.
This replaced the original `--fix` idea so doctor can keep its
"read-only, always" guarantee.

## Limitations

- **Version-bound.** Store parsing follows `devin-internals-spec` (schema
  v15–v17). A newer Devin schema reports `schema-unknown` WARN — counts
  still run where the layout is readable, but exact column semantics are
  unverified.
- **Read-only.** It suggests fixes but never touches the data dir. `--fix`
  for the safe items is planned (see `STATUS.md`).
- **Locked stores.** If Devin is running, DBs may report `SQLITE_BUSY` —
  close Devin and re-run.
- macOS paths are implemented but not verified in CI.

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```

Tests run entirely on synthetic fixtures generated by
`devin_internals.fixtures` — no real session data is ever read or required.
`scripts/make_fixture.py <dir>` builds a fixture data dir for manual runs.


## Adapters (MCP server, Devin skill, plugin)

`devin_doctor.mcp_server` exposes two read-only tools via the
`devin-doctor-mcp` entry point (`pip install 'devin-doctor[mcp]'`):
`doctor_check` (same JSON as `check --json`) and `doctor_capabilities`
(same profile as `capabilities`). The `offline` flag is scoped to each
call — it never touches the process environment.

`adapters/` is a self-contained Devin plugin root
(`adapters/.devin-plugin/plugin.json` +
`adapters/skills/devin-doctor/SKILL.md`), installed with
`devin plugins install
Icaro0310/devin-explore#packages/doctor/adapters` — it ships in the
repo, not inside the wheel. The manifest launches the server through
`uvx --from 'devin-doctor[mcp]' devin-doctor-mcp`, which resolves once
the matching PyPI release ships; until then run the source-installed
`devin-doctor-mcp` directly or point a local manifest copy at the
checkout (`uvx --from './packages/doctor[mcp]' devin-doctor-mcp`).

### MCP host configs

Same stdio server in other hosts — server name `devin-doctor`:

**Zed** — `context_servers` in `~/.config/zed/settings.json`:

```jsonc
{ "context_servers": { "devin-doctor": { "command": { "path": "devin-doctor-mcp", "args": [] } } } }
```

**JetBrains AI Assistant** — Settings → Tools → AI Assistant → Model Context Protocol → Add:

```json
{ "mcpServers": { "devin-doctor": { "command": "devin-doctor-mcp" } } }
```

**Antigravity** — `~/.gemini/config/mcp_config.json`:

```json
{ "mcpServers": { "devin-doctor": { "command": "devin-doctor-mcp" } } }
```

## When to use this

- Devin misbehaves — missing history, auth errors, disk pressure — and you want one command to localize the cause.
- You want a health check before debugging: a four-rung surface ladder for every agent session store on the machine (devin-cli, devin-chat, copilot-cli, codex-cli, vscode-chat) plus Devin-environment checks (data health, config, hooks-windows, disk, updates, automation, path-check), each with a `fix:` suggestion.
- You need a paste-able report for a bug report: `devin-doctor report --md`.
- You are on a restricted machine: it is read-only, and `--offline` keeps the whole run local (otherwise only the `updates` check makes a network call, to fetch the version manifest — it self-skips when unreachable).

## When NOT to use this

- You want it to repair things — it is read-only; `--fix` for safe items is planned.
- You need deep parsing of a schema newer than v17 — it reports `schema-unknown` WARN and keeps counting, but layout details are unverified.
- Devin is running and DBs are locked — close Devin and re-run to clear `SQLITE_BUSY`.

## FAQ

**How do I check if my Devin installation's local data is healthy?** Run `devin-doctor check`. It inspects `sessions.db`, `acp-messages/*.db`, `state.vscdb`, `credentials.toml` and disk usage, printing `PASS`/`WARN`/`FAIL` per check with a `fix:` suggestion. Exit code is `0` unless something FAILs. It also diagnoses the other agent session surfaces on the machine (Copilot, Codex, VS Code chat) in the same run — `--surface NAME` scopes that section.

**Is devin-doctor safe to run? Will it modify my data?** It is strictly read-only — it opens Devin's stores for reading and never touches the system's data or scheduled tasks; the single exception is its own state file (`doctor/automation-snapshot.json` under the data dir) that the `automation` check uses to detect tasks removed since the last run. The only network call is the `updates` check's manifest fetch (skip it with `--offline` or `DEVIN_DOCTOR_OFFLINE=1`). Config check masks credential values in output. Fixes are suggested as text, never applied.

**What does "unknown schema version" mean?** Devin's `sessions.db` schema is versioned; devin-doctor understands v15–v17 via devin-internals-spec. A newer version produces a `schema-unknown` WARN: counts still run, but the layout isn't verified — update the tool or devin-internals-spec.

## License

MIT — see [LICENSE](LICENSE).
