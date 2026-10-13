"""Result model shared by every devin-doctor check.

A check is a function ``run(ctx) -> list[Finding]``. Each finding carries a
``PASS``/``WARN``/``FAIL`` status, a one-line message and an optional ``fix``
suggestion. Findings never contain row content from Devin's stores — counts,
sizes and paths only.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Status(Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Finding:
    check: str
    status: Status
    message: str
    fix: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "check": self.check,
            "status": self.status.value,
            "message": self.message,
            "fix": self.fix,
        }


@dataclass
class Context:
    """Inputs every check shares. ``now_ms`` is injectable so tests can pin
    'today' against deterministic fixture timestamps."""

    data_dir: Path
    cwd: Path
    stale_days: int = 30
    now_ms: int | None = None
    acp_warn_bytes: int = 512 * 1024**2
    store_warn_bytes: int = 256 * 1024**2
    total_warn_bytes: int = 4 * 1024**3
    config_dir: Path | None = None
    # Per-call offline switch for the updates check; complements the
    # process-wide DEVIN_DOCTOR_OFFLINE env so the MCP server can scope
    # it to one request instead of mutating os.environ.
    offline: bool = False

    def now(self) -> int:
        if self.now_ms is not None:
            return self.now_ms
        return int(time.time() * 1000)


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            s.value.lower(): sum(1 for f in self.findings if f.status is s)
            for s in Status
        }

    def check_status(self) -> dict[str, str]:
        """Worst status per check id, in first-seen check order."""
        order = {Status.PASS: 0, Status.WARN: 1, Status.FAIL: 2}
        out: dict[str, str] = {}
        for f in self.findings:
            cur = out.get(f.check)
            if cur is None or order[f.status] > order[Status(cur)]:
                out[f.check] = f.status.value
        return out

    @property
    def overall(self) -> Status:
        if any(f.status is Status.FAIL for f in self.findings):
            return Status.FAIL
        if any(f.status is Status.WARN for f in self.findings):
            return Status.WARN
        return Status.PASS


def platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"
