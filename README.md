# devin-explore

<div align="center">

<a href="https://github.com/Icaro0310/devin-explore/actions/workflows/ci.yml"><img src="https://github.com/Icaro0310/devin-explore/actions/workflows/ci.yml/badge.svg" alt="ci"/></a>
<a href="https://www.bestpractices.dev/projects/15340"><img src="https://www.bestpractices.dev/projects/15340/badge" alt="OpenSSF Best Practices"/></a>
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

<!-- DEVIN-WHERE:BEGIN -->
## Where this fits

- **Job:** Understand
- **Product:** [`devin-explore`](https://github.com/Icaro0310/devin-explore)
- **Packages:** `doctor` · `graph` · `history` · `pm` · `search`
- **Mode:** read-only
- **Foundation:** [`devin-internals-spec`](https://github.com/Icaro0310/devin-internals-spec)
- **Ecosystem:** [`awesome-devin`](https://github.com/Icaro0310/awesome-devin) · registry: [`devin-powerups`](https://github.com/Icaro0310/devin-powerups)
<!-- DEVIN-WHERE:END -->

Understand your Devin sessions: diagnose the local installation, export
and search session history, and query a knowledge graph of projects,
files, tools and decisions — all local, no telemetry.

| Package | PyPI | What it does |
|---|---|---|
| [`packages/doctor`](packages/doctor) | [![devin-doctor](https://img.shields.io/pypi/v/devin-doctor)](https://pypi.org/project/devin-doctor/) | Diagnose a Devin Desktop install: stores, schema versions, hooks, MCP servers, disk usage |
| [`packages/history`](packages/history) | [![devin-history](https://img.shields.io/pypi/v/devin-history)](https://pypi.org/project/devin-history/) | Export, audit and search session history — Obsidian-ready markdown, JSON, SQLite-aware |
| [`packages/search`](packages/search) | [![devin-search](https://img.shields.io/pypi/v/devin-search)](https://pypi.org/project/devin-search/) | Full-text search across all Devin sessions |
| [`packages/graph`](packages/graph) | [![devin-graph](https://img.shields.io/pypi/v/devin-graph)](https://pypi.org/project/devin-graph/) | Knowledge graph over sessions: projects, files, tools, decisions as nodes |
| [`packages/pm`](packages/pm) | [![devin-pm](https://img.shields.io/pypi/v/devin-pm)](https://pypi.org/project/devin-pm/) | Turn session history into milestones, status reports and per-repo task tracking |

> **Renamed (Oct 2026):** this repository moved from `Icaro0310/devin-doctor` to `Icaro0310/devin-explore` when it became the `devin-explore` product workspace. PyPI packages and console scripts keep their names; stars, issues and history are preserved by the redirect.

## Layout

```
packages/<name>/   one installable package each (src layout, own tests)
```

Each package ships independently: a tag `<pkg>-vX.Y.Z` publishes only
that package. CI is scoped per path — a change under `packages/graph/`
runs only the graph suite.

The standalone `devin-history`, `devin-search`, `devin-graph` and
`devin-pm` repositories were absorbed into this workspace (F4.3); their
histories are preserved under `packages/` and the old repos are archived
with pointers here.

## Platform support

All packages support Linux, macOS and Windows. Per-package guides live
under `packages/<name>/`.

> **Unofficial community project.** Not affiliated with, endorsed by, or
> sponsored by Cognition AI. "Devin" is a trademark of Cognition AI.
