"""integrations check: half-configured CI detection via git + gh CLI."""

import json
from types import SimpleNamespace

import pytest
from devin_doctor.checks import integrations
from devin_doctor.model import Status


@pytest.fixture(autouse=True)
def _online(monkeypatch):
    # conftest pins DEVIN_DOCTOR_OFFLINE=1 so full-scan tests stay
    # hermetic; these tests inject a fake runner and must reach it.
    monkeypatch.delenv("DEVIN_DOCTOR_OFFLINE", raising=False)


def fake_runner(
    *,
    url="git@github.com:acme/app.git",
    git_rc=0,
    secrets=(),
    workflows=(),
    gh_fail=None,
):
    """A ``subprocess.run`` look-alike dispatching on argv.

    ``workflows=None`` simulates the contents API 404 (no
    ``.github/workflows`` dir); ``gh_fail`` makes every ``gh api`` call
    exit non-zero with that stderr.
    """

    def _run(argv, **kwargs):
        if argv[:3] == ["git", "remote", "get-url"]:
            return SimpleNamespace(
                returncode=git_rc,
                stdout=(url + "\n") if git_rc == 0 else "",
                stderr="" if git_rc == 0 else "error: No such remote",
            )
        assert argv[:2] == ["gh", "api"], argv
        if gh_fail is not None:
            return SimpleNamespace(returncode=1, stdout="", stderr=gh_fail)
        path = argv[2]
        if path.endswith("/actions/secrets"):
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "total_count": len(secrets),
                        "secrets": [{"name": s} for s in secrets],
                    }
                ),
                stderr="",
            )
        if path.endswith("/contents/.github/workflows"):
            if workflows is None:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr='HTTP 404: Not Found '
                    '(https://api.github.com/repositories/1/contents/x)',
                )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [{"name": w, "type": "file"} for w in workflows]
                ),
                stderr="",
            )
        raise AssertionError(f"unexpected gh api path: {path}")

    return _run


def test_both_present_passes(ctx):
    findings = integrations.run(
        ctx,
        runner=fake_runner(
            secrets=("TS_AUTHKEY",), workflows=("ai-review.yml", "ci.yml")
        ),
    )
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert "ai-review" in findings[0].message


def test_secret_only_fails_half_configured(ctx):
    findings = integrations.run(
        ctx,
        runner=fake_runner(secrets=("TS_AUTHKEY",), workflows=("ci.yml",)),
    )
    assert findings[0].status is Status.FAIL
    assert "half-configured" in findings[0].message
    assert "TS_AUTHKEY" in findings[0].message
    assert findings[0].fix


def test_workflow_only_fails_half_configured(ctx):
    findings = integrations.run(
        ctx,
        runner=fake_runner(secrets=(), workflows=("fallback.yml",)),
    )
    assert findings[0].status is Status.FAIL
    assert "half-configured" in findings[0].message
    assert "fallback.yml" in findings[0].message


def test_neither_warns_fallback_absent(ctx):
    # workflows=None -> the contents API 404s (no .github/workflows dir)
    findings = integrations.run(
        ctx, runner=fake_runner(secrets=(), workflows=None)
    )
    assert findings[0].status is Status.WARN
    assert "review fallback absent" in findings[0].message


def test_gh_failure_warns_unavailable(ctx):
    findings = integrations.run(
        ctx, runner=fake_runner(gh_fail="error connecting to api.github.com")
    )
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "unavailable" in findings[0].message


def test_gh_binary_missing_warns_unavailable(ctx):
    def boom(argv, **kwargs):
        if argv[:3] == ["git", "remote", "get-url"]:
            return SimpleNamespace(
                returncode=0, stdout="git@github.com:acme/app.git\n", stderr=""
            )
        raise FileNotFoundError("gh")

    findings = integrations.run(ctx, runner=boom)
    assert findings[0].status is Status.WARN
    assert "unavailable" in findings[0].message


def test_no_git_remote_warns(ctx):
    findings = integrations.run(ctx, runner=fake_runner(git_rc=1))
    assert findings[0].status is Status.WARN


def test_non_github_remote_skips(ctx):
    findings = integrations.run(
        ctx, runner=fake_runner(url="git@gitlab.com:acme/app.git")
    )
    assert findings[0].status is Status.WARN
    assert "github.com" in findings[0].message


def test_https_remote_resolves_slug(ctx):
    findings = integrations.run(
        ctx,
        runner=fake_runner(
            url="https://github.com/acme/app.git",
            secrets=("TS_AUTHKEY",),
            workflows=("fallback.yaml",),
        ),
    )
    assert findings[0].status is Status.PASS


def test_workflow_match_is_case_insensitive(ctx):
    findings = integrations.run(
        ctx,
        runner=fake_runner(
            secrets=("TS_AUTHKEY",), workflows=("AI-Review.yml",)
        ),
    )
    assert findings[0].status is Status.PASS


def test_offline_mode_warns(ctx, monkeypatch):
    monkeypatch.setenv("DEVIN_DOCTOR_OFFLINE", "1")

    def boom(argv, **kwargs):
        raise AssertionError("no subprocess may run offline")

    findings = integrations.run(ctx, runner=boom)
    assert findings[0].status is Status.WARN
    assert "offline" in findings[0].message


def test_run_all_accepts_optional_by_id(ctx):
    from devin_doctor import doctor

    report = doctor.run_all(ctx, optional=["integrations"])
    # ctx.cwd is a tmp dir — `git remote` fails there, so the check
    # reports its WARN-unavailable finding rather than crashing.
    ours = [f for f in report.findings if f.check == "integrations"]
    assert len(ours) == 1
    assert ours[0].status is Status.WARN


def test_run_all_rejects_unknown_optional(ctx):
    from devin_doctor import doctor

    with pytest.raises(ValueError, match="unknown optional"):
        doctor.run_all(ctx, optional=["not-a-check"])
