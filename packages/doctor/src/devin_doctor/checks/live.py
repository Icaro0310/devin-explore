"""Opt-in check (``--live``) — live connectivity probes.

Proves the channel, not the key: a real Slack ``auth.test`` call plus a
``chat.postMessage`` delivery smoke (one ``devin-doctor live-check
<utc-iso>`` diagnostic, never retried) and an Ollama ``/api/tags`` GET,
each bounded to ~8 s. A configured endpoint that fails to answer is a
``FAIL``; nothing configured is a ``WARN`` skip; transport errors
degrade to findings, never crash the run.

Tokens are resolved but never printed: ``SLACK_BOT_TOKEN`` /
``SLACK_USER_TOKEN`` env first, then the first ``*/gateways/.env`` glob
under the home dir and under the ecosystem root
(``$DEVIN_ECOSYSTEM_ROOT``, default ``~/devin``). Only the token's
*source* (env var vs file path) is reported.

The delivery channel resolves like ``gateways/src/slack-notify.js``:
``SLACK_DOCTOR_CHANNEL``, else ``SLACK_NOTIFY_CHANNEL``, else a DM
opened via ``SLACK_NOTIFY_USER`` — read from the same env-first
sources as the token; only the resolved channel id is reported.

Opt-in by flag: the default run and the ``--offline`` gate stay
hermetic — offline wins when both flags are passed.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from devin_doctor.model import Context, Finding, Status

CHECK_ID = "live"

SLACK_AUTH_URL = "https://slack.com/api/auth.test"
SLACK_OPEN_URL = "https://slack.com/api/conversations.open"
SLACK_POST_URL = "https://slack.com/api/chat.postMessage"
SLACK_TOKEN_VARS = ("SLACK_BOT_TOKEN", "SLACK_USER_TOKEN")
# Delivery channel resolution — the same order
# ``gateways/src/slack-notify.js`` uses: an explicit channel first,
# else a DM opened with the notify user.
SLACK_CHANNEL_VARS = ("SLACK_DOCTOR_CHANNEL", "SLACK_NOTIFY_CHANNEL")
SLACK_USER_VARS = ("SLACK_NOTIFY_USER",)
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


def _env_files(
    environ: Mapping[str, str], home: Path
) -> Iterator[Path]:
    """``*/gateways/.env`` candidates in scan order — under ``home``
    first, then under the ecosystem root (``$DEVIN_ECOSYSTEM_ROOT``,
    default ``~/devin``)."""
    roots = [home]
    eco_root = environ.get(ECOSYSTEM_ROOT_ENV) or str(home / "devin")
    roots.append(Path(eco_root).expanduser())
    for root in dict.fromkeys(roots):
        try:
            yield from sorted(root.glob("*/gateways/.env"))
        except OSError:
            continue


def _slack_setting(
    environ: Mapping[str, str], home: Path, var_names: tuple[str, ...]
) -> tuple[str | None, str | None]:
    """``(value, source)`` for the first non-empty var — real env wins
    over the ``*/gateways/.env`` files, scanned in :func:`_env_files`
    order. The source is the var name or the file path, never the
    value."""
    for var in var_names:
        if environ.get(var):
            return environ[var], f"env {var}"
    for env_file in _env_files(environ, home):
        values = _read_env_file(env_file)
        for var in var_names:
            if values.get(var):
                return values[var], str(env_file)
    return None, None


def _slack_token(
    environ: Mapping[str, str], home: Path
) -> tuple[str | None, str | None]:
    """``(token, source)`` — env var first, then the first
    ``*/gateways/.env`` under ``home`` and under the ecosystem root."""
    return _slack_setting(environ, home, SLACK_TOKEN_VARS)


def _slack_api(
    url: str,
    token: str,
    payload: dict[str, Any],
    opener: Callable[..., Any],
) -> tuple[dict[str, Any], str | None]:
    """POST ``payload`` to a Slack Web API method.

    Returns ``(data, None)`` when a JSON body came back — ``data["ok"]``
    may still be ``False``, Slack reports API errors that way — or
    ``({}, "<why>")`` on a transport failure, an HTTP status without a
    JSON body, or an unparseable answer."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
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
            data = None
        if not isinstance(data, dict):
            return {}, f"HTTP {exc.code}"
        return data, None
    except (
        urllib.error.URLError,
        OSError,
        http.client.HTTPException,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        return {}, f"unreachable ({exc})"
    return (data if isinstance(data, dict) else {}), None


def _slack_delivery(
    environ: Mapping[str, str],
    home: Path,
    opener: Callable[..., Any],
    token: str,
    source: str,
    team: str,
) -> Finding:
    """``auth.test`` passed — prove the output path with a real
    ``chat.postMessage`` to the diagnostic channel (explicit
    ``SLACK_DOCTOR_CHANNEL``/``SLACK_NOTIFY_CHANNEL``, else a DM opened
    via ``SLACK_NOTIFY_USER``). Single attempt, never retried."""
    channel, _ = _slack_setting(environ, home, SLACK_CHANNEL_VARS)
    if not channel:
        user, user_source = _slack_setting(environ, home, SLACK_USER_VARS)
        if user:
            data, err = _slack_api(
                SLACK_OPEN_URL, token, {"users": user}, opener
            )
            if err is None and data.get("ok") is True:
                dm = data.get("channel")
                if isinstance(dm, dict):
                    channel = dm.get("id")
            if not channel:
                detail = err or data.get("error", "unknown error")
                return Finding(
                    CHECK_ID,
                    Status.FAIL,
                    f"slack: auth ok, delivery failed: {detail} "
                    f"(conversations.open via {user_source})",
                    fix="Check the SLACK_NOTIFY_USER id or set "
                    "SLACK_DOCTOR_CHANNEL to a channel the token can "
                    "post to.",
                )
    if not channel:
        return Finding(
            CHECK_ID,
            Status.WARN,
            f"slack: auth.test ok (team: {team}, token source: {source})"
            " but no delivery channel "
            "(SLACK_DOCTOR_CHANNEL/SLACK_NOTIFY_CHANNEL/"
            "SLACK_NOTIFY_USER env or */gateways/.env) — "
            "chat.postMessage skipped",
        )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data, err = _slack_api(
        SLACK_POST_URL,
        token,
        {"channel": channel, "text": f"devin-doctor live-check {stamp}"},
        opener,
    )
    if err is None and data.get("ok") is True:
        return Finding(
            CHECK_ID,
            Status.PASS,
            f"slack: auth+deliver ok (channel {channel}) — "
            f"team: {team}, token source: {source}",
        )
    detail = err or data.get("error", "unknown error")
    return Finding(
        CHECK_ID,
        Status.FAIL,
        f"slack: auth ok, delivery failed: {detail} "
        f"(channel {channel})",
        fix="Grant the token chat:write and invite it to the channel, "
        "or point SLACK_DOCTOR_CHANNEL at a reachable one.",
    )


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
    data, err = _slack_api(SLACK_AUTH_URL, token, {}, opener)
    if err is not None:
        if err.startswith("HTTP "):
            return Finding(
                CHECK_ID,
                Status.FAIL,
                f"slack: auth.test {err} (token source: {source})",
            )
        return Finding(
            CHECK_ID,
            Status.FAIL,
            f"slack: auth.test {err} — token source: {source}",
        )
    if data.get("ok") is True:
        team = data.get("team") or data.get("team_id") or "?"
        return _slack_delivery(environ, home, opener, token, source, team)
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
