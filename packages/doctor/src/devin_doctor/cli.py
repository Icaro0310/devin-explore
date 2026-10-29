"""Thin CLI wrapper — all logic lives in the library modules.

- ``devin-doctor check [--data-dir P] [--cwd P] [--stale-days N] [--offline] [--surface NAME] [--json]``
- ``devin-doctor report [--data-dir P] [--cwd P] [--stale-days N] [--offline] [--surface NAME] [--md|--json]``
- ``devin-doctor capabilities [--config-dir P] [--probe-network]``

``--surface NAME`` (check/report/plan) scopes the agent-surface checks to
one surface — the Devin-environment checks always run unscoped.

``--offline`` (check/report/plan) pins ``DEVIN_DOCTOR_OFFLINE=1`` for the
run, so the updates check skips its manifest fetch and nothing touches
the network — the mode the repo's pre-commit gate runs in.

``--integrations``/``--ci`` and ``--live`` (check/report/plan) opt into
the checks that need more than the local stores: the integrations check
uses ``git`` + the ``gh`` CLI against the GitHub API, and the live check
authenticates against Slack/Ollama endpoints.

Exit code: 0 when no FAIL findings, 1 when any check FAILs.
``capabilities`` always exits 0 — its output is a JSON profile, not a
verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from devin_doctor import capabilities, doctor
from devin_doctor.checks.surfaces import SURFACE_NAMES
from devin_doctor.model import Context
from devin_doctor.paths import (
    default_config_dir,
    default_data_dir,
    default_state_dir,
)


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Devin data dir (default: platform location — "
        "%%APPDATA%%/devin on Windows, ~/.config/devin elsewhere)",
    )
    p.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Devin UI config dir (default: platform location)",
    )
    p.add_argument(
        "--cwd",
        type=Path,
        default=Path.cwd(),
        help="project dir whose .devin/ config to check (default: cwd)",
    )
    p.add_argument(
        "--stale-days",
        type=int,
        default=30,
        metavar="N",
        help="flag sessions inactive for more than N days (default: 30)",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="skip every check that needs the network — equivalent to "
        "DEVIN_DOCTOR_OFFLINE=1 (the updates check self-skips when the "
        "registry is unreachable either way)",
    )
    p.add_argument(
        "--surface",
        action="append",
        choices=SURFACE_NAMES,
        metavar="NAME",
        help="scope the agent-surface checks to NAME (repeatable; one of "
        + ", ".join(SURFACE_NAMES)
        + "). Devin-environment checks always run unscoped.",
    )
    p.add_argument(
        "--package",
        action="append",
        metavar="NAME",
        help="scope the publish-readiness check to package NAME "
        "(repeatable; matches the packages/NAME dir or the project "
        "name in its pyproject). Pointing --cwd at a single package "
        "dir scopes the same way.",
    )
    p.add_argument(
        "--integrations",
        "--ci",
        action="store_true",
        help="opt-in: check the repo under --cwd for half-configured "
        "integrations (provisioned secret but no matching workflow, or "
        "vice-versa) via git + the gh CLI against the GitHub API",
    )
    p.add_argument(
        "--live",
        action="store_true",
        help="opt-in: run live connectivity probes — Slack auth.test plus "
        "a chat.postMessage delivery smoke, and Ollama /api/tags, each "
        "bounded to ~8s (proves the channel, not just the key)",
    )
    p.add_argument("--json", action="store_true", help="emit JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devin-doctor",
        description="Diagnose a Devin Desktop installation: stores, schema "
        "versions, data health, config sanity, disk usage. Read-only, always.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="run all checks and print findings")
    _add_common(check)

    report = sub.add_parser(
        "report", help="same findings, formatted for pasting into issues"
    )
    _add_common(report)
    report.add_argument(
        "--md", action="store_true", help="emit markdown (for GitHub issues)"
    )

    plan = sub.add_parser(
        "plan", help="emit a remediation plan for WARN/FAIL findings — "
        "suggestions only, nothing is executed (doctor stays read-only)")
    _add_common(plan)

    cap = sub.add_parser(
        "capabilities",
        help="print the machine capability profile as JSON — read-only, "
        "local probing only (corporate profile is fail-closed)",
    )
    cap.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Devin UI config dir where devin-profile.json is read "
        "(default: platform location)",
    )
    cap.add_argument(
        "--probe-network",
        action="store_true",
        help="NETWORK ACCESS: perform exactly ONE outbound TCP connect "
        "(1.1.1.1:443, or the configured HTTP(S)_PROXY, 2s timeout) plus a "
        "loopback bind test. Without this flag net.* stay 'unknown' and "
        "no network call is made.",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if getattr(args, "offline", False):
        os.environ["DEVIN_DOCTOR_OFFLINE"] = "1"
    if args.command == "capabilities":
        if args.probe_network:
            print(
                "devin-doctor: --probe-network will make ONE outbound TCP "
                "connect (2s timeout) — the only network call this tool "
                "can perform",
                file=sys.stderr,
            )
        profile = capabilities.collect_profile(
            config_dir=args.config_dir or default_config_dir(),
            probe_network=args.probe_network,
        )
        print(capabilities.render_profile(profile))
        return 0
    ctx = Context(
        data_dir=args.data_dir or default_data_dir(),
        cwd=args.cwd,
        stale_days=args.stale_days,
        state_dir=default_state_dir(),
        config_dir=(
            args.config_dir
            if args.config_dir is not None
            else default_config_dir() if args.data_dir is None else None
        ),
        packages=tuple(args.package or ()),
    )
    optional = [
        name
        for name, enabled in (
            ("integrations", getattr(args, "integrations", False)),
            ("live", getattr(args, "live", False)),
        )
        if enabled
    ]
    report = doctor.run_all(
        ctx, surfaces=args.surface, optional=optional or None
    )
    if args.command == "plan":
        if args.json:
            print(json.dumps(
                {"overall": report.overall.value,
                 "steps": doctor.plan_steps(report)}, indent=2))
        else:
            print(doctor.render_plan(report))
        return 0  # a plan is informational — not a verdict
    if args.json:
        print(doctor.render_json(report, ctx))
    elif args.command == "report" and args.md:
        print(doctor.render_markdown(report, ctx))
    else:
        print(doctor.render_text(report))
    return doctor.exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
