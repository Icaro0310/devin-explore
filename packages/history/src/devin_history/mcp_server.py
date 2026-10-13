"""devin-history as an MCP server: ``list`` and ``export`` exposed as tools.

Two tools — ``history_list`` returns the same JSON payload as
``devin-history list --json``; ``history_export`` mirrors
``devin-history export --json`` (the export writes only inside the
caller-specified ``out_dir`` — the tool's own output; the source store
is always opened read-only, never modified).

The logic lives in :func:`do_list` / :func:`do_export`, unit-testable
without a running server or the ``mcp`` package. ``build_server()``
wraps them — needs the ``mcp`` extra:
``pip install 'devin-history[mcp]'``.
"""

from __future__ import annotations

from pathlib import Path

from devin_internals import SchemaError
from devin_internals.parsers import SessionsStore

from devin_history.export import export_sessions
from devin_history.format import sessions_to_dicts
from devin_history.paths import default_sessions_db


def _bad_store(detail: str) -> dict:
    """The error shape for every case the CLI maps to exit 2."""
    return {"error": "bad_store", "detail": detail}


def _open_store(sessions_db: str | None):
    """Resolve + open ``sessions.db`` exactly like the CLI's ``--sessions-db``.

    Returns ``(store, None)`` on success or ``(None, error_dict)`` for
    the exit-2 cases: no default store found, missing file, or an
    unsupported schema version.
    """
    path = (
        Path(sessions_db).expanduser()
        if sessions_db
        else default_sessions_db()
    )
    if path is None:
        return None, _bad_store(
            "no sessions.db found in the default locations; "
            "pass sessions_db"
        )
    if not path.exists():
        return None, _bad_store(f"{path}: no such file")
    try:
        return SessionsStore(path), None
    except SchemaError as exc:
        return None, _bad_store(str(exc))


def do_list(
    sessions_db: str | None = None, limit: int | None = None
) -> dict:
    """Return the ``devin-history list --json`` payload as a dict.

    ``limit`` mirrors the CLI's ``--limit``: unset means every session,
    not a page cap."""
    store, err = _open_store(sessions_db)
    if err is not None:
        return err
    with store:
        sessions = store.sessions(limit=limit)
    return {"sessions": sessions_to_dicts(sessions)}


def do_export(
    out_dir: str,
    sessions_db: str | None = None,
    fmt: str = "md",
    dry_run: bool = False,
    session_id: str | None = None,
) -> dict:
    """Return the ``devin-history export --json`` payload as a dict.

    Writes one note per session plus an index into ``out_dir`` — the
    tool's own output; the source store is only read. ``dry_run``
    reports what would be written and writes nothing; ``session_id``
    (unique prefix ok) restricts the export to one session.
    """
    store, err = _open_store(sessions_db)
    if err is not None:
        return err
    with store:
        res = export_sessions(
            store,
            out_dir,
            fmt=fmt,
            dry_run=dry_run,
            only_session=session_id,
        )
    return {
        "out_dir": str(res.out_dir),
        "format": res.format,
        "written": res.written,
        "skipped_unchanged": res.skipped_unchanged,
        "skipped_empty": res.skipped_empty,
        "indexed": len(res.index_entries),
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
            "pip install 'devin-history[mcp]'"
        ) from e


def build_server():
    """Build the MCP server app with every devin-history tool registered."""
    server = _make_app("devin-history")

    @server.tool()
    def history_list(
        sessions_db: str = "", limit: int | None = None
    ) -> dict:
        """List Devin sessions, most recent first — the same JSON as
        ``devin-history list --json``. Read-only: the store is opened
        read-only and never modified. ``sessions_db`` overrides the
        auto-detected path; ``limit`` caps the count (default: all,
        like the CLI)."""
        try:
            return do_list(sessions_db=sessions_db or None, limit=limit)
        except Exception as error:  # noqa: BLE001 — tool boundary must not raise
            return _err(error)

    @server.tool()
    def history_export(
        out_dir: str,
        sessions_db: str = "",
        fmt: str = "md",
        dry_run: bool = False,
        session_id: str = "",
    ) -> dict:
        """Export sessions to one note each plus an index in ``out_dir`` —
        the same JSON as ``devin-history export --json``. Writes only
        inside ``out_dir`` (the tool's own output); the source store is
        read-only. ``dry_run=True`` lists what would be written and
        writes nothing; ``session_id`` (unique prefix ok) exports a
        single session."""
        try:
            return do_export(
                out_dir,
                sessions_db=sessions_db or None,
                fmt=fmt,
                dry_run=dry_run,
                session_id=session_id or None,
            )
        except Exception as error:  # noqa: BLE001 — tool boundary must not raise
            return _err(error)

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
