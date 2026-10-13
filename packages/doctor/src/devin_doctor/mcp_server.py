"""devin-doctor as an MCP server: diagnosis + capabilities as tools.

Two tools: ``doctor_check`` runs every check and returns the same JSON
payload as ``devin-doctor check --json``; ``doctor_capabilities`` returns
the same profile as ``devin-doctor capabilities``. Read-only by
construction: doctor has no auto-fix surface, so there is nothing
mutating to expose (or to withhold).

The logic lives in :func:`do_check`, unit-testable without a running
server or the ``mcp`` package. ``build_server()`` wraps it — needs the
``mcp`` extra: ``pip install 'devin-doctor[mcp]'``.
"""

from __future__ import annotations

import json
from pathlib import Path

from devin_doctor import doctor
from devin_doctor.model import Context
from devin_doctor.paths import default_config_dir, default_data_dir


def do_check(
    data_dir: str | None = None,
    config_dir: str | None = None,
    cwd: str | None = None,
    stale_days: int = 30,
    *,
    offline: bool = False,
    now_ms: int | None = None,
) -> dict:
    """Run all checks; return the ``check --json`` payload as a dict.

    Mirrors the CLI's resolution order exactly: an explicit ``data_dir``
    wins, the platform default is the fallback, and ``config_dir``
    defaults to the platform location only when ``data_dir`` was not
    overridden (same rule as the CLI). ``offline`` is scoped to this
    call through ``Context.offline`` — it never touches
    ``DEVIN_DOCTOR_OFFLINE``/``os.environ``, so concurrent online calls
    are unaffected — and skips the updates check's network fetch and
    PATH probes, which can stall an MCP call for seconds. ``now_ms``
    is injectable so tests can pin "today"; unlike ``offline`` it is
    not exposed through the MCP tool.
    """
    ctx = Context(
        data_dir=Path(data_dir) if data_dir else default_data_dir(),
        cwd=Path(cwd) if cwd else Path.cwd(),
        stale_days=stale_days,
        now_ms=now_ms,
        config_dir=(
            Path(config_dir)
            if config_dir
            else default_config_dir() if data_dir is None else None
        ),
        offline=offline,
    )
    report = doctor.run_all(ctx)
    return json.loads(doctor.render_json(report, ctx))


def do_capabilities(
    config_dir: str | None = None,
    *,
    probe_network: bool = False,
) -> dict:
    """The ``capabilities`` payload: what this machine can actually run —
    scheduler/daemon/net probing with the corporate fail-closed default.
    ``probe_network=True`` performs the same single outbound connect the
    CLI documents; False keeps every probe local."""
    from devin_doctor import capabilities

    return capabilities.collect_profile(
        config_dir=Path(config_dir) if config_dir else default_config_dir(),
        probe_network=probe_network,
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
            "pip install 'devin-doctor[mcp]'"
        ) from e


def build_server():
    """Build the MCP server app with every devin-doctor tool registered."""
    server = _make_app("devin-doctor")

    @server.tool()
    def doctor_check(
        data_dir: str = "",
        config_dir: str = "",
        cwd: str = "",
        stale_days: int = 30,
        offline: bool = False,
    ) -> dict:
        """Diagnose the local Devin installation — stores, schema versions,
        data health, config sanity, disk usage — and return the same JSON
        as ``devin-doctor check --json``. Read-only: never modifies state,
        only reports findings with fix suggestions. ``offline=True`` skips
        the updates check's network fetch and PATH probes.
        """
        try:
            return do_check(
                data_dir=data_dir or None,
                config_dir=config_dir or None,
                cwd=cwd or None,
                stale_days=stale_days,
                offline=offline,
            )
        except Exception as error:  # noqa: BLE001 — tool boundary must not raise
            return _err(error)

    @server.tool()
    def doctor_capabilities(config_dir: str = "") -> dict:
        """Report what this machine can do for the Devin ecosystem —
        scheduler/daemon/net capability profile, same JSON as
        ``devin-doctor capabilities``. Read-only and local-only: the
        CLI's ``--probe-network`` flag is deliberately not exposed, so
        this tool never makes a network call.
        """
        try:
            return do_capabilities(config_dir=config_dir or None)
        except Exception as error:  # noqa: BLE001 — tool boundary must not raise
            return _err(error)

    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
