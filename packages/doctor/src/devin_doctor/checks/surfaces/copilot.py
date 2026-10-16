"""Surface check — copilot-cli: ``session-state`` event logs.

Roots (all existing merged, spec §6.3): ``$COPILOT_DIR``, ``$COPILOT_HOME``,
``~/.copilot``. Layouts that coexist: ``<state>/<uuid>/events.jsonl``
(primary) and ``<state>/<uuid>.jsonl`` (flat), under both ``session-state``
and the legacy ``history-session-state``.

Ladder (SURFACE-HEALTH §4.3): present → readable → schema → sane, findings
tagged with their rung + §5 code. A root without a ``session-state`` /
``history-session-state`` container is ``store-missing``; a container
without session artifacts is ``store-empty``. Only headers/envelopes are
read — event payload content is never inspected.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from devin_doctor.checks.surfaces._common import JsonlScan, scan_jsonl
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
        return [
            Finding(
                CHECK_ID,
                Status.PASS,
                "no copilot roots — copilot-cli not installed",
                rung="present",
                note="not-installed",
            )
        ]
    containers = [
        root / state_dir
        for root in roots
        for state_dir in _STATE_DIRS
        if (root / state_dir).is_dir()
    ]
    if not containers:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(roots)} copilot root(s) present, no session-state "
                "container",
                fix="The root holds config but no session store — the CLI "
                "never wrote a session here.",
                rung="present",
                code="store-missing",
            )
        ]
    files = _session_files(roots)
    findings = [
        Finding(
            CHECK_ID,
            Status.PASS,
            f"{len(containers)} session container(s) — "
            f"{len(files)} session log(s)",
            rung="present",
        )
    ]
    if not files:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                "session containers present, 0 session logs — installed "
                "but unused",
                rung="sane",
                code="store-empty",
            )
        )
        return findings

    scans: dict[Path, JsonlScan] = {}
    empty: list[str] = []
    unreadable: list[str] = []
    total = 0
    for path in files:
        try:
            size = path.stat().st_size
        except OSError:
            unreadable.append(path.name)
            continue
        total += size
        if size == 0:
            empty.append(path.name)
            continue
        scan = scan_jsonl(path)
        if not scan.ok:
            unreadable.append(path.name)
            continue
        scans[path] = scan

    if unreadable:
        shown = ", ".join(unreadable[:3]) + (
            " …" if len(unreadable) > 3 else ""
        )
        findings.append(
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"{len(unreadable)}/{len(files)} session log(s) "
                f"unreadable ({shown})",
                fix="Store-corrupt — a line is not JSON; the log was "
                "truncated mid-write or damaged.",
                rung="readable",
                code="store-corrupt",
            )
        )
        return findings
    if not scans:
        # every artifact is a zero-byte shell — emptiness is sane's call
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(empty)} zero-byte session log(s) — empty-store",
                fix="Never-written session files — safe to delete or "
                "ignore.",
                rung="sane",
                code="empty-store",
            )
        )
        return findings
    findings.append(
        Finding(
            CHECK_ID,
            Status.PASS,
            f"{len(scans)} session log(s), {human_size(total)} — all "
            "readable",
            rung="readable",
        )
    )

    unknown = [p.name for p, s in scans.items() if not _envelope_ok(s.first)]
    if unknown:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(unknown)} log(s) with unrecognized event envelope "
                "(schema-unknown)",
                fix="Expected {id,parentId,timestamp,type,data} — written "
                "by a newer copilot-cli than the spec knows.",
                rung="schema",
                code="schema-unknown",
            )
        )
    else:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                "all session logs carry the documented event envelope",
                rung="schema",
            )
        )

    events = sum(s.records for s in scans.values())
    if empty:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(empty)} zero-byte session log(s) — empty-store",
                fix="Never-written session files — safe to delete or "
                "ignore.",
                rung="sane",
                code="empty-store",
            )
        )
    elif events == 0:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                "0 events across all session logs — installed but unused",
                rung="sane",
                code="store-empty",
            )
        )
    else:
        findings.append(
            Finding(CHECK_ID, Status.PASS, f"{events} events", rung="sane")
        )
    return findings
