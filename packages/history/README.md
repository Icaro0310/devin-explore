<div align="center">

<img src="assets/banner.svg" alt="devin-history" width="100%"/>

<a href="https://github.com/Icaro0310/devin-explore/actions/workflows/test-history.yml"><img src="https://github.com/Icaro0310/devin-explore/actions/workflows/test-history.yml/badge.svg" alt="tests"/></a>

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
> Path: Data Scientists · step 3/4 — after `devin-search`, before `devin-metrics`
<!-- DEVIN-ECO:END -->


# devin-history

> **Unofficial community project.** Not affiliated with, endorsed by, or
> sponsored by Cognition AI. "Devin" is a trademark of Cognition AI.

**[Linux](README.linux.md)** · **[Personal Windows](README.windows.md)** · **[Corporate Windows](README.corporate-windows.md)**

Part of the [awesome-devin](https://github.com/Icaro0310/awesome-devin) ecosystem: the curated hub for the devin-* tools.

Export and audit Devin Desktop session history — turns the local
`sessions.db` into Obsidian-ready Markdown notes, a searchable JSON dump,
and an audit report. GUI session metadata (`state.vscdb`) exports as
metadata notes too.

## The problem

Devin Desktop keeps your session history in a local SQLite database
(`%APPDATA%/devin/cli/sessions.db` on Windows, or
`$XDG_DATA_HOME/devin/cli/sessions.db` on Linux; default
`~/.local/share/devin/cli/sessions.db`) — and nowhere else. There is no export
button: once a session scrolls out of the UI (or is pruned by the app), the
prompts, replies and tool calls are effectively gone. You can't search old
sessions, can't answer "what did I ask Devin to do last month", and can't
audit how the agent has been used across projects.

## Prior art

- **tokmesh** and **UniSessions** document/parse `sessions.db`; the schema
  itself is tracked by [`devin-internals-spec`](https://github.com/Icaro0310/devin-internals-spec).
- This project ports two proven scripts that lived in the orchestrator
  workspace (`legacy/`): `devin-history-export.py` (DB → Obsidian notes) and
  `audit_sessions.py` (lifetime audit → CSV + report). They are rewritten
  as library modules — same logic, real tests.
- You can also just run `sqlite3` queries by hand — but then you guess the
  schema on every Devin update.

## What makes it Devin-native

It understands Devin's real stores instead of raw-SQL guessing: it opens
`sessions.db` through `devin-internals-spec`'s typed parsers and **refuses
loudly** when the schema version is one it has never seen (the store has
had 17 migrations already). The export knows about `hidden` sessions,
interrupted threads (last node = user), `tool_call_state` failures and
epoch-millisecond timestamps — details a generic SQLite dump misses.

## Install

Python ≥ 3.10 required; install with `uv` (recommended) or `pipx`.

```bash
uv tool install devin-history
```

or with `pipx` (alternative):

```bash
pipx install devin-history
```

## Usage

```bash
# quick session table (auto-detects %APPDATA%/devin/cli/sessions.db)
devin-history list

# one Obsidian note per session + index.md — safe to re-run (incremental)
devin-history export --out ~/ObsidianVault/Sessions

# searchable JSON dump instead of Markdown
devin-history export --out dump/ --format json

# audit: status/type/project/period groupings + anomalies, optional CSV
devin-history audit --csv audit.csv

# GUI sessions: metadata notes (slug, workspace, folders, lastUpdated) +
# index.json — the GUI keeps no local transcript, so metadata is all there is
devin-history export-gui --out ~/ObsidianVault/Sessions/gui

# everything has --json; point at a specific DB with --sessions-db/--vscdb
devin-history audit --sessions-db path/to/sessions.db --json
devin-history export-gui --vscdb path/to/state.vscdb --out gui-notes/
```

No Devin installed? Try it on a synthetic fixture (stdlib-only, no Devin
data involved):

```bash
pipx install "devin-internals-spec==0.3.0"
devin-inspect make-fixture /tmp/fx
devin-history export --sessions-db /tmp/fx/cli/sessions.db --out /tmp/notes
```

Notes are idempotent: each file embeds the session's `last_activity`
marker, so re-runs skip unchanged sessions (`--all` forces a rewrite,
`--dry-run` previews). The `index.md`/`index.json` at the root carries a
stats block — total sessions, date span, per-project and user/assistant/tool
message totals — alongside the grouped links.

`export-gui` reads the Electron `state.vscdb` store (auto-detected under
`User/globalStorage/` of the Devin config dir, override with `--vscdb`)
and writes one note per `windsurfSpace.sessionWorkspace/*` binding: slug,
label, backend, workspaceId, folders, `lastUpdated`, and `lastAccessed`
when derivable via `windsurfSpace.resourceToSpace` + `windsurfSpace.metadata`.
It is read-only on `state.vscdb`, embeds the same provenance
(`machine_id`/`profile`), and writes an `index.json` index.

## Works with Devin alone (Devin-only mode)

Everything devin-history does happens on your machine: it reads
`sessions.db` and writes Markdown/JSON exports to a folder you choose. No
network, no external service — the Obsidian-style note layout is just a
format, Obsidian itself is not required.

One caution for restricted machines: exports contain raw prompts, paths and
commands, which may include secrets. Keep the export folder private, define a
retention rule, and run
[`devin-redact`](https://github.com/Icaro0310/devin-state) before sharing an
export.

## Platform support

Tested on **Windows and Linux** (`windows-latest` + `ubuntu-latest` in CI).
The CLI session database is auto-detected: `%APPDATA%/devin/cli/sessions.db`
on Windows and `$XDG_DATA_HOME/devin/cli/sessions.db` on Linux (default
`~/.local/share/devin/cli/sessions.db`). A legacy `~/.config/devin` location
is also checked. macOS uses `~/Library/Application Support/devin/`. Override
with `--sessions-db` (see Usage). The GUI `state.vscdb` is auto-detected
under `<config>/User/globalStorage/state.vscdb` (`Devin`/`devin` dir names);
override with `--vscdb`.


### Incremental export from a hook (HI-2 — no daemon)

`export --session-id ID` exports a single session (unique prefix ok) and
merges its entry into the existing index instead of rebuilding it.
`export --from-hook` reads `{"session_id": ...}` from the stdin payload a
SessionEnd hook pipes in — fail-soft (no payload → exit 0, nothing
written). Register once via the hooks dispatcher:

    python tools/hooks_dispatch.py register SessionEnd history-export \
      "devin-history export --out ~/notes/devin-history --from-hook"

Each ended session then lands as a note automatically; a periodic full
`export` keeps the index complete.

## Limitations

- **Schema-gated.** Only `sessions.db` schema v15–v17 is accepted; anything
  newer fails loudly rather than misreading (update `devin-internals-spec`
  first).
- **Opaque payloads.** `chat_message` and `tool_call_*_json` inner formats
  are undocumented and unstable — decoding is best-effort and tolerant, not
  contractual.
- **Inferred status.** The DB has no final-state column; "completed" /
  "interrupted" / "abandoned" are heuristics (see `docs/SPEC.md` §4).
- **No billing.** Token/cost data is not stored locally; only
  `num_tokens_preceding` (context size) exists.
- **GUI sessions are metadata-only.** `export-gui` exports the
  session↔workspace bindings kept in `state.vscdb` (slug, label, folders,
  timestamps) — the GUI stores no transcript locally. GUI transcripts under
  `User/acp-messages/*.db`, session locks and log correlation are planned
  for M2.
- **Read-only by design** — the tool never writes to Devin's stores.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Fixtures are generated at test time by `devin_internals.fixtures` (real v17
DDL, synthetic rows) — no binary fixtures are committed.

## When to use this

- You want your Devin session history out of the app before sessions scroll
  away or are pruned — there is no built-in export button.
- You keep an Obsidian vault (or any Markdown folder) and want one note per
  session plus an `index.md`, idempotent across re-runs.
- You need a lifetime audit: groupings by status, task type, project and
  period, plus anomaly detection (empty, orphan, long-running sessions).
- You want a deterministic, read-only export you can schedule — nothing is
  ever written back to Devin's stores.

## When NOT to use this

- You need instant ranked search rather than a static export — use
  [`devin-search`](https://github.com/Icaro0310/devin-explore) on top of the
  same database.
- You need GUI session *transcripts* — `export-gui` exports the metadata
  bindings in `state.vscdb`; the `acp-messages/*.db` transcript stores are
  M2.
- Your `sessions.db` schema is newer than v17 — the tool refuses loudly
  instead of misreading it; update `devin-internals-spec` first.

## FAQ

**What is devin-history?** A CLI that exports Devin's local `sessions.db`
into Obsidian-ready Markdown notes, a JSON dump, or an audit report. It
auto-detects the database on Windows, Linux and macOS and never writes to
Devin's stores.

**Is it safe to re-run the export?** Yes. Each note embeds the session's
`last_activity` marker, so unchanged sessions are skipped on re-runs.
`--all` forces a rewrite and `--dry-run` previews without writing.

**Does it send my session data anywhere?** No. Everything happens on your
machine: it reads the local database and writes files to a folder you
choose. Note that exports contain raw prompts and paths — keep the output
folder private or run `devin-redact` before sharing.

**Why does it refuse to read my database?** Because the schema version is
outside the supported v15–v17 range. The store has had 17 migrations
already; devin-history fails loudly on unknown versions rather than
silently misparsing your history.

## License

MIT — see [LICENSE](LICENSE).
