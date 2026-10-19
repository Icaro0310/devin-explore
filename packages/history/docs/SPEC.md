# SPEC — devin-history

Export and audit Devin Desktop session history: turn the local `sessions.db`
into Obsidian-ready Markdown notes, a searchable JSON dump, and an audit
report — without touching the database (all reads are `mode=ro`).

**Unofficial community project.** Devin's local stores are private internals;
their schema changes without notice (17 migrations so far). This tool depends
on [`devin-internals-spec`](https://github.com/Icaro0310/devin-internals-spec)
for schema detection and typed parsers instead of guessing SQL.

## 1. Scope

In scope (M1):

- CLI `sessions.db` (`cli/sessions.db`, schema v15–v17): `sessions`,
  `message_nodes`, `tool_call_state`, `prompt_history`.
- Three commands: `export`, `audit`, `list`; `--json` on all.
- Idempotent export (one file per session + `index.<fmt>`).

Out of scope (see §6):

- GUI sessions (`User/acp-messages/*.db`), `state.vscdb`, session locks,
  logs, summaries, `.devin/memory` JSONL — the legacy scripts covered some
  of these; the library defers them.

## 2. Invariants

1. **Read-only.** The source database is opened via
   `devin_internals.parsers.SessionsStore` (`file:...?mode=ro`). No command
   ever writes to it; tests assert the file hash is unchanged.
2. **Fail loud on schema drift.** Unknown schema versions
   (`> LATEST_KNOWN_SCHEMA`) or known-but-unsupported ones (`< 15`) raise
   `SchemaError`; the CLI exits 2 with the detector's message.
3. **Deterministic output.** No wall-clock timestamps are embedded in
   exported files — re-exporting an unchanged DB is byte-identical.
4. **Opaque payloads tolerated.** `chat_message`, `metadata`,
   `tool_call_*_json` are marked *unstable* in SCHEMA.md; decoding accepts
   the observed `role`/`content` and `role`/`text` shapes and degrades
   gracefully — it never crashes on an unknown payload.
5. **Timestamps are epoch ms** (SCHEMA.md); a seconds fallback keeps
   pre-ms data renderable.

## 3. Modules (`src/devin_history/`)

| module | role |
|---|---|
| `cli.py` | thin argparse wrapper; exit codes; `--json` plumbing |
| `export.py` | idempotent per-session export + index; `ExportResult` |
| `audit.py` | per-session stats, inferred status, task classification, anomalies |
| `format.py` | pure emitters: session md/json, index, audit md/csv/dict, list table |
| `messages.py` | tolerant `chat_message` decoding (`ChatMessage`) |
| `paths.py` | default `sessions.db` candidates per platform |
| `times.py` | epoch-ms helpers (`to_seconds`, `fmt_ts`, `duration_minutes`) |

`cli.py` contains no business logic — it resolves the DB path, opens a
`SessionsStore`, calls a library function, prints.

## 4. CLI contract

```
devin-history export --sessions-db PATH --out DIR [--format md|json]
                     [--all] [--dry-run] [--json]
devin-history audit  --sessions-db PATH [--csv FILE] [--json]
devin-history list   [--sessions-db PATH] [--limit N] [--json]
```

- `--sessions-db` is optional; when omitted, `paths.default_sessions_db()`
  checks `%APPDATA%/devin/cli/sessions.db` (Windows),
  `~/Library/Application Support/devin/cli/sessions.db` (macOS) and
  `$XDG_CONFIG_HOME/devin/cli/sessions.db` → `~/devin/cli/sessions.db`
  (Linux). Missing store → exit 2.
- `export` writes `{YYYY-MM-DD}_{session_id}.{fmt}` per session plus
  `index.{fmt}` (stats block: totals, date span, per-project/per-format
  counts, message totals; then md: grouped by project, json: entry list).
  Idempotency:
  the session's `last_activity` epoch-ms marker is embedded in frontmatter /
  top-level field; unchanged → skipped. Sessions with no user messages or
  `< 2` nodes are counted as empty and skipped.
- Every exported session — note, JSON record and `index.json` entry —
  carries the SESSION-EVIDENCE anchor triple
  (`devin-internals-spec` `docs/SESSION-EVIDENCE.md` §1): `surface`
  (`devin-cli`; `devin-chat` for `export-gui` notes), `native_id` (the
  store's own session id) and `ref` — the SESSION-SURFACES §4 canonical id
  `<surface>:<workspace-hash>:native:<id>` when a workspace identity exists,
  else the ephemeral `<surface>:ephemeral:<index>` display key (position in
  the full ordered listing, stable under `--session-id`/`--since`
  filtering). A pre-anchor note's `last_activity` marker does not count as
  up-to-date: unchanged sessions missing `ref` are re-rendered on re-run.
- `audit` prints a Markdown report (groupings: status / task type / project /
  month; anomaly list; session table) or JSON via `--json`; `--csv` also
  writes a flat CSV.
- `list` prints a fixed-width table (id, created, duration, model, project,
  title) most-recently-active first, or `--json`.

### Status inference (audit)

`sessions.db` has no final-state column, so `audit` infers:

| condition | status |
|---|---|
| `hidden = 1` | `hidden/archived` |
| no message nodes | `empty` |
| last node role = user | `interrupted (no final reply)` |
| 0 tool calls + ≤1 user msg | `abandoned (no actions)` |
| ≥5 failed tool calls | `completed with failures` |
| 1–4 failed tool calls | `completed with warnings` |
| otherwise | `completed (inferred)` |

### Anomalies

- `empty` — session row with no `message_nodes`.
- `orphan` — `session_id` referenced by `message_nodes` /
  `tool_call_state` / `prompt_history` but absent from `sessions`
  (possible because SQLite FKs are not enforced).
- `long-running` — `last_activity - created ≥ 480 min` (parameterized).

## 5. Testing

- Fixtures are generated at test time via
  `devin_internals.fixtures.create_sessions_db()` — real v17 DDL, synthetic
  rows; no committed binary fixtures.
- Required green: export note-per-session + index, rerun idempotent, audit
  groupings + anomaly flags, `list`/`--json` shapes, unknown schema fails
  loud, source DB untouched.

## 6. M2 queue

- `--redact` output pipeline via `devin-redact`.
- Obsidian vault writer (MOC + folder conventions from the legacy script).
- SessionEnd hook integration for incremental export.
- `acp-messages/*.db` (GUI sessions) export/audit via `AcpMessagesStore`.
- PyPI publish.
