"""Devin Desktop GUI transcripts — ``User/acp-messages/*.db``.

Each ``*.db`` is one Desktop chat session's transcript: a ``meta`` table
(schema version, title inside an ``info`` JSON blob, message_count) and
``messages(position, kind, payload)``. ``state.vscdb`` binds a session to
its workspace under a generated slug; correlation to a transcript is
best-effort (meta ``*session_id*`` value == slug → filename stem ==
slug → meta title == binding label), mirroring the needle-search
devin-search applies to the same meta key space.

Payload shapes are undocumented upstream (SCHEMA.md: *unstable*) — text
is pulled from the known keys first and a recursive string walk as the
fallback, the same tolerance doctrine as ``messages.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from devin_internals.parsers import AcpMessagesStore

MAX_FILES = 500  # acp-messages/*.db files scanned at most per export


@dataclass(frozen=True)
class TranscriptMessage:
    role: str  # normalized: user / assistant / thought / tool / system / <raw>
    text: str


@dataclass(frozen=True)
class AcpTranscript:
    """One acp-messages db decoded for export."""

    path: Path
    session_id: str  # meta *session_id* value, else the filename stem
    title: str | None
    message_count: int | None
    truncated: bool
    marker: int  # file mtime ms — the idempotency change signal
    messages: tuple[TranscriptMessage, ...] = field(default_factory=tuple)


class RedactionError(RuntimeError):
    """Raised when redaction cannot run — the note must not be written."""


_ROLE_ALIASES = {
    "user": "user",
    "user_message": "user",
    "assistant": "assistant",
    "agent": "assistant",
    "agent_message": "assistant",
    "agent_thought": "thought",
    "thought": "thought",
    "tool": "tool",
    "tool_call": "tool",
    "tool_result": "tool",
    "system": "system",
}


def _normalize_role(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        return "unknown"
    # acp kinds may be namespaced ("fixture.user_message"); last segment wins.
    short = raw.rsplit(".", 1)[-1].lower()
    return _ROLE_ALIASES.get(short, short)


def _block_text(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        inner = block.get("content")
        if isinstance(inner, dict):
            inner = inner.get("text")
        if isinstance(inner, str):
            return inner
        text = block.get("text")
        if isinstance(text, str):
            return text
    return ""


def _walk_strings(value: Any, out: list[str], _depth: int = 0) -> None:
    if _depth > 8:
        return
    if isinstance(value, str):
        if value.strip():
            out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _walk_strings(v, out, _depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _walk_strings(v, out, _depth + 1)


def _payload_text(d: dict[str, Any]) -> str:
    for key in ("content", "text", "message"):
        value = d.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts = [_block_text(b) for b in value]
            text = "\n".join(p for p in parts if p)
            if text:
                return text
    parts: list[str] = []
    _walk_strings(d, parts)
    return "\n".join(parts)


def _message(kind: str, payload_raw: str) -> TranscriptMessage | None:
    try:
        d = json.loads(payload_raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(d, dict):
        role = _normalize_role(d.get("role") or d.get("kind") or kind)
        text = _payload_text(d)
    elif isinstance(d, str):
        role, text = _normalize_role(kind), d
    else:
        return None
    if not text.strip():
        return None
    return TranscriptMessage(role, text)


def _meta_value(meta: dict[str, str], *needles: str) -> str | None:
    """First meta value whose *lowercased* key contains a needle — the
    upstream key space is open (``fixture.session_id``,
    ``sessionId``, …), so this is a substring match, not a lookup."""
    for needle in needles:
        for key, value in meta.items():
            if needle in key.lower():
                return value
    return None


def load_transcript(path: str | Path) -> AcpTranscript | None:
    """Decode one ``acp-messages/*.db`` — ``None`` when it is not a
    conforming transcript store (unreadable, missing tables, empty)."""
    path = Path(path)
    try:
        with AcpMessagesStore(path) as store:
            meta = store.meta()
            typed = store.typed_meta()
            messages = tuple(
                m
                for m in (
                    _message(msg.kind, msg.payload)
                    for msg in store.messages()
                )
                if m is not None
            )
    except Exception:  # noqa: BLE001 — a stray non-acp db must not abort
        return None
    try:
        marker = int(path.stat().st_mtime * 1000)
    except OSError:
        marker = 0
    title = typed.title
    if title is None:
        t = _meta_value(meta, "title")
        title = t if isinstance(t, str) else None
    return AcpTranscript(
        path=path,
        session_id=_meta_value(meta, "session_id", "sessionid", "slug")
        or path.stem,
        title=title,
        message_count=typed.message_count,
        truncated=typed.truncated,
        marker=marker,
        messages=messages,
    )


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def load_dir(acp_dir: str | Path, *, max_files: int = MAX_FILES) -> list[AcpTranscript]:
    """Every decodable transcript under ``acp_dir`` (bounded), newest
    file first so the cap keeps the most recent sessions."""
    acp_dir = Path(acp_dir)
    if not acp_dir.is_dir():
        return []
    paths = sorted(
        acp_dir.glob("*.db"), key=lambda p: (-_mtime(p), p.name)
    )[:max_files]
    out = []
    for path in paths:
        t = load_transcript(path)
        if t is not None:
            out.append(t)
    return out


def match_transcript(
    slug: str, label: str | None, transcripts: list[AcpTranscript]
) -> AcpTranscript | None:
    """Best-effort binding of a vscdb GUI session to its acp transcript:
    meta session-id == slug, then filename stem == slug, then meta
    title == the binding's label."""
    for t in transcripts:
        if t.session_id == slug:
            return t
    for t in transcripts:
        if t.path.stem == slug:
            return t
    if label:
        for t in transcripts:
            if t.title and t.title == label:
                return t
    return None


def require_redactor():
    """Import the redaction engine — hard fail when devin-redact is not
    installed so a metadata-only run never half-exports transcripts."""
    try:
        from devin_redact.engine import redact_text
    except ImportError as exc:  # pragma: no cover — dep is declared
        raise RedactionError(
            "devin-redact is required for transcript export "
            "(pip install 'devin-history' pulls it): "
            f"{exc}"
        ) from exc
    return redact_text


def redact(
    text: str, redact_text, edits: list[dict] | None = None
) -> str:
    """Fail-closed redaction of one content string.

    ``redact_text`` is injected (``require_redactor``) so callers keep the
    import preflighted once per run; edits are appended to ``edits`` for
    the per-note redaction count. Any engine error propagates as
    ``RedactionError`` — the caller skips the file, nothing is written.
    """
    try:
        new_text, found = redact_text(text)
    except Exception as exc:
        raise RedactionError(str(exc)) from exc
    if edits is not None:
        edits.extend(found)
    return new_text
