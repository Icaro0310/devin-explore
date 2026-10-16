<div align="center">

<img src="assets/banner.svg" alt="devin-search" width="100%"/>

<a href="https://github.com/Icaro0310/devin-explore/actions/workflows/test-search.yml"><img src="https://github.com/Icaro0310/devin-explore/actions/workflows/test-search.yml/badge.svg" alt="ci"/></a>


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
> Interface: CLI  
> Path: Data Scientists · step 2/4 — after `devin-graph`, before `devin-history`
<!-- DEVIN-ECO:END -->


# devin-search

> **Unofficial community project.** Not affiliated with, endorsed by, or
> sponsored by Cognition AI. "Devin" is a trademark of Cognition AI.

**[Linux](README.linux.md)** · **[Personal Windows](README.windows.md)** · **[Corporate Windows](README.corporate-windows.md)**

Part of the [awesome-devin](https://github.com/Icaro0310/awesome-devin) ecosystem: the curated hub for the devin-* tools.

Full-text search across all your agent sessions — find that command, that
error message, that file path or that decision from months ago in under a
second. Devin (`sessions.db`, `acp-messages/*.db`), GitHub Copilot CLI,
OpenAI Codex CLI and VS Code chat sessions all land in one index.

## The problem

Devin keeps your entire session history in local SQLite stores
(`sessions.db`, `User/acp-messages/*.db`) — but offers no way to search
across them. You remember Devin fixed a flaky test or ran a specific
`kubectl` command three weeks ago, and the only way back is scrolling
through sessions one by one. Generic `grep` over the raw databases mostly
hits JSON noise and knows nothing about who said what.

## Prior art

- Full-text search over chat/agent history is well-established:
  everything here leans on SQLite's built-in **FTS5** engine with
  **BM25** ranking — the same approach used by ripgrep-style tools,
  mail clients and `sqlite-utils`.
- **tokmesh** and **UniSessions** document/parse the CLI `sessions.db`;
  the schema itself is tracked by
  [`devin-internals-spec`](https://github.com/Icaro0310/devin-internals-spec),
  which this project uses for versioned, read-only access.
- **devin-history** (sibling repo) exports sessions to Markdown/JSON;
  devin-search complements it with instant ranked lookup instead of a
  static dump.

## What makes it Devin-native

Results are **role-tagged and session-linked**, not raw grep hits. The
indexer understands Devin's real message structure through
`devin-internals-spec`'s typed parsers: user prompts vs assistant replies
vs tool calls (including `prompt_history` shell commands and GUI
`acp-messages` sessions), each stamped with its working directory and a
`ref` back to the exact source row. And because the store schema has had
17 migrations already, indexing **fails loudly** on an unknown schema
version instead of silently misreading it.

## Install

Requires Python ≥ 3.10 and `pipx` or `uv`: `uv tool install devin-search` or `pip install devin-search`. Per-OS setup lives in the platform guides: [Linux](README.linux.md) · [Personal Windows](README.windows.md) · [Corporate Windows](README.corporate-windows.md).


## Usage

```bash
# build/update the index (auto-detects Devin's stores; safe to re-run —
# only new rows are indexed each time)
devin-search index

# search everything
devin-search query "kubectl delete pod"

# filters: role, project, date, limit — --json on every command
devin-search query "TypeError" --role assistant --project myrepo
devin-search query "migration" --since 2026-09-01 --limit 5 --json

# link hits to devin-history export notes
devin-search query "kubectl" --history-dir ~/notes/devin-history

# deterministic index: sessions.db only, never the auto-detected dirs
# (--no-acp, --no-copilot, --no-codex and --no-vscode-chat each disable
# one source; --copilot-dir/--codex-dir/--vscode-chat-dir pin it to one
# root instead of auto-detecting)
devin-search index --no-acp --no-copilot --no-codex --no-vscode-chat

# zero-hit stats from the local query log (evidence for/against semantic
# search — SE-1's trigger; log is opt-out via query --no-log)
devin-search misses
```

Hits print as `WHEN · ROLE · PROJECT · SESSION · SNIPPET` with the match
wrapped in `«»`; each hit carries a `ref` (e.g. `node:1234`,
`devin-chat:file.db:7`, `codex-cli:…/rollout-x.jsonl:41`,
`vscode-chat:…/vs-1.jsonl:0:resp`) pointing back to the exact source row.

With `--history-dir <dir>` pointing at a
[`devin-history`](https://github.com/Icaro0310/devin-explore) export
directory, each hit whose session has an exported note gets a `note: <path>`
line under its row (and a `history_note` field in `--json`). Notes are
matched by the `<YYYY-MM-DD>_<session-id>.md` filename devin-history writes
(`.json` exports work as a fallback). A missing note produces no link and
no error — the flag is purely additive.

## Works with Devin alone (Devin-only mode)

devin-search builds and queries a fully local index over Devin's session
stores. Nothing is sent anywhere; the index lives on your disk next to the
data it covers.

## Platform support

Tested on **Windows and Linux** (`windows-latest` + `ubuntu-latest` in CI).
The Devin stores are auto-detected from the OS-specific locations listed
in the platform guides — [Linux](README.linux.md) ·
[Personal Windows](README.windows.md) ·
[Corporate Windows](README.corporate-windows.md) — and
`--sessions-db`/`--acp-dir` override them.

The non-Devin surfaces auto-detect per the session-surface spec
(`devin-internals-spec` → `docs/SESSION-SURFACES.md` §6.3–§6.5):
**copilot-cli** merges `$COPILOT_DIR`, `$COPILOT_HOME` and `~/.copilot`
(`session-state/` dir + flat files, legacy `history-session-state/`);
**codex-cli** reads `$CODEX_HOME` else `~/.codex` (newest `state_<N>.sqlite`
plus `sessions/`/`archived_sessions/` rollouts); **vscode-chat** merges the
`Code` and `Code - Insiders` `User` dirs plus `~/.vscode-server/data/User`
(`workspaceStorage/*/chatSessions` + `globalStorage/*ChatSessions`). Use
`--copilot-dir`/`--codex-dir`/`--vscode-chat-dir` to pin one root or
`--no-copilot`/`--no-codex`/`--no-vscode-chat` to disable a surface.

## Limitations

- **Schema-gated.** Only `sessions.db` schema v15–v17 is accepted; newer
  versions fail loudly (update `devin-internals-spec` first).
- **Opaque payloads.** `chat_message`, `tool_call_*_json`, acp
  `payload`, copilot event `data`, codex rollout payloads and
  vscode-chat `requests[]` shapes are undocumented/unstable — text
  extraction is best-effort and tolerant, not contractual. Malformed
  JSONL lines are skipped, never fatal.
- **Keyword search only (M1).** BM25 over tokens — no synonyms or
  embeddings; semantic search is an opt-in M2 candidate.
- **Deletion detection.** Devin-cli stores keep a content digest over
  the covered rows, so mid-table deletes and in-place row updates are
  re-indexed on the next `index` run; copilot files do the same via a
  covered-prefix fingerprint. Tail-pruned sources are re-indexed
  automatically. The digest only covers sources as they existed on the
  last run — a source deleted *and* recreated between runs is picked
  up, but a store modified while `devin-search` is running is not.
- **Read-only by design** — the tool never writes to Devin's stores; the
  only file it creates is `search.db`.

## Development

```bash
pip install -e ".[dev]"
pytest
```

Fixtures are generated at test time by `devin_internals.fixtures` (real
v17 DDL, synthetic rows) plus per-surface writers in `tests/conftest.py`
for the copilot/codex/vscode-chat stores — no binary fixtures are
committed.

## Adapters (MCP server, Devin skill, plugin)

`devin_search.mcp_server` exposes one read-only tool via the
`devin-search-mcp` entry point (`pip install 'devin-search[mcp]'`):
`search_query` (same JSON as `devin-search query --json`).

`adapters/` is a self-contained Devin plugin root
(`adapters/.devin-plugin/plugin.json` +
`adapters/skills/devin-search/SKILL.md`), installed with
`devin plugins install
Icaro0310/devin-explore#packages/search/adapters` — it ships in the
repo, not inside the wheel. The manifest launches the server through
`uvx --from 'devin-search[mcp]' devin-search-mcp`, which resolves once
the matching PyPI release ships; until then run the source-installed
`devin-search-mcp` directly or point a local manifest copy at the
checkout (`uvx --from './packages/search[mcp]' devin-search-mcp`).

### MCP host configs

Same stdio server in other hosts — server name `devin-search`:

**Zed** — `context_servers` in `~/.config/zed/settings.json`:

```jsonc
{ "context_servers": { "devin-search": { "command": { "path": "devin-search-mcp", "args": [] } } } }
```

**JetBrains AI Assistant** — Settings → Tools → AI Assistant → Model Context Protocol → Add:

```json
{ "mcpServers": { "devin-search": { "command": "devin-search-mcp" } } }
```

**Antigravity** — `~/.gemini/config/mcp_config.json`:

```json
{ "mcpServers": { "devin-search": { "command": "devin-search-mcp" } } }
```

## When to use this

- You remember Devin ran a command, hit an error, touched a file, or made a
  decision, and scrolling sessions one by one is too slow.
- You want ranked results tagged by role (user / assistant / tool call) with
  a `ref` back to the exact source row.
- Your session history must stay on disk — the index is fully local, no
  telemetry, no network calls.
- You want incremental indexing: re-running `devin-search index` only picks
  up new rows.

## When NOT to use this

- You need semantic or synonym-aware search — M1 is keyword BM25 only;
  embeddings are an opt-in M2 candidate.
- You need session *observability* (activity, context size, token peaks) — use `devin-metrics`;
  or relationship queries — use `devin-graph`.
- Your `sessions.db` schema is outside v15–v17 — indexing refuses loudly
  rather than misreading it.

## FAQ

**What is devin-search?** A local full-text search engine over your Devin
session history. It indexes Devin's SQLite stores with FTS5/BM25 and answers
queries like `devin-search query "kubectl delete pod"` in under a second,
with results tagged by role and linked back to the source row.

**Does devin-search send my session data anywhere?** No. Everything runs
locally: it reads Devin's stores read-only and writes a single `search.db`
index next to your data. There are no network calls and no telemetry.

**Does it write to or modify Devin's databases?** No. Devin's stores are
opened read-only by design; the only file devin-search creates is its own
`search.db` index.

**How is this different from `devin-history`?** devin-history exports
sessions to static Markdown/JSON files. devin-search complements it with
instant ranked lookup across all sessions, without exporting anything.

## License

MIT — see [LICENSE](LICENSE).
