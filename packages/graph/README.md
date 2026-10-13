<div align="center">

<img src="assets/banner.svg" alt="devin-graph" width="100%"/>

<a href="https://github.com/Icaro0310/devin-explore/actions/workflows/test-graph.yml"><img src="https://github.com/Icaro0310/devin-explore/actions/workflows/test-graph.yml/badge.svg" alt="ci"/></a>

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
> For: Developers, AI engineers, Data Scientists  
> Interface: CLI / Python library  
> Path: Data Scientists · step 1/4 — before `devin-search`
<!-- DEVIN-ECO:END -->

# devin-graph

> **Unofficial community project.** Not affiliated with, endorsed by, or
> sponsored by Cognition AI. "Devin" is a trademark of Cognition AI.

**[Linux](README.linux.md)** · **[Personal Windows](README.windows.md)** · **[Corporate Windows](README.corporate-windows.md)**

Part of the [awesome-devin](https://github.com/Icaro0310/awesome-devin) ecosystem: the curated hub for the devin-* tools.

A knowledge graph over Devin sessions: sessions, projects, files and tools
become nodes — queryable ("which sessions touched file X?", "which tools does
project Y depend on?") and exportable for visualization.

## The problem

After dozens of Devin sessions you lose the thread: which sessions touched
that config file, which tools a project relies on, which projects share the
same files. The data exists in `sessions.db` (`tool_call_state` records every
call the agent made) but there is no way to query across sessions — only
per-session scrolling in the UI.

## Prior art

- Code-knowledge graphs (Sourcegraph, Glean-style indexes) map *code*, not
  *agent activity*; they don't know what your AI sessions touched.
- `devin-internals-spec` provides the schema + typed read-only parsers this
  tool builds on; `devin-history` exports the same store to notes but has no
  cross-session structure.
- You could hand-write SQL — but the `tool_call_json` payload format is
  undocumented and changes; here it's extracted defensively in one place.

## What makes it Devin-native

Edges come from **ground truth, not prose**: `file_touched` edges are
extracted from `tool_call_state` payloads (file paths in fs/terminal tool
calls) and anchored to the session's `working_directory`. That data simply
doesn't exist outside Devin's store — remove Devin and there is no graph to
build. Schema drift is gated by `devin-internals-spec`'s version detector.

## Install

Requires Python ≥ 3.10 and `pipx` or `uv`. Per-OS setup lives in the platform guides: [Linux](README.linux.md) · [Personal Windows](README.windows.md) · [Corporate Windows](README.corporate-windows.md).

## Usage

```bash
# build the graph (auto-detects the Devin sessions.db for your OS) — safe to
# re-run: unchanged sessions are skipped
devin-graph build --graph graph.db

# canned queries
devin-graph query file "src/app.py"        --graph graph.db
devin-graph query tool "execute"           --graph graph.db
devin-graph query project "my-repo"        --graph graph.db
devin-graph query shared-files             --graph graph.db
devin-graph query projects-graph           --graph graph.db --json

# D3-friendly dump: {"meta", "nodes", "edges"}
devin-graph export --format json --graph graph.db --out graph.json
```

Matching is forgiving (`src/app.py` finds `/repo/alpha/src/app.py`);
everything has `--json`. `query shared-files` lists every file touched
by two or more distinct projects, with the project and session lists —
exportable as JSON like the other queries. The source DB is opened
`mode=ro` and never written — tests assert its hash is unchanged.

## Works with Devin alone (Devin-only mode)

devin-graph builds `graph.db` locally from Devin's session stores — the whole
pipeline is offline. Note that the derived database contains the same
sensitive content as the sessions themselves (prompts, paths, commands):
keep it private like the originals.

## Platform support

Tested on **Windows and Linux** (`windows-latest` + `ubuntu-latest` in CI).
The CLI session DB is auto-detected: `%APPDATA%/devin/cli/sessions.db` on
Windows and `$XDG_DATA_HOME/devin/cli/sessions.db` on Linux (default
`~/.local/share/devin/cli/sessions.db`). A legacy `~/.config/devin` location
is also checked. Pass `--sessions-db` to override.

`commit` nodes + `produced`/`referenced` edges attribute sessions to git SHAs seen in tool calls (exact when the SHA appears in a `git commit`/`git push` call, `seen` otherwise). `devin-graph sql "SELECT ..."` runs read-only SQL over graph.db (SELECT/WITH only, `query_only` pragma).

`--vscdb` (auto-detected on build) adds GUI coverage: `gui_session` nodes keyed by the generated session slug, `gui_workspace` edges to their workspace project, enriched with `lastAccessed` when a `resourceToSpace` editor URI links a space to the slug. Best-effort: only the observed `windsurfSpace.*` keys are read; unknown/malformed keys are skipped. Pass `--vscdb none` to disable.

`devin-graph view --out file.html` renders the graph as a self-contained HTML page (embedded JSON + vanilla-JS force layout — zero CDN, works fully offline). `--limit` caps nodes (highest-degree kept, flagged TRUNCATED in the header).

## Limitations

- **Schema-gated.** Only `sessions.db` schema v15–v17; newer fails loudly
  (update `devin-internals-spec` first).
- **Heuristic path extraction.** `tool_call_*_json` is an unstable, opaque
  format: paths are collected from path-ish keys plus path-looking tokens in
  command strings — best-effort, not contractual. A brand-new tool payload
  shape may yield partial edges.
- **CLI sessions only (M1).** GUI sessions (`acp-messages/*.db`) and
  `state.vscdb` are planned for M2.
- **Not a code index.** Nodes are files *the agent touched*, not repo
  contents; no symbol/AST knowledge.
- **Read-only by design** on Devin stores; `graph.db` is the only thing it
  writes.

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```

Fixtures are generated at test time by `devin_internals.fixtures` (real v17
DDL, synthetic rows) — no binary fixtures are committed. See
[docs/SPEC.md](docs/SPEC.md) for the graph model and
[STATUS.md](STATUS.md) for the roadmap.

## When to use this

- You need cross-session questions: which sessions touched file X, which
  tools project Y depends on, which projects share files.
- You want edges from ground truth — real `tool_call_state` payloads, not
  heuristics over chat text.
- You want a graph export to visualize agent activity
  (`devin-graph export --format json` produces a D3-ready dump).
- You want incremental builds — re-running `devin-graph build` skips
  unchanged sessions.

## When NOT to use this

- You need a code index — nodes are files the agent *touched*, not repo
  contents; there is no symbol/AST knowledge.
- You need message-content search (use `devin-search`) or activity/context metrics
  (use `devin-metrics`).
- You need GUI session data — M1 covers CLI `sessions.db` only;
  `acp-messages` is planned for M2.

## FAQ

**What is devin-graph?** A local tool that turns Devin's session database
into a knowledge graph: sessions, projects, files, and tools become nodes,
with edges extracted from real tool-call payloads. You query it with
`devin-graph query file|tool|project|shared-files` and export it as JSON
for visualization.

**How is devin-graph different from devin-search?** devin-search finds text:
ranked full-text hits inside session content. devin-graph finds structure:
which sessions touched which files, which tools which projects use, and
which projects share files — relationships, not text matches.

**Does it write to Devin's databases?** No. The source `sessions.db` is
opened `mode=ro` and never written — the test suite asserts its hash is
unchanged. The only file devin-graph creates is its own `graph.db`.

## License

MIT — see [LICENSE](LICENSE).
