"""pr-ready-not-merged check: ecosystem PR scan via ``gh pr list``."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from devin_doctor.checks import prs
from devin_doctor.model import Status


@pytest.fixture(autouse=True)
def _online(monkeypatch):
    # conftest pins DEVIN_DOCTOR_OFFLINE=1 so full-scan tests stay
    # hermetic; these tests inject a fake runner and must reach it.
    monkeypatch.delenv("DEVIN_DOCTOR_OFFLINE", raising=False)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _created(ctx, hours_ago):
    return _iso(
        datetime.fromtimestamp(ctx.now() / 1000, tz=timezone.utc)
        - timedelta(hours=hours_ago)
    )


def check_run(name, conclusion="SUCCESS", status="COMPLETED", stamp=""):
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": stamp or "2026-01-01T00:00:00Z",
        "completedAt": stamp or "2026-01-01T00:01:00Z",
    }


def status_ctx(context, state="SUCCESS", stamp=""):
    return {
        "__typename": "StatusContext",
        "context": context,
        "state": state,
        "startedAt": stamp or "2026-01-01T00:00:00Z",
        "targetUrl": "",
    }


def make_pr(
    ctx,
    number=1,
    *,
    review="",
    checks=(),
    auto=None,
    hours_ago=2,
    title="a pull request",
    slug="Icaro0310/repo",
):
    return {
        "number": number,
        "title": title,
        "reviewDecision": review,
        "statusCheckRollup": list(checks),
        "autoMergeRequest": auto,
        "createdAt": _created(ctx, hours_ago),
        "url": f"https://github.com/{slug}/pull/{number}",
    }


def write_registry(ctx, slugs):
    """A minimal devin-powerups registry under ``ctx.cwd`` — the first
    location ``ecosystem.locate_registry`` probes."""
    reg_dir = ctx.cwd / "devin-powerups"
    reg_dir.mkdir(parents=True, exist_ok=True)
    reg_dir.joinpath("registry.json").write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "name": slug.rsplit("/", 1)[-1],
                        "url": f"https://github.com/{slug}",
                    }
                    for slug in slugs
                ]
            }
        ),
        encoding="utf-8",
    )


def fake_runner(prs_by_slug=None, errors=None):
    """A ``subprocess.run`` look-alike answering ``gh pr list -R <slug>``.

    ``prs_by_slug`` maps slug -> PR dict list; ``errors`` maps slug ->
    ``(returncode, stderr)``. Unlisted slugs return an empty list."""
    prs_by_slug = prs_by_slug or {}
    errors = errors or {}

    def _run(argv, **kwargs):
        assert argv[:3] == ["gh", "pr", "list"], argv
        slug = argv[argv.index("-R") + 1]
        if slug in errors:
            rc, stderr = errors[slug]
            return SimpleNamespace(returncode=rc, stdout="", stderr=stderr)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(prs_by_slug.get(slug, [])),
            stderr="",
        )

    return _run


def run_check(ctx, runner, slugs=("Icaro0310/repo",)):
    write_registry(ctx, slugs)
    return prs.run(ctx, runner=runner)


def test_registry_missing_passes_skipped(ctx):
    findings = prs.run(ctx, runner=fake_runner())
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert "registry not found" in findings[0].message


def test_registry_without_github_repos_passes(ctx):
    ctx.cwd.joinpath("devin-powerups").mkdir()
    ctx.cwd.joinpath("devin-powerups", "registry.json").write_text(
        json.dumps({"repositories": [{"name": "x", "url": "https://gitlab.com/x/y"}]}),
        encoding="utf-8",
    )
    findings = prs.run(ctx, runner=fake_runner())
    assert findings[0].status is Status.PASS
    assert "no github.com repos" in findings[0].message


def test_offline_warns(ctx, monkeypatch):
    monkeypatch.setenv("DEVIN_DOCTOR_OFFLINE", "1")

    def boom(argv, **kwargs):
        raise AssertionError("no subprocess may run offline")

    findings = prs.run(ctx, runner=boom)
    assert findings[0].status is Status.WARN
    assert "offline" in findings[0].message


def test_ready_but_unmerged_warns(ctx):
    pr = make_pr(
        ctx,
        141,
        review="",
        checks=[
            check_run("ci / test"),
            status_ctx("devin-judge/review"),
            status_ctx("Review Coverage"),
        ],
        auto=None,
        hours_ago=5,
    )
    findings = run_check(ctx, fake_runner({"Icaro0310/repo": [pr]}))
    flagged = [f for f in findings if f.status is Status.WARN]
    assert len(flagged) == 1
    assert "Icaro0310/repo#141" in flagged[0].message
    assert flagged[0].fix and "gh pr merge" in flagged[0].fix
    # the summary PASS still reports the scan counts
    summary = next(f for f in findings if f.status is Status.PASS)
    assert "1 ready-but-unmerged" in summary.message


def test_approved_review_decision_flags(ctx):
    pr = make_pr(
        ctx,
        7,
        review="APPROVED",
        checks=[check_run("ci")],
        hours_ago=1,
    )
    findings = run_check(ctx, fake_runner({"Icaro0310/repo": [pr]}))
    assert any(
        f.status is Status.WARN and "#7" in f.message for f in findings
    )


def test_auto_merge_armed_does_not_flag(ctx):
    pr = make_pr(
        ctx,
        9,
        review="APPROVED",
        checks=[check_run("ci")],
        auto={"enabledAt": "2026-01-01T00:00:00Z", "mergeMethod": "SQUASH"},
        hours_ago=5,
    )
    findings = run_check(ctx, fake_runner({"Icaro0310/repo": [pr]}))
    assert not any(f.status is Status.WARN for f in findings)
    assert "0 ready-but-unmerged" in findings[0].message


def test_failing_check_does_not_flag(ctx):
    pr = make_pr(
        ctx,
        3,
        review="APPROVED",
        checks=[check_run("ci", conclusion="FAILURE")],
        hours_ago=5,
    )
    findings = run_check(ctx, fake_runner({"Icaro0310/repo": [pr]}))
    assert not any(f.status is Status.WARN for f in findings)


def test_pending_check_does_not_flag(ctx):
    pr = make_pr(
        ctx,
        4,
        review="APPROVED",
        checks=[
            check_run("ci"),
            check_run("e2e", status="IN_PROGRESS", conclusion=None),
        ],
        hours_ago=5,
    )
    findings = run_check(ctx, fake_runner({"Icaro0310/repo": [pr]}))
    assert not any(f.status is Status.WARN for f in findings)


def test_cancelled_check_does_not_block(ctx):
    """CANCELLED is the concurrency-flake conclusion — the re-trigger
    storms leave dozens behind; they must not hide a ready PR."""
    pr = make_pr(
        ctx,
        5,
        review="APPROVED",
        checks=[
            check_run("approve / approve", conclusion="CANCELLED"),
            check_run("ci / test"),
            check_run("merge / merge", conclusion="CANCELLED"),
        ],
        hours_ago=5,
    )
    findings = run_check(ctx, fake_runner({"Icaro0310/repo": [pr]}))
    assert any(f.status is Status.WARN and "#5" in f.message for f in findings)


def test_latest_run_wins_over_stale_failure(ctx):
    """A FAILURE from an old attempt superseded by a newer SUCCESS must
    not block — dedupe keeps the newest entry per check name."""
    pr = make_pr(
        ctx,
        6,
        review="APPROVED",
        checks=[
            check_run("ci / test", conclusion="FAILURE", stamp="2026-01-01T00:00:00Z"),
            check_run("ci / test", conclusion="SUCCESS", stamp="2026-01-02T00:00:00Z"),
        ],
        hours_ago=5,
    )
    findings = run_check(ctx, fake_runner({"Icaro0310/repo": [pr]}))
    assert any(f.status is Status.WARN and "#6" in f.message for f in findings)


def test_latest_failure_wins_over_stale_success(ctx):
    pr = make_pr(
        ctx,
        8,
        review="APPROVED",
        checks=[
            check_run("ci / test", conclusion="SUCCESS", stamp="2026-01-01T00:00:00Z"),
            check_run("ci / test", conclusion="FAILURE", stamp="2026-01-02T00:00:00Z"),
        ],
        hours_ago=5,
    )
    findings = run_check(ctx, fake_runner({"Icaro0310/repo": [pr]}))
    assert not any(f.status is Status.WARN for f in findings)


def test_young_pr_does_not_flag(ctx):
    pr = make_pr(
        ctx,
        10,
        review="APPROVED",
        checks=[check_run("ci")],
        hours_ago=0.1,  # 6 minutes — inside the 30min grace window
    )
    findings = run_check(ctx, fake_runner({"Icaro0310/repo": [pr]}))
    assert not any(f.status is Status.WARN for f in findings)


def test_unapproved_pr_does_not_flag(ctx):
    pr = make_pr(
        ctx,
        11,
        review="CHANGES_REQUESTED",
        checks=[check_run("ci")],
        hours_ago=5,
    )
    findings = run_check(ctx, fake_runner({"Icaro0310/repo": [pr]}))
    assert not any(f.status is Status.WARN for f in findings)


def test_no_checks_at_all_does_not_flag(ctx):
    """An empty rollup is vacuously green — require at least one real
    SUCCESS/SKIPPED/NEUTRAL before calling a PR 'ready'."""
    pr = make_pr(ctx, 12, review="APPROVED", checks=[], hours_ago=5)
    findings = run_check(ctx, fake_runner({"Icaro0310/repo": [pr]}))
    assert not any(f.status is Status.WARN for f in findings)


def test_gone_repo_skipped_silently(ctx):
    findings = run_check(
        ctx,
        fake_runner(
            errors={"Icaro0310/gone": (1, "GraphQL: Could not resolve to a Repository")}
        ),
        slugs=("Icaro0310/gone",),
    )
    assert all(f.status is Status.PASS for f in findings)
    assert "unreachable (gone)" in findings[0].message


def test_unavailable_repo_warns_incomplete_scan(ctx):
    pr = make_pr(ctx, 1, review="APPROVED", checks=[check_run("ci")], hours_ago=5)
    findings = run_check(
        ctx,
        fake_runner(
            {"Icaro0310/repo": [pr]},
            errors={"Icaro0310/flaky": (1, "HTTP 502: bad gateway")},
        ),
        slugs=("Icaro0310/repo", "Icaro0310/flaky"),
    )
    incomplete = [f for f in findings if "incomplete" in f.message]
    assert len(incomplete) == 1 and incomplete[0].status is Status.WARN
    assert "Icaro0310/flaky" in incomplete[0].message


def test_all_repos_failing_warns_unavailable(ctx):
    findings = run_check(
        ctx,
        fake_runner(errors={"Icaro0310/a": (1, "HTTP 401: Bad credentials")}),
        slugs=("Icaro0310/a",),
    )
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "unavailable" in findings[0].message


def test_registry_dedupes_shared_repo_url(ctx):
    """Several registry products point at one GitHub repo (monorepo
    absorption) — the scan must query each slug once."""
    reg_dir = ctx.cwd / "devin-powerups"
    reg_dir.mkdir(parents=True, exist_ok=True)
    reg_dir.joinpath("registry.json").write_text(
        json.dumps(
            {
                "repositories": [
                    {"name": "devin-history", "url": "https://github.com/Icaro0310/devin-explore"},
                    {"name": "devin-pm", "url": "https://github.com/Icaro0310/devin-explore"},
                    {"name": "devin-search", "url": "https://github.com/Icaro0310/devin-explore"},
                ]
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def counting(argv, **kwargs):
        calls.append(argv[argv.index("-R") + 1])
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")

    prs.run(ctx, runner=counting)
    assert calls == ["Icaro0310/devin-explore"]


def test_run_all_accepts_optional_by_id(ctx):
    from devin_doctor import doctor

    report = doctor.run_all(ctx, optional=["pr-ready-not-merged"])
    ours = [f for f in report.findings if f.check == "pr-ready-not-merged"]
    # ctx.cwd is a tmp dir with no registry — the check PASSes 'skipped'.
    assert ours and ours[0].status is Status.PASS


def test_cli_flag_runs_check(ctx, capsys):
    from devin_doctor.cli import main

    write_registry(ctx, ["Icaro0310/repo"])
    rc = main(
        [
            "check",
            "--data-dir",
            str(ctx.data_dir),
            "--cwd",
            str(ctx.cwd),
            "--prs",
            "--offline",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "pr-ready-not-merged" in payload["checks"]
