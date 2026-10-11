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
devin-doctor report --md              # markdown, for pasting into issues
devin-doctor capabilities             # machine capability profile, JSON
```

Each check prints `PASS`/`WARN`/`FAIL`, a one-line finding and a `fix:`
suggestion. Exit code is `0` unless something `FAIL`s (then `1`).

The seven checks:

| check | reports |
|---|---|
| `stores` | present/missing, sizes and row counts for `sessions.db`, `acp-messages/*.db`, `state.vscdb` |
| `schema` | schema version vs the supported range (15–17); layout recognition for the ledger-less stores |
| `health-of-data` | empty sessions, orphan `message_nodes`, sessions idle > `--stale-days`, `SQLITE_BUSY` locks |
| `config` | `credentials.toml` presence + validity (**values masked**); `.devin/` hooks/MCP config sanity |
| `hooks-windows` | hooks/MCP commands that would pop a visible console window on Windows (`cmd /c` without a hidden wrapper, `powershell` without `-WindowStyle Hidden`, direct `.bat`/`.ps1`, `python.exe` vs `pythonw.exe`) — each with file, hook event and a wrapper fix |
| `disk` | total data-dir size, largest DBs, `acp-messages` accumulation trend |
| `updates` | installed tool versions vs the DevKit manifest (the only check that touches the network — fetches `manifest.json` from GitHub; self-skips when unreachable, or force off with `DEVIN_DOCTOR_OFFLINE=1` / `--offline`) |

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
directories, writes nothing, and never contacts a network service. No VM, no
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
  v15–v17). A newer Devin schema reports FAIL — "unknown version" — by
  design, rather than silently misreading.
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

## When to use this

- Devin misbehaves — missing history, auth errors, disk pressure — and you want one command to localize the cause.
- You want a health check before debugging: seven checks (stores, schema, data health, config, hooks-windows, disk, updates), each with a `fix:` suggestion.
- You need a paste-able report for a bug report: `devin-doctor report --md`.
- You are on a restricted machine: it is read-only, and `--offline` keeps the whole run local (otherwise only the `updates` check makes a network call, to fetch the version manifest — it self-skips when unreachable).

## When NOT to use this

- You want it to repair things — it is read-only; `--fix` for safe items is planned.
- Your Devin schema is newer than v17 — it reports "unknown version" FAIL rather than misread.
- Devin is running and DBs are locked — close Devin and re-run to clear `SQLITE_BUSY`.

## FAQ

**How do I check if my Devin installation's local data is healthy?** Run `devin-doctor check`. It inspects `sessions.db`, `acp-messages/*.db`, `state.vscdb`, `credentials.toml` and disk usage, printing `PASS`/`WARN`/`FAIL` per check with a `fix:` suggestion. Exit code is `0` unless something FAILs.

**Is devin-doctor safe to run? Will it modify my data?** It is strictly read-only — it opens Devin's stores for reading and writes nothing to the data dir. The only network call is the `updates` check's manifest fetch (skip it with `--offline` or `DEVIN_DOCTOR_OFFLINE=1`). Config check masks credential values in output. Fixes are suggested as text, never applied.

**What does "unknown schema version" mean?** Devin's `sessions.db` schema is versioned; devin-doctor understands v15–v17 via devin-internals-spec. A newer version produces a deliberate FAIL instead of a silent misread — update the tool or devin-internals-spec.

## License

MIT — see [LICENSE](LICENSE).
