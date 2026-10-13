"""devin-pm as an MCP server: the project rollup exposed as a tool.

One tool, ``pm_rollup`` — groups ``sessions.db`` per working directory
and returns the same JSON payload as ``devin-pm registry``: every
project's session count, status, milestones, last activity and cost
(``project=<name>`` narrows it to one entry). Read-only by
construction: the store is opened read-only and nothing is written.

The logic lives in :func:`do_rollup`, unit-testable without a running
server or the ``mcp`` package. ``build_server()`` wraps it — needs the
``mcp`` extra: ``pip install 'devin-pm[mcp]'``.
"""

from __future__ import annotations

from pathlib import Path

from devin_internals.parsers import SessionsStore

from devin_pm.milestones import collect_milestones
from devin_pm.projects import (
    default_sessions_db,
    find_project,
    group_sessions,
)
from devin_pm.registry import build_registry


def do_rollup(
    sessions_db: str | None = None,
    project: str | None = None,
    *,
    generated: str | None = None,
) -> dict:
    """Return the ``devin-pm registry`` JSON payload as a dict — or, when
    ``project`` is given, that single project's registry entry.

    A missing store maps to the CLI's exit-2 case
    (``{"error": "no_db"}``), an unknown project name to
    ``{"error": "unknown_project"}`` with the known names in ``detail``.
    ``generated`` pins the registry timestamp — an injectable seam for
    tests, not a tool parameter.
    """
    db_path = (
        Path(sessions_db).expanduser()
        if sessions_db
        else default_sessions_db()
    )
    if not db_path.is_file():
        return {
            "error": "no_db",
            "detail": f"{db_path}: no sessions.db there — "
            "pass sessions_db",
        }
    with SessionsStore(db_path) as store:
        sessions = store.sessions()
        version = store.schema_info["schema_version"]
    projects = group_sessions(sessions)

    if project:
        found = find_project(projects, project)
        if found is None:
            known = ", ".join(p.name for p in projects) or "(none)"
            return {
                "error": "unknown_project",
                "detail": f"unknown project {project!r} — "
                f"known projects: {known}",
            }
        registry = build_registry(
            [found],
            milestones={found.name: collect_milestones(found)},
            sessions_db=db_path,
            schema_version=version,
            generated=generated,
        )
        return registry["projects"][0]

    return build_registry(
        projects,
        milestones={p.name: collect_milestones(p) for p in projects},
        sessions_db=db_path,
        schema_version=version,
        generated=generated,
    )


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
            "pip install 'devin-pm[mcp]'"
        ) from e


def build_server():
    """Build the MCP server app with every devin-pm tool registered."""
    server = _make_app("devin-pm")

    @server.tool()
    def pm_rollup(sessions_db: str = "", project: str = "") -> dict:
        """Aggregate project progress from Devin session history — the same
        JSON as ``devin-pm registry``: per-project session counts, status
        totals, milestones, last activity and cost, plus global totals.
        Pass ``project`` (a working-directory basename, case-insensitive)
        to get just that project's entry. Read-only: the store is opened
        read-only and never modified. ``sessions_db`` overrides the
        auto-detected path."""
        try:
            return do_rollup(
                sessions_db=sessions_db or None,
                project=project or None,
            )
        except Exception as error:  # noqa: BLE001 — tool boundary must not raise
            return _err(error)

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
