"""automation check: scheduled tasks whose targets vanished, or that
vanished themselves (the 'orphan automation' case — a crontab line that
disappeared for days with nothing reporting it).

All surface probes are pure fakes: ``runner`` maps argv to canned
``CompletedProcess`` outputs; no real crontab/systemctl/schtasks call ever
runs.
"""

import json
import subprocess

import pytest
from devin_doctor.checks import automation
from devin_doctor.model import Status


def _proc(stdout="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, "")


def make_runner(table):
    """argv tuple -> (rc, stdout). Missing key -> tool absent (None)."""

    def runner(argv):
        rc, stdout = table.get(tuple(argv), (None, None))
        if rc is None:
            return None
        return _proc(stdout, rc)

    return runner


def _no_surfaces(argv):
    return None


def _snapshot(ctx):
    return ctx.data_dir / "doctor" / "automation-snapshot.json"


def _seed_snapshot(ctx, tasks):
    """tasks: list of (surface, name, command)."""
    path = _snapshot(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "tasks": [
                    {"surface": s, "name": n, "command": c}
                    for s, n, c in tasks
                ],
            }
        ),
        encoding="utf-8",
    )


def _find(findings, needle, surface=None):
    return [
        f
        for f in findings
        if needle in f.message
        and (surface is None or f.message.startswith(f"{surface}:"))
    ]


CRONTAB = """\
# daily ecosystem upkeep
0 8 * * * /home/u/devin/devin-ecosystem/devin-janitor/run.sh --quiet
SHELL=/bin/bash
*/30 * * * * devin-metrics collect
@daily echo ok
"""


def test_missing_path_is_fail(ctx, tmp_path):
    script = tmp_path / "gone.py"
    table = {
        ("crontab", "-l"): (0, f"0 8 * * * {script}\n"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    fails = _find(findings, "missing path", surface="cron")
    assert len(fails) == 1 and fails[0].status is Status.FAIL
    assert str(script) in fails[0].message


def test_existing_path_passes(ctx, tmp_path):
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/sh\n")
    table = {
        ("crontab", "-l"): (0, f"0 8 * * * {script}\n"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    assert not any(f.status is Status.FAIL for f in findings)
    ok = _find(findings, "1 scheduled task(s)", surface="cron")
    assert ok and ok[0].status is Status.PASS


def test_removed_task_is_fail(ctx):
    """The learned-heartbeat incident: 'office-freshness-check' was in the
    snapshot but is gone from the current crontab -> FAIL."""
    # snapshot identities for cron are the command line (schedule fields
    # stripped) — exactly what the check persists after a real run.
    _seed_snapshot(
        ctx,
        [
            ("cron", "/home/u/scripts/office-freshness-check.sh",
             "/home/u/scripts/office-freshness-check.sh"),
            ("cron", "/home/u/bin/daily.sh", "/home/u/bin/daily.sh"),
        ],
    )
    table = {
        ("crontab", "-l"): (0, "0 8 * * * /home/u/bin/daily.sh\n"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    removed = _find(findings, "removed since last check", surface="cron")
    assert len(removed) == 1
    assert removed[0].status is Status.FAIL
    assert "office-freshness-check" in removed[0].message
    # the snapshot was rewritten without the removed entry
    written = json.loads(_snapshot(ctx).read_text())
    names = [t["name"] for t in written["tasks"]]
    assert names == ["/home/u/bin/daily.sh"]


def test_first_run_records_baseline(ctx):
    table = {
        ("crontab", "-l"): (0, CRONTAB),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(
        ctx, runner=make_runner(table), which=lambda c: f"/usr/bin/{c}"
    )
    baseline = _find(findings, "baseline snapshot recorded")
    assert baseline and baseline[0].status is Status.PASS
    assert _snapshot(ctx).is_file()
    # second identical run -> no removal findings
    findings = automation.run(
        ctx, runner=make_runner(table), which=lambda c: f"/usr/bin/{c}"
    )
    removed = _find(findings, "removed since last check")
    assert not any(f.status is Status.FAIL for f in removed)
    assert _find(findings, "no scheduled tasks removed since last check")


def test_absorbed_repo_target_is_fail(ctx, tmp_path):
    """devin-history was absorbed into devin-explore/packages/history —
    a cron entry still pointing at the stale checkout must FAIL."""
    ws = tmp_path / "ws"
    registry_dir = ws / "devin-powerups"
    registry_dir.mkdir(parents=True)
    registry_dir.joinpath("registry.json").write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": "devin-history",
                        "local_dir": "devin-explore",
                        "package": {"path": "packages/history"},
                    },
                    {"name": "devin-doctor", "package": {"path": "packages/doctor"}},
                ]
            }
        )
    )
    stale = ws / "devin-history"
    stale.mkdir()
    script = stale / "export.py"
    script.write_text("# fixture\n")
    ctx.cwd = ws  # registry discovered by walking up from cwd
    table = {
        ("crontab", "-l"): (0, f"0 6 * * * {script}\n"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    absorbed = _find(findings, "absorbed repo 'devin-history'", surface="cron")
    assert len(absorbed) == 1 and absorbed[0].status is Status.FAIL
    assert "devin-explore/packages/history" in absorbed[0].message


def test_registry_missing_skips_absorbed_check(ctx, tmp_path):
    job = tmp_path / "job.sh"
    job.write_text("# fixture\n")
    table = {
        ("crontab", "-l"): (0, f"0 8 * * * {job.as_posix()}\n"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    skip = _find(findings, "absorbed-repo check skipped")
    assert skip and skip[0].status is Status.PASS
    assert not any(f.status is Status.FAIL for f in findings)


def test_unavailable_surfaces_report_cleanly(ctx):
    findings = automation.run(ctx, runner=_no_surfaces, plat="linux")
    assert findings
    assert all(f.status is Status.PASS for f in findings)
    assert _find(findings, "surface unavailable", surface="cron")
    assert _find(findings, "surface unavailable", surface="systemd-user")
    assert _find(findings, "surface unavailable", surface="task-scheduler")
    # nothing enumerated -> no snapshot written, no removal spam
    assert not _snapshot(ctx).exists()


def test_systemd_user_units_checked(ctx, tmp_path):
    missing = tmp_path / "uninstalled" / "devin-scout.py"
    interpreter = tmp_path / "python3"  # exists — only the script is gone
    interpreter.write_text("# fixture\n")
    real = tmp_path / "real.sh"
    real.write_text("#!/bin/sh\n")
    units_out = (
        "devin-scout.timer   loaded active waiting   devin-scout.timer\n"
        "devin-backup.service loaded inactive dead    devin-backup.service\n"
    )
    timer_unit = (
        "# /home/u/.config/systemd/user/devin-scout.timer\n"
        "[Timer]\nOnCalendar=daily\nUnit=devin-scout.service\n"
    )
    service_unit = (
        "[Service]\nType=oneshot\n"
        f"ExecStart={interpreter.as_posix()} {missing.as_posix()}\n"
    )
    backup_unit = f"[Service]\nExecStart={real.as_posix()}\n"
    table = {
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (0, units_out),
        ("systemctl", "--user", "cat", "devin-scout.timer"): (0, timer_unit),
        ("systemctl", "--user", "cat", "devin-scout.service"): (0, service_unit),
        ("systemctl", "--user", "cat", "devin-backup.service"): (0, backup_unit),
        ("crontab", "-l"): (1, "no crontab for u"),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    fails = _find(
        findings, "missing path", surface="systemd-user"
    )
    assert len(fails) == 1 and str(missing) in fails[0].message


def test_bare_command_not_on_path_is_fail(ctx, tmp_path):
    script = tmp_path / "ok.sh"
    script.write_text("#!/bin/sh\n")
    table = {
        ("crontab", "-l"): (0, f"0 8 * * * devin-janitor run && {script}\n"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(
        ctx,
        runner=make_runner(table),
        which=lambda c: None if c == "devin-janitor" else f"/usr/bin/{c}",
    )
    fails = _find(findings, "not on PATH", surface="cron")
    assert len(fails) == 1 and "devin-janitor" in fails[0].message


def test_task_scheduler_xml_parsed_on_windows(ctx, tmp_path):
    script = tmp_path / "task.py"
    script.write_text("# fixture\n")
    csv_out = '"\\Devin Scout","10/18/2026 8:00:00 AM","Ready"\r\n'
    xml_out = (
        '<?xml version="1.0"?>'
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<Actions><Exec>"
        f"<Command>{script}</Command><Arguments>--daily</Arguments>"
        "</Exec></Actions></Task>"
    )
    table = {
        ("schtasks", "/query", "/fo", "csv", "/nh"): (0, csv_out),
        ("schtasks", "/query", "/tn", "Devin Scout", "/xml"): (0, xml_out),
        ("crontab", "-l"): (1, ""),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table), plat="windows")
    ok = _find(findings, "1 scheduled task(s)", surface="task-scheduler")
    assert ok and ok[0].status is Status.PASS


def test_parse_crontab_skips_comments_and_env():
    commands = automation._parse_crontab(CRONTAB)
    assert commands == [
        "/home/u/devin/devin-ecosystem/devin-janitor/run.sh --quiet",
        "devin-metrics collect",
        "echo ok",
    ]


def test_task_targets_finds_paths_and_commands(tmp_path):
    work = tmp_path / "work"
    script = work / "run.py"
    log = tmp_path / "var" / "log" / "x.log"
    targets, commands = automation._task_targets(
        f"cd {work.as_posix()} && python3 {script.as_posix()} --v"
        f" > {log.as_posix()}",
        posix=True,
        home=tmp_path,
    )
    paths = {t.path: t.kind for t in targets}
    assert paths[work] == "exists"
    assert paths[script] == "exists"
    # redirection targets check the parent dir, not the file itself
    assert paths[log] == "parent"
    assert commands == ["python3"]


def test_task_targets_subcommand_args_not_flagged(tmp_path):
    """``script.sh run`` — 'run' is an argument, not a missing command."""
    tool = tmp_path / "tool.sh"
    targets, commands = automation._task_targets(
        f"{tool.as_posix()} run", posix=True, home=tmp_path
    )
    assert [t.path for t in targets] == [tool]
    assert commands == []


def test_task_targets_nested_sh_c_payload(tmp_path):
    srv = tmp_path / "srv"
    targets, commands = automation._task_targets(
        f"sh -c 'cd {srv.as_posix()} && ./deploy.sh'",
        posix=True,
        home=tmp_path,
    )
    # the inner 'cd <srv>' scopes './deploy.sh' to it
    assert srv in {t.path for t in targets}
    assert srv / "deploy.sh" in {t.path for t in targets}
    assert commands == ["sh"]


def test_task_targets_dbus_object_path_arg_skipped(tmp_path):
    """qdbus positional object paths are not filesystem references."""
    qdbus = tmp_path / "bin" / "qdbus"
    targets, commands = automation._task_targets(
        f"{qdbus.as_posix()} org.kde.kcminit /kcminit"
        " org.kde.KCMInit.runPhase1",
        posix=True,
        home=tmp_path,
    )
    assert [t.path for t in targets] == [qdbus]
    assert commands == []


def test_corrupt_snapshot_resets_baseline(ctx, tmp_path):
    job = tmp_path / "job.sh"
    job.write_text("# fixture\n")
    path = _snapshot(ctx)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    table = {
        ("crontab", "-l"): (0, f"0 8 * * * {job.as_posix()}\n"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    warn = _find(findings, "snapshot unreadable")
    assert warn and warn[0].status is Status.WARN
    assert _find(findings, "baseline snapshot recorded")


def test_empty_crontab_reports_no_tasks(ctx):
    table = {
        ("crontab", "-l"): (1, "no crontab for u"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    assert _find(findings, "no scheduled tasks", surface="cron")
    assert not any(f.status is Status.FAIL for f in findings)


@pytest.mark.parametrize("bad", ["{oops", "[]", '{"tasks": 42}'])
def test_load_snapshot_rejects_malformed(tmp_path, bad):
    path = tmp_path / "snap.json"
    path.write_text(bad)
    assert automation._load_snapshot(path) is None
