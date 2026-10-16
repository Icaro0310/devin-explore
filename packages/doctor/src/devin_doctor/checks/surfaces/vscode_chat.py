"""Surface check — vscode-chat: ``chatSessions/*.json{,l}``.

User dirs (spec §6.5): ``<base>/<Variant>/User`` for ``Code`` and
``Code - Insiders`` plus the remote ``~/.vscode-server/data/User`` — every
existing dir merges. Stores live in
``workspaceStorage/<wsDir>/chatSessions/`` and
``globalStorage/{emptyWindow,transferred}ChatSessions/``; a ``.jsonl``
mutation log wins over a same-id ``.json`` snapshot.

Ladder: present → readable → schema → sane. Documents are parsed for
shape only — ``requests`` content is never inspected.
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


def _chat_files(user_dirs: Iterable[Path]) -> list[tuple[Path, int]]:
    """``(file, size)`` per chat store — ``.jsonl`` wins a same-id pair."""
    out: list[Path] = []

    def collect(chat_dir: Path) -> None:
        if not chat_dir.is_dir():
            return
        try:
            entries = sorted(chat_dir.iterdir())
        except OSError:
            return
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

    for user_dir in user_dirs:
        for name in _GLOBAL_DIRS:
            collect(user_dir / "globalStorage" / name)
        ws_root = user_dir / "workspaceStorage"
        if ws_root.is_dir():
            try:
                ws_entries = sorted(ws_root.iterdir())
            except OSError:
                ws_entries = []
            for entry in ws_entries:
                if entry.is_dir():
                    collect(entry / "chatSessions")

    files: list[tuple[Path, int]] = []
    for path in out:
        try:
            files.append((path, path.stat().st_size))
        except OSError:
            files.append((path, -1))
    return files


def _readable(path: Path) -> tuple[Any | None, bool]:
    """``(doc-or-first-record, ok)`` — ``.json`` snapshots parse whole,
    ``.jsonl`` logs must have every terminated line parse."""
    if path.name.endswith(".jsonl"):
        scan = scan_jsonl(path)
        return scan.first, scan.ok
    doc = json_document(path)
    return doc, doc is not None


def run(ctx: Context) -> list[Finding]:
    del ctx  # surface roots are machine-global — the ctx dirs do not apply
    user_dirs = default_vscode_chat_dirs()
    if not user_dirs:
        return []  # not detected — no VS Code variant stores chats here
    files = _chat_files(user_dirs)
    if not files:
        return [
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(user_dirs)} user dir(s) present, no chatSessions — "
                "installed but unused",
            )
        ]

    unreadable: list[str] = []
    unknown: list[str] = []
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
        head, ok = _readable(path)
        if not ok:
            unreadable.append(path.name)
            continue
        if path.name.endswith(".jsonl"):
            if not isinstance(head, dict) or not isinstance(
                head.get("kind"), int
            ) or head["kind"] not in _MUTATION_KINDS:
                unknown.append(path.name)
        elif not isinstance(head, dict) or not (_KNOWN_KEYS & set(head)):
            unknown.append(path.name)

    findings: list[Finding] = []
    if unreadable:
        shown = ", ".join(unreadable[:3]) + (
            " …" if len(unreadable) > 3 else ""
        )
        findings.append(
            Finding(
                CHECK_ID,
                Status.FAIL,
                f"{len(unreadable)}/{len(files)} chat store(s) unreadable "
                f"({shown})",
                fix="Store-corrupt — a .json snapshot does not parse or a "
                ".jsonl log's first line is not JSON.",
            )
        )
    else:
        findings.append(
            Finding(
                CHECK_ID,
                Status.PASS,
                f"{len(files) - empty} chat session file(s), "
                f"{human_size(total)} — all readable",
            )
        )
    if unknown:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{len(unknown)} store(s) with unrecognized shape "
                "(schema-unknown)",
                fix="Expected a session document or mutation kinds 0–3 — "
                "written by a VS Code version the spec does not know.",
            )
        )
    if len(files) - empty == 0:
        findings.append(
            Finding(
                CHECK_ID,
                Status.WARN,
                f"{empty} empty chat session file(s) — installed but "
                "unused",
            )
        )
    return findings
