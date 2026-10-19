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
    # the removed entry stays in the baseline — a removal keeps
    # alerting until the task returns or the entry is deleted
    written = json.loads(_snapshot(ctx).read_text())
    names = sorted(t["name"] for t in written["tasks"])
    assert names == [
        "/home/u/bin/daily.sh",
        "/home/u/scripts/office-freshness-check.sh",
    ]
    # second run: still missing -> still FAIL, not a pass
    findings2 = automation.run(ctx, runner=make_runner(table))
    again = _find(findings2, "removed since last check", surface="cron")
    assert len(again) == 1 and again[0].status is Status.FAIL


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


# ---- review regressions (PR #98 r1) -----------------------------------------


def test_crontab_query_failure_preserves_baseline(ctx, tmp_path):
    """`crontab -l` failing (not 'no crontab') is unavailable, not
    empty — saved tasks must not be flagged removed nor dropped."""
    _seed_snapshot(
        ctx,
        [("cron", "/home/u/bin/daily.sh", "/home/u/bin/daily.sh")],
    )
    table = {
        ("crontab", "-l"): (1, "permission denied"),  # stderr-less fake
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    assert _find(findings, "surface unavailable", surface="cron")
    assert not _find(findings, "removed since last check")
    # baseline kept the cron task
    written = json.loads(_snapshot(ctx).read_text())
    assert [t["name"] for t in written["tasks"]] == [
        "/home/u/bin/daily.sh"
    ]


def test_unavailable_surface_keeps_its_snapshot_entries(ctx):
    """One scheduler answering does not erase another's history."""
    _seed_snapshot(
        ctx,
        [
            ("cron", "/home/u/cronjob.sh", "/home/u/cronjob.sh"),
            ("systemd-user", "devin-scout.timer", "/bin/true"),
        ],
    )
    table = {
        ("crontab", "-l"): (0, "0 8 * * * /home/u/cronjob.sh\n"),
        # systemd surface does not answer this run
    }
    findings = automation.run(ctx, runner=make_runner(table))
    assert not [
        f for f in _find(findings, "scheduled task removed")
        if f.status is Status.FAIL
    ]
    written = json.loads(_snapshot(ctx).read_text())
    surfaces = {t["surface"] for t in written["tasks"]}
    assert "systemd-user" in surfaces  # survived the unanswered probe


def test_bare_relative_script_arg_checked(tmp_path):
    """`python3 backup.py` — no slash, still a real file reference."""
    missing = tmp_path / "backup.py"
    targets, commands = automation._task_targets(
        "python3 backup.py", posix=True, home=tmp_path
    )
    assert missing in {t.path for t in targets}
    assert commands == ["python3"]


def test_flag_value_not_flagged_as_missing_script(tmp_path):
    """`cmd --handler skills.audit` — the flag's value is not a script
    path even when it looks file-ish (real crontab false-positive)."""
    targets, _ = automation._task_targets(
        "python3 script.py --handler skills.audit",
        posix=True,
        home=tmp_path,
    )
    assert tmp_path / "script.py" in {t.path for t in targets}
    assert tmp_path / "skills.audit" not in {t.path for t in targets}


def test_interpreter_module_payload_not_a_path(tmp_path):
    """`python3 -m http.server` — a module name is never a file."""
    targets, _ = automation._task_targets(
        "python3 -m http.server 8080", posix=True, home=tmp_path
    )
    assert targets == []


def test_switch_flag_does_not_hide_script_arg(tmp_path):
    """`python3 -u backup.py` — boolean switches take no value; the
    script after them is still a filesystem reference."""
    targets, _ = automation._task_targets(
        "python3 -u backup.py", posix=True, home=tmp_path
    )
    assert tmp_path / "backup.py" in {t.path for t in targets}


def test_option_terminator_before_script(tmp_path):
    """`python3 -- backup.py` — '--' ends options, the script follows."""
    targets, _ = automation._task_targets(
        "python3 -- backup.py", posix=True, home=tmp_path
    )
    assert tmp_path / "backup.py" in {t.path for t in targets}


def test_preload_file_flag_checked(tmp_path):
    """`node -r preload.js` — '-r'/'--require' take a preload FILE, not
    a code string."""
    targets, _ = automation._task_targets(
        "node -r preload.js app.js", posix=True, home=tmp_path
    )
    assert tmp_path / "preload.js" in {t.path for t in targets}
    assert tmp_path / "app.js" in {t.path for t in targets}


def test_shell_c_single_token_payload_checked(tmp_path):
    """`sh -c ./missing.sh` — a single-token '-c' payload is still a
    command and its path is checked."""
    targets, _ = automation._task_targets(
        "sh -c ./missing.sh", posix=True, home=tmp_path
    )
    assert tmp_path / "missing.sh" in {t.path for t in targets}


def test_python_c_inline_code_not_a_path(tmp_path):
    """`python3 -c "print(1)"` — '-c' on a non-shell interpreter is
    inline code, never a path."""
    targets, commands = automation._task_targets(
        "python3 -c print\\(1\\)", posix=True, home=tmp_path
    )
    assert targets == []
    assert commands == ["python3"]


def test_non_interpreter_fileish_arg_not_checked(tmp_path):
    """`tar -czf out.tar.gz src/` — output args of non-interpreters are
    not verified (the file may be created by the run itself)."""
    targets, _ = automation._task_targets(
        "tar -czf out.tar.gz src/", posix=True, home=tmp_path
    )
    assert not any(t.path.name == "out.tar.gz" for t in targets)


def test_redact_covers_url_userinfo_and_bearer():
    assert (
        automation._redact("curl https://user:pw@h/x")
        == "curl https://***:***@h/x"
    )
    assert (
        automation._redact(
            "curl -H 'Authorization: Bearer abcdef0123456789'"
        )
        == "curl -H 'Authorization: Bearer ***'"
    )


def test_working_directory_scopes_relative_exec(ctx, tmp_path):
    """WorkingDirectory=<jobs> + ExecStart=... ./backup.py resolves
    under the unit's working dir, not home."""
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "backup.py").write_text("# fixture\n")
    interpreter = tmp_path / "python3"
    interpreter.write_text("# fixture\n")
    units_out = "jobs.service loaded active exited jobs.service\n"
    unit = (
        "[Service]\n"
        f"WorkingDirectory={jobs.as_posix()}\n"
        f"ExecStart={interpreter.as_posix()} ./backup.py\n"
    )
    table = {
        ("crontab", "-l"): (1, "no crontab for u"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (0, units_out),
        ("systemctl", "--user", "cat", "jobs.service"): (0, unit),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    assert not _find(findings, "missing path", surface="systemd-user")


def test_glued_separator_still_checks_script(tmp_path):
    """`python3 <missing>; echo done` — the glued ';' cannot hide
    the missing script behind one token."""
    missing = tmp_path / "missing.py"
    targets, _ = automation._task_targets(
        f"python3 {missing.as_posix()}; echo done",
        posix=True,
        home=tmp_path,
    )
    assert any(t.path == missing for t in targets)


def test_redirect_input_file_must_exist(tmp_path):
    """`reporter < <missing>` — input redirects check the file
    itself, not just its parent dir."""
    missing = tmp_path / "missing.txt"
    targets, _ = automation._task_targets(
        f"reporter < {missing.as_posix()}", posix=True, home=tmp_path
    )
    assert any(
        t.path == missing and t.kind == "exists" for t in targets
    )


def test_heredoc_delimiter_is_not_a_path(tmp_path):
    """`cmd <<EOF` — EOF is a delimiter, not a file to check."""
    targets, _ = automation._task_targets(
        "python3 - <<EOF\nprint(1)\nEOF", posix=True, home=tmp_path
    )
    assert not any(t.path.name == "EOF" for t in targets)


def test_cron_secret_redacted_from_name_and_snapshot(ctx, tmp_path):
    """A credential arg in a cron line never lands in findings or the
    on-disk baseline."""
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/sh\n")
    line = (
        f"0 8 * * * {script.as_posix()} --api-key sk-live-1234 "
        "TOKEN=hunter2"
    )
    table = {
        ("crontab", "-l"): (0, line + "\n"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    blob = "\n".join(f.message for f in findings)
    blob += _snapshot(ctx).read_text()
    assert "sk-live-1234" not in blob
    assert "hunter2" not in blob
    assert "***" in blob


def test_schtasks_unreadable_warns(ctx, tmp_path):
    """A task whose /xml query fails is counted, not silently healthy."""
    script = tmp_path / "task.py"
    script.write_text("# fixture\n")
    csv_out = (
        '"\\Devin Scout","10/18/2026 8:00:00 AM","Ready"\r\n'
        '"\\Broken Task","10/18/2026 8:00:00 AM","Ready"\r\n'
    )
    table = {
        ("crontab", "-l"): (1, ""),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
        ("schtasks", "/query", "/fo", "csv", "/nh"): (0, csv_out),
        ("schtasks", "/query", "/tn", "Devin Scout", "/xml"): (
            0,
            (
                "<Task><Actions><Exec><Command>"
                f"{script.as_posix()}</Command></Exec></Actions></Task>"
            ),
        ),
        ("schtasks", "/query", "/tn", "Broken Task", "/xml"): (1, ""),
    }
    findings = automation.run(
        ctx, runner=make_runner(table), plat="windows"
    )
    warn = _find(findings, "unreadable", surface="task-scheduler")
    assert warn and warn[0].status is Status.WARN
    assert "1 task(s)" in warn[0].message


def test_quoted_separator_in_path_not_split(tmp_path):
    """`python3 '/srv/a;b.py'` — a quoted ';' is literal, not a shell
    boundary; the fragments must never be checked."""
    targets, _ = automation._task_targets(
        f"python3 '{tmp_path.as_posix()}/a;b.py'",
        posix=True,
        home=tmp_path,
    )
    names = {t.path.name for t in targets}
    assert "a;b.py" in names
    assert "a" not in names


def test_fd_dup_does_not_swallow_next_token(tmp_path):
    """`python3 2>&1 <missing>` — fd duplication is self-contained;
    the script arg after it is still a target."""
    missing = tmp_path / "missing.py"
    targets, _ = automation._task_targets(
        f"python3 2>&1 {missing.as_posix()}",
        posix=True,
        home=tmp_path,
    )
    assert any(t.path == missing for t in targets)


def test_working_directory_reset_by_empty_directive():
    """`WorkingDirectory=` with an empty value clears an earlier one —
    drop-ins are concatenated into one `cat` output."""
    _cmds, workdir, _activates = automation._unit_payload(
        "[Service]\n"
        "WorkingDirectory=/srv/old\n"
        "WorkingDirectory=\n"
        "ExecStart=/usr/bin/python3 ./job.py\n"
    )
    assert workdir is None


def test_secret_differing_cron_jobs_are_distinct(ctx, tmp_path):
    """`job.sh --token alice` vs `--token bob` — redaction must not
    collapse them into one snapshot identity."""
    script = tmp_path / "job.sh"
    script.write_text("# fixture\n")
    table = {
        ("crontab", "-l"): (
            0,
            (
                f"0 8 * * * {script.as_posix()} --token alice\n"
                f"0 9 * * * {script.as_posix()} --token bob\n"
            ),
        ),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    assert _find(findings, "baseline snapshot recorded")
    cron_rows = [
        t
        for t in json.loads(_snapshot(ctx).read_text())["tasks"]
        if t["surface"] == "cron"
    ]
    assert len(cron_rows) == 2
    assert len({r["id"] for r in cron_rows}) == 2  # distinct identities
    assert "alice" not in _snapshot(ctx).read_text()
    # remove alice's job — removal of ONE of the pair still FAILs
    table[("crontab", "-l")] = (
        0, f"0 9 * * * {script.as_posix()} --token bob\n"
    )
    findings = automation.run(ctx, runner=make_runner(table))
    removed = _find(findings, "removed since last check", surface="cron")
    assert len(removed) == 1 and removed[0].status is Status.FAIL


def test_secret_flag_value_never_reaches_path_finding(ctx, tmp_path):
    """`up.sh --token /secrets/k.pem` — the credential value is
    redacted before extraction, so no finding can print it."""
    script = tmp_path / "up.sh"
    script.write_text("# fixture\n")
    secret_path = tmp_path / "secrets" / "k.pem"
    table = {
        ("crontab", "-l"): (
            0,
            (
                f"0 8 * * * {script.as_posix()}"
                f" --token {secret_path.as_posix()}\n"
            ),
        ),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    blob = "\n".join(f.message for f in findings)
    blob += _snapshot(ctx).read_text()
    assert "k.pem" not in blob
    assert not _find(findings, "missing path", surface="cron")


def test_unloaded_unit_file_still_inventoried(ctx, tmp_path):
    """An installed-but-unloaded timer appears via list-unit-files —
    list-units alone would miss it and call it removed."""
    timer_unit = (
        "[Timer]\nOnCalendar=daily\nUnit=devin-nightly.service\n"
    )
    service = tmp_path / "nightly.sh"
    service.write_text("#!/bin/sh\n")
    service_unit = (
        "[Service]\nType=oneshot\n"
        f"ExecStart={service.as_posix()}\n"
    )
    table = {
        ("crontab", "-l"): (1, "no crontab for u"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (0, ""),
        ("systemctl", "--user", "list-unit-files",
         "--type=service,timer", "--no-legend", "--no-pager"): (
            0, "devin-nightly.timer enabled -\n"
        ),
        ("systemctl", "--user", "cat", "devin-nightly.timer"): (
            0, timer_unit
        ),
        ("systemctl", "--user", "cat", "devin-nightly.service"): (
            0, service_unit
        ),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    assert _find(
        findings, "1 scheduled task(s)", surface="systemd-user"
    )


# ---- review regressions (PR #98 r3) -----------------------------------------


def test_restored_cron_job_clears_its_removal(ctx):
    """A legacy name-keyed row that is carried while missing must not
    gain an 'id' — the restored job has to re-key to its digest."""
    job = "/home/u/bin/nightly.sh"
    _seed_snapshot(ctx, [("cron", job, job)])
    table = {
        ("crontab", "-l"): (1, "no crontab for u"),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    findings = automation.run(ctx, runner=make_runner(table))
    assert _find(findings, "scheduled task removed", surface="cron")
    # the job comes back — the sticky removal must clear
    table[("crontab", "-l")] = (0, f"0 8 * * * {job}\n")
    findings = automation.run(ctx, runner=make_runner(table))
    assert not [
        f for f in _find(findings, "scheduled task removed")
        if f.status is Status.FAIL
    ]


def test_cron_credential_rotation_is_not_a_removal(ctx, tmp_path):
    """`--token alice` -> `--token bob`: same scheduled job, no
    false removal — identity digests the REDACTED command."""
    script = tmp_path / "job.sh"
    script.write_text("# fixture\n")
    table = {
        ("crontab", "-l"): (
            0, f"0 8 * * * {script.as_posix()} --token alice\n"
        ),
        ("systemctl", "--user", "list-units", "--type=service,timer",
         "--all", "--no-legend", "--no-pager"): (1, ""),
    }
    automation.run(ctx, runner=make_runner(table))
    table[("crontab", "-l")] = (
        0, f"0 8 * * * {script.as_posix()} --token bob\n"
    )
    findings = automation.run(ctx, runner=make_runner(table))
    assert not [
        f for f in _find(findings, "scheduled task removed")
        if f.status is Status.FAIL
    ]
    # and the baseline still tracks exactly one cron job
    cron_rows = [
        t for t in json.loads(_snapshot(ctx).read_text())["tasks"]
        if t["surface"] == "cron"
    ]
    assert len(cron_rows) == 1


def test_node_require_preload_file_checked(tmp_path):
    """`node --require missing.js app.js` — the preload file must be
    inspected, not swallowed as an option value."""
    targets, _ = automation._task_targets(
        "node --require missing.js app.js",
        posix=True,
        home=tmp_path,
    )
    paths = {t.path for t in targets}
    assert tmp_path / "missing.js" in paths
    assert tmp_path / "app.js" in paths


def test_node_import_equals_form_checked(tmp_path):
    """`--import=x.js` carries the preload path inline."""
    targets, _ = automation._task_targets(
        "node --import=inline.js app.js",
        posix=True,
        home=tmp_path,
    )
    assert tmp_path / "inline.js" in {t.path for t in targets}


def test_node_require_module_name_not_checked(tmp_path):
    """`-r dotenv/config` is a node module, not a cwd-relative file —
    it must not produce a bogus path target."""
    targets, _ = automation._task_targets(
        "node -r dotenv/config app.js",
        posix=True,
        home=tmp_path,
    )
    assert tmp_path / "dotenv" / "config" not in {
        t.path for t in targets
    }


def test_leading_fd_redirect_does_not_hide_command(tmp_path):
    """`2>log missing-tool` — the fd number is part of the redirect,
    not the command word."""
    targets, commands = automation._task_targets(
        "2>/tmp/err.log python3 missing.py",
        posix=True,
        home=tmp_path,
    )
    assert "python3" in commands
    assert "2" not in commands
    assert tmp_path / "missing.py" in {t.path for t in targets}


def test_fd_dup_with_split_target_consumed(tmp_path):
    """`cmd 2>& 1` — a bare fd number after '>&' is not a path."""
    targets, _ = automation._task_targets(
        "echo hi 2>& 1", posix=True, home=tmp_path
    )
    assert tmp_path / "1" not in {t.path for t in targets}


def test_combined_redirect_checks_parent_not_file(tmp_path):
    """`cmd &> out.log` — the file may be created at run time;
    only its parent dir is verifiable."""
    log = tmp_path / "new.log"
    targets, _ = automation._task_targets(
        f"echo done &> {log.as_posix()}", posix=True, home=tmp_path
    )
    by_path = {t.path: t.kind for t in targets}
    assert by_path.get(log) == "parent"


# ---- review regressions (PR #98 r4) -----------------------------------------


def test_inline_preload_env_var_expands(tmp_path, monkeypatch):
    """`node --require=$VAR/pre.js` — env vars inside inline preload
    values are expanded before the existence check."""
    monkeypatch.setenv("DJAEV_TEST_PRE", str(tmp_path))
    targets, _ = automation._task_targets(
        "node --require=$DJAEV_TEST_PRE/pre.js app.js",
        posix=True,
        home=tmp_path,
    )
    assert tmp_path / "pre.js" in {t.path for t in targets}
    assert tmp_path / "app.js" in {t.path for t in targets}


def test_inline_preload_unresolved_var_is_not_a_path(
    tmp_path, monkeypatch
):
    """`--require=$UNSET/pre.js` — an env var that cannot expand is
    environment-dependent, not a missing literal path."""
    monkeypatch.delenv("DJAEV_TEST_UNSET_XQ", raising=False)
    targets, _ = automation._task_targets(
        "node --require=$DJAEV_TEST_UNSET_XQ/pre.js app.js",
        posix=True,
        home=tmp_path,
    )
    assert all("$" not in str(t.path) for t in targets)
    assert tmp_path / "app.js" in {t.path for t in targets}


def test_spaced_digit_before_redirect_is_a_word(tmp_path):
    """`123 >out` — a digit separated from the operator by whitespace
    is a command word, not an fd prefix; only '2>out' (glued) is."""
    log = tmp_path / "out.log"
    targets, commands = automation._task_targets(
        f"123 >{log.as_posix()}", posix=True, home=tmp_path
    )
    assert commands == ["123"]
    assert any(t.path == log and t.kind == "parent" for t in targets)


def test_digits_glued_or_spaced_vs_word_suffix(tmp_path):
    """io_number is only an fd when the digit run is a standalone
    word glued to the operator: '2>out' strips it, 'python3 >out'
    keeps 'python3', '123 >out' keeps '123' as a command word and
    '/tmp/job2>out' keeps the '2' inside the path."""
    log = tmp_path / "out.log"
    job = tmp_path / "job2"
    # glued fd -> the real command survives, not the digit
    _, commands = automation._task_targets(
        f"2>{log.as_posix()} python3 backup.py",
        posix=True,
        home=tmp_path,
    )
    assert commands == ["python3"]
    # digit ending a word is literal, not an fd
    _, commands = automation._task_targets(
        f"python3 >{log.as_posix()}", posix=True, home=tmp_path
    )
    assert commands == ["python3"]
    # '/tmp/job2 >out' — the '2' belongs to the path
    targets, _ = automation._task_targets(
        f"{job.as_posix()} >{log.as_posix()}", posix=True, home=tmp_path
    )
    assert job in {t.path for t in targets}
    assert any(t.path == log and t.kind == "parent" for t in targets)
