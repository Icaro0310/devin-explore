"""Surface check — copilot-cli: ``session-state`` event logs.

Roots (all existing merged, spec §6.3): ``$COPILOT_DIR``, ``$COPILOT_HOME``,
``~/.copilot``. Layouts that coexist: ``<state>/<uuid>/events.jsonl``
(primary) and ``<state>/<uuid>.jsonl`` (flat), under both ``session-state``
and the legacy ``history-session-state``.

Ladder: present → readable → schema → sane. Only headers/envelopes are
read — event payload content is never inspected.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from devin_doctor.checks.surfaces._common import scan_jsonl
from devin_doctor.model import Context, Finding, Status
from devin_doctor.paths import default_copilot_dirs, human_size

CHECK_ID = "copilot-cli"

_STATE_DIRS = ("session-state", "history-session-state")
# The events.jsonl envelope documented in spec §6.3.
_ENVELOPE_KEYS = frozenset({"id", "parentId", "timestamp", "type", "data"})


def _session_files(roots: Iterable[Path]) -> list[Path]:
    """Every session event log under the copilot roots, deduped."""
    files: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        for state_dir in _STATE_DIRS:
            base = root / state_dir
            if not base.is_dir():
                continue
            try:
                entries = sorted(base.iterdir())
            except OSError:
                continue
            for entry in entries:
                path: Path | None = None
                if entry.is_dir():
                    candidate = entry / "events.jsonl"
                    if candidate.is_file():
                        path = candidate
                elif entry.is_file() and entry.name.endswith(".jsonl"):
                    path = entry
                if path is not None and str(path) not in seen:
                    seen.add(str(path))
                    files.append(path)
    return files


def _envelope_ok(event: Any) -> bool:
    return isinstance(event, dict) and _ENVELOPE_KEYS <= set(event)


def run(ctx: Context) -> list[Finding]:
    del ctx  # surface roots are machine-global — the ctx dirs do not apply
    roots = default_copilot_dirs()
    if not roots:
        return []  # not detected — copilot-cli is not installed here
    files = _session_files(roots)
    if not files:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(roots)} copilot root(s) present, no session logs — "
                "installed but unused",
            )
        ]

    unreadable: list[str] = []
    unknown: list[str] = []
    events = 0
    total = 0
    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            unreadable.append(path.name)
            continue
        total += size
        if size == 0:
            continue  # empty file — the sane rung reports emptiness
        scan = scan_jsonl(path)
        if not scan.ok:
            unreadable.append(path.name)
            continue
        if not _envelope_ok(scan.first):
            unknown.append(path.name)
        events += scan.records

    findings: list[Finding] = []
    if unreadable:
        shown = ", ".join(unreadable[:3]) + (
            " …" if len(unreadable) > 3 else ""
        )
        findings.append(
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"{len(unreadable)}/{len(files)} session log(s) unreadable "
                f"({shown})",
                fix="Store-corrupt — first line is not JSON; the log was "
                "truncated mid-write or damaged.",
            )
        )
    else:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                f"{len(files)} session log(s), {human_size(total)} — "
                "all readable",
            )
        )
    if unknown:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(unknown)} log(s) with unrecognized event envelope "
                "(schema-unknown)",
                fix="Expected {id,parentId,timestamp,type,data} — written "
                "by a newer copilot-cli than the spec knows.",
            )
        )
    if events == 0:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                "0 events across all session logs — installed but unused",
            )
        )
    else:
        findings.append(
            Finding(CHECK_ID, Status.PASS, f"{events} events")
        )
    return findings
