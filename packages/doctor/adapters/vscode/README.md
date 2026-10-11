# devin-doctor for VS Code

Ecosystem health checks from [devin-doctor](https://pypi.org/project/devin-doctor/),
inside the editor.

This is a thin adapter: the extension runs `devin-doctor check --json` and
renders the result — no check logic is reimplemented here. All findings,
thresholds and remediation steps come from the CLI, so the editor surface can
never drift from what `devin-doctor` says on the command line.

## Features

- Status bar item showing the overall verdict (PASS / WARN / FAIL) with the
  per-severity counts in the tooltip. Click it to re-run the check.
- **devin-doctor: Run health check** — refresh the verdict on demand.
- **devin-doctor: Open full report** — the CLI's markdown report in an editor.
- **devin-doctor: Show remediation plan** — the ordered fix plan in the output
  channel.
- Warning notification when checks fail, so a broken environment is visible
  without polling the terminal.

## Requirements

`devin-doctor` installed and on `PATH`:

```bash
uv tool install devin-doctor
```

or set `devinDoctor.executable` to an absolute path.

## Settings

| Setting | Default | Description |
|---|---|---|
| `devinDoctor.executable` | `devin-doctor` | CLI executable (name or absolute path) |
| `devinDoctor.checkOnStartup` | `false` | Run a check when VS Code starts |
| `devinDoctor.extraArgs` | `[]` | Extra args appended to every CLI call (e.g. `["--data-dir", "…"]`) |

## Privacy

The extension runs the local CLI and shows its output. It makes no network
calls of its own; the only outbound calls possible are the ones devin-doctor
documents for itself (update freshness hint, disable via
`DEVIN_DOCTOR_OFFLINE=1`).

## License

MIT — see LICENSE.
