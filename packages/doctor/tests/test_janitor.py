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


def test_last_trigger_monotonic_fallback(monkeypatch):
    # freeze both clocks — on a host with <2h uptime the raw
    # (monotonic - 7200) timestamp is negative and the parser rejects
    # it before the conversion is even exercised (#143 re-review)
    monkeypatch.setattr(janitor.time, "monotonic", lambda: 10_000.0)
    monkeypatch.setattr(janitor.time, "time", lambda: 1_800_000_000.0)
    usec = int((10_000.0 - 7200) * 1e6)
    runner = make_runner({
        ("systemctl", "--user", "show", "t.timer", "-p",
         "LastTriggerUSec", "--value"): (0, "n/a\n"),
        ("systemctl", "--user", "show", "t.timer", "-p",
         "LastTriggerUSecMonotonic", "--value"): (0, f"{usec}\n"),
    })
    got = janitor._last_trigger_s(runner, "t.timer")
    assert got is not None and abs(got - (1_800_000_000.0 - 7200)) < 5


def test_calendar_interval_iter_dot_format():
    runner = make_runner({
        ("systemd-analyze", "calendar", "--iterations=4", "expr"): (
            0,
            (
                "Normalized form: x\n"
                "    Next elapse: Thu 2026-11-26 05:30:00 WET\n"
                "       Iter. #2: Fri 2026-11-27 05:30:00 WET\n"
                "       Iter. #3: Sat 2026-11-28 05:30:00 WET\n"
                "       Iter. #4: Sun 2026-11-29 05:30:00 WET\n"
            ),
        ),
    })
    assert janitor._calendar_interval_seconds(runner, "expr") == 86400.0


def test_cron_empty_first_seen_uses_state_birth(ctx, tmp_path):
    home = tmp_path / "home"
    state = home / ".devin" / "janitor-reap.json"
    ledger = home / ".devin" / "memory" / "l.jsonl"
    command = (
        "devin-janitor reap --json "
        f"--state-file {state.as_posix()} "
        f"--ledger-file {ledger.as_posix()}"
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
        f"cd {work.as_posix()} && devin-janitor reap --apply "
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
    apply_cmd = EXEC_APPLY.format(
        state=state.as_posix(), ledger=ledger.as_posix()
    )
    dry_cmd = EXEC_DRY.format(
        state=state.as_posix(), ledger=ledger.as_posix()
    )
    service_text = (
        "[Service]\n"
        f"ExecStart={apply_cmd}\n"
        "ExecStart=\n"
        f"ExecStart={dry_cmd}\n"
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


def test_systemd_scan_cats_all_units(ctx, tmp_path):
    calls = []
    base = make_runner({
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (
            0,
            (
                "devin-janitor-reap.timer   loaded active waiting   t\n"
                "unrelated.timer            loaded active waiting   u\n"
            ),
        ),
        ("systemctl", "--user", "list-unit-files",
         "--type=service,timer", "--no-legend", "--no-pager"): (1, ""),
        ("systemctl", "--user", "cat", "devin-janitor-reap.timer"): (
            0, TIMER_UNIT),
        ("systemctl", "--user", "cat", "devin-janitor-reap.service"): (
            0, SERVICE_TEMPLATE.format(exec_start=EXEC_APPLY.format(
                state=(tmp_path / "s.json").as_posix(),
                ledger=(tmp_path / "l.jsonl").as_posix()))),
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
    # detection is by ExecStart, not name — an unrelated-looking unit
    # must still be read in case it runs the reaper (#143 re-review)
    assert "unrelated.timer" in catted


def test_systemd_detects_reaper_behind_renamed_unit(ctx, tmp_path):
    """A unit without 'janitor'/'reap' in its name but whose ExecStart
    runs the reaper must still be detected as scheduled (#143
    re-review: name-filtered enumeration reported 'not scheduled')."""
    base = make_runner({
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (
            0,
            "devin-cleanup.timer            loaded active waiting   t\n",
        ),
        ("systemctl", "--user", "list-unit-files",
         "--type=service,timer", "--no-legend", "--no-pager"): (1, ""),
        ("systemctl", "--user", "cat", "devin-cleanup.timer"): (
            0, TIMER_UNIT),
        ("systemctl", "--user", "cat", "devin-cleanup.service"): (
            0, SERVICE_TEMPLATE.format(exec_start=EXEC_APPLY.format(
                state=(tmp_path / "s.json").as_posix(),
                ledger=(tmp_path / "l.jsonl").as_posix()))),
        ("systemctl", "--user", "is-active",
         "devin-cleanup.timer"): (0, "active\n"),
        ("systemctl", "--user", "is-enabled",
         "devin-cleanup.timer"): (0, "enabled\n"),
        ("crontab", "-l"): (1, "no crontab for u", ""),
    })
    findings = janitor.run(ctx, runner=base, which=lambda c: None,
                           plat="linux", now_s=NOW)
    assert findings[0].status is Status.PASS
    assert "not scheduled" not in findings[0].message.lower()


def test_schtasks_only_queries_matching_tasks(ctx, tmp_path):
    xml = """<?xml version="1.0"?>
<Task><Actions><Exec>
<Command>C:\\Tools\\devin-janitor.exe</Command>
<Arguments>reap --apply</Arguments>
</Exec></Actions></Task>"""
    calls = []
    base = make_runner({
        ("schtasks", "/query", "/fo", "csv", "/nh"): (
            0,
            (
                '"\\\\janitor reap","N/A","x"\n'
                '"\\\\other task","N/A","x"\n'
            ),
        ),
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
    # every task is /xml-queried — the reaper may hide behind a name
    # without 'janitor' in it (#143 re-review)
    assert details == {"\\janitor reap", "\\other task"}
    assert findings[0].status is Status.WARN


# ---------------------------------------------------------------------------
# Devin Review wave — command-position parsing, cwd-at-reap, surface caps
# ---------------------------------------------------------------------------


def test_parse_reap_args_cd_scoped_to_reap_point(tmp_path):
    """`cd X && devin-janitor reap … && cd Y` — relative flag paths
    resolve against the directory in effect AT the reap token, not the
    line's final cd (#143 review)."""
    home = tmp_path / "home"
    cmd = "cd /a && devin-janitor reap --state-file s.json && cd /b"
    args = janitor._parse_reap_args(cmd, home, home)
    assert args is not None
    assert args.state_file == Path("/a/s.json")


def test_parse_reap_args_rejects_non_command_mentions(tmp_path):
    """'devin-janitor reap' inside another command's arguments — quoted
    or bare — is text, not a scheduled invocation (#143 review)."""
    home = tmp_path / "home"
    for cmd in (
        "grep 'devin-janitor reap' /var/log/syslog",
        "grep devin-janitor reap /var/log/syslog",
        "echo devin-janitor reap",
        "logger 'devin-janitor reap --apply' done",
    ):
        assert janitor._parse_reap_args(cmd, home, home) is None, cmd


def test_parse_reap_args_windows_exe_posix_false(tmp_path):
    """Task Scheduler commands keep Windows path backslashes — the
    posix=False lexer must still find the exe + subcommand."""
    home = tmp_path / "home"
    args = janitor._parse_reap_args(
        r"C:\Tools\devin-janitor.exe reap --apply",
        home,
        home,
        posix=False,
    )
    assert args is not None and args.apply is True


def test_cron_cd_after_reap_keeps_reap_cwd(ctx, tmp_path):
    """End-to-end: `cd work && devin-janitor reap … && cd other` — the
    state/ledger/kill-switch resolve under `work`, and a `reap.disabled`
    there is still seen (#143 review)."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    other = tmp_path / "other"
    command = (
        f"cd {work.as_posix()} && devin-janitor reap --apply "
        "--state-file reap-state.json --ledger-file reap-ledger.jsonl "
        f"&& cd {other.as_posix()} && ./rotate"
    )
    _write_state(work / "reap-state.json", first_seen={"1:2": NOW - 3600})
    (work / "reap-ledger.jsonl").write_text("x\n")
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
    findings = _run(ctx, table, home=home)
    assert findings[0].status is Status.PASS
    assert "state file present" in findings[0].message
    (work / "reap.disabled").write_text("")
    findings = _run(ctx, table, home=home)
    assert findings[0].status is Status.WARN
    assert "reap.disabled" in findings[0].message


def test_cron_grep_reap_pattern_is_not_a_reaper(ctx, tmp_path):
    """A cron line that merely MENTIONS 'devin-janitor reap' (a log
    audit) must not be reported as a scheduled reaper (#143 review)."""
    home = tmp_path / "home"
    table = {
        ("crontab", "-l"): (
            0,
            "*/5 * * * * grep 'devin-janitor reap' /var/log/sweeps.log\n",
        ),
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
    findings = _run(
        ctx,
        table,
        home=home,
        which=lambda c: "/usr/local/bin/devin-janitor",
    )
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert "not scheduled" in findings[0].message


def test_systemd_grep_reap_is_not_a_reaper(ctx):
    """ExecStart that only GREPS for 'devin-janitor reap' in logs is not
    a reaper schedule — no 'sweep never finishes' WARN (#143 review)."""
    last_trigger = time.strftime(
        "%a %Y-%m-%d %H:%M:%S %Z",
        time.localtime(NOW - 3600),
    )
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
            "log-audit.timer   loaded active waiting   log-audit.timer\n",
        ),
        (
            "systemctl",
            "--user",
            "list-unit-files",
            "--type=service,timer",
            "--no-legend",
            "--no-pager",
        ): (1, ""),
        ("systemctl", "--user", "cat", "log-audit.timer"): (
            0,
            "[Timer]\nOnUnitActiveSec=1h\n\n[Install]\nWantedBy=timers.target\n",
        ),
        ("systemctl", "--user", "cat", "log-audit.service"): (
            0,
            SERVICE_TEMPLATE.format(
                exec_start="/usr/bin/grep 'devin-janitor reap' "
                "/var/log/sweeps.log"
            ),
        ),
        ("systemctl", "--user", "is-active", "log-audit.timer"): (
            0,
            "active\n",
        ),
        ("systemctl", "--user", "is-enabled", "log-audit.timer"): (
            0,
            "enabled\n",
        ),
        (
            "systemctl",
            "--user",
            "show",
            "log-audit.timer",
            "-p",
            "LastTriggerUSec",
            "--value",
        ): (0, last_trigger + "\n"),
        ("crontab", "-l"): (1, "no crontab for u", ""),
    }
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert "not scheduled" in findings[0].message


def test_systemd_reap_wrapper_execstart_detected(ctx, tmp_path):
    """The production install wraps reap in devin-janitor-reap.sh —
    '<wrapper> <janitor> reap …' must still parse as a reap schedule
    (#143 review)."""
    table, state, ledger = _systemd_table(
        tmp_path,
        exec_start=(
            "/home/u/.local/bin/devin-janitor-reap.sh "
            "/home/u/.local/bin/devin-janitor reap --apply --json "
            "--state-file {state} --ledger-file {ledger}"
        ),
        last_trigger_us=int(NOW * 1e6),
    )
    _write_state(state, first_seen={"1:2": NOW - 3600})
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("x\n")
    findings = _run(ctx, table)
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert "--apply" in findings[0].message


TASK_SCHEDULER_NS_XML = """<?xml version="1.0"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
<Actions><Exec>
<Command>C:\\Tools\\devin-janitor.exe</Command>
<Arguments>reap --apply</Arguments>
</Exec></Actions></Task>"""


def test_schtasks_xml_real_namespace_generic_name(ctx):
    """Real schtasks /xml output carries the Task Scheduler namespace —
    the reader must match it, and detection is by COMMAND so a generic
    task name still surfaces as a reap schedule (#143 review)."""
    table = {
        ("schtasks", "/query", "/fo", "csv", "/nh"): (
            0,
            '"\\\\nightly sweep","N/A","Ready"\n',
        ),
        ("schtasks", "/query", "/tn", "\\nightly sweep", "/xml"): (
            0,
            TASK_SCHEDULER_NS_XML,
        ),
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
    findings = _run(ctx, table, plat="windows")
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "Linux-only" in findings[0].message
    assert "nightly sweep" in findings[0].message


def test_schtasks_reaper_beyond_inventory_cap_still_found(ctx):
    """Automation caps /xml probing at 200 tasks for its own inventory;
    the janitor check hunts by COMMAND and must inspect every listed
    task — a reaper at position 201 is still found (#143 review)."""
    fillers = [f"task{i:03d}" for i in range(200)]
    csv_lines = "".join(
        f'"\\\\{name}","N/A","Ready"\n' for name in fillers
    )
    csv_lines += '"\\\\janitor reap","N/A","Ready"\n'
    table = {
        ("schtasks", "/query", "/fo", "csv", "/nh"): (0, csv_lines),
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
    for name in fillers:
        table[("schtasks", "/query", "/tn", f"\\{name}", "/xml")] = (
            0,
            "<Task/>",
        )
    table[("schtasks", "/query", "/tn", "\\janitor reap", "/xml")] = (
        0,
        TASK_SCHEDULER_NS_XML,
    )
    findings = _run(ctx, table, plat="windows")
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "Linux-only" in findings[0].message


def test_schtasks_unreadable_tasks_warn_instead_of_pass(ctx):
    """A task whose /xml probe fails leaves part of the surface
    uninspected — with no reap found, 'not scheduled' is unverifiable,
    so the check WARNs instead of PASSing (#143 review)."""
    table = {
        ("schtasks", "/query", "/fo", "csv", "/nh"): (
            0,
            '"\\\\nightly sweep","N/A","Ready"\n',
        ),
        ("schtasks", "/query", "/tn", "\\nightly sweep", "/xml"): (1, ""),
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
    findings = _run(
        ctx,
        table,
        plat="windows",
        which=lambda c: "/usr/local/bin/devin-janitor",
    )
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "cannot be ruled out" in findings[0].message


def test_journalctl_hint_names_activated_service(ctx, tmp_path):
    """The dead-sweep fix must point 'journalctl -u' at the service the
    timer activates — explicit Unit= or the homonymous .service
    (#143 review)."""
    ledger = tmp_path / "memory" / "janitor-reap-ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("x\n")
    # state file deliberately absent → 'fired but never wrote' branch
    state = tmp_path / "state" / "janitor-reap.json"
    exec_cmd = EXEC_APPLY.format(
        state=state.as_posix(), ledger=ledger.as_posix()
    )
    last_trigger = time.strftime(
        "%a %Y-%m-%d %H:%M:%S %Z",
        time.localtime(NOW - 3600),
    )
    for unit_directive, expected in (
        ("Unit=custom-sweep.service\n", "custom-sweep.service"),
        ("", "devin-janitor-reap.service"),
    ):
        activated = (
            "custom-sweep.service"
            if unit_directive
            else "devin-janitor-reap.service"
        )
        timer_text = (
            "[Timer]\n"
            + unit_directive
            + "OnUnitActiveSec=3min\nAccuracySec=30s\n\n"
            "[Install]\nWantedBy=timers.target\n"
        )
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
                "devin-janitor-reap.timer   loaded active waiting   t\n",
            ),
            (
                "systemctl",
                "--user",
                "list-unit-files",
                "--type=service,timer",
                "--no-legend",
                "--no-pager",
            ): (1, ""),
            ("systemctl", "--user", "cat", "devin-janitor-reap.timer"): (
                0,
                timer_text,
            ),
            ("systemctl", "--user", "cat", activated): (
                0,
                SERVICE_TEMPLATE.format(exec_start=exec_cmd),
            ),
            ("systemctl", "--user", "is-active", "devin-janitor-reap.timer"): (
                0,
                "active\n",
            ),
            (
                "systemctl",
                "--user",
                "is-enabled",
                "devin-janitor-reap.timer",
            ): (0, "enabled\n"),
            (
                "systemctl",
                "--user",
                "show",
                "devin-janitor-reap.timer",
                "-p",
                "LastTriggerUSec",
                "--value",
            ): (0, last_trigger + "\n"),
            ("crontab", "-l"): (1, "no crontab for u", ""),
        }
        findings = _run(ctx, table)
        assert len(findings) == 1, unit_directive
        assert findings[0].status is Status.WARN
        assert (
            f"journalctl --user -u {expected}" in findings[0].fix
        ), unit_directive


# ---------------------------------------------------------------------------
# Devin Review wave 2 — schtasks Command/Arguments split, systemd ExecStart
# prefixes, shell '-c' clusters, flock lockfile operand
# ---------------------------------------------------------------------------


def test_schtasks_spaced_command_stays_one_argument():
    """'<Command>' is the executable ALONE — Task Scheduler stores it
    separately from '<Arguments>'. 'C:\\Program Files\\…' + args must
    not re-split into a phantom 'C:\\Program' path that FAILs the
    inventory and hides the reaper (#143 review)."""
    xml = """<?xml version="1.0"?>
<Task><Actions><Exec>
<Command>C:\\Program Files\\Tools\\devin-janitor.exe</Command>
<Arguments>reap --apply</Arguments>
</Exec></Actions></Task>"""
    commands = janitor.automation._schtasks_commands(xml)
    assert commands == [
        '"C:\\Program Files\\Tools\\devin-janitor.exe" reap --apply'
    ]


def test_schtasks_spaced_command_reap_detected(ctx):
    """End-to-end: an action whose Command contains spaces (and whose
    Arguments carry no quotes) still surfaces as a reap schedule —
    the Windows surface warns Linux-only like any other (#143 review)."""
    xml = """<?xml version="1.0"?>
<Task><Actions><Exec>
<Command>C:\\Program Files\\Tools\\devin-janitor.exe</Command>
<Arguments>reap --apply</Arguments>
</Exec></Actions></Task>"""
    table = {
        ("schtasks", "/query", "/fo", "csv", "/nh"): (
            0,
            '"\\\\nightly sweep","N/A","Ready"\n',
        ),
        ("schtasks", "/query", "/tn", "\\nightly sweep", "/xml"): (0, xml),
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
    findings = _run(ctx, table, plat="windows")
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "Linux-only" in findings[0].message
    assert "nightly sweep" in findings[0].message


def test_unit_payload_strips_execstart_prefixes():
    """systemd.exec run-semantics prefixes on the executable — '-',
    '@', ':', '+', '!', '!!' and combos — come off when the unit text
    is read, so '-/path/cmd' still reads as '/path/cmd' (#143 review)."""
    for prefix in ("-", "@", "+", "!", "!!", "-@", "+!"):
        text = (
            "[Service]\n"
            f"ExecStart={prefix}/usr/bin/devin-janitor reap --apply\n"
        )
        commands, _wd, _activates = janitor.automation._unit_payload(text)
        assert commands == [
            "/usr/bin/devin-janitor reap --apply"
        ], prefix


def test_systemd_execstart_prefix_dash_detected(ctx, tmp_path):
    """'ExecStart=-/…/devin-janitor reap …' — systemd's
    ignore-exit-status prefix must not hide the exe: the classifier
    used to keep the '-' and discard the exe as an option, missing
    the reaper on a generically-named timer (#143 review)."""
    state = tmp_path / "state" / "janitor-reap.json"
    ledger = tmp_path / "memory" / "janitor-reap-ledger.jsonl"
    exec_cmd = "-" + EXEC_APPLY.format(
        state=state.as_posix(), ledger=ledger.as_posix()
    )
    base = make_runner({
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
            "devin-cleanup.timer   loaded active waiting   t\n",
        ),
        (
            "systemctl",
            "--user",
            "list-unit-files",
            "--type=service,timer",
            "--no-legend",
            "--no-pager",
        ): (1, ""),
        ("systemctl", "--user", "cat", "devin-cleanup.timer"): (
            0,
            TIMER_UNIT,
        ),
        ("systemctl", "--user", "cat", "devin-cleanup.service"): (
            0,
            SERVICE_TEMPLATE.format(exec_start=exec_cmd),
        ),
        ("systemctl", "--user", "is-active", "devin-cleanup.timer"): (
            0,
            "active\n",
        ),
        ("systemctl", "--user", "is-enabled", "devin-cleanup.timer"): (
            0,
            "enabled\n",
        ),
        ("crontab", "-l"): (1, "no crontab for u", ""),
    })
    findings = janitor.run(
        ctx, runner=base, which=lambda c: None, plat="linux", now_s=NOW
    )
    assert findings[0].status is Status.PASS
    assert "--apply" in findings[0].message
    assert "not scheduled" not in findings[0].message.lower()


def test_parse_reap_args_shell_c_option_clusters(tmp_path):
    """'c' inside a combined short-option cluster — 'bash -lc',
    'sh -xc', attached '-c<payload>' — still opens the shell's '-c'
    payload for supported shells (#143 review)."""
    home = tmp_path / "home"
    for cmd in (
        "bash -lc 'devin-janitor reap --apply --state-file /s.json'",
        "sh -xc 'devin-janitor reap --apply'",
        "zsh -lxc 'devin-janitor reap --apply'",
        "bash -c'devin-janitor reap --apply'",
        "bash -lo vi -c 'devin-janitor reap --apply'",
    ):
        args = janitor._parse_reap_args(cmd, home, home)
        assert args is not None and args.apply is True, cmd


def test_parse_reap_args_script_dash_c_is_not_shell_c(tmp_path):
    """Once the shell's operand (the script name) appears its option
    list is over — 'bash deploy.sh -c …' passes '-c' to the SCRIPT,
    not to bash; treating the payload as a -c string would invent a
    reaper that never runs (#143 review)."""
    home = tmp_path / "home"
    for cmd in (
        "bash deploy.sh -c 'devin-janitor reap --apply'",
        "sh -c 'true' script.sh -c 'devin-janitor reap --apply'",
        "bash -- -c 'devin-janitor reap --apply'",
    ):
        assert janitor._parse_reap_args(cmd, home, home) is None, cmd


def test_cron_bash_lc_reap_detected(ctx, tmp_path):
    """End-to-end cron: 'bash -lc' (login shell + command) must reach
    the nested reap invocation like plain 'sh -c' does (#143 review)."""
    home = tmp_path / "home"
    table = {
        ("crontab", "-l"): (
            0,
            "*/5 * * * * bash -lc 'devin-janitor reap --apply'\n",
        ),
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
    findings = _run(ctx, table, home=home)
    assert findings[0].status is Status.PASS
    assert findings[0].message.startswith("janitor reap (cron:")
    assert "--apply" in findings[0].message


def test_parse_reap_args_flock_wrapped_reap(tmp_path):
    """'flock [opts] <lockfile> <cmd>' — flock consumes its options
    and exactly ONE lockfile/fd operand before the wrapped command;
    the lockfile must not read as the executable (#143 review)."""
    home = tmp_path / "home"
    for cmd in (
        "flock -n /tmp/reap.lock devin-janitor reap --apply",
        "flock /tmp/reap.lock devin-janitor reap --apply",
        "/usr/bin/flock -n /tmp/reap.lock devin-janitor reap --apply",
        "flock -w 5 /tmp/reap.lock devin-janitor reap --apply",
        "flock -nw 5 /tmp/reap.lock devin-janitor reap --apply",
        "flock 9 devin-janitor reap --apply",
        "timeout 30 flock -n /tmp/l devin-janitor reap --apply",
        "flock /tmp/l bash -lc 'devin-janitor reap --apply'",
    ):
        args = janitor._parse_reap_args(cmd, home, home)
        assert args is not None and args.apply is True, cmd


def test_parse_reap_args_flock_dash_c_and_script_guard(tmp_path):
    """'flock <file> -c <cmd>' runs the operand via the shell — the
    payload is a nested command line; but once the WRAPPED command
    appears, a later '-c' belongs to it, not to a shell (#143 review)."""
    home = tmp_path / "home"
    for cmd in (
        "flock -n /tmp/reap.lock -c 'devin-janitor reap --apply'",
        "flock /tmp/x.lock --command 'devin-janitor reap --apply'",
    ):
        args = janitor._parse_reap_args(cmd, home, home)
        assert args is not None and args.apply is True, cmd
    assert (
        janitor._parse_reap_args(
            "flock /tmp/l deploy.sh -c 'devin-janitor reap --apply'",
            home,
            home,
        )
        is None
    )


def test_cron_flock_wrapped_reap_detected(ctx, tmp_path):
    """End-to-end cron: the common 'flock -n <lock> devin-janitor
    reap' idiom must schedule-detect like the bare command (#143
    review)."""
    home = tmp_path / "home"
    table = {
        ("crontab", "-l"): (
            0,
            "*/5 * * * * flock -n /tmp/reap.lock devin-janitor reap --apply\n",
        ),
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
    findings = _run(ctx, table, home=home)
    assert findings[0].status is Status.PASS
    assert findings[0].message.startswith("janitor reap (cron:")
    assert "--apply" in findings[0].message


def test_automation_flock_lockfile_not_a_target(tmp_path):
    """automation's extractor consumes the same flock operands —
    '/tmp/reap.lock' is not a missing-path FAIL and the wrapped
    command still reaches command position (#143 review)."""
    home = tmp_path / "home"
    targets, commands = janitor.automation._task_targets(
        "flock -n /tmp/reap.lock devin-janitor reap --apply",
        posix=True,
        home=home,
    )
    assert targets == []
    assert commands == ["devin-janitor"]
