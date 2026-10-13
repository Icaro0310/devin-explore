---
name: devin-doctor
description: "Diagnose a Devin Desktop installation (stores, schema versions, data health, config, disk usage) when the user reports install errors, corrupted stores, or asks whether the environment is healthy. Read-only: reports findings and fix suggestions, never repairs on its own."
triggers: [model, user]
allowed-tools:
  - exec
  - read
---

# devin-doctor

When the user reports a Devin installation error, corrupted stores, odd
session behavior, or asks whether the environment is healthy, diagnose
before guessing: call the `doctor_check` MCP tool — it returns the same
JSON payload as the CLI. When only the Python package is installed
(no plugin/MCP connection), run it directly:

```bash
devin-doctor check --json
```

For a machine capability profile (scheduler/daemon/net), use the
`doctor_capabilities` tool, or `devin-doctor capabilities` on a package
install.

## Reading the result

- `overall` is `PASS`/`WARN`/`FAIL`; `findings[]` carries one entry per
  check with `status`, `message` and an optional `fix` suggestion.
- `FAIL` findings explain what is actually broken — report them with
  their `fix` text. `WARN` findings are worth mentioning, not alarming.
- If the JSON contains `error`, the tool could not run (bad path,
  missing store) — say so instead of inventing a diagnosis.

## Rules

- Read-only by design. There is no fix/apply command — never improvise
  one. Present `fix` suggestions for the user to approve and run
  themselves.
- Point `--data-dir`/`--config-dir` at the user's actual Devin
  directories when they are non-standard; otherwise let the tool use the
  platform defaults.
- `--stale-days N` controls how long a session must be inactive before
  it counts as stale (default 30).
