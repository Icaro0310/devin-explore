---
name: devin-history
description: "Look up or export Devin session history when the user asks about a previous session or wants a report of what was done. Read-only: the source store is never modified; export writes only to the output dir you pass."
triggers: [model, user]
allowed-tools:
  - exec
  - read
---

# devin-history

When the user asks about a previous session — what was done, when, in
which project — or wants a report of the work history, answer from the
store instead of guessing:

```bash
devin-history list --json                  # recent sessions
devin-history export --out DIR --json      # one note per session + index
```

Or, when this plugin's MCP server is connected, call `history_list` /
`history_export` — same payloads.

## Reading the result

- `list` → `{"sessions": [...]}` newest first: `id`, `title`, `project`,
  `created`, `duration_min`, `hidden`.
- `export` → `{"written", "skipped_unchanged", "skipped_empty",
  "indexed"}`. Notes land in `--out` as `<YYYY-MM-DD>_<session-id>.md`
  plus an `index.md` (or `.json` with `--format json`). Sessions with no
  user messages are skipped as empty.
- If the JSON contains `error`, the store could not be opened
  (`bad_store` = missing file or unsupported schema) — say so instead of
  inventing a history.

## Rules

- Read-only against `sessions.db`. `export` writes only inside `--out`;
  nothing else on disk is touched.
- `--session-id` accepts a unique prefix for an incremental export.
- `--dry-run` reports what would be written; use it before pointing at a
  real notes dir.
