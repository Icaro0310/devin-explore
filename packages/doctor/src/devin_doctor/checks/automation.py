"""Check 8 — automation: scheduled tasks that silently broke.

Scheduled automation fails quietly: a cron line pointing at a deleted
script, a systemd user timer whose target was uninstalled, a Task
Scheduler entry for a repo that was absorbed into a monorepo — nothing
reports the breakage until someone notices the missing output days later
(the ``office-freshness`` cron line once vanished for ~3.5 days before
anyone saw it).

This check enumerates the three scheduling surfaces — user crontab
(``crontab -l``), systemd user units (``systemctl --user``) and Windows
Task Scheduler (``schtasks``) — extracts the script/binary paths each
task references and reports:

- a FAIL per referenced path that no longer exists;
- a FAIL per task that disappeared since the last run, via a snapshot
  persisted at ``<state-dir>/automation-snapshot.json`` (the only data
  this check writes — doctor's own state, never the system's; a
  ``.gitignore`` beside it keeps the baseline out of ``git add -A``
  even when ``DEVIN_DOCTOR_STATE_DIR`` points inside a checkout).
  ``<state-dir>`` is ``paths.default_state_dir()`` (Linux
  ``$XDG_DATA_HOME/devin-doctor``, Windows ``%LOCALAPPDATA%`` +
  ``devin-doctor``) — deliberately NOT under ``ctx.data_dir``:
  ``--data-dir`` names the install being inspected and can point at a
  repo checkout, and the baseline must never be written inside one.
  Removed tasks stay in the baseline and keep failing until they are
  restored or the entry is deleted from the snapshot — a silent removal
  is not acknowledged by one report;
- a FAIL when a referenced path lives inside a repo checkout the
  ecosystem registry (``devin-powerups/registry.json``) marks as
  absorbed/archived — ``local_dir`` differs from the repo ``name`` and
  the entry carries a ``package.path``.

Unavailable surfaces (no crontab binary, no user systemd bus, non-
Windows host) report cleanly instead of erroring; an unreachable
registry skips the absorbed-repo sub-check silently. A surface that
fails to answer is *unavailable*, never "empty" — its snapshot entries
survive the run so a flaky probe cannot masquerade as a mass removal.

Command lines can carry credentials (``token=abc``, ``--api-key x``):
the stored identity and snapshot rows are redacted via ``_redact`` so
reports and the on-disk baseline never persist them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import Any

from devin_doctor.model import Context, Finding, Status, platform_name
from devin_doctor.paths import default_state_dir

CHECK_ID = "automation"

SNAPSHOT_NAME = "automation-snapshot.json"

_CMD_TIMEOUT = 20
_MAX_SCHTASKS = 200
_MAX_DEPTH = 3

_CRON_SPECIALS = re.compile(r"^@\w+$")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_BOUNDARY = re.compile(r"[0-9]*[|&;(){}<>]+[0-9]*")
_UNRESOLVABLE = re.compile(r"[*?[\]${}%`]")
_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]")
_SYSTEMD_UNIT = re.compile(r"^(\S+\.(?:service|timer))\s")
_EXEC_LINE = re.compile(r"^ExecStart(?:Pre|Post)?=(.*)$")

# Words that keep command position open: shell syntax and prefix wrappers
# (``sudo cmd``, ``flock -n /x cmd``, ``env A=1 cmd`` — the payload still
# follows).
_CONTROL_WORDS = frozenset({
    "if", "then", "else", "elif", "fi", "for", "while", "until", "do",
    "done", "case", "esac", "in", "!", "time", "sudo", "doas", "env",
    "nohup", "nice", "flock", "chronic", "timeout", "stdbuf", "command",
    "exec", "eval", "xargs", "parallel", "watch", "setsid",
})

# Words that consume command position but are always present (shell
# builtins) — never worth a PATH lookup.
_BUILTINS = frozenset({
    "cd", "echo", "printf", "set", "unset", "export", "umask", "test",
    "[", "[[", "true", "false", "exit", "return", "source", ".",
    "readonly", "shift", "trap", "wait", "builtin", "sleep", "kill",
    "local", "declare", "typeset", "let", "read", "jobs", "fg", "bg",
})

_BARE_COMMAND = re.compile(r"^[A-Za-z0-9_][\w.+-]*$")

# Arg-position paths are only worth an existence check when they look like
# a file (``script.py``, ``config.toml``): positional args such as D-Bus
# object paths (``/org/kde/foo``) or output dirs would be false alarms.
_FILEISH = re.compile(r"\.[A-Za-z0-9]{1,6}$")

# A bare file-looking arg (``backup.py``) is only checked as a filesystem
# reference when the running command is an interpreter that takes a
# script argument — otherwise ``--handler skills.audit`` or
# ``-m http.server`` would read as missing files.
_INTERPRETER = re.compile(
    r"^(?:[a-z]*sh|pythonw?[\d.]*|node(?:js)?|deno|ruby|perl|pwsh|"
    r"powershell)(?:\.exe)?$",
    re.IGNORECASE,
)

# POSIX-family shells whose ``-c`` takes a nested command line.
_POSIX_SHELL = re.compile(r"^(?:a|ba|da|k|z)?sh(?:\.exe)?$", re.IGNORECASE)

# Interpreter flags whose next token is a module name, inline code or an
# option value — never a path. ``-r``/``--require``/``--import`` are
# deliberately absent: they take a preload FILE that must be checked.
# ``-c`` is handled per-interpreter (nested command for shells, inline
# code elsewhere). Short switches (``-u``, ``-I``) take no value and are
# not suppressed; long options (``--handler x``) are assumed to take a
# value.
_PAYLOAD_FLAGS = frozenset({"-m", "-e", "-X", "-W"})

# Interpreter flags whose next token is a preload FILE (node/deno) —
# deliberately NOT in _PAYLOAD_FLAGS so the fileish check sees it.
_PRELOAD_FLAGS = frozenset({"-r", "--require", "--import"})

_REDIRECT = re.compile(r"\d*[<>]+&?\d*|&>>?")

# Credential-looking fragments are scrubbed before a command is stored
# or printed: ``KEY=value`` env assignments and ``--flag value`` /
# ``--flag=value`` arguments whose name smells like a secret.
_SECRET_ENV = re.compile(
    r"(?i)\b((?:[A-Za-z_][A-Za-z0-9_]*)?(?:TOKEN|SECRET|PASSWORD|"
    r"PASSWD|API_?KEY|CREDENTIAL)[A-Za-z0-9_]*)=\S+"
)
_SECRET_FLAG = re.compile(
    r"(?i)(--?[A-Za-z0-9_-]*(?:token|secret|pass(?:word)?|api[-_]?key|"
    r"cred(?:ential)?|auth)[A-Za-z0-9_-]*)(=|\s+)(\S+)"
)
_URL_USERINFO = re.compile(r"(://)[^\s/:@]+:[^\s/@]+@")
_BEARER = re.compile(r"\b([Bb]earer)\s+[A-Za-z0-9._~+/=-]{8,}")


@dataclass(frozen=True)
class _Target:
    """A resolved path a task references and how to verify it: ``exists``
    (the file itself) or ``parent`` (a redirection target — the file may
    not exist yet, its directory must)."""

    path: Path
    kind: str


@dataclass(frozen=True)
class _Task:
    """One scheduled entry. ``name`` is the stable identity — unit/task
    name for systemd/schtasks, the command line itself for cron.
    ``cwd`` is the working directory the scheduler resolves relatives
    against (``WorkingDirectory=`` for systemd units)."""

    surface: str
    name: str
    command: str
    cwd: str | None = None
    # Non-reversible identity for the snapshot key when ``name`` alone
    # could collide — two cron jobs that differ only in a redacted
    # credential must still be tracked as distinct tasks.
    identity: str | None = None


def _redact(text: str) -> str:
    """Replace credential-looking env assignments, flag values, URL
    userinfo and bearer tokens with ``***`` so task identities,
    findings and snapshots never persist a secret from a scheduled
    command line. (Minimal local set — the full catalog lives in
    devin-redact and doesn't fit doctor's dependency surface.)"""
    text = _SECRET_ENV.sub(lambda m: m.group(1) + "=***", text)
    text = _SECRET_FLAG.sub(
        lambda m: m.group(1) + m.group(2) + "***", text
    )
    text = _URL_USERINFO.sub(r"\1***:***@", text)
    return _BEARER.sub(r"\1 ***", text)


def _run(argv: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Probe seam: ``None`` when the tool is missing or timed out."""
    try:
        return subprocess.run(
            argv, capture_output=True, text=True,
            check=False, timeout=_CMD_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------------------
# surface enumeration — each returns (tasks, unavailable_reason)
# ---------------------------------------------------------------------------


def _parse_crontab(text: str) -> list[str]:
    """Command lines from ``crontab -l`` output — comments and
    ``VAR=value`` environment lines skipped, schedule fields stripped."""
    commands: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _ENV_ASSIGNMENT.match(line):
            continue
        if _CRON_SPECIALS.match(line.split(None, 1)[0]):
            parts = line.split(None, 1)
            if len(parts) == 2:
                commands.append(parts[1].strip())
            continue
        parts = line.split(None, 5)
        if len(parts) >= 6:
            commands.append(parts[5].strip())
    return commands


def _cron_tasks(
    runner: Callable[[list[str]], subprocess.CompletedProcess[str] | None],
) -> tuple[list[_Task] | None, str | None, list[str]]:
    res = runner(["crontab", "-l"])
    if res is None:
        return None, "crontab not installed", []
    if res.returncode != 0:
        # 'no crontab for <user>' is the empty surface; anything else is
        # a failed probe — reporting [] would flag every saved cron task
        # as removed and erase its baseline. (Some builds print the
        # marker to stdout, so both streams are checked.)
        output = f"{res.stderr or ''}\n{res.stdout or ''}".lower()
        if "no crontab" in output:
            return [], None, []
        detail = (res.stderr or res.stdout or "").strip() or (
            f"exit {res.returncode}"
        )
        return None, f"crontab -l failed: {detail}", []
    # Identity: digest of the REDACTED command plus a rank among jobs
    # sharing that redacted text. A credential rotation leaves the
    # scrubbed text (and so the identity) unchanged — no false removal —
    # while two jobs differing only in a scrubbed secret still get
    # distinct keys, so removing one of the pair is detectable.
    groups: dict[str, list[str]] = {}
    for command in _parse_crontab(res.stdout or ""):
        groups.setdefault(_redact(command), []).append(command)
    tasks: list[_Task] = []
    for redacted, members in groups.items():
        digest = hashlib.sha256(redacted.encode()).hexdigest()[:16]
        for rank, command in enumerate(members):
            tasks.append(
                _Task(
                    "cron",
                    redacted,
                    command,
                    identity=f"{digest}-{rank}",
                )
            )
    return tasks, None, []


def _unit_payload(text: str) -> tuple[list[str], str | None, str | None]:
    """``(exec_lines, working_directory, timer_target)`` from a unit
    ``cat`` — WorkingDirectory feeds path resolution, ``Unit=`` names
    the service a timer activates."""
    commands: list[str] = []
    workdir: str | None = None
    activates: str | None = None
    for ul in text.splitlines():
        stripped = ul.strip()
        exec_match = _EXEC_LINE.match(stripped)
        if exec_match:
            commands.append(exec_match.group(1).strip())
        elif stripped.startswith("WorkingDirectory="):
            # an empty directive RESETS the directory (drop-ins are
            # concatenated into one 'cat' — a later 'WorkingDirectory='
            # clears an earlier one)
            workdir = stripped.split("=", 1)[1].strip() or None
        elif stripped.startswith("Unit="):
            target = stripped.split("=", 1)[1].strip()
            if target:
                activates = target
    return commands, workdir, activates


def _systemd_user_tasks(
    runner: Callable[[list[str]], subprocess.CompletedProcess[str] | None],
) -> tuple[list[_Task] | None, str | None, list[str]]:
    res = runner([
        "systemctl", "--user", "list-units",
        "--type=service,timer", "--all", "--no-legend", "--no-pager",
    ])
    if res is None:
        return None, "systemctl not installed", []
    if res.returncode != 0:
        return None, "no user systemd bus", []
    units: list[str] = []
    seen: set[str] = set()
    for line in (res.stdout or "").splitlines():
        match = _SYSTEMD_UNIT.match(line.lstrip("●* ").lstrip())
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            units.append(match.group(1))
    # list-units only knows loaded units — an installed but unloaded
    # timer (boot not reached, generator-off) is invisible there, so its
    # unit FILE has to be enumerated too or the task vanishes from the
    # inventory and a later diff would call it removed.
    files = runner([
        "systemctl", "--user", "list-unit-files",
        "--type=service,timer", "--no-legend", "--no-pager",
    ])
    if files is not None and files.returncode == 0:
        for line in (files.stdout or "").splitlines():
            name = line.split(None, 1)[0].strip() if line.strip() else ""
            if name.endswith((".service", ".timer")) and name not in seen:
                seen.add(name)
                units.append(name)
    tasks: list[_Task] = []
    for unit in units:
        names = [unit]
        commands: list[str] = []
        workdir: str | None = None
        cat = runner(["systemctl", "--user", "cat", unit])
        if cat is not None and cat.returncode == 0:
            cmds, wd, activates = _unit_payload(cat.stdout or "")
            commands += cmds
            if wd:
                workdir = wd
            if unit.endswith(".timer") and activates:
                names.append(activates)
        # Timers carry no ExecStart — read the activated service too.
        if unit.endswith(".timer") and len(names) == 1:
            names.append(unit[: -len(".timer")] + ".service")
        for name in names[1:]:
            cat = runner(["systemctl", "--user", "cat", name])
            if cat is None or cat.returncode != 0:
                continue
            cmds, wd, _ = _unit_payload(cat.stdout or "")
            commands += cmds
            if wd:
                workdir = wd
        tasks.append(
            _Task(
                "systemd-user",
                unit,
                " ; ".join(commands),
                cwd=_expand_workdir(workdir),
            )
        )
    return tasks, None, []


def _expand_workdir(workdir: str | None) -> str | None:
    """Expand ``%h`` and ``~`` in a systemd WorkingDirectory; an
    unresolvable specifier is dropped rather than misresolved."""
    if not workdir:
        return None
    wd = os.path.expandvars(workdir).replace("%h", str(Path.home()))
    return None if "%" in wd else os.path.expanduser(wd)


def _schtasks_commands(xml_text: str) -> list[str]:
    """``<Command>`` + ``<Arguments>`` from ``schtasks /query /xml``."""
    try:
        root = ET.fromstring(xml_text.lstrip())
    except ET.ParseError:
        return []
    commands: list[str] = []
    for exec_action in root.iter("{*}Exec"):
        command = exec_action.findtext("{*}Command")
        if not command:
            continue
        arguments = exec_action.findtext("{*}Arguments")
        commands.append(
            f"{command.strip()} {arguments.strip()}".strip()
            if arguments
            else command.strip()
        )
    return commands


def _task_scheduler_tasks(
    runner: Callable[[list[str]], subprocess.CompletedProcess[str] | None],
    plat: str,
) -> tuple[list[_Task] | None, str | None, list[str]]:
    if plat != "windows":
        return None, "non-Windows host", []
    res = runner(["schtasks", "/query", "/fo", "csv", "/nh"])
    if res is None:
        return None, "schtasks not found", []
    if res.returncode != 0:
        return None, "schtasks query failed", []
    names: list[str] = []
    for line in (res.stdout or "").splitlines():
        match = re.match(r'^"?\\?([^",]+)', line)
        if match and match.group(1).strip():
            names.append(match.group(1).strip())
    notes: list[str] = []
    if len(names) > _MAX_SCHTASKS:
        notes.append(
            f"listing truncated at {_MAX_SCHTASKS} of {len(names)} "
            "tasks — the rest are unchecked"
        )
    tasks: list[_Task] = []
    unreadable = 0
    for name in names[:_MAX_SCHTASKS]:
        commands: list[str] = []
        detail = runner(["schtasks", "/query", "/tn", name, "/xml"])
        if detail is not None and detail.returncode == 0:
            commands = _schtasks_commands(detail.stdout or "")
        else:
            unreadable += 1
        tasks.append(_Task("task-scheduler", name, " ; ".join(commands)))
    if unreadable:
        notes.append(
            f"{unreadable} task(s) unreadable (schtasks /xml failed) — "
            "their targets were not inspected"
        )
    return tasks, None, notes


# ---------------------------------------------------------------------------
# command-line target extraction
# ---------------------------------------------------------------------------


def _is_path_token(token: str) -> bool:
    return "/" in token or "\\" in token or token.startswith("~")


def _lex(command: str, posix: bool) -> list[str]:
    """Punctuation-aware shell tokenize: operators (';', '&&', '>',
    '2>&1') come out as their own tokens while punctuation inside quotes
    stays literal — ``cmd '/srv/a;b.py'`` is one path, not a boundary."""
    lexer = shlex.shlex(command, posix=posix, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _resolve_path(token: str, cwd: Path) -> Path:
    """``~`` expansion plus cwd-relative resolution (cron's cwd is the
    user's home; a ``cd dir &&`` prefix moves it)."""
    if token.startswith("~"):
        path = Path(os.path.expanduser(token))
    else:
        path = Path(token)
    if not path.is_absolute():
        path = cwd / path
    return path


def _task_targets(
    command: str, *, posix: bool, home: Path, cwd: Path | None = None,
    depth: int = 0,
) -> tuple[list[_Target], list[str]]:
    """Split a scheduled command into ``(targets, bare_commands)``.

    ``targets`` are resolved filesystem references — the command itself,
    file-looking argument paths (``script.py``, ``conf.toml`` — plus a
    bare ``backup.py`` when the running command is an interpreter,
    resolved under ``cwd``) and the target of redirections (``>f``
    checks the parent dir, ``<f`` checks the file itself — a missing
    input file breaks the job before exec). ``bare_commands`` are bare
    words found in command position (first word of each ``&&``/``|``/``;``
    segment) for a PATH lookup. Quoted ``sh -c '...'`` payloads and
    ``cd dir &&`` prefixes are understood, so crontab idioms like
    ``cd $HOME/repo && python3 scripts/job.py`` resolve correctly.
    """
    # A descriptor number GLUED to a redirect operator ('2>log',
    # '1>&2') is part of the operator — strip it before lexing so the
    # digit cannot steal command position and hide the real command.
    # Shell io_number rules: the digit run must be a whole word
    # (preceded by whitespace, an operator or line start) — trailing
    # digits of a word are literal ('python3 >out', '/tmp/job2>out')
    # — and spaced digits ('123 >out') are a real word too.
    command = re.sub(r"(?<![^\s;&|(){}])\d+(?=[<>])", "", command)
    try:
        tokens = _lex(command, posix)
    except ValueError:
        tokens = command.split()
    targets: list[_Target] = []
    commands: list[str] = []
    cwd = cwd or home
    expect_cmd = True
    cd_next = False
    redirect_next: str | None = None
    # next token is an interpreter option value / module / inline code
    # ('-m', '-e', '-X', '-W', long options) — never a path.
    payload_next = False
    # next token is a shell '-c' payload — a nested command line.
    shell_next = False
    last_cmd: str | None = None
    for raw in tokens:
        token = raw.strip("\"'`")
        if not token:
            continue
        # A quoted payload with whitespace is either an existing
        # space-bearing path or a nested command line — recurse it only
        # when it actually looks command-like (contains a path or a
        # separator), so 'echo "some words"' is not mistaken for one.
        if re.search(r"\s", token):
            if _resolve_path(token, cwd).exists():
                targets.append(_Target(_resolve_path(token, cwd), "exists"))
            elif depth < _MAX_DEPTH and re.search(r"[/\\|&;<>]", token):
                sub_t, sub_c = _task_targets(
                    token, posix=posix, home=cwd, cwd=cwd,
                    depth=depth + 1,
                )
                targets += sub_t
                commands += sub_c
            expect_cmd = False
            payload_next = False
            shell_next = False
            continue
        # The lexer emits shell operators as their own tokens, so each
        # piece below is a whole word — a ';' or '&' inside quotes stays
        # literal (e.g. a path named 'a;b.py') instead of reading as a
        # boundary.
        for piece in (token,):
            if not piece:
                continue
            if _REDIRECT.fullmatch(piece):
                # an operator cannot be a pending flag's value — clear
                # both consume-states before classifying it.
                payload_next = False
                shell_next = False
                if piece.startswith("&"):
                    # '&>' / '&>>' — combined stdout+stderr to a file
                    # the job may create; check the parent dir.
                    redirect_next = "parent"
                    continue
                if "&" in piece:
                    # fd duplication — '2>&1' is self-contained, but
                    # '>&'/'<&' can be followed by a bare fd number
                    # ('>& 2') or, in bash, a file ('>& out.log').
                    redirect_next = "fd"
                    continue
                if "<<" in piece:
                    # heredoc ('<<EOF') — the next word is the delimiter,
                    # not a path.
                    redirect_next = "skip"
                elif "<" in piece:
                    redirect_next = "exists"
                else:
                    redirect_next = "parent"
                continue
            if _SHELL_BOUNDARY.fullmatch(piece):
                expect_cmd = True
                cd_next = False
                redirect_next = None
                payload_next = False
                shell_next = False
                last_cmd = None
                continue
            if _ENV_ASSIGNMENT.match(piece):
                continue
            if payload_next:
                # '-m module' / '-e code' / long-option value of an
                # interpreter — a name or code string, never a path.
                payload_next = False
                continue
            if shell_next:
                # '-c' payload of a POSIX shell — a nested command line
                # (possibly a single token); inspect it.
                shell_next = False
                if depth < _MAX_DEPTH:
                    sub_t, sub_c = _task_targets(
                        piece, posix=posix, home=cwd, cwd=cwd,
                        depth=depth + 1,
                    )
                    targets += sub_t
                    commands += sub_c
                continue
            if piece in _CONTROL_WORDS:
                continue
            if piece == "cd":
                cd_next = True
                expect_cmd = False
                continue
            if piece[0] in "-@:!+":
                # systemd ExecStart modifier prefix ('-/path',
                # '@/path', ':/path') vs a plain flag ('--force').
                stripped = piece.lstrip("@!:-+")
                if stripped.startswith(("/", "~")) or _WINDOWS_ABS.match(
                    stripped
                ):
                    piece = stripped
                elif piece.startswith("-"):
                    if last_cmd is not None and _INTERPRETER.match(
                        last_cmd
                    ):
                        # arity-aware: '-m'/'-e'/'-X'/'-W' and long
                        # options take a value; '-r'/'--require' take a
                        # preload FILE (left to the fileish check);
                        # '-c' of a shell is a nested command; '--' ends
                        # options; short switches ('-u', '-I') take none.
                        if piece == "--":
                            pass
                        elif piece == "-c":
                            if _POSIX_SHELL.fullmatch(last_cmd):
                                shell_next = True
                            else:
                                payload_next = True
                        elif piece in _PRELOAD_FLAGS:
                            # '-r'/'--require'/'--import' take a preload
                            # FILE — fall through so the fileish rule
                            # checks it (module names without an
                            # extension are skipped there anyway).
                            pass
                        elif (
                            "=" in piece
                            and piece.partition("=")[0] in _PRELOAD_FLAGS
                        ):
                            # '--require=x.js' — the path is inline;
                            # env vars are expanded first and an
                            # unresolvable one makes the preload
                            # environment-dependent — uncheckable, not
                            # a missing path.
                            value = os.path.expandvars(
                                piece.partition("=")[2]
                            )
                            if not _UNRESOLVABLE.search(
                                value
                            ) and _FILEISH.search(value):
                                targets.append(
                                    _Target(
                                        _resolve_path(value, cwd),
                                        "exists",
                                    )
                                )
                        elif piece in _PAYLOAD_FLAGS or piece.startswith(
                            "--"
                        ):
                            payload_next = True
                    continue
            if "://" in piece:
                continue
            expanded = os.path.expandvars(piece)
            if _UNRESOLVABLE.search(expanded):
                continue  # glob / unresolvable variable — not checkable
            piece = expanded
            if redirect_next:
                # >/path checks the parent dir (file may not exist
                # yet); </path checks the input file itself; 'skip'
                # consumed a heredoc delimiter; 'fd' consumes a bare
                # descriptor number or '>& file' treats the file as an
                # output (parent dir). A redirect target never occupies
                # command position — 'cmd' may still follow ('2>log
                # python3 x.py' runs python3, not '2').
                mode = redirect_next
                redirect_next = None
                if mode == "fd" and re.fullmatch(r"[\d-]+", piece):
                    continue
                if mode != "skip":
                    targets.append(
                        _Target(
                            _resolve_path(piece, cwd),
                            "parent" if mode == "fd" else mode,
                        )
                    )
                continue
            elif _is_path_token(piece) or cd_next:
                resolved = _resolve_path(piece, cwd)
                if cd_next:
                    # 'cd dir &&' moves the cwd for what follows;
                    # the dir itself is also a live reference.
                    targets.append(_Target(resolved, "exists"))
                    cwd = resolved
                    cd_next = False
                elif expect_cmd or _FILEISH.search(piece):
                    targets.append(_Target(resolved, "exists"))
                # else: positional non-file path arg (D-Bus object
                # path, output dir, ...) — not verifiable, skipped.
                if expect_cmd:
                    last_cmd = PurePath(piece.replace("\\", "/")).name
            elif (
                expect_cmd
                and piece not in _BUILTINS
                and _BARE_COMMAND.match(piece)
            ):
                commands.append(piece)
                last_cmd = piece
            elif (
                last_cmd is not None
                and _INTERPRETER.match(last_cmd)
                and _BARE_COMMAND.match(piece)
                and _FILEISH.search(piece)
            ):
                # 'python3 backup.py' — a bare script arg after an
                # interpreter is a real file reference; option values
                # are already consumed via payload_next.
                targets.append(_Target(_resolve_path(piece, cwd), "exists"))
            expect_cmd = False
    return targets, commands


# ---------------------------------------------------------------------------
# ecosystem registry — absorbed/archived repo detection
# ---------------------------------------------------------------------------


def _locate_registry(ctx: Context, home: Path) -> Path | None:
    """Find ``devin-powerups/registry.json`` under known workspace roots:
    ``ctx.cwd`` and its ancestors, ``~`` and ``~/devin``, plus one level
    of container dirs below them (e.g. ``~/devin/devin-ecosystem/``)."""
    candidates: list[Path] = []
    for root in (ctx.cwd, *ctx.cwd.parents):
        candidates.append(root / "devin-powerups" / "registry.json")
    candidates.append(home / "devin-powerups" / "registry.json")
    candidates.append(home / "devin" / "devin-powerups" / "registry.json")
    for base in (home, home / "devin", ctx.cwd):
        try:
            children = sorted(base.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and child.name != "devin-powerups":
                candidates.append(child / "devin-powerups" / "registry.json")
    for path in dict.fromkeys(candidates):
        if path.is_file():
            return path
    return None


def _absorbed_repos(
    registry: dict[str, Any], workspace_root: Path
) -> dict[str, tuple[Path, str]]:
    """Repo name -> (stale checkout dir, new location label).

    A repo counts as absorbed when its ``local_dir`` points inside a
    different checkout (dir name differs from the repo ``name``) and it
    ships a ``package.path`` — e.g. ``devin-history`` now lives at
    ``devin-explore/packages/history`` so ``<ws>/devin-history`` is a
    stale checkout.
    """
    absorbed: dict[str, tuple[Path, str]] = {}
    repos = registry.get("repositories")
    if not isinstance(repos, list):
        return absorbed
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        name = repo.get("name")
        local_dir = repo.get("local_dir")
        package = repo.get("package")
        package_path = package.get("path") if isinstance(package, dict) else None
        if not name or not local_dir or not package_path:
            continue
        local_name = PurePath(str(local_dir).replace("\\", "/")).name
        if local_name != name:
            absorbed[str(name)] = (
                workspace_root / str(name),
                f"{local_dir}/{package_path}",
            )
    return absorbed


# ---------------------------------------------------------------------------
# snapshot — removed-since-last-check detection
# ---------------------------------------------------------------------------


def _snapshot_path(ctx: Context) -> Path:
    """The baseline lives in doctor's own state dir, never inside the
    inspected ``data_dir`` (which ``--data-dir`` can point at any dir —
    a ``doctor/`` folder written into a repo checkout once got swept
    into a public commit)."""
    return (ctx.state_dir or default_state_dir()) / SNAPSHOT_NAME


def _load_snapshot(
    path: Path,
) -> dict[tuple[str, str], tuple[str, str, bool]] | None:
    """``{(surface, identity): (display_name, command, had_id)}`` —
    ``None`` when absent/unreadable. ``identity`` is the stored ``id``
    (a digest for cron, the task name elsewhere); rows written before
    identities existed fall back to their (already redacted) name."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        return None
    out: dict[tuple[str, str], tuple[str, str, bool]] = {}
    cron_rows: list[tuple[str, str]] = []
    for task in tasks:
        if (
            isinstance(task, dict)
            and task.get("surface")
            and task.get("name")
        ):
            # Redact on load too — a snapshot written before redaction
            # existed still diffs cleanly against redacted identities.
            surface = str(task["surface"])
            name = _redact(str(task["name"]))
            command = _redact(str(task.get("command") or ""))
            task_id = task.get("id")
            if surface == "cron" and command:
                # Cron identities are recomputed from the stored
                # (redacted) command — never trusted from the row's
                # 'id'. This migrates raw-command digests AND rows a
                # carried legacy entry wrote with id=<name>, so a
                # restored job always re-keys to the same identity the
                # enumerator produces.
                cron_rows.append((name, command))
                continue
            key = (surface, str(task_id)) if task_id else (surface, name)
            out[key] = (name, command, bool(task_id))
    # Rank cron rows sharing a redacted command the same way the
    # enumerator does — only the key SET must be reproducible.
    cron_groups: dict[str, list[tuple[str, str]]] = {}
    for name, command in cron_rows:
        digest = hashlib.sha256(command.encode()).hexdigest()[:16]
        cron_groups.setdefault(digest, []).append((name, command))
    for digest, members in cron_groups.items():
        for rank, (name, command) in enumerate(members):
            out[("cron", f"{digest}-{rank}")] = (name, command, True)
    return out


def _write_snapshot(path: Path, rows: list[dict[str, str]]) -> None:
    payload = {
        "version": 1,
        "tasks": sorted(
            rows, key=lambda r: (r["surface"], r["id"])
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _gitignore_state_dir(path.parent)


def _gitignore_state_dir(state_dir: Path) -> None:
    """Keep the baseline out of ``git add -A`` wherever the state dir
    lands. The default ``devin-doctor`` dir can never sit inside a
    checkout, but the ``DEVIN_DOCTOR_STATE_DIR`` override can — and the
    snapshot holds absolute paths and scheduled commands a public
    commit must never see. A ``.gitignore`` only governs its own dir
    and below, so this cannot hide anything outside the state dir.
    Best-effort: a guard failure never outranks the baseline write that
    already happened."""
    ignore = state_dir / ".gitignore"
    try:
        try:
            existing = ignore.read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = ""
        covered = {
            line.strip().lstrip("/")
            for line in existing.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        if covered & {SNAPSHOT_NAME, "*", "*.json"}:
            return
        sep = "" if not existing or existing.endswith("\n") else "\n"
        ignore.write_text(
            existing + sep + SNAPSHOT_NAME + "\n", encoding="utf-8"
        )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# check entry point
# ---------------------------------------------------------------------------


def run(
    ctx: Context,
    *,
    runner: Callable[
        [list[str]], subprocess.CompletedProcess[str] | None
    ] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    plat: str | None = None,
) -> list[Finding]:
    runner = runner or _run
    plat = plat or platform_name()
    posix = plat != "windows"
    home = Path.home()

    findings: list[Finding] = []
    enumerated: dict[str, list[_Task]] = {}
    for surface, enumerator in (
        ("cron", lambda: _cron_tasks(runner)),
        ("systemd-user", lambda: _systemd_user_tasks(runner)),
        ("task-scheduler", lambda: _task_scheduler_tasks(runner, plat)),
    ):
        tasks, reason, notes = enumerator()
        if tasks is None:
            findings.append(
                Finding(
                    CHECK_ID,
                    Status.PASS,
                    f"{surface}: surface unavailable ({reason})",
                )
            )
        else:
            # Redact before anything else touches the command — target
            # extraction, findings and the on-disk snapshot all see the
            # scrubbed text ('--token /x/k.pem' can never leak as a
            # resolved path in a report either).
            enumerated[surface] = [
                replace(
                    t, name=_redact(t.name), command=_redact(t.command)
                )
                for t in tasks
            ]
            for note in notes:
                findings.append(
                    Finding(CHECK_ID, Status.WARN, f"{surface}: {note}")
                )

    # Absorbed-repo map: silently skipped when the ecosystem registry
    # cannot be located.
    registry_path = _locate_registry(ctx, home)
    absorbed: dict[str, tuple[Path, str]] = {}
    if registry_path is not None:
        try:
            registry = json.loads(
                registry_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            registry = None
        if isinstance(registry, dict):
            absorbed = _absorbed_repos(registry, registry_path.parent.parent)
        else:
            findings.append(
                Finding(
                    CHECK_ID,
                    Status.PASS,
                    "absorbed-repo check skipped (registry unreadable)",
                )
            )
    else:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                "absorbed-repo check skipped (registry not found)",
            )
        )

    for surface, tasks in enumerated.items():
        surface_failures = 0
        for task in tasks:
            if not task.command:
                continue
            label = task.name
            targets, bare_commands = _task_targets(
                task.command, posix=posix, home=home,
                cwd=Path(task.cwd) if task.cwd else None,
            )
            for target in targets:
                resolved = target.path
                missing = (
                    not resolved.parent.exists()
                    if target.kind == "parent"
                    else not resolved.exists()
                )
                if missing:
                    surface_failures += 1
                    findings.append(
                        Finding(
                            CHECK_ID,
                            Status.FAIL,
                            f"{surface}: '{label}' references missing "
                            f"path {resolved}",
                            fix="Restore the file or update the scheduled "
                            "task to point at its new location.",
                        )
                    )
                    continue
                stale = next(
                    (
                        (name, location)
                        for name, (stale_dir, location) in absorbed.items()
                        if resolved == stale_dir or stale_dir in resolved.parents
                    ),
                    None,
                )
                if stale is not None:
                    surface_failures += 1
                    findings.append(
                        Finding(
                            CHECK_ID,
                            Status.FAIL,
                            f"{surface}: '{label}' points into absorbed "
                            f"repo '{stale[0]}' (now {stale[1]}): {resolved}",
                            fix="Point the task at the repo's new location "
                            "and remove the stale checkout.",
                        )
                    )
            for command_name in bare_commands:
                if which(command_name) is None:
                    surface_failures += 1
                    findings.append(
                        Finding(
                            CHECK_ID,
                            Status.FAIL,
                            f"{surface}: '{label}' references '{command_name}' "
                            "which is not on PATH",
                            fix="Reinstall the tool or fix the scheduled "
                            "command.",
                        )
                    )
        if tasks and surface_failures == 0:
            findings.append(
                Finding(
                    CHECK_ID,
                    Status.PASS,
                    f"{surface}: {len(tasks)} scheduled task(s), "
                    "all referenced targets exist",
                )
            )
        elif not tasks:
            findings.append(
                Finding(CHECK_ID, Status.PASS, f"{surface}: no scheduled tasks")
            )

    # Removed-since-last-check. The snapshot is only diffed/rewritten
    # when at least one surface enumerated — an all-unavailable run must
    # not flag every known task as removed.
    if not enumerated:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                "no scheduling surface available — snapshot left unchanged",
            )
        )
        return findings

    snapshot_path = _snapshot_path(ctx)
    previous = _load_snapshot(snapshot_path)
    # Snapshot keys are (surface, identity) — a non-reversible digest of
    # the raw command for cron (redacted names can collide when two jobs
    # differ only in a credential), the task name elsewhere.
    current: dict[tuple[str, str], _Task] = {
        (task.surface, task.identity or task.name): task
        for tasks in enumerated.values()
        for task in tasks
    }
    enumerated_surfaces = set(enumerated)
    display_names = {
        surface: {t.name for t in tasks}
        for surface, tasks in enumerated.items()
    }
    prev_map = previous or {}
    snapshot_exists = snapshot_path.is_file()
    if previous is None:
        if snapshot_exists:
            findings.append(
                Finding(
                    CHECK_ID,
                    Status.WARN,
                    "automation snapshot unreadable — baseline reset",
                    fix="Delete "
                    f"{snapshot_path} if this warning persists.",
                )
            )
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                f"baseline snapshot recorded ({len(current)} task(s)) — "
                "removals reported from the next run",
            )
        )
        removed_keys: list[tuple[str, str]] = []
    else:
        # Rows written before identity digests existed are keyed by their
        # redacted name — match them against the current display names so
        # one upgrade is not mistaken for a mass removal.
        legacy_matched = {
            key
            for key, (name, _cmd, had_id) in prev_map.items()
            if not had_id
            and key[0] in enumerated_surfaces
            and name in display_names[key[0]]
        }
        # Only surfaces that actually answered may lose tasks — a
        # scheduler that failed to respond keeps its baseline entries,
        # otherwise a flaky probe reads as a mass removal.
        removed_keys = sorted(
            key
            for key in prev_map
            if key[0] in enumerated_surfaces
            and key not in current
            and key not in legacy_matched
        )
        for key in removed_keys:
            findings.append(
                Finding(
                    CHECK_ID,
                    Status.FAIL,
                    f"{key[0]}: scheduled task removed since last check: "
                    f"'{prev_map[key][0]}'",
                    fix="Restore the entry if the removal was accidental; "
                    "the baseline keeps flagging it until the task "
                    "returns or the entry is deleted from "
                    f"{snapshot_path}.",
                )
            )
        if not removed_keys:
            findings.append(
                Finding(
                    CHECK_ID,
                    Status.PASS,
                    "no scheduled tasks removed since last check",
                )
            )
    # Persisted baseline = this run's tasks + entries of unanswered
    # surfaces + tasks still missing (they keep alerting until they are
    # restored or their entry is deleted by hand). Legacy name-keyed
    # rows that matched a current task are superseded by the new
    # digest-keyed row.
    merged_rows = [
        {
            "surface": key[0],
            "id": key[1],
            "name": task.name,
            "command": task.command,
        }
        for key, task in current.items()
    ]
    removed_set = set(removed_keys)
    for key, (name, command, _had_id) in prev_map.items():
        if (
            key[0] not in enumerated_surfaces or key in removed_set
        ) and key not in current:
            merged_rows.append(
                {
                    "surface": key[0],
                    "id": key[1],
                    "name": name,
                    "command": command,
                }
            )
    try:
        _write_snapshot(snapshot_path, merged_rows)
    except OSError as exc:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"automation snapshot could not be written ({exc})",
                fix="Removal detection resets next run — check write "
                "permissions on the state dir.",
            )
        )
    return findings
