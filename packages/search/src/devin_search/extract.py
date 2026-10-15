"""Tolerant extraction of searchable text and roles from Devin payloads.

SCHEMA.md (devin-internals-spec) marks every payload column as *unstable*:
``chat_message`` has been seen as ``{"role", "content"}`` and
``{"role", "text"}`` (with ``content`` sometimes a list of typed blocks),
``tool_call_*_json`` and ACP ``payload`` shapes are undocumented. Extraction
therefore never assumes a shape: known roles are normalized, text is pulled
from known keys first and from a recursive string walk as fallback, and
anything undecodable yields ``None`` rather than a crash.
"""

from __future__ import annotations

import json
from typing import Any

_ROLE_ALIASES = {
    "user": "user",
    "assistant": "assistant",
    "agent": "assistant",
    "thought": "assistant",
    "tool": "tool",
    "tool_call": "tool",
    "tool_result": "tool",
    "system": "system",
}


def normalize_role(raw: Any) -> str:
    """Map a raw role/kind string to a normalized role."""
    if not isinstance(raw, str) or not raw:
        return "unknown"
    # acp kinds may be namespaced ("fixture.user"); take the last segment.
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


def _walk_strings(
    value: Any,
    out: list[str],
    *,
    skip_keys: frozenset[str] = frozenset(),
    _depth: int = 0,
) -> None:
    """Collect string leaves from arbitrary JSON — the fallback path."""
    if _depth > 8:
        return
    if isinstance(value, str):
        if value.strip():
            out.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            if k not in skip_keys:
                _walk_strings(v, out, skip_keys=skip_keys, _depth=_depth + 1)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _walk_strings(v, out, skip_keys=skip_keys, _depth=_depth + 1)


def extract_strings(value: Any, *, skip_keys: frozenset[str] = frozenset()) -> str:
    """Join every string leaf in an arbitrary JSON value.

    Used by the non-Devin collectors, whose message payloads
    (``data``/``response`` blobs) vary in shape between stores. Dict keys
    in ``skip_keys`` are skipped at any depth — structural fields like
    ``type``/``id`` that are not user-visible text.
    """
    parts: list[str] = []
    _walk_strings(value, parts, skip_keys=skip_keys)
    return "\n".join(parts)


def extract_text(d: dict[str, Any]) -> str:
    """Pull human-readable text out of a decoded payload dict."""
    for key in ("content", "text"):
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


def extract_chat_message(raw: str) -> tuple[str, str] | None:
    """``chat_message`` blob → ``(role, text)``; ``None`` when undecodable."""
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    return normalize_role(d.get("role")), extract_text(d)


def extract_payload(raw: str | None, kind: Any = None) -> tuple[str, str] | None:
    """Opaque JSON payload (tool calls, acp messages) → ``(role, text)``.

    ``kind`` supplies the role when the payload itself has none.
    """
    if raw is None:
        return None
    try:
        d = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Not JSON — still searchable as plain text.
        return normalize_role(kind), raw
    if isinstance(d, dict):
        role = normalize_role(d.get("role") or d.get("kind") or kind)
        return role, extract_text(d)
    if isinstance(d, str):
        return normalize_role(kind), d
    return normalize_role(kind), json.dumps(d)
