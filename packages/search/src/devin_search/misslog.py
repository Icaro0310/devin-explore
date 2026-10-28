"""Local query log — the objective trigger for SE-1 (semantic search).

Every ``query`` run appends one JSON line to ``<index>.queries.jsonl``:
``{ts, term, filters, hits}`` — never message content, never paths from
results. ``misses`` aggregates it into a per-month count of searches
that returned zero hits; if lexical search misses often, that is the
evidence that a semantic index (SE-1) is worth building.

The log lives next to the index file, is append-only, and stays local.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path


def log_path(index_path: str | Path) -> Path:
    return Path(str(index_path) + ".queries.jsonl")


def record(
    index_path: str | Path,
    term: str,
    hits: int,
    *,
    role: str | None = None,
    project: str | None = None,
    since: str | None = None,
    tool: str | None = None,
    now: float | None = None,
) -> None:
    entry = {
        "ts": int(now if now is not None else time.time()),
        "term": term,
        "hits": hits,
    }
    if role:
        entry["role"] = role
    if project:
        entry["project"] = project
    if since:
        entry["since"] = since
    if tool:
        entry["tool"] = tool
    p = log_path(index_path)
    try:
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass  # logging is advisory — never break a query over the log


def iter_log(index_path: str | Path):
    p = log_path(index_path)
    if not p.is_file():
        return
    with p.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def summarize(index_path: str | Path) -> dict:
    """Per-month totals + the most-missed terms."""
    per_month: dict[str, dict[str, int]] = {}
    miss_terms: Counter[str] = Counter()
    total = misses = 0
    for e in iter_log(index_path) or ():
        total += 1
        month = time.strftime("%Y-%m", time.localtime(e.get("ts", 0)))
        bucket = per_month.setdefault(month, {"queries": 0, "zero_hit": 0})
        bucket["queries"] += 1
        if e.get("hits", 0) == 0:
            misses += 1
            bucket["zero_hit"] += 1
            term = e.get("term", "")
            if term:
                miss_terms[term] += 1
    return {
        "log": str(log_path(index_path)),
        "total_queries": total,
        "zero_hit": misses,
        "zero_hit_rate": round(misses / total, 3) if total else 0.0,
        "per_month": per_month,
        "top_missed_terms": miss_terms.most_common(10),
    }
