"""Opt-in check (``--prs``) — "PR ready but not merged" detector.

Scans every github.com repository in the ecosystem registry
(``devin-powerups/registry.json``, located via
``devin_doctor.ecosystem.locate_registry``) for open pull requests that
are *done* but stalled: approved, every check terminal-green, older
than :data:`_MIN_AGE_MINUTES` — yet ``autoMergeRequest`` is ``null``.
That is exactly the personal-agent-system#141 failure mode: green
checks + an approval and nobody merged, because nothing watched for
the missing auto-merge arm.

"Approved" follows the ecosystem's own definition: ``reviewDecision
APPROVED`` (a submitted approving review) **or** the
``devin-judge/review`` commit status at ``SUCCESS`` — the laptop-side
watcher posts that status when the judge approves, and PRs routinely
sit at ``reviewDecision: ""`` with the judge already green.

"Checks green" reads ``statusCheckRollup`` after collapsing duplicate
entries to the latest run per check name / status context (the
``pull_request_target`` re-triggers leave dozens of ``CANCELLED``
check runs behind — concurrency flakes, not signal). A PR counts as
green when nothing is pending, nothing failed, and at least one real
``SUCCESS``/``SKIPPED``/``NEUTRAL`` conclusion exists; ``CANCELLED``
never blocks.

Opt-in by flag like ``integrations``/``live``: it needs the ``gh`` CLI
and GitHub API access, so the default run and the ``--offline`` gate
stay hermetic. Repos that 404 (absorbed products whose GitHub repo is
gone) are skipped silently; other per-repo ``gh`` failures are
reported as an incomplete scan, never a crash.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from devin_doctor import ecosystem
from devin_doctor.model import Context, Finding, Status

CHECK_ID = "pr-ready-not-merged"

_GH_TIMEOUT = 20
_MIN_AGE_MINUTES = 30
# The commit-status context the laptop-side judge watcher posts — the
# ecosystem's approval signal when no formal review exists yet.
_REVIEW_CONTEXT = "devin-judge/review"

# Rollup conclusions/states that prove a check passed. CANCELLED is a
# concurrency flake (self-cancelled re-triggers) and blocks nothing.
_GREEN_CONCLUSIONS = frozenset({"SUCCESS", "SKIPPED", "NEUTRAL"})
_BLOCKING_CONCLUSIONS = frozenset(
    {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
)
_BLOCKING_STATES = frozenset({"FAILURE", "ERROR"})
_PENDING_STATES = frozenset({"PENDING", "EXPECTED"})

_UNAVAILABLE_HINTS = ("404", "could not resolve", "not found")


def _gh_pr_list(
    slug: str, runner: Callable[..., Any]
) -> tuple[list[dict[str, Any]], None] | tuple[None, str]:
    """Open PRs for ``slug`` via ``gh pr list`` — ``(prs, None)`` or
    ``(None, error)``. The error string feeds :func:`_is_gone`."""
    try:
        result = runner(
            [
                "gh", "pr", "list", "-R", slug,
                "--state", "open", "--limit", "50",
                "--json",
                (
                    "number,title,reviewDecision,statusCheckRollup,"
                    "autoMergeRequest,createdAt,url"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"gh pr list failed: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        first = detail.splitlines()[0] if detail else "gh pr list error"
        return None, first[:200]
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        return None, f"gh pr list returned invalid JSON: {exc}"
    if not isinstance(data, list):
        return None, "gh pr list returned a non-list payload"
    return [p for p in data if isinstance(p, dict)], None


def _is_gone(error: str) -> bool:
    """The repo itself is gone (absorbed product, deleted repo) — a
    404-style answer is expected churn, not an unavailable scan."""
    lowered = error.lower()
    return any(hint in lowered for hint in _UNAVAILABLE_HINTS)


def _latest_checks(pr: dict[str, Any]) -> list[dict[str, Any]]:
    """One entry per check name / status context — the newest run wins.

    ``statusCheckRollup`` lists *every* run on the head commit, so the
    ``pull_request_target`` re-trigger storms leave the same check
    repeated as CANCELLED/SUCCESS/FAILURE across attempts. ISO-8601
    timestamps compare correctly as strings."""
    latest: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
    for entry in pr.get("statusCheckRollup") or []:
        if not isinstance(entry, dict):
            continue
        typename = entry.get("__typename")
        if typename == "CheckRun":
            key = ("check", str(entry.get("name") or ""))
            ts = str(
                entry.get("completedAt") or entry.get("startedAt") or ""
            )
        elif typename == "StatusContext":
            key = ("status", str(entry.get("context") or ""))
            ts = str(entry.get("startedAt") or "")
        else:
            continue
        if not key[1]:
            continue
        current = latest.get(key)
        if current is None or ts >= current[0]:
            latest[key] = (ts, entry)
    return [entry for _, entry in latest.values()]


def _checks_green(entries: list[dict[str, Any]]) -> bool:
    """Every latest check terminal and non-failing, with at least one
    real green conclusion — an all-CANCELLED rollup proves nothing."""
    green = 0
    for entry in entries:
        if entry.get("__typename") == "CheckRun":
            if entry.get("status") != "COMPLETED":
                return False
            conclusion = entry.get("conclusion")
            if conclusion in _BLOCKING_CONCLUSIONS:
                return False
            if conclusion in _GREEN_CONCLUSIONS:
                green += 1
        elif entry.get("__typename") == "StatusContext":
            state = entry.get("state")
            if state in _PENDING_STATES or state in _BLOCKING_STATES:
                return False
            if state == "SUCCESS":
                green += 1
    return green >= 1


def _approved(pr: dict[str, Any], entries: list[dict[str, Any]]) -> bool:
    """Ecosystem approval: a submitted APPROVED review, or the
    ``devin-judge/review`` commit status at SUCCESS (the watcher posts
    it without a review object, leaving ``reviewDecision`` empty)."""
    if pr.get("reviewDecision") == "APPROVED":
        return True
    return any(
        e.get("__typename") == "StatusContext"
        and e.get("context") == _REVIEW_CONTEXT
        and e.get("state") == "SUCCESS"
        for e in entries
    )


def _parse_ts(value: Any) -> float | None:
    """ISO-8601 → epoch seconds; ``None`` when absent/unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _age_str(minutes: float) -> str:
    if minutes >= 60 * 24:
        return f"{minutes / (60 * 24):.1f}d"
    if minutes >= 60:
        return f"{minutes / 60:.1f}h"
    return f"{minutes:.0f}m"


def _ready_unmerged(
    pr: dict[str, Any], entries: list[dict[str, Any]], now_ms: int
) -> tuple[bool, str]:
    """``(flagged, age)`` — approved + green + no auto-merge + older
    than the grace window. The age grace keeps CI still settling from
    flagging; auto-merge armed means the pipeline is already moving."""
    if pr.get("autoMergeRequest"):
        return False, ""
    if not _approved(pr, entries):
        return False, ""
    if not _checks_green(entries):
        return False, ""
    created = _parse_ts(pr.get("createdAt"))
    if created is None:
        return False, ""
    age_min = (now_ms / 1000 - created) / 60
    if age_min <= _MIN_AGE_MINUTES:
        return False, ""
    return True, _age_str(age_min)


def _flag_finding(slug: str, pr: dict[str, Any], age: str) -> Finding:
    number = pr.get("number", "?")
    title = str(pr.get("title") or "")
    if len(title) > 60:
        title = title[:57] + "..."
    url = pr.get("url") or f"https://github.com/{slug}/pull/{number}"
    return Finding(
        CHECK_ID,
        Status.WARN,
        f"{slug}#{number} '{title}' — approved, checks green, no "
        f"auto-merge, open {age}: {url}",
        fix="Merge it or arm auto-merge before it rots: "
        f"`gh pr merge {number} -R {slug}` (add --auto to arm "
        "auto-merge instead).",
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
                "pr-ready scan unavailable (offline mode)",
            )
        ]
    registry_path = ecosystem.locate_registry(ctx, Path.home())
    if registry_path is None:
        return [
            Finding(
                CHECK_ID,
                Status.PASS,
                "pr-ready scan skipped (registry not found)",
            )
        ]
    slugs = ecosystem.registry_slugs(registry_path)
    if not slugs:
        return [
            Finding(
                CHECK_ID,
                Status.PASS,
                "pr-ready scan skipped (no github.com repos in registry)",
            )
        ]

    findings: list[Finding] = []
    flagged: list[tuple[str, dict[str, Any], str]] = []
    scanned = gone = unavailable = open_prs = 0
    unavailable_slugs: list[str] = []
    first_error = ""
    for slug in slugs:
        prs, error = _gh_pr_list(slug, runner)
        if error is not None:
            if _is_gone(error):
                gone += 1
            else:
                unavailable += 1
                unavailable_slugs.append(slug)
                first_error = first_error or f"{slug}: {error}"
            continue
        scanned += 1
        for pr in prs:
            open_prs += 1
            entries = _latest_checks(pr)
            hit, age = _ready_unmerged(pr, entries, ctx.now())
            if hit:
                flagged.append((slug, pr, age))

    if scanned == 0:
        if unavailable:
            return [
                Finding(
                    CHECK_ID,
                    Status.WARN,
                    f"pr-ready scan unavailable — gh pr list failed "
                    f"for all {unavailable} repo(s) "
                    f"({', '.join(unavailable_slugs[:5])}"
                    f"{', …' if unavailable > 5 else ''}): "
                    f"{first_error}",
                    fix="Check `gh auth status` — the scan needs a "
                    "token that can read the ecosystem repos.",
                )
            ]
        return [
            Finding(
                CHECK_ID,
                Status.PASS,
                f"pr-ready scan: {gone} repo(s) unreachable (gone), "
                "nothing to scan",
            )
        ]

    extras = ""
    if gone or unavailable:
        parts = []
        if gone:
            parts.append(f"{gone} gone")
        if unavailable:
            parts.append(f"{unavailable} unavailable")
        extras = f" ({', '.join(parts)})"
    findings.append(
        Finding(
            CHECK_ID,
            Status.PASS,
            f"pr-ready scan: {scanned} repos, {open_prs} open PRs, "
            f"{len(flagged)} ready-but-unmerged{extras}",
        )
    )
    if unavailable:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"pr-ready scan incomplete: {unavailable} repo(s) "
                f"unavailable ({', '.join(unavailable_slugs[:5])}"
                f"{', …' if unavailable > 5 else ''}) — "
                f"flagged PRs may be undercounted. First error: "
                f"{first_error}",
                fix="Re-run with `gh auth status` clean — a repo that "
                "keeps failing hides its stalled PRs.",
            )
        )
    findings += [_flag_finding(slug, pr, age) for slug, pr, age in flagged]
    return findings
