"""Shared plumbing for the surface checks.

Same read contract as devin-search (SESSION-SURFACES §3): SQLite opens
``mode=ro`` with an ``immutable=1`` retry and a constructed-but-failed
handle is closed before retrying; files are read, never written. Findings
carry counts, sizes and paths — never row content.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, NamedTuple

SQLITE_MAGIC = b"SQLite format 3\x00"


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open a source SQLite db read-only: ``mode=ro`` retried with
    ``immutable=1``; a constructed-but-failed handle is closed first."""
    exc: sqlite3.Error | None = None
    for query in ("mode=ro", "mode=ro&immutable=1"):
        try:
            con = sqlite3.connect(f"file:{path.as_posix()}?{query}", uri=True)
        except sqlite3.Error as error:
            exc = error
            continue
        try:
            con.row_factory = sqlite3.Row
            con.execute("SELECT 1").fetchone()  # force the lazy open
            return con
        except sqlite3.Error as error:
            exc = error
            con.close()
    raise exc if exc is not None else sqlite3.Error(f"{path}: unreadable")


def is_lock_error(exc: BaseException) -> bool:
    """``True`` for SQLITE_BUSY — a live writer holds the store (the
    agent is running), a transient state, not corruption."""
    return isinstance(exc, sqlite3.Error) and "locked" in str(exc).lower()


def has_sqlite_magic(path: Path) -> bool:
    """The 16-byte SQLite header — the cheapest corruption gate."""
    try:
        with path.open("rb") as fh:
            return fh.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def table_names(con: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def row_count(con: sqlite3.Connection, table: str) -> int:
    """``COUNT(*)`` of one table — metadata only, no content columns."""
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


class JsonlScan(NamedTuple):
    """Result of streaming a JSONL log through ``json.loads``."""

    first: Any | None  # first decoded non-blank record (None if none parsed)
    records: int  # decoded non-blank record count
    ok: bool  # False when a terminated line failed to parse


def scan_jsonl(path: Path) -> JsonlScan:
    """Validate every non-blank line of a JSONL log — not just the head.

    A log is only 'readable' when every terminated line parses to a real
    record; the scan bails on the first failure (``ok=False`` →
    store-corrupt FAIL upstream). Two nuances:

    - ``null`` lines: ``null`` is valid JSON but never a valid record —
      no real writer emits it — so it counts as malformed (``ok=False``),
      not as an absent head.
    - Unterminated tail: a final line without ``\\n`` that fails to parse
      is treated as a transient in-progress append — excluded from
      ``records``, not flagged.
    """
    first: Any | None = None
    n = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except (json.JSONDecodeError, RecursionError):
                    if not line.endswith("\n"):
                        break  # unterminated tail — writer mid-append
                    return JsonlScan(first, n, False)
                if record is None:
                    # `null` record — foreign content, not a valid record.
                    return JsonlScan(first, n, False)
                if n == 0:  # record-position gate: `first` may BE null-free
                    first = record
                n += 1
    except OSError:
        return JsonlScan(first, n, False)
    return JsonlScan(first, n, True)


def json_document(path: Path) -> Any | None:
    """Parse a whole-file JSON document; ``None`` on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
