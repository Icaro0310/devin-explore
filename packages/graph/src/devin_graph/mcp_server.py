"""devin-graph as an MCP server: the ``query`` subcommands as one tool.

One tool, ``graph_query`` — the read half of ``devin-graph query`` —
returns the same JSON payloads as ``devin-graph query <what> --json``.
Read-only by construction: ``build``, ``export``, ``sql`` and ``view``
stay CLI-only; opening the graph never writes to it, and the source
stores are never touched.

The logic lives in :func:`do_query`, unit-testable without a running
server or the ``mcp`` package. ``build_server()`` wraps it — needs the
``mcp`` extra: ``pip install 'devin-graph[mcp]'``.
"""

from __future__ import annotations

from pathlib import Path

from devin_graph.query import (
    project_detail,
    projects_graph,
    sessions_for_file,
    sessions_for_tool,
    shared_files,
)
from devin_graph.store import GraphStore

# Same default as the CLI (`devin-graph query ... --graph graph.db`).
DEFAULT_GRAPH = "graph.db"

QUERY_TYPES = ("file", "tool", "project", "projects-graph", "shared-files")


def do_query(query_type: str, target: str = "", graph: str = "") -> dict:
    """Run one canned graph query; return its ``--json`` payload as a dict.

    ``graph.db`` must already exist — the read-write
    :class:`GraphStore` creates the file on open, so the missing-file
    case (the CLI's exit-2) is checked *before* opening and maps to
    ``{"error": "no_graph"}``. The store is then opened read-only
    (``mode=ro`` + ``query_only``): no schema DDL, no repair of an
    incomplete schema.
    """
    path = Path(graph or DEFAULT_GRAPH).expanduser()
    if not path.exists():
        return {
            "error": "no_graph",
            "detail": f"{path}: no such graph file — "
            "run `devin-graph build` first",
        }
    with GraphStore(path, readonly=True) as gs:
        if query_type == "file":
            return sessions_for_file(gs, target)
        if query_type == "tool":
            return sessions_for_tool(gs, target)
        if query_type == "project":
            return project_detail(gs, target)
        if query_type == "projects-graph":
            return projects_graph(gs)
        if query_type == "shared-files":
            return shared_files(gs)
    return {
        "error": "bad_query",
        "detail": f"unknown query_type {query_type!r} — "
        f"expected one of {', '.join(QUERY_TYPES)}",
    }


def _err(error: Exception) -> dict:
    return {"error": type(error).__name__, "detail": str(error)[:500]}


def _make_app(name: str):
    """Return an MCP server app across SDK versions.

    mcp 2.x renamed FastMCP -> MCPServer; both expose the same .tool()
    decorator and .run(transport='stdio'). Support whichever is installed.
    """
    try:  # mcp 2.x
        from mcp.server.mcpserver import MCPServer
        return MCPServer(name)
    except ImportError:
        pass
    try:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP
        return FastMCP(name)
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "The MCP server needs the 'mcp' extra: "
            "pip install 'devin-graph[mcp]'"
        ) from e


def build_server():
    """Build the MCP server app with every devin-graph tool registered."""
    server = _make_app("devin-graph")

    @server.tool()
    def graph_query(
        query_type: str,
        target: str = "",
        graph: str = "",
    ) -> dict:
        """Query the session knowledge graph — the same JSON as
        ``devin-graph query <what> --json``. Read-only: never modifies
        ``graph.db`` or the source stores. ``query_type`` is one of
        ``file`` (sessions that touched a path), ``tool``
        (sessions/projects that used a tool), ``project`` (a project's
        sessions, tools and files), ``projects-graph`` (project adjacency)
        or ``shared-files`` (files touched by ≥2 projects). ``target`` is
        the path/name for the first three; ``graph`` overrides the
        default ``./graph.db`` location."""
        try:
            return do_query(query_type, target=target, graph=graph)
        except Exception as error:  # noqa: BLE001 — tool boundary must not raise
            return _err(error)

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
