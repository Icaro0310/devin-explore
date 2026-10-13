"""Thin CLI wrapper — all logic lives in the library modules."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from devin_internals import SchemaError
from devin_internals.parsers import SessionsStore, StateVscdbStore

from devin_history import __version__
from devin_history.audit import audit_store
from devin_history.export import FORMATS, export_gui_sessions, export_sessions
from devin_history.format import (
    audit_to_dict,
    audit_to_markdown,
    sessions_table,
    sessions_to_dicts,
    write_audit_csv,
)
from devin_history.paths import default_sessions_db, default_state_vscdb


def _open_store(db_arg: str | None) -> SessionsStore:
    path = Path(db_arg).expanduser() if db_arg else default_sessions_db()
    if path is None:
        print(
            "devin-history: no sessions.db found in the default locations; "
            "pass --sessions-db",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not path.exists():
        print(f"devin-history: {path}: no such file", file=sys.stderr)
        raise SystemExit(2)
    try:
        return SessionsStore(path)
    except SchemaError as exc:
        print(f"devin-history: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _open_vscdb(vscdb_arg: str | None) -> StateVscdbStore:
    path = Path(vscdb_arg).expanduser() if vscdb_arg else default_state_vscdb()
    if path is None:
        print(
            "devin-history: no state.vscdb found in the default locations; "
            "pass --vscdb",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not path.exists():
        print(f"devin-history: {path}: no such file", file=sys.stderr)
        raise SystemExit(2)
    try:
        return StateVscdbStore(path)
    except (SchemaError, sqlite3.Error) as exc:
        print(f"devin-history: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--sessions-db",
        help="path to Devin's sessions.db (default: auto-detect "
        "%%APPDATA%%/devin/cli/sessions.db or the Linux/macOS equivalent)",
    )
    sub.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="devin-history",
        description="Export and audit Devin Desktop session history "
        "(sessions.db → Markdown notes, JSON dump, audit report).",
    )
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = p.add_subparsers(dest="command", required=True)

    e = sub.add_parser(
        "export", help="one note per session + index, idempotent")
    _add_common(e)
    e.add_argument("--out", required=True, help="output directory")
    e.add_argument("--format", choices=FORMATS, default="md")
    e.add_argument(
        "--all", action="store_true",
        help="re-export even unchanged sessions")
    e.add_argument(
        "--dry-run", action="store_true",
        help="list what would be written, write nothing")
    e.add_argument(
        "--session-id", metavar="ID",
        help="export only this session (unique prefix ok) — incremental "
        "mode for a SessionEnd hook; the index is merged, not rebuilt")
    e.add_argument(
        "--from-hook", action="store_true",
        help="read the session id from the hook payload on stdin "
        "({\"session_id\": ...}) — implies the incremental mode")
    e.add_argument(
        "--since", type=int, metavar="EPOCH_MS",
        help="export only sessions with activity after this timestamp "
        "(milliseconds, watermark filter)")
    e.add_argument(
        "--incremental", action="store_true",
        help="watermark mode via the shared cursor "
        "(.devin/devin-history-export-state.json): export only sessions "
        "active after the last committed run, then advance the watermark")
    e.set_defaults(func=_cmd_export)

    a = sub.add_parser(
        "audit", help="grouping by status/type/repo/period + anomalies")
    _add_common(a)
    a.add_argument("--csv", help="also write a CSV summary to this path")
    a.set_defaults(func=_cmd_audit)

    l = sub.add_parser("list", help="quick session table")
    _add_common(l)
    l.add_argument("--limit", type=int, default=None)
    l.set_defaults(func=_cmd_list)

    g = sub.add_parser(
        "export-gui",
        help="GUI session metadata (state.vscdb) → md notes + index.json")
    g.add_argument(
        "--vscdb",
        help="path to the GUI state.vscdb (default: auto-detect under "
        "User/globalStorage of the Devin config dir)",
    )
    g.add_argument("--out", required=True, help="output directory")
    g.add_argument(
        "--all", action="store_true",
        help="re-export even unchanged sessions")
    g.add_argument(
        "--dry-run", action="store_true",
        help="list what would be written, write nothing")
    g.add_argument(
        "--json", action="store_true", help="machine-readable JSON output")
    g.set_defaults(func=_cmd_export_gui)
    return p


def _hook_session_id() -> str | None:
    """Session id from a hook JSON payload piped on stdin."""
    if sys.stdin.isatty():
        return None
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return None
    sid = data.get("session_id") or data.get("sessionId")
    return sid if isinstance(sid, str) else None


def _cmd_export(args: argparse.Namespace) -> int:
    only = args.session_id
    if args.from_hook:
        only = _hook_session_id() or args.session_id
        if not only:
            print("devin-history export --from-hook: no session_id in "
                  "stdin payload — nothing exported", file=sys.stderr)
            return 0  # fail-soft in a hook
    cursor = None
    if args.incremental:
        from devin_internals.cursor import Cursor
        cursor = Cursor("devin-history-export")
    with _open_store(args.sessions_db) as store:
        res = export_sessions(
            store, args.out, fmt=args.format, force=args.all,
            dry_run=args.dry_run, only_session=only, since=args.since,
            cursor=cursor)
    if args.json:
        print(json.dumps({
            "out_dir": str(res.out_dir),
            "format": res.format,
            "written": res.written,
            "skipped_unchanged": res.skipped_unchanged,
            "skipped_empty": res.skipped_empty,
            "indexed": len(res.index_entries),
        }, indent=2))
    else:
        verb = "would write" if args.dry_run else "written"
        print(
            f"{verb}: {len(res.written)} · unchanged: "
            f"{len(res.skipped_unchanged)} · empty: {len(res.skipped_empty)} "
            f"· index: {len(res.index_entries)} sessions → {res.out_dir}"
        )
    return 0


def _cmd_export_gui(args: argparse.Namespace) -> int:
    with _open_vscdb(args.vscdb) as store:
        res = export_gui_sessions(
            store, args.out, force=args.all, dry_run=args.dry_run)
    if args.json:
        print(json.dumps({
            "out_dir": str(res.out_dir),
            "written": res.written,
            "skipped_unchanged": res.skipped_unchanged,
            "indexed": len(res.index_entries),
        }, indent=2))
    else:
        verb = "would write" if args.dry_run else "written"
        print(
            f"{verb}: {len(res.written)} · unchanged: "
            f"{len(res.skipped_unchanged)} · index: "
            f"{len(res.index_entries)} sessions → {res.out_dir}"
        )
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    with _open_store(args.sessions_db) as store:
        report = audit_store(store)
    if args.csv:
        write_audit_csv(report, args.csv)
        if not args.json:
            print(f"csv → {args.csv}", file=sys.stderr)
    if args.json:
        print(json.dumps(audit_to_dict(report), ensure_ascii=False, indent=2))
    else:
        print(audit_to_markdown(report))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    with _open_store(args.sessions_db) as store:
        sessions = store.sessions(limit=args.limit)
    if args.json:
        print(json.dumps({"sessions": sessions_to_dicts(sessions)},
                         ensure_ascii=False, indent=2))
    else:
        print(sessions_table(sessions))
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
