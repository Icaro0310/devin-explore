"""Runner + renderers for devin-doctor reports.

``run_all`` executes every registered check (a crashing check degrades to a
FAIL finding instead of aborting the run). Renderers produce the three
output shapes: text lines, the ``--json`` contract, and a markdown report.

The report has two sections: 'surfaces' — one block per agent session
surface that ran, undetected surfaces collapsed to a 'not detected' line —
and 'devin environment' for the checks that diagnose the local Devin
install itself.

An undetected surface still emits its canonical ``present PASS /
not-installed`` finding (SURFACE-HEALTH §3.1) — detection is read from
the ``note`` field, not from an empty finding list.
"""

from __future__ import annotations

import json
import platform
from collections.abc import Sequence
from datetime import datetime, timezone

import devin_doctor
from devin_doctor.checks import DEVIN_CHECKS, SURFACES
from devin_doctor.model import Context, Finding, Report, Status, platform_name


def run_all(ctx: Context, *, surfaces: Sequence[str] | None = None) -> Report:
    """Run every check; ``surfaces`` scopes the surface checks by name.

    The Devin-environment checks always run unscoped — ``--surface``
    narrows only the agent-surface ladder. Unknown surface names raise
    ``ValueError`` (the CLI's argparse ``choices`` catches them first;
    the MCP tool surfaces it as an ``{error, detail}`` reply).
    """
    known = {m.CHECK_ID for m in SURFACES}
    if surfaces is not None:
        unknown = [name for name in surfaces if name not in known]
        if unknown:
            raise ValueError(
                f"unknown surface(s): {', '.join(unknown)} — known: "
                + ", ".join(m.CHECK_ID for m in SURFACES)
            )
        selected = [m for m in SURFACES if m.CHECK_ID in surfaces]
    else:
        selected = list(SURFACES)
    report = Report(surfaces_checked=[m.CHECK_ID for m in selected])
    for check in [*selected, *DEVIN_CHECKS]:
        try:
            report.findings += check.run(ctx)
        except Exception as exc:  # noqa: BLE001 — a doctor must not die
            report.findings.append(
                Finding(
                    check.CHECK_ID,
                    Status.FAIL,
                    f"check crashed: {exc!r}",
                    fix="Report a bug at "
                    "https://github.com/Icaro0310/devin-explore/issues",
                )
            )
    return report


def exit_code(report: Report) -> int:
    return 1 if report.overall is Status.FAIL else 0


def _finding_lines(report: Report, check_id: str, indent: str) -> list[str]:
    lines: list[str] = []
    for f in report.findings:
        if f.check != check_id:
            continue
        lines.append(f"{indent}{f.status.value:<4}  {f.message}")
        if f.fix and f.status is not Status.PASS:
            lines.append(f"{indent}      fix: {f.fix}")
    return lines


def _surface_detected(report: Report, name: str) -> bool:
    """``True`` when the surface emitted anything beyond the canonical
    ``not-installed`` present finding (SURFACE-HEALTH §3.1)."""
    return any(
        f.check == name and f.note != "not-installed"
        for f in report.findings
    )


def render_text(report: Report) -> str:
    lines: list[str] = []
    check_status = report.check_status()
    if report.surfaces_checked:
        surface_set = set(report.surfaces_checked)
        lines.append("surfaces")
        missing: list[str] = []
        for name in report.surfaces_checked:
            if not _surface_detected(report, name):
                missing.append(name)
                continue
            lines.append(f"  {name} — {check_status[name]}")
            lines += _finding_lines(report, name, "    ")
        if missing:
            lines.append(f"  not detected: {', '.join(missing)}")
        lines.append("")
        lines.append("devin environment")
        for check_id, status in check_status.items():
            if check_id in surface_set:
                continue
            lines.append(f"  {check_id} — {status}")
            lines += _finding_lines(report, check_id, "    ")
        absent = [
            c.CHECK_ID for c in DEVIN_CHECKS if c.CHECK_ID not in check_status
        ]
        if absent:
            lines.append(f"  not detected: {', '.join(absent)}")
    else:
        # Legacy flat rendering — no surface check ran.
        for check_id, status in check_status.items():
            lines.append(f"{check_id} — {status}")
            lines += _finding_lines(report, check_id, "  ")
    counts = report.counts()
    lines.append("")
    lines.append(
        f"overall: {report.overall.value} "
        f"({counts['pass']} PASS, {counts['warn']} WARN, {counts['fail']} FAIL)"
    )
    return "\n".join(lines)


def render_json(report: Report, ctx: Context) -> str:
    check_status = report.check_status()
    payload = {
        "tool": "devin-doctor",
        "version": devin_doctor.__version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": platform_name(),
        "data_dir": str(ctx.data_dir),
        "cwd": str(ctx.cwd),
        "overall": report.overall.value,
        "summary": report.counts(),
        "checks": check_status,
        "findings": [f.as_dict() for f in report.findings],
    }
    if report.surfaces_checked:
        payload["surfaces"] = {
            "detected": {
                name: check_status[name]
                for name in report.surfaces_checked
                if _surface_detected(report, name)
            },
            "not_detected": [
                name
                for name in report.surfaces_checked
                if not _surface_detected(report, name)
            ],
        }
    return json.dumps(payload, indent=2)


def render_markdown(report: Report, ctx: Context) -> str:
    counts = report.counts()
    check_status = report.check_status()
    lines = [
        "# devin-doctor report",
        "",
        f"- generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- devin-doctor: {devin_doctor.__version__}",
        f"- platform: {platform_name()} ({platform.python_version()})",
        f"- data dir: `{ctx.data_dir}`",
        f"- project dir: `{ctx.cwd}`",
        (f"- **overall: {report.overall.value}** "
         f"({counts['pass']} PASS / {counts['warn']} WARN / {counts['fail']} FAIL)"),
    ]
    if report.surfaces_checked:
        lines += ["", "## surfaces", ""]
        missing = []
        for name in report.surfaces_checked:
            if _surface_detected(report, name):
                lines.append(f"- **{name}** — {check_status[name]}")
            else:
                missing.append(name)
        if missing:
            lines.append(f"- not detected: {', '.join(missing)}")
    lines += [
        "",
        "| check | status | finding |",
        "|---|---|---|",
    ]
    for f in report.findings:
        msg = f.message.replace("|", "\\|")
        lines.append(f"| {f.check} | {f.status.value} | {msg} |")
    fixes = [f for f in report.findings if f.fix and f.status is not Status.PASS]
    if fixes:
        lines += ["", "## Suggested fixes", ""]
        lines += [f"- **{f.check}**: {f.fix}" for f in fixes]
    return "\n".join(lines)


def render_plan(report: Report) -> str:
    """DR-1: a remediation plan — suggestions only, nothing is executed.

    The original ``--fix`` idea became ``plan`` so devin-doctor stays
    read-only: it emits the steps a human (or another tool) may run.
    """
    lines = [
        "# devin-doctor remediation plan",
        "# read-only output — nothing below was executed",
        "",
    ]
    actionable = [
        f for f in report.findings
        if f.status in (Status.WARN, Status.FAIL)
    ]
    if not actionable:
        lines.append("# all checks passed — nothing to do")
        return "\n".join(lines)
    n = 0
    for f in actionable:
        lines.append(f"## [{f.status.value}] {f.check}: {f.message}")
        if f.fix:
            n += 1
            for line in f.fix.strip().splitlines():
                lines.append(f"{n}. {line.strip()}")
        lines.append("")
    return "\n".join(lines)


def plan_steps(report: Report) -> list[dict[str, object]]:
    """Machine-readable remediation steps for ``plan --json``."""
    steps = []
    for f in report.findings:
        if f.status in (Status.WARN, Status.FAIL):
            steps.append({
                "check": f.check,
                "status": f.status.value,
                "finding": f.message,
                "fix": f.fix,
            })
    return steps
