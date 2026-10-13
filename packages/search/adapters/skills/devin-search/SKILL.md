---
name: devin-search
description: "Full-text search across all Devin sessions — query the session history before assuming some command, error or decision isn't in it. Read-only: queries never modify the index or the source stores."
triggers: [model, user]
allowed-tools:
  - exec
  - read
---

# devin-search

Before assuming a fact, command or error message is not in the session
history, query it:

```bash
devin-search query "the term" --json
```

Or, when this plugin's MCP server is connected, call `search_query` —
same payload. Filters: `--role`, `--project`, `--since`, `--limit`.

## Reading the result

- `{"term": ..., "hits": [...]}` — each hit has `session_id`, `role`,
  `ts`, `project`, a `snippet` with «markers» around the match, a
  `ref` pointing at the source doc, and a `rank` score.
- Empty `hits` means the index simply has no match — the CLI exits 1;
  it is not an error. Say "nothing found" rather than guessing.
- `{"error": "no_index"}` means there is no search index yet — tell the
  user it needs building first (`devin-search index`); don't invent
  results.

## Rules

- Read-only: a query never writes to the index or the source stores.
- Free-text terms are token-ANDed automatically — keep terms short and
  specific rather than pasting whole sentences.
- `since` accepts `YYYY-MM-DD` or epoch milliseconds.
