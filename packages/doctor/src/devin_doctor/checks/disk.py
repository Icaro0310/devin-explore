"""Check 5 — disk: total data-dir footprint, largest DB files, and
``acp-messages`` accumulation (count/bytes/mtime span)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from devin_doctor.model import Context, Finding, Status
from devin_doctor.paths import human_size, locate_stores

CHECK_ID = "disk"

_TOP_N = 3
# Hard bounds for the data-dir walk. Real data dirs hold millions of files
# under Cache/blob_storage, and an unbounded rglob + per-file stats made
# this check take minutes; past either limit the scan stops and every
# finding reports its numbers as lower bounds ("≥", "(scan capped)").
_MAX_FILES = 250_000
_MAX_SECONDS = 45.0
_DB_SUFFIXES = (".db", ".vscdb")


@dataclass(frozen=True)
class _ScannedFile:
    """A file found by the walk, with its stat fields captured exactly once."""

    path: Path
    size: int
    suffix: str
    mtime: float


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _walk_files(root: Path) -> tuple[list[_ScannedFile], bool]:
    """Single-pass scandir walk of ``root``.

    Each regular file is stat'ed exactly once via its ``DirEntry`` and the
    record is shared by all three findings. Returns ``(files, capped)`` —
    ``capped`` is True when the walk stopped early at ``_MAX_FILES`` files
    or ``_MAX_SECONDS`` elapsed, whichever came first, so totals computed
    from the result are lower bounds. Symlinks are not followed.
    """
    files: list[_ScannedFile] = []
    capped = False
    deadline = time.monotonic() + _MAX_SECONDS
    pending = [str(root)]
    while pending and not capped:
        if time.monotonic() >= deadline:
            capped = True  # queued dirs (incl. empty ones) also stop
            break
        current = pending.pop()
        try:
            scan = os.scandir(current)
        except OSError:
            continue  # unreadable dir — skip it the way rglob would
        with scan:
            for entry in scan:
                if len(files) >= _MAX_FILES or time.monotonic() >= deadline:
                    capped = True
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        files.append(
                            _ScannedFile(
                                path=Path(entry.path),
                                size=st.st_size,
                                suffix=os.path.splitext(entry.name)[1],
                                mtime=st.st_mtime,
                            )
                        )
                except OSError:
                    continue  # vanished between scandir and stat
    return files, capped


def _total_finding(ctx: Context, total: int, n_files: int, capped: bool) -> Finding:
    if capped:
        message = (
            f"data dir ≥ {human_size(total)} across ≥{n_files} files (scan capped)"
        )
    else:
        message = f"data dir total: {human_size(total)} across {n_files} files"
    if total > ctx.total_warn_bytes:
        return Finding(
            CHECK_ID,
            Status.WARN,
            f"{message} (>{human_size(ctx.total_warn_bytes)})",
            fix="Largest offenders are usually acp-messages DBs and Cache/"
            "blob_storage — archive or prune old session data.",
        )
    if capped:
        # A capped walk can prove WARN (observed bytes over threshold) but
        # never PASS — the unread remainder may hold the bulk.
        return Finding(
            CHECK_ID,
            Status.WARN,
            f"{message} — remainder unread, total unknown",
            fix="Scan stopped at the file/time bound — inspect the data dir "
            "directly (Cache/blob_storage are the usual bulk).",
        )
    return Finding(CHECK_ID, Status.PASS, message)


def _largest_finding(ctx: Context, files: list[_ScannedFile], capped: bool) -> Finding:
    marker = " (scan capped)" if capped else ""
    dbs = [f for f in files if f.suffix in _DB_SUFFIXES]
    top = sorted(dbs, key=lambda f: f.size, reverse=True)[:_TOP_N]
    if not top:
        if capped:
            return Finding(
                CHECK_ID,
                Status.WARN,
                f"no .db/.vscdb stores in scanned portion{marker} — "
                "remainder unread",
                fix="Scan stopped at the file/time bound — inspect the data "
                "dir directly for oversized stores.",
            )
        return Finding(
            CHECK_ID,
            Status.PASS,
            f"no .db/.vscdb stores found under data dir{marker}",
        )
    listing = ", ".join(f"{f.path.name} ({human_size(f.size)})" for f in top)
    oversized = [f for f in top if f.size > ctx.store_warn_bytes]
    if oversized:
        return Finding(
            CHECK_ID,
            Status.WARN,
            f"largest stores: {listing} — over "
            f"{human_size(ctx.store_warn_bytes)}{marker}",
            fix="Consider exporting old sessions (devin-history) and vacuuming "
            "— an offline VACUUM is planned for --fix (M2).",
        )
    if capped:
        return Finding(
            CHECK_ID,
            Status.WARN,
            f"largest stores in scanned portion: {listing}{marker} — "
            "remainder unread",
            fix="Scan stopped at the file/time bound — inspect the data dir "
            "directly for oversized stores.",
        )
    return Finding(CHECK_ID, Status.PASS, f"largest stores: {listing}{marker}")


def _acp_finding(ctx: Context, capped: bool) -> Finding:
    # locate_stores is a targeted glob outside the bounded walk and every db
    # is stat'ed fresh here — acp numbers stay exact (never lower bounds),
    # and live growth during the walk cannot hide a threshold breach. The
    # capped marker still rides every message for scan transparency.
    marker = " (scan capped)" if capped else ""
    stores = locate_stores(ctx.data_dir)
    if not stores.acp_dbs:
        return Finding(
            CHECK_ID,
            Status.PASS,
            f"acp-messages: no accumulation (0 db files){marker}",
        )
    total = 0
    mtimes: list[float] = []
    for path in stores.acp_dbs:
        try:
            st = path.stat()
        except OSError:
            continue  # vanished between glob and stat
        total += st.st_size
        mtimes.append(st.st_mtime)
    if not mtimes:
        return Finding(
            CHECK_ID,
            Status.PASS,
            f"acp-messages: no accumulation (0 db files){marker}",
        )
    mtimes.sort()
    span = f"{_day(mtimes[0])} .. {_day(mtimes[-1])}"
    message = (
        f"acp-messages accumulation: {len(stores.acp_dbs)} db(s), "
        f"{human_size(total)}, span {span}{marker}"
    )

    if total > ctx.acp_warn_bytes:
        return Finding(
            CHECK_ID,
            Status.WARN,
            f"{message} (>{human_size(ctx.acp_warn_bytes)})",
            fix="Per-session acp DBs accumulate forever — archive sessions you "
            "no longer need.",
        )
    return Finding(CHECK_ID, Status.PASS, message)


def run(ctx: Context) -> list[Finding]:
    if not ctx.data_dir.is_dir():
        return [
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"Devin data dir not found: {ctx.data_dir}",
                fix="Pass --data-dir pointing at the Devin data directory.",
            )
        ]
    files, capped = _walk_files(ctx.data_dir)
    total = sum(f.size for f in files)
    return [
        _total_finding(ctx, total, len(files), capped),
        _largest_finding(ctx, files, capped),
        _acp_finding(ctx, capped),
    ]
