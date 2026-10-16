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
from typing import Any

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


def first_json_line(path: Path) -> Any | None:
    """Parse the first non-blank line of a JSONL file.

    Returns the decoded value, or ``None`` when the file is unreadable,
    empty or the line is not JSON — callers cannot distinguish the three,
    which is fine: all of them fail the readable rung the same way.
    """
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    return json.loads(stripped)
    except (OSError, json.JSONDecodeError):
        return None
    return None


def json_document(path: Path) -> Any | None:
    """Parse a whole-file JSON document; ``None`` on any failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def line_count(path: Path) -> int:
    """Non-blank line count of a JSONL file — the cheap event count."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if line.strip())
    except OSError:
        return 0
