"""Locate the task-ledger store without importing devin-memory.

Mirrors ``devin_memory.ledger.storage.ledger_dir`` precedence so the
check can report 'not-installed' vs 'missing store' before deciding
whether the optional dependency is even needed.
"""

from __future__ import annotations

import os
from pathlib import Path


def ledger_store_dir() -> Path:
    env = os.environ.get("DEVIN_LEDGER_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "devin-memory" / "ledger"
