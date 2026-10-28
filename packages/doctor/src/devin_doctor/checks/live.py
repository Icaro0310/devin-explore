"""Opt-in check (``--live``) — live connectivity probes.

Proves the channel, not the key: a real Slack ``auth.test`` call and an
Ollama ``/api/tags`` GET, each bounded to ~8 s. A configured endpoint
that fails to answer is a ``FAIL``; nothing configured is a ``WARN``
skip; transport errors degrade to findings, never crash the run.

Tokens are resolved but never printed: ``SLACK_BOT_TOKEN`` /
``SLACK_USER_TOKEN`` env first, then the first ``*/gateways/.env`` glob
under the home dir and under the ecosystem root
(``$DEVIN_ECOSYSTEM_ROOT``, default ``~/devin``). Only the token's
*source* (env var vs file path) is reported.

Opt-in by flag: the default run and the ``--offline`` gate stay
hermetic — offline wins when both flags are passed.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from devin_doctor.model import Context, Finding, Status

CHECK_ID = "live"

SLACK_AUTH_URL = "https://slack.com/api/auth.test"
SLACK_TOKEN_VARS = ("SLACK_BOT_TOKEN", "SLACK_USER_TOKEN")
# ``OLLAMA_HOST`` wins when set; otherwise these candidates are tried in
# order (local installs only — this check never guesses remote hosts).
OLLAMA_CANDIDATES = ("http://localhost:11434",)
ECOSYSTEM_ROOT_ENV = "DEVIN_ECOSYSTEM_ROOT"

_TIMEOUT = 8


def _read_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` pairs from a dotenv-style file. Best-effort:
    ``export`` prefixes, quotes and comments are stripped; an unreadable
    file parses as empty."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip().strip('"').strip("'")
        out[key.strip()] = value
    return out


def _slack_token(
    environ: Mapping[str, str], home: Path
) -> tuple[str | None, str | None]:
    """``(token, source)`` — env var first, then the first
    ``*/gateways/.env`` under ``home`` and under the ecosystem root."""
    for var in SLACK_TOKEN_VARS:
        if environ.get(var):
            return environ[var], f"env {var}"
    roots = [home]
    eco_root = environ.get(ECOSYSTEM_ROOT_ENV) or str(home / "devin")
    roots.append(Path(eco_root).expanduser())
    for root in dict.fromkeys(roots):
        try:
            env_files = sorted(root.glob("*/gateways/.env"))
        except OSError:
            continue
        for env_file in env_files:
            values = _read_env_file(env_file)
            for var in SLACK_TOKEN_VARS:
                if values.get(var):
                    return values[var], str(env_file)
    return None, None


def _slack_finding(
    environ: Mapping[str, str],
    home: Path,
    opener: Callable[..., Any],
) -> Finding:
    token, source = _slack_token(environ, home)
    if not token:
        return Finding(
            CHECK_ID,
            Status.WARN,
            "slack: no token found (SLACK_BOT_TOKEN/SLACK_USER_TOKEN env "
            "or */gateways/.env) — auth.test skipped",
        )
    request = urllib.request.Request(
        SLACK_AUTH_URL,
        data=b"{}",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with opener(request, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # An HTTP status is still a response — Slack may answer 4xx with
        # a JSON body on bad auth.
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except (OSError, ValueError, AttributeError):
            data = {}
        if not data:
            return Finding(
                CHECK_ID,
                Status.FAIL,
                f"slack: auth.test HTTP {exc.code} (token source: {source})",
            )
    except (
        urllib.error.URLError,
        OSError,
        http.client.HTTPException,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        return Finding(
            CHECK_ID,
            Status.FAIL,
            f"slack: auth.test unreachable ({exc}) — token source: {source}",
        )
    if not isinstance(data, dict):
        data = {}
    if data.get("ok") is True:
        team = data.get("team") or data.get("team_id") or "?"
        return Finding(
            CHECK_ID,
            Status.PASS,
            f"slack: auth.test ok (team: {team}, token source: {source})",
        )
    return Finding(
        CHECK_ID,
        Status.FAIL,
        f"slack: auth.test failed: {data.get('error', 'unknown error')} "
        f"(token source: {source})",
        fix="Rotate or re-export the Slack token — the stored one no "
        "longer authenticates.",
    )


def _ollama_finding(
    environ: Mapping[str, str], opener: Callable[..., Any]
) -> Finding:
    host = (environ.get("OLLAMA_HOST") or "").strip().rstrip("/")
    candidates = (host,) if host else OLLAMA_CANDIDATES
    last_error: Exception | None = None
    for base in candidates:
        url = f"{base}/api/tags"
        try:
            with opener(url, timeout=_TIMEOUT) as resp:
                body = resp.read()
            models = json.loads(body.decode("utf-8")).get("models", [])
            n = len(models) if isinstance(models, list) else 0
            return Finding(
                CHECK_ID,
                Status.PASS,
                f"ollama: {base} reachable ({n} model(s) installed)",
            )
        except urllib.error.HTTPError as exc:
            # Any HTTP status proves the daemon is listening.
            return Finding(
                CHECK_ID,
                Status.PASS,
                f"ollama: {base} reachable (HTTP {exc.code} on /api/tags)",
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return Finding(
                CHECK_ID,
                Status.FAIL,
                f"ollama: {base} answered /api/tags with invalid JSON "
                f"({exc})",
            )
        except (
            urllib.error.URLError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            last_error = exc
    if host:
        return Finding(
            CHECK_ID,
            Status.FAIL,
            f"ollama: OLLAMA_HOST={host} unreachable ({last_error})",
            fix="Start the Ollama daemon or fix OLLAMA_HOST.",
        )
    return Finding(
        CHECK_ID,
        Status.WARN,
        f"ollama: nothing at {candidates[0]} and OLLAMA_HOST unset — "
        "probe skipped",
    )


def run(
    ctx: Context,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[Finding]:
    env = os.environ if environ is None else environ
    if ctx.offline or env.get("DEVIN_DOCTOR_OFFLINE") == "1":
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                "live probes unavailable (offline mode)",
            )
        ]
    home_dir = Path.home() if home is None else home
    return [
        _slack_finding(env, home_dir, opener),
        _ollama_finding(env, opener),
    ]
