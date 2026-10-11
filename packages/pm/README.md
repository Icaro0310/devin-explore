<div align="center">

<img src="assets/banner.svg" alt="devin-pm" width="100%"/>

<a href="https://github.com/Icaro0310/devin-explore/actions/workflows/test-pm.yml"><img src="https://github.com/Icaro0310/devin-explore/actions/workflows/test-pm.yml/badge.svg" alt="ci"/></a>


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
> For: Local-first ops, Maintainers  
> Interface: CLI / Registry  
> Path: Local-first ops · step 2/5 — after `devin-explore`, before `devin-office`
<!-- DEVIN-ECO:END -->


# devin-pm

> **Unofficial community project.** Not affiliated with, endorsed by, or
> sponsored by Cognition AI. "Devin" is a trademark of Cognition AI.

**[Linux](README.linux.md)** · **[Personal Windows](README.windows.md)** · **[Corporate Windows](README.corporate-windows.md)**

Part of the [awesome-devin](https://github.com/Icaro0310/awesome-devin) ecosystem: the curated hub for the devin-* tools.

A project manager over your Devin sessions — it reads `sessions.db`
(and optionally the GUI's `state.vscdb`), groups work per
repository/project, and generates status reports, milestones and a
machine-readable project registry.

## The problem

Every Devin CLI session is recorded in a local `sessions.db`, but the app
gives you no project-level view of it. After a few weeks the database holds
a hundred sessions and you cannot answer the basic questions: *which repos
did I actually work on? what is the state of project X? which milestones
are still open?* The history is all there — it is just locked in a flat
session list with no grouping, no rollups, no output you can hand to a
report or another tool.

## Prior art

The orchestrator workspace proved the idea with a one-off audit script
(`audit_sessions.py` → `sessions_report.md` / `sessions_summary.csv`): a
lifetime audit of 100+ sessions grouped by `working_directory`. This
project turns that script into a maintained tool on top of
[`devin-internals-spec`](https://github.com/Icaro0310/devin-internals-spec)'s
schema-gated `SessionsStore` parser — it does not re-implement the DB
reading or re-invent the grouping.

## What makes it Devin-native

*It's a project manager over your Devin sessions — it reads `sessions.db`
and gives you per-repo status, milestones and a registry.*

1. **Side-by-side:** Devin lists sessions but cannot group them per
   repository, tag one as a milestone marker, or emit a project registry —
   `devin-pm` does what the base tool cannot do at all.
2. **No-Devin:** remove Devin and there is no `sessions.db` — the extra
   disappears entirely.
3. **Safe by construction:** parsing goes through `devin-internals-spec`'s
   schema-version gate, so a new Devin migration fails loudly instead of
   silently corrupting your rollup.

## Install

Python ≥ 3.10 required; install with `uv` (recommended) or `pipx`.

```bash
uv tool install devin-pm
```

or with `pipx` (alternative):

```bash
pipx install devin-pm
```

For development:

```bash
pip install -e ".[dev]"
pytest
```

## Usage

```bash
devin-pm status                          # per-project rollup table
devin-pm status --json                   # same, machine-readable
devin-pm status --vscdb                  # also merge GUI sessions (auto-detect)
devin-pm status --vscdb path/state.vscdb # pin the GUI store
devin-pm report --project my-repo        # markdown status report
devin-pm report                          # global report, all projects
devin-pm report --project x --out x.md   # write to file
devin-pm milestones --project my-repo    # milestone list + done %
devin-pm registry --out registry.json    # machine-readable registry
devin-pm verify                          # diff tracked projects vs hub registry
devin-pm verify --json                   # same, machine-readable
```

`--sessions-db PATH` overrides the database location on every subcommand;
otherwise `devin-pm` auto-detects `%APPDATA%/devin/cli/sessions.db`
(`DEVIN_PM_SESSIONS_DB` env var also works). All reads are read-only.

### GUI sessions (`--vscdb`)

The Desktop app stores session→workspace bindings in the Electron store
`<config>/Devin/User/globalStorage/state.vscdb` — keys of the form
`windsurfSpace.sessionWorkspace/<backend>/<slug>` holding JSON
`{workspaceId, label, folders[], lastUpdated}`. Passing `--vscdb` on any
subcommand merges them into the same project grouping (PM-1):

```bash
devin-pm status --vscdb                # bare flag: auto-detect
devin-pm report --vscdb PATH           # pin a specific state.vscdb
```

GUI sessions have no transcript — they group by `workspaceId` (falling
back to the first `folders[]` entry, then `label`) and are marked
gui-sourced everywhere: status `gui` in reports, a `GUI` column in the
`status` table, a `source` column in session tables, and `gui_sessions`
counts in `--json`/`registry` output. A bare `--vscdb` with no store
found warns and continues with `sessions.db` only; an explicit missing
`PATH` exits `2`. `DEVIN_PM_STATE_VSCDB` overrides auto-detection. The
store is opened `mode=ro` — read-only, always.

### Path normalization (grouping keys)

The same repo may be recorded as `C:\Users\X\repo`, `/c/Users/X/repo`
(MSYS/Git-Bash) or `\\wsl.localhost\Ubuntu\home\u\repo` (WSL UNC). Before
PM-3 that split one project into three groups. Grouping keys now fold:
`C:\x` ⇄ `C:/x` ⇄ `/c/x` ⇄ `/cygdrive/c/x` (drive-rooted paths are
case-folded — the Windows FS is case-insensitive), WSL UNC prefixes map
to the in-distro POSIX path, and separators/trailing slashes collapse.
Output keeps the original path; only the grouping key is normalized.
POSIX paths keep their case (`/home/u/Foo` ≠ `/home/u/foo`).

### Verify against the ecosystem registry

`devin-pm verify` cross-checks the projects pm tracks against the
authoritative ecosystem catalog in `devin-powerups/registry.json`:

```bash
devin-pm verify --registry ../devin-powerups/registry.json
```

`--registry` defaults to `../devin-powerups/registry.json` relative to the
current directory (then the checkout sibling of this package). The report
has three sections:

- **in registry but unknown to pm** — cataloged repos with no sessions on
  this machine (informational, never counts as drift);
- **tracked by pm but missing from registry** — projects with sessions that
  the registry does not list. Entries count as drift only when they look
  ecosystem-shaped (`devin-*` name or a checkout under the hub's parent
  directory); others are tagged `[non-ecosystem]`;
- **field drift** — for matched repos, `name`/`description` read from the
  checkout's `pyproject.toml` and `url` from the git `origin` remote vs the
  registry values. Unobservable fields (no pyproject, no remote) are
  skipped, never guessed.

`--pm-registry FILE` verifies a saved `devin-pm registry --out` document
instead of opening `sessions.db`. Exit code: `1` on drift, `0` when clean —
usable as a maintenance gate. Everything is local and read-only
(`sessions.db`, `registry.json`, `pyproject.toml`, `.git/config`).

### Milestones

Tag a session by naming its title `milestone: <name>` — that marks a
milestone in the session's project; archiving (hiding) the session marks
it done. Or list them manually in `milestones.json` at the project root:

```json
{"milestones": [{"name": "M1 — core", "done": true}, "M2 — polish"]}
```

File entries win over session-detected ones on name collision.

### Exit codes

`0` ok · `1` read/parse error (`verify`: also drift found) · `2` missing
db / unknown project / missing inputs.

## Works with Devin alone (Devin-only mode)

devin-pm computes its reports straight from the local `sessions.db`
(`%APPDATA%\devin\cli\sessions.db` on Windows,
`~/.local/share/devin/cli/sessions.db` on Linux). Output goes to your
terminal or a local file — nothing external is contacted, and no VM, message
queue or model server is involved.

## Platform support

Tested on **Windows and Linux** (`windows-latest` + `ubuntu-latest` in CI).
Devin's `sessions.db` is auto-detected per platform — `%APPDATA%\devin\` on
Windows, `~/.local/share/devin/` (`XDG_DATA_HOME`) on Linux with
`XDG_CONFIG_HOME` and `~` fallbacks (same multi-root order as
`devin-history` and `state.vscdb` detection),
`~/Library/Application Support/devin/` on macOS. Override with the
`DEVIN_PM_SESSIONS_DB` env var (see Usage). The GUI `state.vscdb` lives at
`<config>/Devin/User/globalStorage/state.vscdb` (`%APPDATA%` on Windows,
`XDG_CONFIG_HOME`/`~/.config` on Linux); override with
`DEVIN_PM_STATE_VSCDB`.

## Limitations

- **Private, volatile internals.** `sessions.db` is an implementation
  detail of Devin; parsing is gated on the known schema versions (15–17)
  and refuses anything newer rather than guessing.
- **Cost is best-effort.** The DB does not record billing in a documented
  field; `cogs_json` is unstable. Where no recognizable cost field exists,
  reports show `-` and the registry emits `null` — unknown, not zero.
- **GUI coverage is bindings-only.** `--vscdb` reads session→workspace
  bindings from `state.vscdb`; GUI transcripts (`acp-messages/*.db`) are
  not covered, so gui sessions carry no cost/milestone detail beyond a
  `milestone:` label.
- **Read-only.** This project never writes to Devin's databases; the only
  files it writes are the ones you ask for (`--out`, `milestones.json` is
  yours to author).
- **Grouping normalizes spellings, not machines.** `C:\x` ⇄ `/c/x` and
  WSL-UNC prefixes fold into one key, but `/home/u/repo` vs
  `c:/users/u/repo` stay distinct — nothing in the path proves they are
  the same directory. POSIX paths keep their case.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Fixtures-first TDD — see [docs/SPEC.md](docs/SPEC.md) for the data
contracts and [CONTRIBUTING.md](CONTRIBUTING.md) for ground rules.

## When to use this

- You have weeks of Devin sessions and want a per-repo rollup — which
  projects exist, their session counts, latest activity, status — without
  scrolling the app's flat session list.
- You want Markdown status reports or a JSON registry to feed docs,
  dashboards or other tools (`devin-pm registry --out registry.json`).
- You track milestones and want them detected automatically from
  `milestone: <name>` session titles, or curated in a `milestones.json`
  file at the project root.
- You want strictly read-only rollups that fail loudly on unknown
  `sessions.db` schema versions instead of silently misreading them.

## When NOT to use this

- You need GUI session transcripts — `--vscdb` covers the `state.vscdb`
  workspace bindings (which GUI session worked on which workspace), not
  the `acp-messages/*.db` message stores.
- You need reliable cost or billing rollups — the DB has no documented cost
  field; reports show `-`/`null` where it is unknown, never an estimate.
- You need live session state — devin-pm reports on the `sessions.db`
  snapshot at read time; for live activity see
  [`devin-office`](https://github.com/Icaro0310/devin-control).

## FAQ

**What is devin-pm?** A CLI that turns Devin's flat `sessions.db` into a
project-management view: per-repository status tables, Markdown reports,
milestone tracking and a machine-readable registry. It is read-only — the
only files it writes are the reports you ask for.

**How are sessions grouped into projects?** By working directory: each
session records where it ran, and sessions sharing that path become one
project. The grouping key normalizes path spellings — `C:\x`, `/c/x` and
`/cygdrive/c/x` fold together (case-insensitive on drive-rooted paths),
and `\\wsl.localhost\<distro>\…` maps to the in-distro POSIX path — while
POSIX paths keep their case. With `--vscdb`, GUI session→workspace
bindings join the same grouping and are marked `gui`.

**How do I mark a milestone?** Name a session's title `milestone: <name>` —
that marks it in the session's project, and archiving (hiding) the session
marks it done. Alternatively, list milestones in `milestones.json` at the
project root; file entries win on name collision.

**Why does my report show `-` for cost?** Because `sessions.db` does not
record billing in a documented field. Where no recognizable cost exists,
devin-pm reports `null`/`-` — unknown — rather than printing a misleading
zero.

## License

MIT — see [LICENSE](LICENSE).
