"""Offline-core guard: the core must not open sockets.

Release checklist item: "no-network core test (CI): fails if the core
opens a socket". An autouse fixture monkeypatches ``socket.socket.connect``,
``socket.socket.connect_ex`` and ``socket.create_connection`` to raise
``OfflineCoreError`` for the duration of every test in this file, then the
tests run the repo's core operations end to end. This is a guard, not a
mock: any in-process network access fails the suite.

Intentional online paths are excluded by design: none exist in the core —
``build``/``query``/``export``/``view`` are read-only on local SQLite
stores and write local files. Only in-process sockets are blocked here.

Opt-out: mark a test ``@pytest.mark.network`` to run it without the socket
block (reserved for tests that intentionally exercise the network).

Run with:
``PYTHONPATH=src:../devin-internals-spec/src python -m pytest tests/test_offline_core.py``
(this repo imports ``devin_internals`` from the sibling checkout).
"""

from __future__ import annotations

import json
import socket

import pytest
from devin_graph.cli import main
from devin_internals.schema import LATEST_KNOWN_SCHEMA


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


def test_build_and_export_offline(sessions_db, tmp_path, capsys):
    """build sessions.db → graph.db, then export — all offline."""
    graph = tmp_path / "graph.db"

    assert main(["build", "--sessions-db", str(sessions_db), "--vscdb",
                 "none", "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "extracted: 3" in out
    assert graph.exists()

    assert main(["export", "--format", "json", "--graph", str(graph)]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["nodes"]) > 0 and len(data["edges"]) > 0
    # fixture emits the installed spec's latest schema (v17 at 0.3.1,
    # v18 at 0.3.3) — pin to the constant, not a literal
    assert data["meta"]["schema_version"] == LATEST_KNOWN_SCHEMA


def test_query_offline(sessions_db, tmp_path, capsys):
    graph = tmp_path / "graph.db"
    assert main(["build", "--sessions-db", str(sessions_db), "--vscdb",
                 "none", "--graph", str(graph)]) == 0
    capsys.readouterr()

    assert main(["query", "shared-files", "--graph", str(graph),
                 "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["files"]
