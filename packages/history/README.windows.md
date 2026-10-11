# devin-history, Personal Windows guide

This guide covers unrestricted Windows setup. For restricted machines, see [README.corporate-windows.md](README.corporate-windows.md); for features, shared commands, limitations, and the safety model, see [README.md](README.md).

Personal Windows uses the extended runtime: local execution plus optional Devin VM/QwenPaw delegation when this artifact supports it.

## Prerequisites

- `uv` and Python 3.10 or newer; `uv` can manage Python.

## Install

Install the isolated Python CLI:

```powershell
uv tool install "devin-history"
```

## Devin paths

Session data normally lives under `%APPDATA%\devin\cli\`; UI state and ACP stores under `%APPDATA%\Devin\User\`.
Use `--sessions-db`/`--vscdb` for non-default locations.

## Environment notes

- Delegated runtime is optional; this guide installs local tooling only.
- Corporate Windows is a separate local-only environment.
- macOS is planned but not claimed as tested.

## Personal Windows specifics

- **Python:** `uv` manages its own Python, which also avoids the Microsoft Store `python.exe` alias stub (it opens the Store instead of running). If you install Python from python.org anyway, tick "Add python.exe to PATH".
- **Shell:** PowerShell 7 + Windows Terminal is the recommended setup; every command also works in `cmd.exe` and Windows PowerShell 5.1, none require admin.
- **Install location:** executables live under `%USERPROFILE%\.local\bin`; data under `%APPDATA%\devin`. Nothing touches `Program Files` or the registry.
- **WSL:** treat it as a Linux machine, follow [README.linux.md](README.linux.md) inside it.
- **Uninstall:** `uv tool uninstall <package>` (or `npm uninstall -g` for a Node.js tool) removes the CLI; delete `%APPDATA%\devin` to remove local data. No services or scheduled tasks are left behind.

## Recurring runs (optional)

_Weekly archive of sessions to notes (`--out` is required); run before janitor cleanup._

Quoted path arguments inside `/tr` fail differently in cmd.exe and PowerShell 5.1, so this job goes through a wrapper `.cmd` file in your own profile directory (quoting inside the file works normally, and no other user can replace it). The task inherits your user `PATH`; if the tool is not on it, write the tool's full path inside the `.cmd`. Register with the `ScheduledTask` cmdlets, which take the path literally and replace cleanly on rerun (`-Force`):

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\devin-tasks"   # cmd.exe: mkdir "%USERPROFILE%\devin-tasks"
```

Save this as `%USERPROFILE%\devin-tasks\devin-history.cmd`:

```batch
devin-history export --out "%USERPROFILE%\devin-history-export"
```

```powershell
$action  = New-ScheduledTaskAction -Execute "$env:USERPROFILE\devin-tasks\devin-history.cmd"
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 5AM
Register-ScheduledTask -TaskName "devin-history" -Action $action -Trigger $trigger -Force
```

_cmd.exe users: the same registration works from Task Scheduler's GUI (Action = the .cmd path)._

Runs under your account, no admin needed. Adjust `/sc`/`/st` (or `/sc onlogon` for daemons) to taste.


## Troubleshooting

- If a command is not found, reopen PowerShell and run `uv tool update-shell`.
