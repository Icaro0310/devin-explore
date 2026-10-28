"""live check: Slack auth.test + Ollama /api/tags connectivity probes."""

import io
import json
import urllib.error

import pytest
from devin_doctor.checks import live
from devin_doctor.model import Status


@pytest.fixture(autouse=True)
def _online(monkeypatch):
    # conftest pins DEVIN_DOCTOR_OFFLINE=1 and the real env may carry
    # tokens/hosts — all network inputs are controlled per test.
    monkeypatch.delenv("DEVIN_DOCTOR_OFFLINE", raising=False)
    for var in ("SLACK_BOT_TOKEN", "SLACK_USER_TOKEN", "OLLAMA_HOST"):
        monkeypatch.delenv(var, raising=False)


class FakeResp:
    """A ``urlopen``-shaped context manager returning a fixed body."""

    def __init__(self, body: bytes, code: int = 200):
        self._body = body
        self.code = code

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def opener_returning(body: bytes):
    def _open(url, timeout=None):
        return FakeResp(body)

    return _open


def opener_raising(exc: Exception):
    def _open(url, timeout=None):
        raise exc

    return _open


def slack_only(findings):
    return next(f for f in findings if f.message.startswith("slack:"))


def ollama_only(findings):
    return next(f for f in findings if f.message.startswith("ollama:"))


def _run(ctx, *, opener, environ, home=None):
    return live.run(
        ctx, opener=opener, environ=environ, home=home or ctx.cwd
    )


# --- slack ------------------------------------------------------------------


def test_slack_ok_passes(ctx):
    findings = _run(
        ctx,
        opener=opener_returning(
            json.dumps({"ok": True, "team": "Acme"}).encode()
        ),
        environ={"SLACK_BOT_TOKEN": "xoxb-test-token"},
    )
    f = slack_only(findings)
    assert f.status is Status.PASS
    assert "Acme" in f.message
    assert "xoxb-test-token" not in f.message  # never printed


def test_slack_api_error_fails(ctx):
    findings = _run(
        ctx,
        opener=opener_returning(
            json.dumps({"ok": False, "error": "invalid_auth"}).encode()
        ),
        environ={"SLACK_BOT_TOKEN": "xoxb-bad"},
    )
    f = slack_only(findings)
    assert f.status is Status.FAIL
    assert "invalid_auth" in f.message
    assert "xoxb-bad" not in f.message


def test_slack_http_error_body_still_parsed(ctx):
    def _open(url, timeout=None):
        raise urllib.error.HTTPError(
            "https://slack.com/api/auth.test",
            401,
            "Unauthorized",
            {},
            io.BytesIO(json.dumps({"ok": False, "error": "invalid_auth"}).encode()),
        )

    findings = _run(ctx, opener=_open, environ={"SLACK_USER_TOKEN": "x"})
    f = slack_only(findings)
    assert f.status is Status.FAIL
    assert "invalid_auth" in f.message


def test_slack_unreachable_fails(ctx):
    findings = _run(
        ctx,
        opener=opener_raising(urllib.error.URLError("conn refused")),
        environ={"SLACK_BOT_TOKEN": "xoxb-t"},
    )
    f = slack_only(findings)
    assert f.status is Status.FAIL
    assert "unreachable" in f.message


def test_slack_no_token_warns_skip(ctx, tmp_path):
    findings = _run(
        ctx,
        opener=opener_returning(b'{"ok": true}'),
        environ={},
        home=tmp_path,
    )
    f = slack_only(findings)
    assert f.status is Status.WARN
    assert "no token" in f.message


def test_slack_token_from_gateways_env_file(ctx, tmp_path):
    env_file = tmp_path / "ecosystem" / "gateways" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text(
        "# comment\nSLACK_BOT_TOKEN=dotenv-token\nOTHER=1\n",
        encoding="utf-8",
    )
    findings = _run(
        ctx,
        opener=opener_returning(json.dumps({"ok": True}).encode()),
        environ={},
        home=tmp_path,
    )
    f = slack_only(findings)
    assert f.status is Status.PASS
    assert "gateways/.env" in f.message  # source, not value
    assert "dotenv-token" not in f.message


def test_slack_token_from_ecosystem_root(ctx, tmp_path):
    eco = tmp_path / "eco-root"
    env_file = eco / "proj" / "gateways" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("SLACK_USER_TOKEN=eco-token\n", encoding="utf-8")
    findings = _run(
        ctx,
        opener=opener_returning(json.dumps({"ok": True}).encode()),
        environ={"DEVIN_ECOSYSTEM_ROOT": str(eco)},
        home=tmp_path / "elsewhere",
    )
    f = slack_only(findings)
    assert f.status is Status.PASS
    assert "eco-token" not in f.message


# --- ollama -----------------------------------------------------------------


def test_ollama_ok_reports_model_count(ctx):
    findings = _run(
        ctx,
        opener=opener_returning(
            json.dumps({"models": [{"name": "a"}, {"name": "b"}]}).encode()
        ),
        environ={},
    )
    f = ollama_only(findings)
    assert f.status is Status.PASS
    assert "2 model(s)" in f.message


def test_ollama_explicit_host_unreachable_fails(ctx):
    findings = _run(
        ctx,
        opener=opener_raising(urllib.error.URLError("conn refused")),
        environ={"OLLAMA_HOST": "http://gpu-box:11434"},
    )
    f = ollama_only(findings)
    assert f.status is Status.FAIL
    assert "gpu-box" in f.message


def test_ollama_default_unreachable_warns_skip(ctx):
    findings = _run(
        ctx,
        opener=opener_raising(urllib.error.URLError("conn refused")),
        environ={},
    )
    f = ollama_only(findings)
    assert f.status is Status.WARN
    assert "skipped" in f.message


def test_ollama_http_error_still_proves_daemon(ctx):
    def _open(url, timeout=None):
        raise urllib.error.HTTPError(url, 500, "err", {}, None)

    findings = _run(ctx, opener=_open, environ={})
    f = ollama_only(findings)
    assert f.status is Status.PASS
    assert "HTTP 500" in f.message


# --- offline + plumbing -----------------------------------------------------


def test_offline_mode_warns(ctx):
    def boom(url, timeout=None):
        raise AssertionError("no network offline")

    findings = _run(
        ctx, opener=boom, environ={"DEVIN_DOCTOR_OFFLINE": "1"}
    )
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "offline" in findings[0].message


def test_run_returns_one_finding_per_probe(ctx, tmp_path):
    findings = _run(
        ctx,
        opener=opener_raising(urllib.error.URLError("down")),
        environ={},
        home=tmp_path,
    )
    assert [f.message.split(":")[0] for f in findings] == [
        "slack",
        "ollama",
    ]
    assert all(f.check == live.CHECK_ID for f in findings)
