"""Tests for the optional task-ledger doctor check."""

from __future__ import annotations

import sys
import types

from devin_doctor.checks import ledger
from devin_doctor.model import Context, Status


def _ctx(tmp_path) -> Context:
    return Context(data_dir=tmp_path, cwd=tmp_path,
                   state_dir=tmp_path / "state", now_ms=0)


def test_not_installed_passes_with_note(tmp_path, monkeypatch):
    """No store, no dep → PASS note=not-installed (hermetic default)."""
    monkeypatch.setenv("DEVIN_LEDGER_DIR", str(tmp_path / "nope"))
    for m in ("devin_memory", "devin_memory.ledger",
              "devin_memory.ledger.integrations"):
        monkeypatch.delitem(sys.modules, m, raising=False)
    # Ensure the import really fails even if devin-memory is installed.
    monkeypatch.setitem(sys.modules, "devin_memory", None)
    findings = ledger.run(_ctx(tmp_path))
    assert len(findings) == 1
    assert findings[0].status is Status.PASS
    assert findings[0].note == "not-installed"


def test_store_without_dep_warns(tmp_path, monkeypatch):
    """Ledger store exists but devin-memory missing → WARN + install hint."""
    store = tmp_path / "ledger"
    store.mkdir()
    (store / "ledger.jsonl").write_text('{"op":"add"}\n', encoding="utf-8")
    monkeypatch.setenv("DEVIN_LEDGER_DIR", str(store))
    monkeypatch.setitem(sys.modules, "devin_memory", None)
    findings = ledger.run(_ctx(tmp_path))
    assert findings[0].status is Status.WARN
    assert "devin-doctor[ledger]" in (findings[0].fix or "")


def test_healthy_ledger_passes(tmp_path, monkeypatch):
    store = tmp_path / "ledger"
    store.mkdir()
    (store / "ledger.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("DEVIN_LEDGER_DIR", str(store))

    pkg = types.ModuleType("devin_memory")
    sub = types.ModuleType("devin_memory.ledger")
    integ = types.ModuleType("devin_memory.ledger.integrations")

    class _L:
        def __init__(self, path=None):
            self.dir = path

    sub.Ledger = _L
    integ.check = lambda ledger: (True, [])
    pkg.ledger = sub
    monkeypatch.setitem(sys.modules, "devin_memory", pkg)
    monkeypatch.setitem(sys.modules, "devin_memory.ledger", sub)
    monkeypatch.setitem(sys.modules, "devin_memory.ledger.integrations", integ)

    findings = ledger.run(_ctx(tmp_path))
    assert findings[0].status is Status.PASS
    assert "healthy" in findings[0].message


def test_corrupt_ledger_fails(tmp_path, monkeypatch):
    store = tmp_path / "ledger"
    store.mkdir()
    (store / "ledger.jsonl").write_text("not json\n", encoding="utf-8")
    monkeypatch.setenv("DEVIN_LEDGER_DIR", str(store))

    pkg = types.ModuleType("devin_memory")
    sub = types.ModuleType("devin_memory.ledger")
    integ = types.ModuleType("devin_memory.ledger.integrations")

    class _L:
        def __init__(self, path=None):
            self.dir = path

    sub.Ledger = _L
    integ.check = lambda ledger: (False, ["ledger.jsonl corrupt at line 1"])
    pkg.ledger = sub
    monkeypatch.setitem(sys.modules, "devin_memory", pkg)
    monkeypatch.setitem(sys.modules, "devin_memory.ledger", sub)
    monkeypatch.setitem(sys.modules, "devin_memory.ledger.integrations", integ)

    findings = ledger.run(_ctx(tmp_path))
    assert findings[0].status is Status.FAIL
    assert "ledger doctor" in (findings[0].fix or "")
