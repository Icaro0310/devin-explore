"""Opt-in check (``--integrations`` / ``--ci``) — "half-configured CI".

For the repository under ``ctx.cwd`` the check resolves ``owner/name``
from ``git remote get-url origin`` and compares two GitHub views through
the ``gh`` CLI:

* ``repos/{slug}/actions/secrets`` — secret *names* only; the API never
  exposes values and neither does this check;
* ``repos/{slug}/contents/.github/workflows`` — workflow file names.

Each row of :data:`RULES` pairs a required secret with the workflow-name
markers that prove the integration is wired. Both present -> ``PASS``,
exactly one -> ``FAIL`` "half-configured", neither -> ``WARN`` (a repo
may legitimately skip the integration) and any ``git``/``gh`` failure ->
``WARN`` "unavailable" — the check never crashes a run.

The check is opt-in: it only runs when its flag is passed, so the
default run and the ``--offline`` pre-commit gate stay hermetic. When
both flags are given, offline wins.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from devin_doctor.model import Context, Finding, Status

CHECK_ID = "integrations"

_GIT_TIMEOUT = 10
_GH_TIMEOUT = 15


@dataclass(frozen=True)
class IntegrationRule:
    """One repo integration: the secret it needs and the workflow-name
    substrings (matched case-insensitively against file names in
    ``.github/workflows``) that prove it is wired. ``label`` is the
    human name used in findings."""

    required_secret: str
    workflow_name_substrings: tuple[str, ...]
    label: str


# New integrations join the table here — one row per secret/workflow
# pair the ecosystem expects to be provisioned together.
RULES: tuple[IntegrationRule, ...] = (
    IntegrationRule(
        required_secret="TS_AUTHKEY",
        workflow_name_substrings=("fallback", "ai-review"),
        label="ai-review fallback",
    ),
)


class _Unavailable(Exception):
    """``git``/``gh`` produced no usable data (absent, error, timeout)."""


class _NotFound(_Unavailable):
    """The API path does not exist (HTTP 404) — counts as 'absent'."""


def _slug_from_remote(url: str) -> str | None:
    """``owner/name`` from a GitHub remote URL (``https://``,
    ``git@``/``ssh://`` forms), or ``None`` for non-GitHub remotes."""
    match = re.search(r"github\.com[:/](?P<rest>.+)$", url.strip())
    if not match:
        return None
    rest = match.group("rest").strip("/")
    rest = rest.removesuffix(".git")
    parts = rest.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return f"{parts[0]}/{parts[1]}"


def _origin_url(cwd: Path, runner: Callable[..., Any]) -> str:
    try:
        result = runner(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(cwd),
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _Unavailable(f"git remote get-url origin failed: {exc}") from exc
    if result.returncode != 0:
        raise _Unavailable("no git remote 'origin' under cwd")
    return (result.stdout or "").strip()


def _gh_api(path: str, runner: Callable[..., Any]) -> Any:
    try:
        result = runner(
            ["gh", "api", path],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _Unavailable(f"gh api failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        first = detail.splitlines()[0] if detail else "gh api error"
        if "404" in detail or "not found" in detail.lower():
            raise _NotFound(first)
        raise _Unavailable(first[:200])
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise _Unavailable(f"gh api returned invalid JSON: {exc}") from exc


def _secret_names(slug: str, runner: Callable[..., Any]) -> set[str]:
    try:
        data = _gh_api(f"repos/{slug}/actions/secrets", runner)
    except _NotFound:
        return set()
    secrets = data.get("secrets", []) if isinstance(data, dict) else []
    return {
        s["name"]
        for s in secrets
        if isinstance(s, dict) and isinstance(s.get("name"), str)
    }


def _workflow_names(slug: str, runner: Callable[..., Any]) -> list[str]:
    try:
        data = _gh_api(f"repos/{slug}/contents/.github/workflows", runner)
    except _NotFound:
        return []
    if not isinstance(data, list):
        return []
    return [
        e["name"]
        for e in data
        if isinstance(e, dict) and isinstance(e.get("name"), str)
    ]


def _rule_finding(
    rule: IntegrationRule, secrets: set[str], workflows: list[str]
) -> Finding:
    has_secret = rule.required_secret in secrets
    markers = tuple(m.lower() for m in rule.workflow_name_substrings)
    matched = [w for w in workflows if any(m in w.lower() for m in markers)]
    marker_list = "/".join(rule.workflow_name_substrings)

    if has_secret and matched:
        return Finding(
            CHECK_ID,
            Status.PASS,
            f"{rule.label}: secret '{rule.required_secret}' and workflow "
            f"'{matched[0]}' both present",
        )
    if has_secret:
        return Finding(
            CHECK_ID,
            Status.FAIL,
            f"{rule.label} half-configured: secret "
            f"'{rule.required_secret}' exists but no workflow name "
            f"contains {marker_list}",
            fix=f"Add a '.github/workflows/*{rule.workflow_name_substrings[0]}*' "
            "workflow that uses the secret — or delete the unused secret.",
        )
    if matched:
        return Finding(
            CHECK_ID,
            Status.FAIL,
            f"{rule.label} half-configured: workflow '{matched[0]}' "
            f"exists but secret '{rule.required_secret}' is missing",
            fix=f"Provision the '{rule.required_secret}' repo secret "
            "(Settings -> Secrets and variables -> Actions) or remove "
            "the workflow.",
        )
    return Finding(
        CHECK_ID,
        Status.WARN,
        f"{rule.label} absent — review fallback absent (no "
        f"'{rule.required_secret}' secret, no matching workflow)",
        fix="Some repos legitimately skip this integration; if it should "
        "be wired, provision both the secret and the workflow.",
    )


def run(
    ctx: Context,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> list[Finding]:
    if ctx.offline or os.environ.get("DEVIN_DOCTOR_OFFLINE") == "1":
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                "integrations check unavailable (offline mode)",
            )
        ]
    try:
        url = _origin_url(ctx.cwd, runner)
        slug = _slug_from_remote(url)
        if slug is None:
            return [
                Finding(
                    CHECK_ID,
                    Status.WARN,
                    "integrations check skipped — 'origin' is not a "
                    "github.com remote",
                )
            ]
        secrets = _secret_names(slug, runner)
        workflows = _workflow_names(slug, runner)
    except _Unavailable as exc:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                f"integrations check unavailable ({exc})",
            )
        ]
    return [_rule_finding(rule, secrets, workflows) for rule in RULES]
