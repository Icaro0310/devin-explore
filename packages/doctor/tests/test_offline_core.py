"""Offline-core guard: the core must not open sockets.

Release checklist item: "no-network core test (CI): fails if the core
opens a socket". An autouse fixture monkeypatches ``socket.socket.connect``,
``socket.socket.connect_ex`` and ``socket.create_connection`` to raise
``OfflineCoreError`` for the duration of every test in this file, then the
tests run the repo's core operations end to end. This is a guard, not a
mock: any in-process network access fails the suite.

Intentional online paths are excluded by design: ``capabilities
--probe-network`` performs one documented TCP connect and is an explicit
opt-in flag — the capability profile is fail-closed without it, which is
exactly what this guard verifies.

Opt-out: mark a test ``@pytest.mark.network`` to run it without the socket
block (reserved for tests that intentionally exercise the network).

Run with:
``PYTHONPATH=src:../devin-internals-spec/src python -m pytest tests/test_offline_core.py``
(this repo imports ``devin_internals`` from the sibling checkout).
"""

from __future__ import annotations

import socket

import pytest
from devin_doctor.cli import main


class OfflineCoreError(RuntimeError):
    """Raised when core code tries to open a network connection."""


def _offline_fail(*args, **kwargs):
    raise OfflineCoreError("core opened a socket during the offline-core test")


@pytest.fixture(autouse=True)
def _block_sockets(request, monkeypatch):
    """Block all outbound sockets; opt out with ``@pytest.mark.network``."""
    if request.node.get_closest_marker("network"):
        return
    monkeypatch.setattr(socket.socket, "connect", _offline_fail)
    monkeypatch.setattr(socket.socket, "connect_ex", _offline_fail)
    monkeypatch.setattr(socket, "create_connection", _offline_fail)


def test_socket_block_is_active():
    """Sanity check: the guard itself raises on any connect attempt."""
    with pytest.raises(OfflineCoreError):
        socket.create_connection(("127.0.0.1", 1), timeout=0.01)
    with pytest.raises(OfflineCoreError):
        socket.socket().connect(("127.0.0.1", 1))


def test_check_scan_offline(ctx, capsys):
    """`check` over the healthy conftest data dir — all checks local."""
    rc = main([
        "check",
        "--data-dir", str(ctx.data_dir),
        "--cwd", str(ctx.cwd),
        "--stale-days", "3650",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    for check_id in ("devin-cli", "devin-chat", "health-of-data", "config"):
        assert check_id in out


def test_plan_on_broken_dir_offline(broken_ctx, capsys):
    """`plan` emits a remediation plan for the broken fixture, offline.

    ``plan`` is informational and always exits 0 — the broken fixture just
    guarantees there are WARN/FAIL findings to plan around.
    """
    rc = main([
        "plan",
        "--data-dir", str(broken_ctx.data_dir),
        "--cwd", str(broken_ctx.cwd),
    ])
    out = capsys.readouterr().out
    assert rc == 0  # a plan is informational — not a verdict
    assert "nothing below was executed" in out
    assert out.strip()


def test_capabilities_is_offline_by_default(capsys):
    """The capability profile never touches the network unless the
    explicit ``--probe-network`` opt-in is given."""
    import json

    rc = main(["capabilities"])
    assert rc == 0
    profile = json.loads(capsys.readouterr().out)
    assert isinstance(profile, dict) and profile
