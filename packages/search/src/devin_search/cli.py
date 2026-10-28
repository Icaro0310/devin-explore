"""Thin CLI wrapper — all logic lives in the library modules."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from devin_internals import SchemaError

from devin_search import __version__
from devin_search.fmt import (
    hits_table,
    hits_to_dicts,
    stats_line,
    stats_to_dict,
)
from devin_search.history import attach_history_notes
from devin_search.index import build_index
from devin_search.paths import (
    acp_dir_candidates,
    codex_root_candidates,
    default_acp_dir,
    default_codex_dir,
    default_copilot_dirs,
    default_index_path,
    default_sessions_db,
    default_vscode_chat_dirs,
    sessions_db_candidates,
)
from devin_search.query import StaleIndexError, parse_since, search


def _resolve_index(arg: str | None) -> Path:
    return Path(arg).expanduser() if arg else default_index_path()


def _detected_or_candidate(
    found: Path | None, candidates: list[Path]
) -> Path | None:
    """Auto-detected store, else the canonical candidate location.

    "Not found" is not "not requested": a managed source whose store
    vanished since the last run must still sweep its stale docs —
    ``build_index`` treats a given-but-absent path as a drop. Passing the
    canonical candidate keeps auto-detected sweeps working; explicit
    flags are validated separately below."""
    return found if found is not None else (
        candidates[0] if candidates else None
    )


def _cmd_index(args: argparse.Namespace) -> int:
    sessions_db = (
        Path(args.sessions_db).expanduser()
        if args.sessions_db
        else _detected_or_candidate(
            default_sessions_db(), sessions_db_candidates()
        )
    )
    acp_dir = (
        None
        if args.no_acp
        else Path(args.acp_dir).expanduser()
        if args.acp_dir
        else _detected_or_candidate(
            default_acp_dir(), acp_dir_candidates()
        )
    )
    copilot_dirs = (  # [] already means "scanned, absent" for build_index
        None
        if args.no_copilot
        else [Path(args.copilot_dir).expanduser()]
        if args.copilot_dir
        else default_copilot_dirs()
    )
    codex_dir = (
        None
        if args.no_codex
        else Path(args.codex_dir).expanduser()
        if args.codex_dir
        else _detected_or_candidate(
            default_codex_dir(), codex_root_candidates()
        )
    )
    vscode_chat_dirs = (  # [] already means "scanned, absent"
        None
        if args.no_vscode_chat
        else [Path(args.vscode_chat_dir).expanduser()]
        if args.vscode_chat_dir
        else default_vscode_chat_dirs()
    )
    # Explicitly named stores must exist — a typo'd path is a user error,
    # not a vanished source sweeping its docs. A --no-* flag wins: the
    # supplied path is then unused, so it must not be validated.
    for raw, disabled in (
        (args.sessions_db, False),
        (args.acp_dir, args.no_acp),
        (args.copilot_dir, args.no_copilot),
        (args.codex_dir, args.no_codex),
        (args.vscode_chat_dir, args.no_vscode_chat),
    ):
        if not disabled and raw and not Path(raw).expanduser().exists():
            print(f"devin-search: {raw}: no such file", file=sys.stderr)
            return 2
    index_path = _resolve_index(args.index)
    codex_sessions = os.environ.get("CODEX_SESSIONS_DIR", "").strip()
    have_store = (
        (sessions_db is not None and sessions_db.is_file())
        or (acp_dir is not None and acp_dir.is_dir())
        or any(Path(d).is_dir() for d in copilot_dirs or [])
        or (codex_dir is not None and Path(codex_dir).is_dir())
        or bool(codex_sessions and Path(codex_sessions).is_dir())
        or any(Path(d).is_dir() for d in vscode_chat_dirs or [])
    )
    if not have_store and not index_path.exists():
        print(
            "devin-search: no session stores found in the default "
            "locations; pass --sessions-db, --acp-dir, --copilot-dir, "
            "--codex-dir and/or --vscode-chat-dir",
            file=sys.stderr,
        )
        return 2
    try:
        stats = build_index(
            index_path,
            sessions_db=sessions_db,
            acp_dir=acp_dir,
            copilot_dirs=copilot_dirs,
            codex_dir=codex_dir,
            vscode_chat_dirs=vscode_chat_dirs,
            rebuild=args.rebuild,
        )
    except (SchemaError, sqlite3.Error, OSError) as exc:
        # Corrupt/locked stores (and a corrupt index file) are input
        # errors — report them, never traceback.
        print(f"devin-search: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(stats_to_dict(stats), ensure_ascii=False, indent=2))
    else:
        print(stats_line(stats))
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    try:
        since = parse_since(args.since) if args.since else None
    except ValueError:
        print(
            f"devin-search: --since {args.since!r} is not a date "
            "(YYYY-MM-DD) or epoch ms",
            file=sys.stderr,
        )
        return 2
    try:
        hits = search(
            _resolve_index(args.index),
            args.term,
            role=args.role,
            project=args.project,
            since=since,
            tool=args.tool,
            limit=args.limit,
        )
    except (FileNotFoundError, StaleIndexError, sqlite3.Error, OSError) as exc:
        print(f"devin-search: {exc}", file=sys.stderr)
        return 2
    if not args.no_log:
        from devin_search.misslog import record
        record(_resolve_index(args.index), args.term, len(hits),
               role=args.role, project=args.project, since=args.since,
               tool=args.tool)
    if args.history_dir:
        hits = attach_history_notes(hits, args.history_dir)
    if args.json:
        print(
            json.dumps(
                {"term": args.term, "version": __version__,
                 "hits": hits_to_dicts(hits)},
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(hits_table(hits))
    return 0 if hits else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="devin-search",
        description="Full-text search across all agent sessions "
        "(Devin sessions.db + acp-messages, Copilot CLI, Codex CLI and "
        "VS Code chatSessions → local FTS5 index).",
    )
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = p.add_subparsers(dest="command", required=True)

    ix = sub.add_parser(
        "index", help="build/update the local search index (incremental)"
    )
    ix.add_argument(
        "--sessions-db",
        help="path to Devin's sessions.db (default: auto-detect)",
    )
    ix.add_argument(
        "--acp-dir",
        help="path to Devin's User/acp-messages dir (default: auto-detect)",
    )
    ix.add_argument(
        "--no-acp",
        action="store_true",
        help="index sessions.db only — never auto-detect the real "
        "acp-messages dir (also the deterministic choice for tests)",
    )
    ix.add_argument(
        "--copilot-dir",
        help="path to a GitHub Copilot CLI root (default: auto-detect "
        "$COPILOT_DIR, $COPILOT_HOME, ~/.copilot — all existing merged)",
    )
    ix.add_argument(
        "--no-copilot",
        action="store_true",
        help="never auto-detect Copilot session-state dirs",
    )
    ix.add_argument(
        "--codex-dir",
        help="path to a Codex CLI root (default: auto-detect $CODEX_HOME "
        "or ~/.codex)",
    )
    ix.add_argument(
        "--no-codex",
        action="store_true",
        help="never auto-detect the Codex state db and rollouts",
    )
    ix.add_argument(
        "--vscode-chat-dir",
        help="path to one VS Code <Variant>/User dir holding "
        "chatSessions stores (default: auto-detect Code + "
        "Code - Insiders + ~/.vscode-server/data/User)",
    )
    ix.add_argument(
        "--no-vscode-chat",
        action="store_true",
        help="never auto-detect VS Code chatSessions dirs",
    )
    ix.add_argument("--index", help="index file path (default: app dir)")
    ix.add_argument(
        "--rebuild",
        action="store_true",
        help="drop and rebuild the index from scratch",
    )
    ix.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )
    ix.set_defaults(func=_cmd_index)

    q = sub.add_parser("query", help="search the index")
    q.add_argument("term", help="free-text search (tokens ANDed)")
    q.add_argument("--role", help="filter: user/assistant/tool/system/shell")
    q.add_argument(
        "--project", help="substring filter on the session working directory"
    )
    q.add_argument(
        "--since", help="only hits at/after this date (YYYY-MM-DD or epoch ms)"
    )
    q.add_argument(
        "--tool", help="filter: exact tool name on tool-call docs "
        "(e.g. shell; devin-cli tool_call_state only)"
    )
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--index", help="index file path (default: app dir)")
    q.add_argument(
        "--history-dir",
        help="devin-history export dir — hits link to their "
        "<YYYY-MM-DD>_<session-id>.md notes when present",
    )
    q.add_argument(
        "--json", action="store_true", help="machine-readable JSON output"
    )
    q.add_argument(
        "--no-log", action="store_true",
        help="skip the local query log (<index>.queries.jsonl)",
    )
    q.set_defaults(func=_cmd_query)

    m = sub.add_parser(
        "misses",
        help="zero-hit query stats from the local query log "
        "(the objective trigger for semantic search)",
    )
    m.add_argument("--index", help="index file path (default: app dir)")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=_cmd_misses)
    return p


def _cmd_misses(args: argparse.Namespace) -> int:
    from devin_search.misslog import summarize
    s = summarize(_resolve_index(args.index))
    if args.json:
        print(json.dumps({**s, "version": __version__},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"query log: {s['log']}")
    print(f"  {s['total_queries']} queries · "
          f"{s['zero_hit']} zero-hit ({s['zero_hit_rate']:.0%})")
    for month, b in sorted(s["per_month"].items()):
        print(f"  {month}: {b['queries']} queries, "
              f"{b['zero_hit']} zero-hit")
    if s["top_missed_terms"]:
        print("  top missed terms:")
        for term, n in s["top_missed_terms"]:
            print(f"    {n:>3}× {term}")
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
