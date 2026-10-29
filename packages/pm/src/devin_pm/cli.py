"""``devin-pm`` — thin CLI wrapper; all logic lives in the library.

Subcommands (all read-only against ``sessions.db``):

- ``status``      per-project rollup table (``--json`` for machines)
- ``report``      markdown report for ``--project`` or every project
- ``milestones``  milestone list + done % for ``--project``
- ``registry``    emit machine-readable ``registry.json``
- ``verify``      cross-check tracked projects against the ecosystem
  hub registry (``devin-powerups/registry.json``)
- ``digest``      weekly-ish markdown digest of activity since a
  watermark (``--since`` epoch/ISO, ``--incremental`` via the shared
  ``pm-digest`` cursor, ``--stdout`` or a ``pm-digest-YYYY-Www.md``
  file)

Every subcommand takes ``--vscdb [PATH]`` (PM-1): GUI session→workspace
bindings from ``state.vscdb`` are merged into the grouping, marked as
``gui``-sourced in output.

Exit codes: 0 ok · 1 read/parse error (``verify``: drift found) ·
2 missing db / unknown project / missing inputs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path

from devin_internals.parsers import Session, SessionsStore
from devin_internals.schema import SchemaError

from devin_pm.digest import (
    CURSOR_NAME,
    SLACK_SAFE_LIMIT,
    parse_since,
    render_slack_digest,
    run_digest,
)
from devin_pm.milestones import (
    MilestonesError,
    collect_milestones,
    done_fraction,
)
from devin_pm.projects import (
    Project,
    default_sessions_db,
    find_project,
    group_sessions,
)
from devin_pm.registry import build_registry, write_registry
from devin_pm.report import (
    ms_to_iso,
    render_global_report,
    render_project_report,
    render_status_table,
)
from devin_pm.verify import (
    RegistryError,
    add_fix_hints,
    default_registry,
    projects_from_pm_registry,
    verify,
)
from devin_pm.verify import (
    render_text as render_verify_text,
)
from devin_pm.vscdb import (
    default_state_vscdb,
    load_gui_sessions,
)


class _CliError(RuntimeError):
    """Expected CLI failure; ``exit_code`` goes to the process."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        self.exit_code = exit_code
        super().__init__(message)


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2))


def _resolve_db(args: argparse.Namespace) -> Path:
    path = (
        Path(args.sessions_db).expanduser()
        if args.sessions_db
        else default_sessions_db()
    )
    if not path.is_file():
        raise _CliError(
            f"{path}: no sessions.db there — pass --sessions-db or set "
            "DEVIN_PM_SESSIONS_DB",
            exit_code=2,
        )
    return path


def _resolve_vscdb(args: argparse.Namespace) -> Path | None:
    """``None`` → CLI sessions only; a Path merges GUI sessions in (PM-1).

    ``--vscdb`` absent → ``None``; bare ``--vscdb`` → auto-detect (missing
    store warns and continues CLI-only); ``--vscdb PATH`` → that file.
    """
    val = getattr(args, "vscdb", None)
    if val is None:
        return None
    if val == "auto":
        found = default_state_vscdb()
        if found is None or not found.is_file():
            print(
                "warning: no state.vscdb found — continuing with "
                "sessions.db only",
                file=sys.stderr,
            )
            return None
        return found
    path = Path(val).expanduser()
    if not path.is_file():
        raise _CliError(
            f"{path}: no state.vscdb there — pass --vscdb PATH or set "
            "DEVIN_PM_STATE_VSCDB",
            exit_code=2,
        )
    return path


def _load(db_path: Path) -> tuple[list[Session], int]:
    with SessionsStore(db_path) as store:
        return store.sessions(), store.schema_info["schema_version"]


def _projects(
    db_path: Path, vscdb_path: Path | None = None
) -> tuple[list[Project], int]:
    sessions, version = _load(db_path)
    if vscdb_path is not None:
        sessions = [*sessions, *load_gui_sessions(vscdb_path)]
    return group_sessions(sessions), version


def _require_project(projects: list[Project], name: str) -> Project:
    project = find_project(projects, name)
    if project is None:
        known = ", ".join(p.name for p in projects) or "(none)"
        raise _CliError(
            f"unknown project '{name}' — known projects: {known}",
            exit_code=2,
        )
    return project


def _project_json(project: Project) -> dict:
    return {
        "name": project.name,
        "working_directory": project.working_directory,
        "sessions": project.session_count,
        "gui_sessions": project.gui_session_count,
        "status": project.status_counts,
        "last_activity": ms_to_iso(project.last_activity_at),
        "cost": project.cost,
    }


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    projects, _ = _projects(_resolve_db(args), _resolve_vscdb(args))
    if args.json:
        _print_json([_project_json(p) for p in projects])
    else:
        print(render_status_table(projects))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    projects, _ = _projects(_resolve_db(args), _resolve_vscdb(args))
    if args.project:
        project = _require_project(projects, args.project)
        text = render_project_report(project, collect_milestones(project))
    else:
        text = render_global_report(
            projects, {p.name: collect_milestones(p) for p in projects}
        )
    if args.out:
        out = Path(args.out)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        print(text, end="")
    return 0


def _milestones_json(project: Project) -> dict:
    ms = collect_milestones(project)
    done, total, pct = done_fraction(ms)
    return {
        "project": project.name,
        "milestones": [
            {
                "name": m.name,
                "done": m.done,
                "session_id": m.session_id,
                "source": m.source,
            }
            for m in ms
        ],
        "done": done,
        "total": total,
        "percent": round(pct, 1),
    }


def cmd_milestones(args: argparse.Namespace) -> int:
    projects, _ = _projects(_resolve_db(args), _resolve_vscdb(args))
    project = _require_project(projects, args.project)
    if args.json:
        _print_json(_milestones_json(project))
        return 0
    ms = collect_milestones(project)
    done, total, pct = done_fraction(ms)
    print(f"Project: {project.name}")
    if not ms:
        print("(no milestones — tag a session title `milestone: <name>` or "
              "add milestones.json to the project root)")
        return 0
    for m in ms:
        where = f" (session {m.session_id})" if m.session_id else ""
        print(f"- [{'x' if m.done else ' '}] {m.name}{where}")
    print(f"Done: {done}/{total} ({pct:.0f}%)")
    return 0


def cmd_registry(args: argparse.Namespace) -> int:
    db_path = _resolve_db(args)
    vscdb_path = _resolve_vscdb(args)
    projects, version = _projects(db_path, vscdb_path)
    registry = build_registry(
        projects,
        milestones={p.name: collect_milestones(p) for p in projects},
        sessions_db=db_path,
        state_vscdb=vscdb_path,
        schema_version=version,
    )
    if args.out:
        print(f"wrote {write_registry(registry, args.out)}")
    else:
        _print_json(registry)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    if args.registry:
        registry_path = Path(args.registry).expanduser()
    else:
        found = default_registry()
        if found is None:
            raise _CliError(
                "hub registry.json not found — pass --registry PATH "
                "(looked for ../devin-powerups/registry.json)",
                exit_code=2,
            )
        registry_path = found
    try:
        if args.pm_registry:
            projects = projects_from_pm_registry(
                Path(args.pm_registry).expanduser()
            )
        else:
            projects, _ = _projects(
                _resolve_db(args), _resolve_vscdb(args)
            )
        report = verify(projects, registry_path)
    except FileNotFoundError as exc:
        raise _CliError(
            f"{exc.filename or exc}: no such file — pass --registry/"
            "--pm-registry/--sessions-db",
            exit_code=2,
        ) from exc
    except RegistryError as exc:
        raise _CliError(str(exc), exit_code=1) from exc
    if args.fix_hints:
        add_fix_hints(report)
    if args.json:
        _print_json(report)
    else:
        print(render_verify_text(report, hints=args.fix_hints))
    return 1 if report["drift"] else 0


def cmd_digest(args: argparse.Namespace) -> int:
    projects, _ = _projects(_resolve_db(args), _resolve_vscdb(args))
    since_ms = None
    if args.since is not None:
        try:
            since_ms = parse_since(args.since)
        except ValueError as exc:
            raise _CliError(str(exc), exit_code=2) from exc
    cursor = None
    if args.incremental:
        from devin_internals.cursor import Cursor

        cursor = Cursor(CURSOR_NAME)
    result = run_digest(
        projects,
        since_ms=since_ms,
        cursor=cursor,
        out_dir=args.out_dir,
        stdout=args.stdout or args.slack,
        renderer=render_slack_digest if args.slack else None,
    )
    if result.report is None:
        print(
            f"no new project activity since "
            f"{ms_to_iso(result.since_ms)} — nothing to digest"
        )
        return 0
    if args.stdout or args.slack:
        print(result.report, end="")
    else:
        print(f"wrote {result.out_path}")
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devin-pm",
        description=(
            "Project manager over Devin sessions — groups sessions.db per "
            "working directory into projects, reports, milestones and a "
            "machine-readable registry. Read-only, always."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def with_db(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument(
            "--sessions-db",
            metavar="PATH",
            help="path to sessions.db (default: auto-detect "
            "%%APPDATA%%/devin/cli/sessions.db, or DEVIN_PM_SESSIONS_DB)",
        )
        p.add_argument(
            "--vscdb",
            nargs="?",
            const="auto",
            metavar="PATH",
            help="also group GUI sessions from state.vscdb (read-only) — "
            "bare flag auto-detects "
            "<config>/Devin/User/globalStorage/state.vscdb or "
            "DEVIN_PM_STATE_VSCDB; PATH pins the file",
        )
        return p

    p = with_db(sub.add_parser("status", help="per-project rollup table"))
    p.add_argument("--json", action="store_true", help="JSON output")
    p.set_defaults(func=cmd_status)

    p = with_db(sub.add_parser("report", help="markdown status report"))
    p.add_argument(
        "--project",
        metavar="NAME",
        help="project name (working-directory basename); omit for a "
        "global report over all projects",
    )
    p.add_argument("--out", metavar="FILE", help="write markdown to FILE")
    p.set_defaults(func=cmd_report)

    p = with_db(
        sub.add_parser("milestones", help="list a project's milestones")
    )
    p.add_argument("--project", required=True, metavar="NAME")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.set_defaults(func=cmd_milestones)

    p = with_db(
        sub.add_parser("registry", help="emit registry.json")
    )
    p.add_argument(
        "--out",
        metavar="FILE",
        help="write registry.json to FILE (default: stdout)",
    )
    p.set_defaults(func=cmd_registry)

    p = with_db(
        sub.add_parser(
            "verify",
            help="cross-check tracked projects against the "
            "devin-powerups hub registry",
        )
    )
    p.add_argument(
        "--registry",
        metavar="PATH",
        help="hub registry.json (default: "
        "../devin-powerups/registry.json relative to cwd, then the "
        "checkout sibling of this package)",
    )
    p.add_argument(
        "--pm-registry",
        metavar="FILE",
        help="verify a saved `devin-pm registry --out` document instead "
        "of reading sessions.db",
    )
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument(
        "--fix-hints",
        action="store_true",
        help="append a remediation hint to every divergence — expected "
        "value plus the suggested action (text mode prints a 'hint:' "
        "line per item; --json adds a 'hint' key per item)",
    )
    p.set_defaults(func=cmd_verify)

    p = with_db(
        sub.add_parser(
            "digest",
            help="weekly-ish markdown digest of project/milestone "
            "activity since a watermark",
        )
    )
    p.add_argument(
        "--since",
        metavar="EPOCH|ISO",
        help="include sessions with activity after this timestamp — "
        "epoch seconds/ms or ISO-8601 (e.g. 2026-10-13); default: the "
        "last 7 days, or the committed cursor watermark with "
        "--incremental",
    )
    p.add_argument(
        "--incremental",
        action="store_true",
        help="watermark mode via the shared cursor "
        "(.devin/pm-digest-state.json): digest only sessions active "
        "after the last committed run, then advance the watermark — "
        "missing/corrupt state fails open to a full digest",
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="print the digest instead of writing "
        "pm-digest-YYYY-Www.md (still advances the cursor under "
        "--incremental)",
    )
    p.add_argument(
        "--slack",
        action="store_true",
        help="print a Slack-mrkdwn digest instead of markdown — *bold* "
        "headers, bullet sessions (no tables), hard-capped at "
        f"{SLACK_SAFE_LIMIT} chars so it can be piped verbatim into "
        "slack-notify (implies --stdout; still advances the cursor "
        "under --incremental)",
    )
    p.add_argument(
        "--out-dir",
        metavar="DIR",
        help="directory for pm-digest-YYYY-Www.md (default: cwd)",
    )
    p.set_defaults(func=cmd_digest)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except _CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except MilestonesError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SchemaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
