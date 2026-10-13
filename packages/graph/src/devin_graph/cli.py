"""Thin CLI wrapper — all logic lives in the library modules."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from devin_internals import SchemaError
from devin_internals.parsers import SessionsStore

from devin_graph import __version__
from devin_graph.paths import default_sessions_db, default_state_vscdb
from devin_graph.query import (
    project_detail,
    projects_graph,
    sessions_for_file,
    sessions_for_tool,
    shared_files,
)
from devin_graph.store import GraphStore
from devin_graph.vscdb import extract_vscdb

DEFAULT_GRAPH = "graph.db"


def _open_sessions(db_arg: str | None) -> SessionsStore:
    path = Path(db_arg).expanduser() if db_arg else default_sessions_db()
    if path is None:
        print(
            "devin-graph: no sessions.db found in the default locations; "
            "pass --sessions-db",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not path.exists():
        print(f"devin-graph: {path}: no such file", file=sys.stderr)
        raise SystemExit(2)
    try:
        return SessionsStore(path)
    except SchemaError as exc:
        print(f"devin-graph: {exc}", file=sys.stderr)
        raise SystemExit(2)


def _open_graph(graph_arg: str | None) -> GraphStore:
    path = Path(graph_arg or DEFAULT_GRAPH).expanduser()
    if not path.exists():
        print(
            f"devin-graph: {path}: no such graph file — build one with "
            "'devin-graph build --graph graph.db'",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return GraphStore(path, readonly=True)


def _add_graph(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--graph", default=DEFAULT_GRAPH,
        help="path to the graph database (default: ./graph.db)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="devin-graph",
        description="Knowledge graph over Devin sessions: projects, files "
        "and tools as queryable nodes (read-only on Devin stores).",
    )
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="extract sessions.db → graph.db "
                       "(incremental)")
    b.add_argument(
        "--sessions-db",
        help="path to Devin's sessions.db (default: auto-detect "
        "%%APPDATA%%/devin/cli/sessions.db or the Linux/macOS equivalent)")
    _add_graph(b)
    b.add_argument(
        "--vscdb",
        help="path to the GUI state.vscdb for gui_session coverage "
        "(default: auto-detect; pass 'none' to disable)")
    b.add_argument("--json", action="store_true",
                   help="machine-readable JSON output")
    b.set_defaults(func=_cmd_build)

    q = sub.add_parser("query", help="canned queries over the graph")
    qsub = q.add_subparsers(dest="what", required=True)
    for name, helptext, argname in (
        ("file", "which sessions touched a file", "path"),
        ("tool", "which sessions/projects used a tool", "name"),
        ("project", "sessions/tools/files of a project", "name"),
    ):
        sq = qsub.add_parser(name, help=helptext)
        sq.add_argument(argname)
        _add_graph(sq)
        sq.add_argument("--json", action="store_true")
        sq.set_defaults(func=_cmd_query)
    sq = qsub.add_parser("projects-graph",
                         help="project adjacency JSON for visualization")
    _add_graph(sq)
    sq.add_argument("--json", action="store_true")
    sq.set_defaults(func=_cmd_query)
    sq = qsub.add_parser("shared-files",
                         help="files touched by two or more projects")
    _add_graph(sq)
    sq.add_argument("--json", action="store_true")
    sq.set_defaults(func=_cmd_query)

    e = sub.add_parser("export", help="dump nodes+edges (D3-friendly)")
    e.add_argument("--format", choices=["json"], default="json")
    _add_graph(e)
    e.add_argument("--out", help="write to file instead of stdout")
    e.set_defaults(func=_cmd_export)

    sq2 = sub.add_parser(
        "sql", help="read-only SQL over graph.db (SELECT/WITH only)")
    sq2.add_argument("statement", help="a SELECT or WITH ... SELECT statement")
    _add_graph(sq2)
    sq2.add_argument("--json", action="store_true")
    sq2.set_defaults(func=_cmd_sql)
    v = sub.add_parser(
        "view", help="self-contained HTML viewer (offline, no CDN)")
    v.add_argument("--out", default="graph-view.html",
                   help="output file (default: graph-view.html)")
    v.add_argument("--limit", type=int, default=500,
                   help="max nodes rendered (default 500; highest-degree "
                   "nodes are kept when truncating)")
    _add_graph(v)
    v.set_defaults(func=_cmd_view)
    return p


def _cmd_build(args: argparse.Namespace) -> int:
    graph_path = Path(args.graph).expanduser()
    extras = []
    vscdb = args.vscdb
    if vscdb is None:
        found = default_state_vscdb()
        vscdb = str(found) if found else "none"
    if vscdb != "none":
        vpath = Path(vscdb).expanduser()
        if vpath.is_file():
            try:
                extras.append(("vscdb", extract_vscdb(vpath)))
            except ValueError as exc:
                print(f"warning: {exc}", file=sys.stderr)
        else:
            print(f"warning: {vpath}: no such file — skipping GUI coverage",
                  file=sys.stderr)
    with _open_sessions(args.sessions_db) as ss, \
            GraphStore(graph_path) as gs:
        res = gs.build(ss, extras=extras)
        nodes = len(gs.nodes())
        edges = len(gs.edges())
    if args.json:
        print(json.dumps({
            "graph": str(graph_path),
            "extracted": len(res.extracted),
            "skipped": res.skipped,
            "removed": len(res.removed),
            "nodes": nodes,
            "edges": edges,
        }, indent=2))
    else:
        print(
            f"extracted: {len(res.extracted)} · skipped: {res.skipped} · "
            f"removed: {len(res.removed)} · nodes: {nodes} · edges: {edges} "
            f"→ {graph_path}")
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    with _open_graph(args.graph) as gs:
        if args.what == "file":
            data = sessions_for_file(gs, args.path)
        elif args.what == "tool":
            data = sessions_for_tool(gs, args.name)
        elif args.what == "project":
            data = project_detail(gs, args.name)
        elif args.what == "shared-files":
            data = shared_files(gs)
        else:
            data = projects_graph(gs)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        _print_query(args.what, data)
    return 0


def _print_query(what: str, data: dict) -> None:
    if what == "shared-files":
        if not data["files"]:
            print("no files shared between projects")
        for f in data["files"]:
            print(f"{f['file']}  ({len(f['projects'])} projects)")
            print(f"  projects: {', '.join(f['projects'])}")
            print(f"  sessions: {', '.join(f['sessions'])}")
        return
    if what == "projects-graph":
        for n in data["nodes"]:
            print(f"{n['id']}  ({len(n['sessions'])} sessions)")
        for link in data["links"]:
            print(f"{link['source']} ↔ {link['target']}  "
                  f"weight={link['weight']}")
        return
    if "files" in data and what == "file":
        for f in data["files"]:
            print(f"file: {f}")
    for s in data.get("sessions", []):
        print(f"{s['id']}  {s.get('title') or 'Untitled'}  "
              f"({s.get('project') or '-'})")
    if what == "tool" and data.get("projects"):
        print("projects: " + ", ".join(data["projects"]))
    if what == "project":
        if data.get("tools"):
            print("tools: " + ", ".join(data["tools"]))
        for f in data.get("files", []):
            print(f"file: {f}")
    if what == "file":
        for t in data.get("tool_calls", []):
            print(f"call: {t['id']} → {t['file']}")


def _cmd_export(args: argparse.Namespace) -> int:
    with _open_graph(args.graph) as gs:
        data = gs.export()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).expanduser().write_text(text + "\n", encoding="utf-8")
        print(f"exported {len(data['nodes'])} nodes, {len(data['edges'])} "
              f"edges → {args.out}")
    else:
        print(text)
    return 0


def _cmd_view(args: argparse.Namespace) -> int:
    from devin_graph.view import write_view
    with _open_graph(args.graph) as gs:
        out = write_view(gs.export(), args.out, limit=args.limit)
    print(f"wrote {out} ({out.stat().st_size:,} bytes) — open it in a browser")
    return 0


def _cmd_sql(args: argparse.Namespace) -> int:
    stmt = args.statement.strip().rstrip(";")
    head = stmt.split(None, 1)[0].lower() if stmt else ""
    if head not in ("select", "with"):
        print("devin-graph: sql accepts SELECT/WITH only", file=sys.stderr)
        return 2
    import sqlite3

    path = Path(args.graph).expanduser()
    if not path.exists():
        print(f"devin-graph: {path}: no such graph file", file=sys.stderr)
        return 2
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        con.execute("PRAGMA query_only = ON")
        cur = con.execute(stmt)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchall()
    except sqlite3.Error as exc:
        print(f"devin-graph: {exc}", file=sys.stderr)
        return 2
    finally:
        con.close()
    if args.json:
        print(json.dumps(
            [dict(zip(cols, r)) for r in rows], ensure_ascii=False, indent=2))
    else:
        print("  ".join(cols))
        for r in rows:
            print("  ".join("" if v is None else str(v) for v in r))
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
