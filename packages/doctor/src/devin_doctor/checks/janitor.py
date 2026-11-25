"""janitor-reap — a scheduled ``devin-janitor reap`` that is silently inert.

``devin-janitor reap`` (devin-state) is the orphan-process reaper: a
systemd user timer — or a cron line — sweeps every few minutes,
dry-run by default through a ~2-week calibration window, then
``--apply``. Its failure mode is quiet: ``attribution_ok:false``
(devin_office unresolvable inside the janitor venv), a ``reap.disabled``
kill-switch next to the state file, a missing ``--apply`` after
calibration, or an empty decision ledger all leave the timer "running"
while nothing can ever be reaped. The only symptom is a WARNING line in
sweep output nobody reads (issue #113: ~2 weeks of attribution_ok:false
across every sweep, discovered during an incident).

This check enumerates the same scheduling surfaces as
``checks/automation.py`` — user crontab, systemd user units, Windows
Task Scheduler — keeps the entries that invoke ``devin-janitor reap``
and verifies, from the filesystem and unit files only (the janitor is
a uv-installed tool — never imported):

- ``reap.disabled`` next to ``--state-file`` (or ``--disable-file``)
  exists — every sweep exits before any /proc scan;
- the sweep runs without ``--apply`` although the calibration window
  has elapsed. Elapsed is proven by the oldest ``first_seen`` epoch in
  the state file or a unit fragment's mtime (~14 days per the install
  spec); before that, dry-run is the DESIGNED state and does not warn;
- the ``--ledger-file`` is missing or empty — zero
  would_kill/killed/survivor/circuit decisions recorded. It shares the
  calibration gate: an empty ledger is expected early, suspicious only
  once evidence should exist. A permanently quiet machine (zero
  runaway orphans ever) can stay empty legitimately — the WARN says so
  and the fix is verifying attribution once, not silencing the check;
- the state file exists but its mtime is stale versus the schedule
  interval (``OnUnitActiveSec``/``OnCalendar`` — the latter resolved
  via ``systemd-analyze calendar`` — or a cron-field simulation) —
  sweeps are not completing;
- a systemd timer reporting other than ``active``/``enabled``;
- the timer's ``LastTriggerUSec`` newer than the state file's mtime by
  more than the service grace — the sweep fires but never finishes a
  write;
- reap units with no timer/cron schedule at all (service file only).

``devin-janitor reap`` is itself Linux-only (reads ``/proc``), so a
reap schedule found on macOS/Windows is inert by definition — every
run exits 2. Surfaces that cannot be probed are skipped silently (the
automation check already reports their availability). Sweep *content*
(attribution_ok, eligible counts) lives only in the ``--json``
stdout/journal — the ledger records decisions, not sweep summaries, so
ledger emptiness is the filesystem-visible proxy for "the reaper may
be inert".
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

from devin_doctor.checks import automation
from devin_doctor.model import Context, Finding, Status, platform_name

CHECK_ID = "janitor-reap"

# devin-state install spec: dry-run calibration is two weeks — --apply is
# enabled only after reviewed precision. The same horizon gates the
# "no --apply" and "empty ledger" signals: before it both are the
# designed state of a fresh install.
CALIBRATION_SECONDS = 14 * 24 * 3600

# State-file staleness = expected max gap between sweeps plus slack for
# AccuracySec, suspend/resume and a slow oneshot (RuntimeMaxSec ~90s).
_STALE_GRACE_SECONDS = 600
# A timer trigger newer than the state mtime by more than this means the
# sweep fired but never completed a state write.
_TRIGGER_GRACE_SECONDS = 600

# cron interval simulation horizon — long enough for monthly-ish
# schedules to fire twice; rarer schedules yield no interval and the
# staleness check is skipped rather than guessed.
_CRON_HORIZON = timedelta(days=35)

_REAP_WORD = re.compile(r"\breap\b", re.IGNORECASE)
_OPERATOR = re.compile(r"[;&|<>()]+")
_PATH_FLAGS = frozenset({"state-file", "ledger-file", "disable-file"})

# Executable basenames that make the following ``reap`` token the
# janitor subcommand: the uv entry point (``devin-janitor``, optionally
# with a launcher extension) and the ecosystem's ExecStart wrapper
# ``devin-janitor-reap.sh`` — it execs ``"$@"`` verbatim, so its
# argument tail IS the wrapped command line.
_JANITOR_EXE = re.compile(
    r"^devin-janitor(?:\.(?:exe|bat|cmd|ps1|sh))?$", re.IGNORECASE
)
_REAP_WRAPPER_EXE = re.compile(
    r"^devin-janitor-reap(?:\.(?:sh|exe|bat|cmd|ps1))?$", re.IGNORECASE
)

# janitor cli.py defaults (devin_janitor/cli.py + reap.py) — mirrored so
# an ExecStart that omits the flags resolves the same paths the tool
# would use, relative to the unit's WorkingDirectory (home for user
# units and cron).
_DEFAULT_STATE_FILE = ".devin/janitor-reap.json"
_DEFAULT_LEDGER_FILE = ".devin/memory/janitor-reap-ledger.jsonl"

_CRON_SPECIAL_INTERVALS = {
    "@hourly": 3600.0,
    "@daily": 86400.0,
    "@midnight": 86400.0,
    "@weekly": 604800.0,
    "@monthly": 31 * 86400.0,
    "@yearly": 366 * 86400.0,
    "@annually": 366 * 86400.0,
}

_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_DOW_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}

_TIMESPAN_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([A-Za-zµ]+)?")
_TIMESPAN_UNITS = {
    "us": 1e-6,
    "usec": 1e-6,
    "µs": 1e-6,
    "ms": 1e-3,
    "msec": 1e-3,
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "min": 60.0,
    "mins": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "w": 604800.0,
    "week": 604800.0,
    "weeks": 604800.0,
    "month": 30 * 86400.0,
    "months": 30 * 86400.0,
    "y": 365 * 86400.0,
    "year": 365 * 86400.0,
    "years": 365 * 86400.0,
}


@dataclass(frozen=True)
class _ReapArgs:
    """``devin-janitor reap`` arguments resolved to absolute paths."""

    apply: bool
    state_file: Path
    ledger_file: Path
    disable_file: Path | None


@dataclass(frozen=True)
class _Install:
    """One deduplicated reap schedule.

    ``scheduled=False`` marks a reap unit (service file) that nothing
    fires — installed but inert by absence of a trigger.
    """

    surface: str  # "systemd-user" | "cron" | "task-scheduler"
    label: str  # unit/task name (or redacted cron command) for messages
    command: str  # raw scheduled command
    args: _ReapArgs | None  # None → no parseable reap invocation
    interval_s: float | None = None  # expected max gap between sweeps
    timer_unit: str | None = None  # systemd timer name, when known
    scheduled: bool = True


# ---------------------------------------------------------------------------
# ExecStart / cron command parsing
# ---------------------------------------------------------------------------


def _lex(command: str, posix: bool = True) -> list[str]:
    """Punctuation-aware tokenize — shell operators surface as their own
    tokens (same lexer automation uses for scheduled commands).
    ``posix=False`` keeps Windows path backslashes literal for the
    Task Scheduler surface (cron/systemd commands are POSIX)."""
    try:
        lexer = shlex.shlex(command, posix=posix, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.split()


def _resolve_arg_path(value: str, cwd: Path, home: Path) -> Path:
    """``~``/``%h``/env-var expansion plus cwd-relative resolution —
    systemd ExecStart specifiers and cron's home cwd both land here."""
    expanded = os.path.expandvars(value).replace("%h", str(home))
    path = Path(os.path.expanduser(expanded))
    # Only genuinely relative paths join cwd. A rooted-but-driveless
    # path (``/a/b`` on Windows — ``is_absolute()`` is False there yet
    # ``root`` is set) must pass through unchanged: joining it would
    # silently re-root it onto cwd's drive.
    if not path.is_absolute() and not path.root:
        path = cwd / path
    return path


def _args_from_tokens(tokens: list[str], cwd: Path, home: Path) -> _ReapArgs:
    """Flag scan over the tokens following the ``reap`` subcommand —
    ``--flag value`` and ``--flag=value`` forms; stops at the first
    shell operator so trailing ``&& …`` payloads are not misread."""
    apply_ = False
    state_file: Path | None = None
    ledger_file: Path | None = None
    disable_file: Path | None = None
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        if _OPERATOR.fullmatch(token):
            break
        if token == "--":
            i += 1
            continue
        if token.startswith("--"):
            flag, eq, inline = token[2:].partition("=")
            if flag == "apply":
                apply_ = True
            elif flag in _PATH_FLAGS:
                value = inline if eq else ""
                if not eq:
                    if (
                        i + 1 < n
                        and not _OPERATOR.fullmatch(tokens[i + 1])
                        and not tokens[i + 1].startswith("--")
                    ):
                        i += 1
                        value = tokens[i]
                    else:
                        i += 1
                        continue
                resolved = _resolve_arg_path(value, cwd, home)
                if flag == "state-file":
                    state_file = resolved
                elif flag == "ledger-file":
                    ledger_file = resolved
                else:
                    disable_file = resolved
        i += 1
    return _ReapArgs(
        apply=apply_,
        state_file=state_file or _resolve_arg_path(_DEFAULT_STATE_FILE, cwd, home),
        ledger_file=ledger_file or _resolve_arg_path(_DEFAULT_LEDGER_FILE, cwd, home),
        disable_file=disable_file,
    )


def _parse_reap_args(
    command: str,
    cwd: Path,
    home: Path,
    depth: int = 0,
    posix: bool = True,
) -> _ReapArgs | None:
    """``_ReapArgs`` for the first ``devin-janitor reap`` invocation in
    ``command``, ``None`` when there is none.

    ``reap`` only counts as the janitor subcommand in *command
    position* — the token right after a ``devin-janitor`` executable
    (or after the ``devin-janitor-reap`` wrapper, whose arguments ARE
    the wrapped command line) at the start of a pipeline segment.
    Arguments to other programs — ``grep 'devin-janitor reap' …``
    included — are never invocations; only executable payloads recurse
    (a POSIX shell's ``-c`` string, the wrapper's argument tail),
    bounded by ``depth``. ``cd DIR`` prefixes move relative flag-path
    resolution and are tracked only up to the ``reap`` token itself —
    a ``cd`` later in the line cannot retroactively move them."""
    tokens: list[str] = []
    for raw in _lex(command, posix):
        # posix=False keeps quote characters on grouped tokens —
        # strip them so '"C:\\janitor.exe"' still reads as a path.
        token = raw.strip("\"'")
        if token:
            tokens.append(token)
    return _scan_reap_tokens(tokens, cwd, home, depth, posix)


def _scan_reap_tokens(
    tokens: list[str], cwd: Path, home: Path, depth: int, posix: bool
) -> _ReapArgs | None:
    """Left-to-right scan of one command line for the first
    ``devin-janitor reap`` invocation — see ``_parse_reap_args`` for
    the recognition rules. Command position opens at the start and
    after every shell operator; env assignments, control words
    (``sudo``, ``env``, …) and stray option tokens keep it open, the
    same heuristic automation's target extraction uses."""
    expect_cmd = True
    cd_next = False
    shell_next = False
    redirect_next = False
    last_cmd = ""
    i = 0
    n = len(tokens)
    while i < n:
        token = tokens[i]
        if _OPERATOR.fullmatch(token):
            redirect_next = ">" in token or "<" in token
            if not redirect_next:
                # ';'/'&&'/'|'/… open a new command position; a
                # redirection does not — '> f cmd' keeps the position
                # that preceded it (leading redirects are legal shell).
                expect_cmd = True
                last_cmd = ""
            cd_next = shell_next = False
            i += 1
            continue
        if redirect_next:
            redirect_next = False
            i += 1
            continue
        if cd_next:
            cwd = _resolve_arg_path(token, cwd, home)
            cd_next = False
            expect_cmd = False
            i += 1
            continue
        if shell_next:
            # POSIX-shell '-c' payload — a nested command line.
            shell_next = False
            expect_cmd = False
            if depth < 2:
                sub = _parse_reap_args(token, cwd, home, depth + 1, posix)
                if sub is not None:
                    return sub
            i += 1
            continue
        if expect_cmd:
            if automation._ENV_ASSIGNMENT.match(token):
                i += 1
                continue
            if token in automation._CONTROL_WORDS:
                i += 1
                continue
            if token[0].isdigit():
                # numeric argument of a prefix wrapper ('timeout 60 cmd',
                # 'nice -n 5 cmd') — never a command word itself.
                i += 1
                continue
            base = token.replace("\\", "/").rsplit("/", 1)[-1].lower()
            if base == "cd":
                nxt = tokens[i + 1] if i + 1 < n else None
                if nxt is None or _OPERATOR.fullmatch(nxt):
                    cwd = home  # bare 'cd' lands in $HOME
                else:
                    cd_next = True
                expect_cmd = False
                i += 1
                continue
            if token.startswith("-"):
                # option in command position ('sudo -E cmd') — does not
                # consume command position.
                i += 1
                continue
            expect_cmd = False
            if _JANITOR_EXE.match(base):
                # the subcommand must be the next token ('--' tolerated)
                j = i + 1
                if j < n and tokens[j] == "--":
                    j += 1
                if j < n and tokens[j] == "reap":
                    return _args_from_tokens(tokens[j + 1 :], cwd, home)
                # 'devin-janitor run …' — keep scanning later segments
            elif _REAP_WRAPPER_EXE.match(base) and depth < 2:
                # the wrapper execs its argument tail verbatim — recurse
                # on the rest of THIS segment only.
                end = i + 1
                while end < n and not _OPERATOR.fullmatch(tokens[end]):
                    end += 1
                sub = _scan_reap_tokens(
                    tokens[i + 1 : end], cwd, home, depth + 1, posix
                )
                if sub is not None:
                    return sub
            last_cmd = base
            i += 1
            continue
        if token == "-c" and automation._POSIX_SHELL.fullmatch(last_cmd):
            shell_next = True
        i += 1
    return None


def _classify(
    name: str, command: str, cwd: Path, home: Path, posix: bool = True
) -> tuple[str | None, _ReapArgs | None]:
    """``("reap", args)`` for a real reap invocation, ``("unverifiable",
    None)`` for a janitor+reap-named unit whose command cannot be read,
    ``(None, None)`` otherwise."""
    args = (
        _parse_reap_args(command, cwd, home, posix=posix) if command else None
    )
    if args is not None and "janitor" in f"{name} {command}".lower():
        return "reap", args
    if "janitor" in name.lower() and _REAP_WORD.search(name):
        return "unverifiable", None
    return None, None


# ---------------------------------------------------------------------------
# schedule enumeration — reuses automation's private surface probes
# ---------------------------------------------------------------------------


def _cron_lines(text: str) -> list[tuple[str, str]]:
    """``(schedule, command)`` pairs from ``crontab -l`` output —
    comments and ``VAR=value`` environment lines skipped, ``@special``
    schedules kept whole."""
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if automation._ENV_ASSIGNMENT.match(line):
            continue
        if automation._CRON_SPECIALS.match(line.split(None, 1)[0]):
            parts = line.split(None, 1)
            if len(parts) == 2:
                out.append((parts[0], parts[1].strip()))
        else:
            parts = line.split(None, 5)
            if len(parts) >= 6:
                out.append((" ".join(parts[:5]), parts[5].strip()))
    return out


# ---------------------------------------------------------------------------
# schedule intervals — staleness needs 'how often should this fire'
# ---------------------------------------------------------------------------


def _timespan_seconds(text: str) -> float | None:
    """systemd timespan (``3min``, ``1h 30min``, ``90s``) → seconds."""
    total = 0.0
    matched = False
    for match in _TIMESPAN_PART.finditer(text):
        unit = (match.group(2) or "s").lower()
        if unit not in _TIMESPAN_UNITS:
            return None
        total += float(match.group(1)) * _TIMESPAN_UNITS[unit]
        matched = True
    return total if matched else None


_CALENDAR_STAMP = re.compile(
    r"(?:Next elapse|Iter(?:ation)?\.?\s*#\d+):\s+(?:\w+\s+)?"
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
)


def _calendar_interval_seconds(runner, expr: str) -> float | None:
    """Longest gap between elapses of a systemd ``OnCalendar`` expr, via
    ``systemd-analyze calendar --iterations`` — ``None`` when the tool
    or the expression cannot be resolved."""
    res = runner(["systemd-analyze", "calendar", "--iterations=4", expr])
    if res is None or res.returncode != 0:
        return None
    stamps: list[datetime] = []
    for line in (res.stdout or "").splitlines():
        match = _CALENDAR_STAMP.search(line)
        if match:
            try:
                stamps.append(
                    datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=timezone.utc
                    )
                )
            except ValueError:
                pass
    if len(stamps) < 2:
        return None
    stamps.sort()
    diffs = [(later - earlier).total_seconds() for earlier, later in pairwise(stamps)]
    return max(diffs) if diffs else None


def _service_unit_for_timer(runner, timer_unit: str) -> str:
    """Unidade onde os erros do oneshot ficam registados: o serviço
    que o timer activa — ``Unit=`` quando declarado, senão o
    ``.service`` homónimo. É a unidade certa para 'journalctl -u'
    quando se inspecciona uma sweep que morre a meio."""
    res = runner(["systemctl", "--user", "cat", timer_unit])
    if res is not None and res.returncode == 0:
        _cmds, _wd, activates = automation._unit_payload(res.stdout or "")
        if activates:
            return activates
    if timer_unit.endswith(".timer"):
        return timer_unit[: -len(".timer")] + ".service"
    return timer_unit


def _timer_interval_seconds(runner, timer_unit: str) -> float | None:
    """Expected max gap between sweeps for a systemd timer — the minimum
    period across its recurring directives (``OnUnitActiveSec`` & co.
    timespans, ``OnCalendar`` exprs). Boot-only timers have no periodic
    interval → ``None``."""
    res = runner(["systemctl", "--user", "cat", timer_unit])
    if res is None or res.returncode != 0:
        return None
    periods: list[float] = []
    calendars: list[str] = []
    for unit_line in (res.stdout or "").splitlines():
        stripped = unit_line.strip()
        for prefix in (
            "OnUnitActiveSec=",
            "OnActiveSec=",
            "OnUnitInactiveSec=",
        ):
            if stripped.startswith(prefix):
                value = _timespan_seconds(stripped[len(prefix) :].strip())
                if value:
                    periods.append(value)
        if stripped.startswith("OnCalendar="):
            expr = stripped.split("=", 1)[1].strip()
            if expr:
                calendars.append(expr)
    for expr in calendars:
        value = _calendar_interval_seconds(runner, expr)
        if value:
            periods.append(value)
    return min(periods) if periods else None


def _cron_num(token: str, names: dict[str, int]) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return names.get(token)


def _cron_values(
    field: str, lo: int, hi: int, names: dict[str, int]
) -> set[int] | None:
    """Allowed values of one cron field — ``None`` for unrestricted ``*``.
    Raises ``ValueError`` on malformed input."""
    field = field.strip().lower()
    if field == "*":
        return None
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        rng = part
        if "/" in part:
            rng, step_text = part.split("/", 1)
            try:
                step = int(step_text)
            except ValueError:
                raise ValueError(f"bad cron step {part!r}") from None
        if step < 1:
            raise ValueError(f"bad cron step {part!r}")
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            start_text, end_text = rng.split("-", 1)
            start = _cron_num(start_text, names)
            end = _cron_num(end_text, names)
            if start is None or end is None:
                raise ValueError(f"bad cron range {part!r}")
        else:
            start = end = _cron_num(rng, names)
            if start is None:
                raise ValueError(f"bad cron value {part!r}")
        start = max(start, lo)
        end = min(end, hi)
        values.update(range(start, end + 1, step))
    return values


def _cron_interval_seconds(schedule: str) -> float | None:
    """Longest gap between fires of a cron schedule, found by simulating
    minute-by-minute over ``_CRON_HORIZON`` — ``None`` for ``@reboot``,
    unparseable fields or schedules too sparse to measure."""
    schedule = schedule.strip()
    if schedule in _CRON_SPECIAL_INTERVALS:
        return _CRON_SPECIAL_INTERVALS[schedule]
    if schedule.startswith("@"):
        return None
    fields = schedule.split()
    if len(fields) != 5:
        return None
    try:
        minute = _cron_values(fields[0], 0, 59, {})
        hour = _cron_values(fields[1], 0, 23, {})
        dom = _cron_values(fields[2], 1, 31, {})
        month = _cron_values(fields[3], 1, 12, _MONTH_NAMES)
        dow = _cron_values(fields[4], 0, 7, _DOW_NAMES)
    except ValueError:
        return None
    if dow is not None:
        # cron accepts 0-7 for Sunday — normalize 7 onto 0.
        dow = {0 if value == 7 else value for value in dow}
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    horizon_minutes = int(_CRON_HORIZON.total_seconds() // 60)
    first = prev = None
    fires = 0
    max_gap = 0
    for offset in range(horizon_minutes):
        moment = base + timedelta(minutes=offset)
        if minute is not None and moment.minute not in minute:
            continue
        if hour is not None and moment.hour not in hour:
            continue
        if month is not None and moment.month not in month:
            continue
        dom_ok = dom is None or moment.day in dom
        cron_dow = (moment.weekday() + 1) % 7  # cron 0/7 = Sunday
        dow_ok = dow is None or cron_dow in dow
        if dom is None and dow is None:
            day_ok = True
        elif dom is None:
            day_ok = dow_ok
        elif dow is None:
            day_ok = dom_ok
        else:
            # both restricted → cron ORs them
            day_ok = dom_ok or dow_ok
        if not day_ok:
            continue
        fires += 1
        if first is None:
            first = offset
        if prev is not None:
            max_gap = max(max_gap, offset - prev)
        prev = offset
    if fires < 2 or first is None or prev is None:
        return None
    max_gap = max(max_gap, first + horizon_minutes - prev)
    return max_gap * 60.0


# ---------------------------------------------------------------------------
# filesystem / systemd state probes
# ---------------------------------------------------------------------------


def _first_seen_epoch(state_file: Path) -> float | None:
    """Oldest ``first_seen`` timestamp in the reap state file — the
    earliest sweep that tracked an orphan, i.e. install-age evidence."""
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    first_seen = data.get("first_seen") if isinstance(data, dict) else None
    if not isinstance(first_seen, dict):
        return None
    epochs = [
        float(value) for value in first_seen.values() if isinstance(value, (int, float))
    ]
    return min(epochs) if epochs else None


def _birth_epoch(path: Path, runner) -> float | None:
    """Creation epoch (statx btime) of ``path`` — the state file's
    birth is the first sweep that ever ran, so it dates the install
    even when ``first_seen`` stayed empty (a quiet machine records no
    orphans). ``None`` when the filesystem or ``stat`` cannot report a
    btime."""
    res = runner(["stat", "-c", "%W", str(path)])
    if res is not None and res.returncode == 0:
        lines = [
            line.strip()
            for line in (res.stdout or "").splitlines()
            if line.strip()
        ]
        if lines:
            try:
                btime = int(lines[-1])
            except ValueError:
                btime = 0
            if btime > 0:
                return float(btime)
    try:
        birthtime = getattr(path.stat(), "st_birthtime", None)
    except OSError:
        return None
    return float(birthtime) if birthtime else None


def _fragment_path(runner, unit: str) -> Path | None:
    """FragmentPath of a systemd unit via ``systemctl show --value``."""
    res = runner(["systemctl", "--user", "show", unit, "-p", "FragmentPath", "--value"])
    if res is None or res.returncode != 0:
        return None
    lines = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    value = lines[-1]
    path = Path(value)
    # is_absolute() — not a "/" prefix check — so Windows-absolute
    # fragment paths (C:\…) still resolve when tests fake systemd
    # output on a Windows runner.
    return path if path.is_absolute() else None


def _install_age_seconds(inst: _Install, now: float, runner) -> float | None:
    """Age of the reaper install in seconds, or ``None`` when nothing
    datable exists. Evidence: the oldest ``first_seen`` epoch (a sweep
    actually ran then) and unit fragment mtimes (install/last-edit).
    Fragment probes are skipped once ``first_seen`` alone proves the
    calibration window elapsed."""
    state_file = inst.args.state_file if inst.args else None
    first_seen = _first_seen_epoch(state_file) if state_file is not None else None
    if first_seen is not None and now - first_seen >= CALIBRATION_SECONDS:
        return now - first_seen
    evidences: list[float] = []
    if first_seen is not None:
        evidences.append(first_seen)
    if inst.args is not None:
        # cron installs have no unit fragment to date — the state and
        # ledger files' birth times mark the first sweep that wrote.
        for dated in (inst.args.state_file, inst.args.ledger_file):
            birth = _birth_epoch(dated, runner)
            if birth is not None:
                evidences.append(birth)
    if inst.timer_unit:
        unit_names = [inst.timer_unit]
        if inst.timer_unit.endswith(".timer"):
            unit_names.append(inst.timer_unit[: -len(".timer")] + ".service")
        for unit in unit_names:
            fragment = _fragment_path(runner, unit)
            if fragment is not None:
                try:
                    evidences.append(fragment.stat().st_mtime)
                except OSError:
                    pass
    if not evidences:
        return None
    return now - min(evidences)


def _ledger_has_evidence(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _systemctl_prop(runner, verb: str, unit: str) -> str | None:
    """First stdout line of ``systemctl --user <verb> <unit>`` — the
    state word for is-active/is-enabled regardless of exit code."""
    res = runner(["systemctl", "--user", verb, unit])
    if res is None:
        return None
    lines = [line.strip() for line in (res.stdout or "").splitlines() if line.strip()]
    return lines[0] if lines else None


_LAST_TRIGGER_STAMP = re.compile(
    r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
)


def _last_trigger_s(runner, unit: str) -> float | None:
    """Epoch seconds when the timer last fired its service — ``None``
    when never/unknown. ``LastTriggerUSec`` prints as a formatted local
    timestamp (``Thu 2026-11-26 05:30:00 WET``) or ``n/a``; the raw
    usec count lives in ``LastTriggerUSecMonotonic``, which shares
    CLOCK_MONOTONIC's base with ``time.monotonic()``."""
    res = runner(
        ["systemctl", "--user", "show", unit, "-p", "LastTriggerUSec", "--value"]
    )
    if res is not None and res.returncode == 0:
        lines = [
            line.strip() for line in (res.stdout or "").splitlines() if line.strip()
        ]
        if lines:
            value = lines[-1]
            try:
                usec = int(value)
            except ValueError:
                usec = None
            if usec is not None:
                if usec > 0:
                    return usec / 1_000_000
            else:
                match = _LAST_TRIGGER_STAMP.search(value)
                if match is not None:
                    try:
                        return (
                            datetime.strptime(
                                match.group(1).replace("T", " "),
                                "%Y-%m-%d %H:%M:%S",
                            )
                            .astimezone()  # naive → local tz, like systemctl
                            .timestamp()
                        )
                    except ValueError:
                        pass
    res = runner(
        [
            "systemctl", "--user", "show", unit,
            "-p", "LastTriggerUSecMonotonic", "--value",
        ]
    )
    if res is None or res.returncode != 0:
        return None
    lines = [
        line.strip() for line in (res.stdout or "").splitlines() if line.strip()
    ]
    if not lines:
        return None
    try:
        usec = int(lines[-1])
    except ValueError:
        return None
    if usec <= 0:
        return None
    return time.time() - max(0.0, time.monotonic() - usec / 1_000_000)


# ---------------------------------------------------------------------------
# per-install evaluation
# ---------------------------------------------------------------------------


def _evaluate(
    inst: _Install, *, now: float, plat: str, runner
) -> tuple[list[str], list[str]]:
    """``(reasons, fix_lines)`` — empty reasons means healthy."""
    if plat != "linux":
        return (
            [
                (
                    f"scheduled on {plat} but 'devin-janitor reap' is "
                    "Linux-only (reads /proc) — every sweep exits 2"
                )
            ],
            ["Remove the schedule or run it on a Linux host."],
        )
    if not inst.scheduled:
        return (
            [
                (
                    f"reap unit '{inst.label}' exists but no timer/cron "
                    "entry fires it — sweeps never run"
                )
            ],
            [
                (
                    "Enable the matching timer "
                    f"(systemctl --user enable --now {inst.label}) or "
                    "remove the stale unit."
                )
            ],
        )
    args = inst.args
    if args is None:
        return (
            [
                (
                    f"scheduled entry '{inst.label}' has no parseable "
                    "'reap' invocation — inertness cannot be verified"
                )
            ],
            ["Inspect the unit's ExecStart / cron line by hand."],
        )

    reasons: list[str] = []
    fixes: list[str] = []

    disable_file = args.disable_file or (args.state_file.parent / "reap.disabled")
    try:
        disabled = disable_file.exists()
    except OSError:
        disabled = False
    if disabled:
        reasons.append(
            f"kill-switch {disable_file} exists — every sweep exits "
            "before any /proc scan"
        )
        fixes.append(f"Remove {disable_file} to re-enable the reaper.")

    ledger_ok = _ledger_has_evidence(args.ledger_file)
    # The calibration gate needs install-age evidence only when a gated
    # signal could fire — a fully --apply install with a populated
    # ledger skips the fragment probes entirely.
    age = (
        _install_age_seconds(inst, now, runner)
        if (not args.apply or not ledger_ok)
        else None
    )
    elapsed = age is not None and age >= CALIBRATION_SECONDS

    if not args.apply and elapsed:
        reasons.append(
            "no --apply although the calibration window has elapsed "
            f"({age / 86400:.0f}d of sweep evidence) — dry-run forever: "
            "would_kill decisions are recorded but nothing is reaped"
        )
        fixes.append(
            "Add --apply to the scheduled command once the ledger "
            "review is done (spec: ~2 weeks of reviewed precision)."
        )
    if not ledger_ok and elapsed:
        try:
            ledger_missing = not args.ledger_file.exists()
        except OSError:
            ledger_missing = True
        reasons.append(
            f"ledger {args.ledger_file} is "
            f"{'missing' if ledger_missing else 'empty'} — zero "
            "would_kill/killed/survivor/circuit decisions recorded; an "
            "inert reaper (e.g. attribution_ok:false) looks identical "
            "in the sweep summary"
        )
        fixes.append(
            "Verify devin_office resolves inside the janitor venv so "
            "attribution works, then run 'devin-janitor reap --json' "
            "once by hand and check attribution_ok."
        )

    try:
        state_mtime = args.state_file.stat().st_mtime
    except OSError:
        state_mtime = None
    if state_mtime is not None and inst.interval_s:
        stale_after = 2 * inst.interval_s + _STALE_GRACE_SECONDS
        if now - state_mtime > stale_after:
            reasons.append(
                f"state file {args.state_file} untouched for "
                f"{(now - state_mtime) / 3600:.1f}h while the schedule "
                f"fires about every {inst.interval_s / 60:.0f}min — "
                "sweeps are not completing"
            )
            fixes.append(
                "Check the timer/cron job's recent runs — the sweep may "
                "be failing before it writes state."
            )

    if inst.timer_unit:
        active = _systemctl_prop(runner, "is-active", inst.timer_unit)
        if active is not None and active != "active":
            reasons.append(
                f"timer {inst.timer_unit} reports '{active}' — sweeps "
                "are installed but not firing"
            )
            fixes.append(
                f"systemctl --user start {inst.timer_unit} (and enable "
                "it to survive reboot)."
            )
        enabled = _systemctl_prop(runner, "is-enabled", inst.timer_unit)
        if enabled in ("disabled", "masked"):
            reasons.append(
                f"timer {inst.timer_unit} is {enabled} — it will not restart on boot"
            )
            fixes.append(f"systemctl --user enable {inst.timer_unit}.")
        last_trigger = _last_trigger_s(runner, inst.timer_unit)
        if last_trigger is not None and state_mtime is None:
            reasons.append(
                f"timer last fired {(now - last_trigger) / 60:.0f}min "
                f"ago but {args.state_file} does not exist — sweeps "
                "start but never finish a state write"
            )
            fixes.append(
                "Inspect 'journalctl --user -u "
                f"{_service_unit_for_timer(runner, inst.timer_unit)}' — "
                "the oneshot is likely dying mid-sweep (timeout, "
                "wrapper guard, missing binary)."
            )
        elif (
            last_trigger is not None
            and last_trigger - state_mtime > _TRIGGER_GRACE_SECONDS
        ):
            reasons.append(
                f"timer last fired {(now - last_trigger) / 60:.0f}min "
                f"ago but {args.state_file} was last written "
                f"{(now - state_mtime) / 3600:.1f}h ago — sweeps start "
                "but never finish a state write"
            )
            fixes.append(
                "Inspect 'journalctl --user -u "
                f"{_service_unit_for_timer(runner, inst.timer_unit)}' — "
                "the oneshot is likely dying mid-sweep (timeout, "
                "wrapper guard, missing binary)."
            )
    return reasons, fixes


def _healthy_message(inst: _Install) -> str:
    args = inst.args
    if args is None:
        return f"janitor reap ({inst.surface}:{inst.label}): scheduled"
    mode = "--apply" if args.apply else "dry-run"
    ledger = (
        "ledger has decision evidence"
        if _ledger_has_evidence(args.ledger_file)
        else "ledger empty (no decisions yet)"
    )
    try:
        state = (
            "state file present"
            if args.state_file.exists()
            else "no state file (empty sweep state)"
        )
    except OSError:
        state = "state file unreadable"
    cadence = (
        f"about every {inst.interval_s / 60:.0f}min"
        if inst.interval_s
        else "interval unknown"
    )
    return (
        f"janitor reap ({inst.surface}:{inst.label}): {mode}, {ledger}, "
        f"{state}, {cadence}"
    )


# ---------------------------------------------------------------------------
# check entry point
# ---------------------------------------------------------------------------


def _systemd_reap_installs(runner, home: Path, tasks) -> list[_Install]:
    """Build ``_Install`` rows from already-enumerated systemd tasks.

    ``.service`` rows duplicating a ``.timer``'s command collapse into
    the timer's install — the timer carries the schedule. A service
    left over with no timer sibling is 'installed but unscheduled'."""
    reap: list[tuple[automation._Task, str, _ReapArgs | None]] = []
    for task in tasks:
        cwd = Path(task.cwd) if task.cwd else home
        kind, args = _classify(task.name, task.command, cwd, home)
        if kind is not None:
            reap.append((task, kind, args))
    timer_cmds = {
        task.command
        for task, _kind, _args in reap
        if task.name.endswith(".timer") and task.command
    }
    installs: list[_Install] = []
    for task, _kind, args in reap:
        is_timer = task.name.endswith(".timer")
        if not is_timer and task.command and task.command in timer_cmds:
            continue
        installs.append(
            _Install(
                surface="systemd-user",
                label=task.name,
                command=task.command,
                args=args,
                # one extra unit cat per timer — the schedule interval
                # the staleness check compares state mtimes against.
                interval_s=(
                    _timer_interval_seconds(runner, task.name) if is_timer else None
                ),
                timer_unit=task.name if is_timer else None,
                scheduled=is_timer,
            )
        )
    return installs


def run(
    ctx: Context,
    *,
    runner=None,
    which=None,
    plat: str | None = None,
    home=None,
    now_s: float | None = None,
) -> list[Finding]:
    runner = runner or automation._run
    which = which or shutil.which
    plat = plat or platform_name()
    home = Path(home).expanduser() if home is not None else Path.home()
    now = float(now_s) if now_s is not None else ctx.now() / 1000.0

    installs: list[_Install] = []
    answered: list[str] = []
    janitor_seen = False

    # --- cron: parsed directly — automation's enumerator strips the
    # schedule fields this check needs for the staleness interval.
    res = runner(["crontab", "-l"])
    if res is not None:
        if res.returncode == 0:
            answered.append("cron")
            for schedule, command in _cron_lines(res.stdout or ""):
                if "janitor" not in command.lower():
                    continue
                janitor_seen = True
                # base cwd is cron's HOME — 'cd DIR' prefixes inside the
                # line are tracked by the parser up to the reap token.
                args = _parse_reap_args(command, home, home)
                if args is None:
                    continue
                installs.append(
                    _Install(
                        surface="cron",
                        label=automation._redact(command)[:80],
                        command=command,
                        args=args,
                        interval_s=_cron_interval_seconds(schedule),
                    )
                )
        else:
            output = f"{res.stderr or ''}\n{res.stdout or ''}".lower()
            if "no crontab" in output:
                answered.append("cron")  # answered: genuinely empty
            # other failures stay 'unavailable' — automation reports them

    # --- systemd user units (services + timers, loaded and installed)
    # SEM predicate: a reaper-detecção decide pelo COMANDO da unidade,
    # não pelo nome — um timer chamado devin-cleanup.timer cujo
    # ExecStart corre o janitor tem de ser inspeccionado na mesma.
    systemd_tasks, _reason, systemd_notes = automation._systemd_user_tasks(
        runner
    )
    if systemd_tasks is not None:
        answered.append("systemd-user")
        for task in systemd_tasks:
            if "janitor" in f"{task.name} {task.command}".lower():
                janitor_seen = True
        installs += _systemd_reap_installs(runner, home, systemd_tasks)

    # --- Windows Task Scheduler (the enumerator self-skips off Windows).
    # limit=None: detection is by COMMAND, so the reaper may hide behind
    # any task name and automation's 200-task inventory cap must not
    # silently apply — every listed task gets its /xml probed. Tasks
    # whose XML stays unreadable are surfaced via the notes instead.
    ts_tasks, _reason, ts_notes = automation._task_scheduler_tasks(
        runner, plat, limit=None
    )
    if ts_tasks is not None:
        answered.append("task-scheduler")
        for task in ts_tasks:
            if "janitor" in f"{task.name} {task.command}".lower():
                janitor_seen = True
            kind, args = _classify(
                task.name, task.command, home, home, posix=False
            )
            if kind is None:
                continue
            installs.append(
                _Install(
                    surface="task-scheduler",
                    label=task.name,
                    command=task.command,
                    args=args,
                    scheduled=True,
                )
            )

    if not installs:
        if not answered:
            return [
                Finding(
                    CHECK_ID,
                    Status.WARN,
                    "no scheduling surface available — reap schedule unverifiable",
                )
            ]
        if which("devin-janitor") or janitor_seen:
            # Enumeration notes mean part of a surface went uninspected
            # (unreadable unit cats, failed schtasks /xml) — 'not
            # scheduled' would be a claim the evidence cannot support.
            enum_notes = [
                f"{surface}: {note}"
                for surface, notes in (
                    ("systemd-user", systemd_notes),
                    ("task-scheduler", ts_notes),
                )
                for note in notes
            ]
            if enum_notes:
                return [
                    Finding(
                        CHECK_ID,
                        Status.WARN,
                        "devin-janitor present but whether 'reap' is "
                        "scheduled cannot be ruled out — enumeration "
                        "incomplete: " + "; ".join(enum_notes),
                        fix=(
                            "Re-run once the failing probes recover, or "
                            "inspect the listed units/tasks by hand — an "
                            "unreadable entry may still schedule the "
                            "reaper."
                        ),
                    )
                ]
            return [
                Finding(
                    CHECK_ID,
                    Status.PASS,
                    "devin-janitor present but 'reap' is not scheduled — "
                    "the orphan reaper never runs",
                )
            ]
        return []  # no janitor traces — 'not detected' like peers

    findings: list[Finding] = []
    for inst in installs:
        reasons, fixes = _evaluate(inst, now=now, plat=plat, runner=runner)
        if reasons:
            findings.append(
                Finding(
                    CHECK_ID,
                    Status.WARN,
                    f"janitor reap ({inst.surface}:{inst.label}): "
                    + "; ".join(reasons),
                    fix="\n".join(f"- {fix}" for fix in fixes),
                )
            )
        else:
            findings.append(Finding(CHECK_ID, Status.PASS, _healthy_message(inst)))
    return findings
