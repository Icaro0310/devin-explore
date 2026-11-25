"""janitor-reap check: a scheduled ``devin-janitor reap`` that is
silently inert — the issue #113 incident shape (attribution_ok:false
for ~2 weeks, indistinguishable from healthy in sweep summaries).

All surface probes are pure fakes: ``runner`` maps argv to canned
``CompletedProcess`` outputs; no real crontab/systemctl/schtasks call
ever runs. ``now_s`` pins 'now' so state-file mtimes, first_seen
epochs and timer triggers are deterministic.
"""

import json
import os
import subprocess
import time
from pathlib import Path

from devin_doctor.checks import janitor
from devin_doctor.model import Status


def _proc(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


def make_runner(table):
    """argv tuple -> (rc, stdout[, stderr]). Missing key -> tool absent."""

    def runner(argv):
        entry = table.get(tuple(argv))
        if entry is None:
            return None
        if len(entry) == 2:
            rc, stdout = entry
            return _proc(stdout, rc)
        rc, stdout, stderr = entry
        return _proc(stdout, rc, stderr)

    return runner


def _no_surfaces(argv):
    return None


def _find(findings, needle):
    return [f for f in findings if needle in f.message]


NOW = time.time()
DAY = 24 * 3600

EXEC_APPLY = (
    "/home/u/.local/bin/devin-janitor reap --apply --json "
    "--state-file {state} --ledger-file {ledger}"
)
EXEC_DRY = (
    "/home/u/.local/bin/devin-janitor reap --json "
    "--state-file {state} --ledger-file {ledger}"
)

TIMER_UNIT = """\
[Unit]
Description=devin-janitor reap sweep

[Timer]
OnBootSec=3min
OnUnitActiveSec=3min
AccuracySec=30s

[Install]
WantedBy=timers.target
"""

SERVICE_TEMPLATE = """\
[Service]
Type=oneshot
ExecStart={exec_start}
TimeoutStartSec=60
"""


def _write_state(path, first_seen=None, pidmap=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "first_seen": first_seen or {},
                "pidmap": pidmap or {},
            }
        ),
        encoding="utf-8",
    )
    return path


def _systemd_table(
    tmp_path,
    *,
    exec_start,
    timer_text=TIMER_UNIT,
    service_text=None,
    units=None,
    is_active="active",
    is_enabled="enabled",
    fragment=None,
    last_trigger_us=None,
    calendar=None,
):
    """Runner table for a devin-janitor-reap.timer + .service pair."""
    state = tmp_path / "state" / "janitor-reap.json"
    ledger = tmp_path / "memory" / "janitor-reap-ledger.jsonl"
    # as_posix(): backslash paths would not survive the POSIX shlex
    # lexer the check applies to scheduled commands on Windows.
    cmd = exec_start.format(state=state.as_posix(), ledger=ledger.as_posix())
    if service_text is None:
        service_text = SERVICE_TEMPLATE.format(exec_start=cmd)
    unit_names = units or [
        "devin-janitor-reap.timer   loaded active waiting   devin-janitor-reap.timer",
        "devin-janitor-reap.service loaded inactive dead    devin-janitor-reap.service",
    ]
    table = {
        (
            "systemctl",
            "--user",
            "list-units",
            "--type=service,timer",
            "--all",
            "--no-legend",
            "--no-pager",
        ): (0, "\n".join(unit_names)),
        (
            "systemctl",
            "--user",
            "list-unit-files",
            "--type=service,timer",
            "--no-legend",
            "--no-pager",
        ): (1, ""),
        ("systemctl", "--user", "cat", "devin-janitor-reap.timer"): (0, timer_text),
        ("systemctl", "--user", "cat", "devin-janitor-reap.service"): (0, service_text),
        ("systemctl", "--user", "is-active", "devin-janitor-reap.timer"): (
            0,
            f"{is_active}\n",
        ),
        ("systemctl", "--user", "is-enabled", "devin-janitor-reap.timer"): (
            0,
            f"{is_enabled}\n",
        ),
        ("crontab", "-l"): (1, "no crontab for u", ""),
    }
    frag = fragment if fragment is not None else tmp_path / "frag.timer"
    frag.write_text("# fragment\n")
    table[
        (
            "systemctl",
            "--user",
            "show",
            "devin-janitor-reap.timer",
            "-p",
            "FragmentPath",
            "--value",
        )
    ] = (0, f"{frag}\n")
    table[
        (
            "systemctl",
            "--user",
            "show",
            "devin-janitor-reap.service",
            "-p",
            "FragmentPath",
            "--value",
        )
    ] = (0, f"{frag}\n")
    if last_trigger_us is not None:
        table[
            (
                "systemctl",
                "--user",
                "show",
                "devin-janitor-reap.timer",
                "-p",
                "LastTriggerUSec",
                "--value",
            )
        ] = (
            0,
            # systemctl prints a formatted local timestamp, not raw usec
            time.strftime(
                "%a %Y-%m-%d %H:%M:%S %Z",
                time.localtime(last_trigger_us / 1e6),
            )
            + "\n",
        )
    if calendar is not None:
        table[("systemd-analyze", "calendar", "--iterations=4", calendar)] = (
            0,
            (
                "Normalized form: x\n"
                "    Next elapse: Thu 2026-11-26 05:30:00 WET\n"
                "       Iter. #2: Fri 2026-11-27 05:30:00 WET\n"
                "       Iter. #3: Sat 2026-11-28 05:30:00 WET\n"
                "       Iter. #4: Sun 2026-11-29 05:30:00 WET\n"
            ),
        )
    return table, state, ledger


def _run(ctx, table, **kwargs):
    kwargs.setdefault("now_s", NOW)
    kwargs.setdefault("which", lambda cmd: None)
    kwargs.setdefault("plat", "linux")
    return janitor.run(ctx, runner=make_runner(table), **kwargs)


# ---------------------------------------------------------------------------
# not-detected / not-scheduled
# ---------------------------------------------------------------------------


def test_no_janitor_traces_is_not_detected(ctx):
    table = {
        ("crontab", "-l"): (1, "no crontab for u", ""),
        (
            "systemctl",
            "--user",
            "list-units",
            "--type=service,timer",
            "--all",
            "--no-legend",
            "--no-pager",
        ): (1, ""),
    }
    findings = _run(ctx, table)
    assert findings == []


def test_no_surface_available_warns_unverifiable(ctx):
    findings = janitor.run(
        ctx,
        runner=_no_surfaces,
        which=lambda c: None,
        plat="linux",
        now_s=NOW,
    )
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "no scheduling surface" in findings[0].message


def test_janitor_present_but_reap_not_scheduled(ctx):
    table = {
        ("crontab", "-l"): (0, "0 8 * * * devin-janitor run --apply\n"),
        (
            "systemctl",
            "--user",
            "list-units",
            "--type=service,timer",
            "--all",
            "--no-legend",
            "--no-pager",
        ): (1, ""),
    }
    findings = _run(ctx, table, which=lambda c: "/usr/local/bin/devin-janitor")
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert "not scheduled" in findings[0].message


def test_janitor_cron_seen_without_which(ctx):
    """A non-reap janitor cron line alone marks janitor as present."""
    table = {
        ("crontab", "-l"): (0, "0 8 * * * devin-janitor run --apply\n"),
        (
            "systemctl",
            "--user",
            "list-units",
            "--type=service,timer",
            "--all",
            "--no-legend",
            "--no-pager",
        ): (1, ""),
    }
    findings = _run(ctx, table)  # which -> None
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert "not scheduled" in findings[0].message


# ---------------------------------------------------------------------------
# healthy paths
# ---------------------------------------------------------------------------


def test_healthy_apply_install_passes(ctx, tmp_path):
    table, state, ledger = _systemd_table(
        tmp_path, exec_start=EXEC_APPLY, last_trigger_us=int(NOW * 1e6)
    )
    _write_state(state, first_seen={"1:2": NOW - 3600}, pidmap={"3:4": "s"})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text('{"decision":"killed"}\n')
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert "--apply" in findings[0].message
    assert "devin-janitor-reap.timer" in findings[0].message


def test_dry_run_within_calibration_passes(ctx, tmp_path):
    """No --apply + empty ledger are the DESIGNED state during the
    calibration window — recent first_seen evidence must not warn."""
    table, state, _ledger = _systemd_table(
        tmp_path, exec_start=EXEC_DRY, last_trigger_us=int(NOW * 1e6)
    )
    _write_state(state, first_seen={"1:2": NOW - 3600})
    # ledger intentionally absent — empty is expected this early
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert "dry-run" in findings[0].message


def test_cron_reap_healthy_passes(ctx, tmp_path):
    state = tmp_path / "home" / ".devin" / "janitor-reap.json"
    ledger = tmp_path / "home" / ".devin" / "memory" / "janitor-reap-ledger.jsonl"
    command = (
        "devin-janitor reap --apply "
        f"--state-file {state.as_posix()} --ledger-file {ledger.as_posix()}"
    )
    _write_state(state, first_seen={"1:2": NOW - 3600})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("x\n")
    table = {
        ("crontab", "-l"): (0, f"*/3 * * * * {command}\n"),
        (
            "systemctl",
            "--user",
            "list-units",
            "--type=service,timer",
            "--all",
            "--no-legend",
            "--no-pager",
        ): (1, ""),
    }
    findings = _run(ctx, table, home=tmp_path / "home")
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert findings[0].message.startswith("janitor reap (cron:")


# ---------------------------------------------------------------------------
# inert signals
# ---------------------------------------------------------------------------


def test_reap_disabled_warns(ctx, tmp_path):
    table, state, ledger = _systemd_table(
        tmp_path, exec_start=EXEC_APPLY, last_trigger_us=int(NOW * 1e6)
    )
    _write_state(state, first_seen={"1:2": NOW - 3600})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("x\n")
    # kill-switch sits next to --state-file (janitor cli.py default)
    (state.parent / "reap.disabled").write_text("")
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "kill-switch" in findings[0].message
    assert "reap.disabled" in findings[0].message


def test_dry_run_past_calibration_warns(ctx, tmp_path):
    table, state, ledger = _systemd_table(
        tmp_path, exec_start=EXEC_DRY, last_trigger_us=int(NOW * 1e6)
    )
    _write_state(state, first_seen={"1:2": NOW - 20 * DAY})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("x\n")  # isolate the --apply signal
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "--apply" in findings[0].message
    assert "calibration" in findings[0].message


def test_empty_ledger_past_calibration_warns(ctx, tmp_path):
    """--apply set + populated-age evidence + empty ledger → the
    attribution_ok:false incident signature."""
    table, state, _ledger = _systemd_table(
        tmp_path, exec_start=EXEC_APPLY, last_trigger_us=int(NOW * 1e6)
    )
    _write_state(state, first_seen={"1:2": NOW - 20 * DAY})
    # ledger never created
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "ledger" in findings[0].message
    assert "attribution" in findings[0].fix


def test_both_gated_signals_consolidate(ctx, tmp_path):
    """The live incident shape: dry-run AND empty ledger past the window
    → one WARN carrying both reasons."""
    table, state, _ledger = _systemd_table(
        tmp_path, exec_start=EXEC_DRY, last_trigger_us=int(NOW * 1e6)
    )
    _write_state(state, first_seen={"1:2": NOW - 20 * DAY})
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "--apply" in findings[0].message
    assert "ledger" in findings[0].message


def test_fragment_mtime_proves_elapsed_without_first_seen(ctx, tmp_path):
    """No orphans ever tracked → first_seen empty; the unit fragment's
    mtime still proves the install outlived calibration."""
    table, state, _ledger = _systemd_table(
        tmp_path, exec_start=EXEC_DRY, last_trigger_us=int(NOW * 1e6)
    )
    frag = tmp_path / "frag.timer"
    os.utime(frag, (NOW - 20 * DAY, NOW - 20 * DAY))
    _write_state(state)  # empty first_seen/pidmap
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "--apply" in findings[0].message


def test_state_file_stale_warns(ctx, tmp_path):
    """Timer fires every 3min but the state file is hours old — sweeps
    are scheduled yet not completing."""
    table, state, ledger = _systemd_table(
        tmp_path, exec_start=EXEC_APPLY, last_trigger_us=int(NOW * 1e6)
    )
    _write_state(state, first_seen={"1:2": NOW - 3600})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("x\n")
    os.utime(state, (NOW - 3 * 3600, NOW - 3 * 3600))
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "state file" in findings[0].message
    assert "not completing" in findings[0].message


def test_cron_state_stale_warns(ctx, tmp_path):
    state = tmp_path / "home" / ".devin" / "janitor-reap.json"
    ledger = tmp_path / "home" / ".devin" / "memory" / "janitor-reap-ledger.jsonl"
    command = (
        "devin-janitor reap --apply "
        f"--state-file {state.as_posix()} --ledger-file {ledger.as_posix()}"
    )
    _write_state(state, first_seen={"1:2": NOW - 3600})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("x\n")
    os.utime(state, (NOW - 5 * 3600, NOW - 5 * 3600))
    table = {
        ("crontab", "-l"): (0, f"*/5 * * * * {command}\n"),
        (
            "systemctl",
            "--user",
            "list-units",
            "--type=service,timer",
            "--all",
            "--no-legend",
            "--no-pager",
        ): (1, ""),
    }
    findings = _run(ctx, table, home=tmp_path / "home")
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "state file" in findings[0].message


def test_timer_inactive_warns(ctx, tmp_path):
    table, state, ledger = _systemd_table(
        tmp_path,
        exec_start=EXEC_APPLY,
        is_active="inactive",
        last_trigger_us=int(NOW * 1e6),
    )
    _write_state(state, first_seen={"1:2": NOW - 3600})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("x\n")
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "inactive" in findings[0].message


def test_timer_disabled_warns(ctx, tmp_path):
    table, state, ledger = _systemd_table(
        tmp_path,
        exec_start=EXEC_APPLY,
        is_enabled="disabled",
        last_trigger_us=int(NOW * 1e6),
    )
    _write_state(state, first_seen={"1:2": NOW - 3600})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("x\n")
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "disabled" in findings[0].message


def test_trigger_fires_but_state_never_written(ctx, tmp_path):
    """LastTriggerUSec newer than the state mtime beyond the grace —
    the oneshot starts but dies before save_reap_state."""
    daily_timer = (
        "[Timer]\nOnCalendar=*-*-* 05:30:00\n\n[Install]\nWantedBy=timers.target\n"
    )
    table, state, ledger = _systemd_table(
        tmp_path,
        exec_start=EXEC_APPLY,
        timer_text=daily_timer,
        calendar="*-*-* 05:30:00",
        last_trigger_us=int((NOW - 3600) * 1e6),
    )
    _write_state(state, first_seen={"1:2": NOW - 3600})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("x\n")
    # state written 3h ago; timer fired 1h ago → gap 2h > 600s grace.
    # Daily interval keeps the wall-clock staleness check quiet so the
    # trigger-vs-write signal is what fires.
    os.utime(state, (NOW - 3 * 3600, NOW - 3 * 3600))
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "never finish a state write" in findings[0].message


def test_service_without_timer_is_unscheduled(ctx, tmp_path):
    state = tmp_path / "state" / "janitor-reap.json"
    ledger = tmp_path / "memory" / "janitor-reap-ledger.jsonl"
    cmd = EXEC_APPLY.format(state=state.as_posix(), ledger=ledger.as_posix())
    table = {
        (
            "systemctl",
            "--user",
            "list-units",
            "--type=service,timer",
            "--all",
            "--no-legend",
            "--no-pager",
        ): (
            0,
            (
                "devin-janitor-reap.service loaded inactive dead    "
                "devin-janitor-reap.service\n"
            ),
        ),
        (
            "systemctl",
            "--user",
            "list-unit-files",
            "--type=service,timer",
            "--no-legend",
            "--no-pager",
        ): (1, ""),
        ("systemctl", "--user", "cat", "devin-janitor-reap.service"): (
            0,
            SERVICE_TEMPLATE.format(exec_start=cmd),
        ),
        ("crontab", "-l"): (1, "no crontab for u", ""),
    }
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "never run" in findings[0].message or "no timer" in findings[0].message


def test_unreadable_execstart_warns_unverifiable(ctx, tmp_path):
    table = {
        (
            "systemctl",
            "--user",
            "list-units",
            "--type=service,timer",
            "--all",
            "--no-legend",
            "--no-pager",
        ): (
            0,
            (
                "devin-janitor-reap.timer loaded active waiting    "
                "devin-janitor-reap.timer\n"
            ),
        ),
        (
            "systemctl",
            "--user",
            "list-unit-files",
            "--type=service,timer",
            "--no-legend",
            "--no-pager",
        ): (1, ""),
        ("systemctl", "--user", "cat", "devin-janitor-reap.timer"): (1, ""),
        ("crontab", "-l"): (1, "no crontab for u", ""),
    }
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "unverifiable" in findings[0].message or "parseable" in findings[0].message


def test_non_linux_reap_schedule_warns(ctx, tmp_path):
    state = tmp_path / "home" / ".devin" / "janitor-reap.json"
    ledger = tmp_path / "home" / ".devin" / "memory" / "janitor-reap-ledger.jsonl"
    command = (
        "devin-janitor reap --apply "
        f"--state-file {state.as_posix()} --ledger-file {ledger.as_posix()}"
    )
    table = {
        ("crontab", "-l"): (0, f"*/5 * * * * {command}\n"),
    }
    findings = _run(ctx, table, plat="macos", home=tmp_path / "home")
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "Linux-only" in findings[0].message


def test_windows_schtasks_reap_warns_linux_only(ctx, tmp_path):
    xml = """<?xml version="1.0"?>
<Task><Actions><Exec>
<Command>C:\\Tools\\devin-janitor.exe</Command>
<Arguments>reap --apply</Arguments>
</Exec></Actions></Task>"""
    table = {
        ("schtasks", "/query", "/fo", "csv", "/nh"): (
            0,
            '"\\\\janitor reap","N/A","Disabled"\n',
        ),
        ("schtasks", "/query", "/tn", "\\janitor reap", "/xml"): (0, xml),
        ("crontab", "-l"): (1, "no crontab for u", ""),
    }
    findings = _run(ctx, table, plat="windows")
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "Linux-only" in findings[0].message


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def test_parse_reap_args_defaults_and_flags(tmp_path):
    home = tmp_path / "home"
    cmd = "/usr/local/bin/devin-janitor reap --json"
    args = janitor._parse_reap_args(cmd, home, home)
    assert args is not None and args.apply is False
    assert args.state_file == home / ".devin" / "janitor-reap.json"
    assert args.ledger_file == (
        home / ".devin" / "memory" / "janitor-reap-ledger.jsonl"
    )
    assert args.disable_file is None


def test_parse_reap_args_inline_and_separate(tmp_path):
    home = tmp_path / "home"
    cmd = (
        "devin-janitor-reap.sh /x/devin-janitor reap --apply "
        "--state-file=/a/b/state.json --ledger-file /c/d/ledger.jsonl"
    )
    args = janitor._parse_reap_args(cmd, home, home)
    assert args is not None
    assert args.apply is True
    assert args.state_file == Path("/a/b/state.json")
    assert args.ledger_file == Path("/c/d/ledger.jsonl")


def test_parse_reap_args_quoted_shell_payload(tmp_path):
    home = tmp_path / "home"
    cmd = "sh -c 'devin-janitor reap --apply --state-file /s.json'"
    args = janitor._parse_reap_args(cmd, home, home)
    assert args is not None and args.apply is True
    assert args.state_file == Path("/s.json")


def test_parse_reap_args_stops_at_operators(tmp_path):
    home = tmp_path / "home"
    cmd = "devin-janitor reap --state-file /s.json && echo --apply"
    args = janitor._parse_reap_args(cmd, home, home)
    assert args is not None
    assert args.apply is False  # '--apply' after && is not a reap arg
    assert args.state_file == Path("/s.json")


def test_parse_reap_args_no_reap_returns_none(tmp_path):
    home = tmp_path / "home"
    assert janitor._parse_reap_args("devin-janitor run --apply", home, home) is None
    assert janitor._parse_reap_args("echo hello", home, home) is None


def test_timespan_seconds():
    assert janitor._timespan_seconds("3min") == 180.0
    assert janitor._timespan_seconds("1h 30min") == 5400.0
    assert janitor._timespan_seconds("90s") == 90.0
    assert janitor._timespan_seconds("bogus") is None


def test_cron_interval_seconds():
    assert janitor._cron_interval_seconds("* * * * *") == 60.0
    assert janitor._cron_interval_seconds("*/5 * * * *") == 300.0
    assert janitor._cron_interval_seconds("0 * * * *") == 3600.0
    assert janitor._cron_interval_seconds("0 5 * * *") == 86400.0
    assert janitor._cron_interval_seconds("@hourly") == 3600.0
    assert janitor._cron_interval_seconds("@reboot") is None
    # weekday-only schedule: longest gap is the weekend (~3 days)
    assert janitor._cron_interval_seconds("0 9 * * mon-fri") == 3 * 86400.0


def test_cron_interval_biweekly_and_sparse():
    # two fires per hour around the clock → 30min max gap
    assert janitor._cron_interval_seconds("0,30 * * * *") == 1800.0
    # a business-hours band has a ~15.5h overnight gap
    assert janitor._cron_interval_seconds("0,30 9-17 * * *") == 55800.0
    # yearly-ish schedule: too sparse to measure inside the horizon
    assert janitor._cron_interval_seconds("0 0 29 2 *") is None


def test_last_trigger_parses_systemctl_date():
    epoch = NOW - 3600
    stamp = time.strftime("%a %Y-%m-%d %H:%M:%S %Z", time.localtime(epoch))
    runner = make_runner({
        ("systemctl", "--user", "show", "t.timer", "-p",
         "LastTriggerUSec", "--value"): (0, f"{stamp}\n"),
    })
    got = janitor._last_trigger_s(runner, "t.timer")
    assert got is not None and abs(got - epoch) < 2


def test_last_trigger_never_fired_returns_none():
    runner = make_runner({
        ("systemctl", "--user", "show", "t.timer", "-p",
         "LastTriggerUSec", "--value"): (0, "n/a\n"),
        ("systemctl", "--user", "show", "t.timer", "-p",
         "LastTriggerUSecMonotonic", "--value"): (0, "0\n"),
    })
    assert janitor._last_trigger_s(runner, "t.timer") is None


def test_last_trigger_monotonic_fallback():
    usec = int((time.monotonic() - 7200) * 1e6)
    runner = make_runner({
        ("systemctl", "--user", "show", "t.timer", "-p",
         "LastTriggerUSec", "--value"): (0, "n/a\n"),
        ("systemctl", "--user", "show", "t.timer", "-p",
         "LastTriggerUSecMonotonic", "--value"): (0, f"{usec}\n"),
    })
    got = janitor._last_trigger_s(runner, "t.timer")
    assert got is not None and abs(got - (time.time() - 7200)) < 5


def test_calendar_interval_iter_dot_format():
    runner = make_runner({
        ("systemd-analyze", "calendar", "--iterations=4", "expr"): (
            0,
            "Normalized form: x\n"
            "    Next elapse: Thu 2026-11-26 05:30:00 WET\n"
            "       Iter. #2: Fri 2026-11-27 05:30:00 WET\n"
            "       Iter. #3: Sat 2026-11-28 05:30:00 WET\n"
            "       Iter. #4: Sun 2026-11-29 05:30:00 WET\n",
        ),
    })
    assert janitor._calendar_interval_seconds(runner, "expr") == 86400.0


def test_cron_empty_first_seen_uses_state_birth(ctx, tmp_path):
    home = tmp_path / "home"
    state = home / ".devin" / "janitor-reap.json"
    ledger = home / ".devin" / "memory" / "l.jsonl"
    command = (
        f"devin-janitor reap --json --state-file {state} "
        f"--ledger-file {ledger}"
    )
    _write_state(state)  # empty first_seen
    table = {
        ("crontab", "-l"): (0, f"*/5 * * * * {command}\n"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
        ("stat", "-c", "%W", str(state)): (0, f"{int(NOW - 20 * DAY)}\n"),
    }
    findings = _run(ctx, table, home=home)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "calibration" in findings[0].message


def test_trigger_fired_but_state_missing_warns(ctx, tmp_path):
    table, _state, ledger = _systemd_table(
        tmp_path, exec_start=EXEC_APPLY,
        last_trigger_us=int((NOW - 3600) * 1e6),
    )
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("x\n")
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "never finish a state write" in findings[0].message


def test_cron_cd_prefix_resolves_relative_paths(ctx, tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    command = (
        f"cd {work} && devin-janitor reap --apply "
        "--state-file reap-state.json --ledger-file reap-ledger.jsonl"
    )
    _write_state(work / "reap-state.json", first_seen={"1:2": NOW - 3600})
    (work / "reap-ledger.jsonl").write_text("x\n")
    table = {
        ("crontab", "-l"): (0, f"*/5 * * * * {command}\n"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = _run(ctx, table, home=home)
    assert findings[0].status is Status.PASS
    assert "state file present" in findings[0].message
    (work / "reap.disabled").write_text("")
    findings = _run(ctx, table, home=home)
    assert findings[0].status is Status.WARN
    assert "reap.disabled" in findings[0].message


def test_execstart_dropin_replacement_drives_diagnosis(ctx, tmp_path):
    state = tmp_path / "state" / "janitor-reap.json"
    ledger = tmp_path / "memory" / "janitor-reap-ledger.jsonl"
    service_text = (
        "[Service]\n"
        f"ExecStart={EXEC_APPLY.format(state=state, ledger=ledger)}\n"
        "ExecStart=\n"
        f"ExecStart={EXEC_DRY.format(state=state, ledger=ledger)}\n"
    )
    table, state, ledger = _systemd_table(
        tmp_path, exec_start=EXEC_APPLY, service_text=service_text,
        last_trigger_us=int(NOW * 1e6),
    )
    _write_state(state, first_seen={"1:2": NOW - 20 * DAY})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("x\n")
    findings = _run(ctx, table)
    assert findings[0].status is Status.WARN
    assert "calibration" in findings[0].message


def test_systemd_scan_only_cats_matching_units(ctx, tmp_path):
    calls = []
    base = make_runner({
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (
            0,
            "devin-janitor-reap.timer   loaded active waiting   t\n"
            "unrelated.timer            loaded active waiting   u\n",
        ),
        ("systemctl", "--user", "list-unit-files",
         "--type=service,timer", "--no-legend", "--no-pager"): (1, ""),
        ("systemctl", "--user", "cat", "devin-janitor-reap.timer"): (
            0, TIMER_UNIT),
        ("systemctl", "--user", "cat", "devin-janitor-reap.service"): (
            0, SERVICE_TEMPLATE.format(exec_start=EXEC_APPLY.format(
                state=tmp_path / "s.json", ledger=tmp_path / "l.jsonl"))),
        ("systemctl", "--user", "is-active",
         "devin-janitor-reap.timer"): (0, "active\n"),
        ("systemctl", "--user", "is-enabled",
         "devin-janitor-reap.timer"): (0, "enabled\n"),
        ("crontab", "-l"): (1, "no crontab for u", ""),
    })

    def runner(argv):
        calls.append(list(argv))
        return base(argv)

    janitor.run(ctx, runner=runner, which=lambda c: None,
                plat="linux", now_s=NOW)
    catted = {a[-1] for a in calls
              if a[:3] == ["systemctl", "--user", "cat"]}
    assert "devin-janitor-reap.timer" in catted
    assert "devin-janitor-reap.service" in catted
    assert "unrelated.timer" not in catted


def test_schtasks_only_queries_matching_tasks(ctx, tmp_path):
    xml = """<?xml version="1.0"?>
<Task><Actions><Exec>
<Command>C:\\Tools\\devin-janitor.exe</Command>
<Arguments>reap --apply</Arguments>
</Exec></Actions></Task>"""
    calls = []
    base = make_runner({
        ("schtasks", "/query", "/fo", "csv", "/nh"): (
            0, '"\\\\janitor reap","N/A","x"\n'
               '"\\\\other task","N/A","x"\n'),
        ("schtasks", "/query", "/tn", "\\janitor reap", "/xml"): (0, xml),
        ("crontab", "-l"): (1, "no crontab for u", ""),
    })

    def runner(argv):
        calls.append(list(argv))
        return base(argv)

    findings = janitor.run(ctx, runner=runner, which=lambda c: None,
                           plat="windows", now_s=NOW)
    details = {a[3] for a in calls
               if a[:3] == ["schtasks", "/query", "/tn"]}
    assert details == {"\\janitor reap"}
    assert findings[0].status is Status.WARN
