# devin-search for VS Code

Full-text search across past Devin sessions from
[devin-search](https://pypi.org/project/devin-search/), inside the editor.

This is a thin adapter: the extension runs `devin-search <cmd> --json` and
renders the result — no search logic is reimplemented here. All ranking,
snippets and stats come from the CLI, so the editor surface can never drift
from what `devin-search` says on the command line.

## Features

- Status bar item showing the last query's hit count. Click it to search again.
- **devin-search: Search past sessions** — prompts for a term, runs
  `devin-search query <term> --json --no-log --limit N`, and offers the hits
  in a quick-pick. Selecting a hit opens a markdown sheet with its metadata
  (when, role, session, project, source, ref, history note) and snippet.
- **devin-search: Show zero-hit query stats** — `devin-search misses --json`
  rendered into the output channel (the objective trigger for semantic
  search).

## Read-only contract

The extension is strictly read-only:

- `query` is always invoked with `--no-log`, so searches from the editor
  never append to `<index>.queries.jsonl`.
- `index` is **not** exposed — it writes the FTS5 index. Build it from a
  terminal (`devin-search index`); the extension tells you when the index
  is missing.
- Exit codes are treated as results, not errors: `query` exits 1 on zero
  hits and 2 when the index/store can't be read.

## CLI compatibility (schema contract)

Pinned via the `devin-search-cli` field in `package.json`:
`devin-search >=0.1,<0.2` (the released CLI is 0.1.x).

The extension renders this `query --json` shape:

```json
{ "term": "…", "hits": [ { "session_id": "…", "role": "…", "ts": 0,
  "project": "…", "session_title": "…", "source": "…", "ref": "…",
  "snippet": "…", "rank": 0.0, "history_note": "…" } ] }
```

Required per hit: `session_id`, `role`, `snippet` (string) and `ts`
(number, epoch ms); the remaining hit fields are rendered when present.
A payload missing required fields surfaces an explicit drift error instead
of a blank result. `misses --json` renders `{log, total_queries, zero_hit,
zero_hit_rate, per_month, top_missed_terms}` defensively — malformed output
is dumped raw to the output channel.

## Requirements

`devin-search` installed and on `PATH`:

```bash
uv tool install devin-search
devin-search index   # build the index once (CLI-only, writes the db)
```

or set `devinSearch.executable` to an absolute path.

## Settings

| Setting | Default | Description |
|---|---|---|
| `devinSearch.executable` | `devin-search` | CLI executable (name or absolute path) |
| `devinSearch.limit` | `20` | Max hits per query (clamped to 1–500; `-1` would mean unlimited in SQLite and could overflow the output buffer) |
| `devinSearch.extraArgs` | `[]` | Extra args appended to every CLI call (e.g. `["--index", "…"]`) |

## Privacy

The extension runs the local CLI and shows its output. It makes no network
calls of its own; the search index is a local FTS5 database built from
Devin's local session stores.

## License

MIT — see LICENSE.
