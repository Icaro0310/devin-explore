---
name: devin-graph
description: "Query the session knowledge graph when the user asks how a session relates to a file, tool or project — which sessions touched a path, which projects share files. Read-only: never writes to graph.db or the source stores."
triggers: [model, user]
allowed-tools:
  - exec
  - read
---

# devin-graph

When the user asks how a session relates to a file or project — which
sessions touched a path, which sessions used a tool, what a project is
made of, where two projects overlap — query the graph:

```bash
devin-graph query file <path> --json        # sessions that touched it
devin-graph query tool <name> --json        # sessions/projects using it
devin-graph query project <name> --json     # sessions, tools, files
devin-graph query projects-graph --json     # project adjacency
devin-graph query shared-files --json       # files touched by ≥2 projects
```

Or, when this plugin's MCP server is connected, call `graph_query` —
`query_type` is the subcommand name, `target` the positional argument —
same payloads.

## Reading the result

- `file` → `{"files": matched paths, "sessions": [...], "tool_calls": [...]}`
- `tool` → `{"sessions": [...], "projects": [...]}`
- `project` → `{"project": key, "sessions": [...], "tools": [...],
  "files": [...]}`
- `projects-graph` → `{"nodes", "links"}` — D3-style adjacency; links
  carry `weight`, `shared_tools`, `shared_files`.
- `shared-files` → `{"count", "files": [{file, projects, sessions}]}`
- `{"error": "no_graph"}` means there is no `graph.db` at the given path
  (default `./graph.db`) — tell the user the graph needs building first;
  don't invent edges.

## Rules

- Read-only: queries never write to `graph.db` or the source stores.
- Matching is forgiving — a suffix like `src/app.py` resolves to the
  full stored path; project names match case-insensitively.
- Sessions are listed with `id`, `title`, `project` and
  `last_activity_at` — newest activity identifies the live work.
