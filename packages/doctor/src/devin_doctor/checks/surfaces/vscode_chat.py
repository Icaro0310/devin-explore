"""Surface check — vscode-chat: ``chatSessions/*.json{,l}``.

User dirs (spec §6.5): ``<base>/<Variant>/User`` for ``Code`` and
``Code - Insiders`` plus the remote ``~/.vscode-server/data/User`` — every
existing dir merges. Stores live in
``workspaceStorage/<wsDir>/chatSessions/`` and
``globalStorage/{emptyWindow,transferred}ChatSessions/``; a ``.jsonl``
mutation log wins over a same-id ``.json`` snapshot.

Ladder (SURFACE-HEALTH §4.5): present → readable → schema → sane, findings
tagged with their rung + §5 code. The ``present`` anchor is a
``chatSessions`` container existing at all — a ``User`` dir without one
is ``store-missing``. Documents are parsed for shape only —
``requests`` content is never inspected.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from devin_doctor.checks.surfaces._common import (
    json_document,
    scan_jsonl,
)
from devin_doctor.model import Context, Finding, Status
from devin_doctor.paths import default_vscode_chat_dirs, human_size

CHECK_ID = "vscode-chat"

_GLOBAL_DIRS = ("emptyWindowChatSessions", "transferredChatSessions")
# Keys that mark a recognizable VS Code chat session document (§6.5).
_KNOWN_KEYS = frozenset(
    {
        "requests",
        "customTitle",
        "title",
        "creationDate",
        "lastMessageDate",
        "version",
        "requesterUsername",
        "responderUsername",
        "sessionId",
        "initialLocation",
        "isImported",
        "agent",
    }
)
_MUTATION_KINDS = frozenset({0, 1, 2, 3})


def _chat_containers(user_dirs: Iterable[Path]) -> list[Path]:
    """Every existing ``chatSessions`` container across the user dirs."""
    containers: list[Path] = []
    for user_dir in user_dirs:
        for name in _GLOBAL_DIRS:
            path = user_dir / "globalStorage" / name
            if path.is_dir():
                containers.append(path)
        ws_root = user_dir / "workspaceStorage"
        if ws_root.is_dir():
            try:
                ws_entries = sorted(ws_root.iterdir())
            except OSError:
                ws_entries = []
            for entry in ws_entries:
                candidate = entry / "chatSessions"
                if entry.is_dir() and candidate.is_dir():
                    containers.append(candidate)
    return containers


def _chat_files(containers: Iterable[Path]) -> list[tuple[Path, int]]:
    """``(file, size)`` per chat store — ``.jsonl`` wins a same-id pair."""
    out: list[Path] = []
    for chat_dir in containers:
        try:
            entries = sorted(chat_dir.iterdir())
        except OSError:
            continue
        by_id: dict[str, Path] = {}
        for entry in entries:
            if not entry.is_file():
                continue
            if entry.name.endswith(".jsonl"):
                sid = entry.name[: -len(".jsonl")]
            elif entry.name.endswith(".json"):
                sid = entry.name[: -len(".json")]
            else:
                continue
            if sid and (sid not in by_id or entry.name.endswith(".jsonl")):
                by_id[sid] = entry
        out.extend(by_id[sid] for sid in sorted(by_id))

    files: list[tuple[Path, int]] = []
    for path in out:
        try:
            files.append((path, path.stat().st_size))
        except OSError:
            files.append((path, -1))
    return files


def _parsed(path: Path) -> tuple[Any | None, bool]:
    """``(doc-or-first-record, ok)`` — ``.json`` snapshots parse whole,
    ``.jsonl`` logs must have every terminated line parse (a dangling
    unterminated tail is a writer mid-append, not corruption)."""
    if path.name.endswith(".jsonl"):
        scan = scan_jsonl(path)
        return scan.first, scan.ok
    doc = json_document(path)
    return doc, doc is not None


def _recognizable(path: Path, doc: Any) -> bool:
    """§4.5 schema rung — a ``.json`` snapshot must carry a known session
    key, a ``.jsonl`` log's first record a mutation ``kind`` 0–3."""
    if not isinstance(doc, dict):
        return False
    if path.name.endswith(".jsonl"):
        kind = doc.get("kind")
        return isinstance(kind, int) and kind in _MUTATION_KINDS
    return bool(_KNOWN_KEYS & set(doc))


def run(ctx: Context) -> list[Finding]:
    del ctx  # surface roots are machine-global — the ctx dirs do not apply
    user_dirs = default_vscode_chat_dirs()
    if not user_dirs:
        return [
            Finding(
                CHECK_ID,
                Status.PASS,
                "no VS Code user dir — vscode-chat not installed",
                rung="present",
                note="not-installed",
            )
        ]
    containers = _chat_containers(user_dirs)
    if not containers:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(user_dirs)} user dir(s) present, no chatSessions "
                "container — installed but unused",
                fix="VS Code ran but never stored a chat session here — "
                "safe to ignore.",
                rung="present",
                code="store-missing",
            )
        ]
    files = _chat_files(containers)
    findings = [
        Finding(
            CHECK_ID,
            Status.PASS,
            f"{len(containers)} chatSessions container(s) — "
            f"{len(files)} session file(s)",
            rung="present",
        )
    ]
    if not files:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                "chatSessions containers present, 0 session files — "
                "installed but unused",
                rung="sane",
                code="store-empty",
            )
        )
        return findings

    unreadable: list[str] = []
    docs: dict[Path, Any] = {}
    total = 0
    empty = 0
    for path, size in files:
        if size <= 0:
            if size == 0:
                empty += 1
            else:
                unreadable.append(path.name)
            continue
        total += size
        doc, ok = _parsed(path)
        if not ok:
            unreadable.append(path.name)
            continue
        docs[path] = doc

    if unreadable:
        shown = ", ".join(unreadable[:3]) + (
            " …" if len(unreadable) > 3 else ""
        )
        findings.append(
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"{len(unreadable)}/{len(files)} chat store(s) "
                f"unreadable ({shown})",
                fix="Store-corrupt — a .json snapshot does not parse or "
                "a .jsonl log has a malformed terminated line.",
                rung="readable",
                code="store-corrupt",
            )
        )
        return findings
    if not docs:
        # every artifact is a zero-byte shell — sane owns emptiness
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{empty} zero-byte chat session file(s) — empty-store",
                fix="Never-written session files — safe to delete or "
                "ignore.",
                rung="sane",
                code="empty-store",
            )
        )
        return findings
    findings.append(
        Finding(
            CHECK_ID,
            Status.PASS,
            f"{len(docs)} chat session file(s), {human_size(total)} — "
            "all readable",
            rung="readable",
        )
    )

    unknown = [
        p.name for p, d in docs.items() if not _recognizable(p, d)
    ]
    if unknown:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(unknown)} store(s) with unrecognized shape "
                "(schema-unknown)",
                fix="Expected a session document or mutation kinds 0–3 "
                "— written by a VS Code version the spec does not know.",
                rung="schema",
                code="schema-unknown",
            )
        )
    else:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                "every store parses to a recognizable session shape",
                rung="schema",
            )
        )

    if empty:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{empty} zero-byte chat session file(s) — empty-store",
                fix="Never-written session files — safe to delete or "
                "ignore.",
                rung="sane",
                code="empty-store",
            )
        )
    else:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                f"{len(docs)} chat session file(s) in use",
                rung="sane",
            )
        )
    return findings
