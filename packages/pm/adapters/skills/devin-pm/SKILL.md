---
name: devin-pm
description: "Aggregate project progress from Devin session history — per-project session counts, status, milestones and cost when the user asks how a project is going. Read-only: the store is never modified."
triggers: [model, user]
allowed-tools:
  - exec
  - read
---

# devin-pm

When the user asks for aggregate project progress — how a project is
doing, how many sessions it had, status counts, cost — roll the session
store up into projects instead of guessing:

```bash
devin-pm registry --json        # every project, machine-readable
```

Or, when this plugin's MCP server is connected, call `pm_rollup` —
same registry payload; pass `project` for a single project's entry.

## Reading the result

- `projects[]` carries `name`, `working_directory`, `sessions`,
  `session_ids`, `status` counts, `last_activity` (ISO), a `milestones`
  block (`total`/`done`/`pending`) and `cost` (`null` means unknown,
  not zero).
- `totals` aggregates project/session counts and known costs.
- `{"error": "no_db"}` means no `sessions.db` resolved;
  `{"error": "unknown_project"}` lists the known names in `detail` —
  report it, don't invent a rollup.

## Rules

- Read-only, always: the store is opened read-only and nothing is
  written.
- A project name is the working-directory basename; lookup is
  case-insensitive.
- Sessions titled `milestone: <name>` feed the milestone counters; a
  hidden session counts as done.
