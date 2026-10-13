"""devin-search as an MCP server: ``query`` exposed as a tool.

One tool, ``search_query`` — runs a full-text query over the search
index and returns the same JSON payload as ``devin-search query --json``.
Read-only by construction: querying never writes to the index or the
source stores (the local query log is not written either), and index
building stays CLI-only.

The logic lives in :func:`do_query`, unit-testable without a running
server or the ``mcp`` package. ``build_server()`` wraps it — needs the
``mcp`` extra: ``pip install 'devin-search[mcp]'``.
"""

from __future__ import annotations

from pathlib import Path

from devin_search.fmt import hits_to_dicts
from devin_search.paths import default_index_path
from devin_search.query import parse_since, search


def do_query(
    term: str,
    index: str | None = None,
    role: str | None = None,
    project: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> dict:
    """Return the ``devin-search query --json`` payload as a dict.

    ``since`` accepts the CLI spellings — ``YYYY-MM-DD`` or epoch
    milliseconds. A missing index maps to the CLI's exit-2 case
    (``{"error": "no_index"}``); zero hits is not an error — the CLI
    exits 1 but still prints the payload.
    """
    index_path = Path(index).expanduser() if index else default_index_path()
    try:
        since_ms = parse_since(since) if since else None
    except ValueError:
        return {
            "error": "bad_since",
            "detail": f"since {since!r} is not a date (YYYY-MM-DD) "
            "or epoch ms",
        }
    try:
        hits = search(
            index_path,
            term,
            role=role,
            project=project,
            since=since_ms,
            limit=limit,
        )
    except FileNotFoundError:
        return {
            "error": "no_index",
            "detail": "run `devin-search index` first",
        }
    return {"term": term, "hits": hits_to_dicts(hits)}


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
            "pip install 'devin-search[mcp]'"
        ) from e


def build_server():
    """Build the MCP server app with every devin-search tool registered."""
    server = _make_app("devin-search")

    @server.tool()
    def search_query(
        term: str,
        index: str = "",
        role: str = "",
        project: str = "",
        since: str = "",
        limit: int = 20,
        no_log: bool = False,
    ) -> dict:
        """Full-text search across all Devin sessions — the same JSON as
        ``devin-search query --json``. Read-only: never modifies the index
        or the source stores. ``term`` tokens are ANDed; ``role`` filters
        user/assistant/tool/system/shell, ``project`` is a substring match
        on the working directory, ``since`` is YYYY-MM-DD or epoch ms.
        ``no_log`` is accepted for CLI parity — the adapter never writes
        the local query log regardless."""
        try:
            return do_query(
                term,
                index=index or None,
                role=role or None,
                project=project or None,
                since=since or None,
                limit=limit,
            )
        except Exception as error:  # noqa: BLE001 — tool boundary must not raise
            return _err(error)

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
