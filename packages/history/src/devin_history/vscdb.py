"""Extract GUI session metadata from Devin's ``state.vscdb`` (HI-1).

GUI sessions live in the Electron ``ItemTable`` key/value store under
``User/globalStorage/state.vscdb`` — there are no ``sessions.db`` rows for
them. The binding found here is metadata (slug, workspace, folders,
timestamps); the transcript bodies live in sibling
``User/acp-messages/*.db`` stores, decoded by ``transcripts.py`` and
correlated back best-effort at export time.

Key family (same parsing devin-graph's ``vscdb.py`` uses for GR-1):

- ``windsurfSpace.sessionWorkspace/<backend>/<slug>`` — JSON
  ``{workspaceId, label, folders[], lastUpdated}`` binding a GUI session
  (identified by a generated slug like ``canyon-newspaper``) to a workspace.
- ``windsurfSpace.resourceToSpace`` — ``{space_id: [editor URIs]}``; URIs
  look like ``vscode-cascade-editor:///cascade-acp/<backend>/<slug>``.
- ``windsurfSpace.metadata`` — ``{space_id: {lastAccessed: ms}}``.

Coverage is best-effort: malformed keys/values degrade to ``None`` fields,
never fatal. The store is opened read-only via ``StateVscdbStore``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from devin_internals.parsers import StateVscdbStore

SESSION_WS_PREFIX = "windsurfSpace.sessionWorkspace/"
RESOURCE_TO_SPACE = "windsurfSpace.resourceToSpace"
SPACE_METADATA = "windsurfSpace.metadata"
WINDSURF_PREFIX = "windsurfSpace."
_EDITOR_URI_PREFIX = "vscode-cascade-editor:///cascade-acp/"


@dataclass(frozen=True)
class GuiSession:
    """One GUI session's workspace binding (transcript lives in
    ``acp-messages`` — see ``transcripts.py``)."""

    slug: str
    backend: str
    label: str | None = None
    workspace_id: str | None = None
    folders: tuple[str, ...] = field(default_factory=tuple)
    last_updated: int | float | None = None
    space_id: str | None = None
    last_accessed: int | float | None = None


def gui_sessions(store: StateVscdbStore) -> list[GuiSession]:
    """All ``windsurfSpace.sessionWorkspace/*`` bindings in ``store``.

    ``last_accessed`` is filled when ``resourceToSpace`` links a space to the
    slug and ``metadata`` records a ``lastAccessed`` for that space.
    """
    rows = store.list_prefix(WINDSURF_PREFIX)
    last_accessed = _space_last_accessed(rows)
    space_of = _space_of_session(rows)

    out: list[GuiSession] = []
    for key, raw in rows.items():
        if not key.startswith(SESSION_WS_PREFIX):
            continue
        backend, sep, slug = key[len(SESSION_WS_PREFIX):].rpartition("/")
        if not sep or not slug:
            continue
        data = _json_dict(raw)
        space = space_of.get(slug)
        out.append(
            GuiSession(
                slug=slug,
                backend=backend,
                label=data.get("label"),
                workspace_id=data.get("workspaceId"),
                folders=tuple(
                    f for f in (data.get("folders") or [])
                    if isinstance(f, str)
                ),
                last_updated=_num(data.get("lastUpdated")),
                space_id=space,
                last_accessed=(
                    last_accessed.get(space) if space else None
                ),
            )
        )
    return out


def _num(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) else None


def _json_dict(raw: Any) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _space_last_accessed(rows: dict) -> dict[str, int]:
    meta = _json_dict(rows.get(SPACE_METADATA))
    out = {}
    for space_id, m in meta.items():
        if isinstance(m, dict) and isinstance(m.get("lastAccessed"), int):
            out[space_id] = m["lastAccessed"]
    return out


def _space_of_session(rows: dict) -> dict[str, str]:
    """session slug → space_id, via resourceToSpace editor URIs."""
    out: dict[str, str] = {}
    rts = _json_dict(rows.get(RESOURCE_TO_SPACE))
    for space_id, uris in rts.items():
        if not isinstance(uris, list):
            continue
        for uri in uris:
            if isinstance(uri, str) and uri.startswith(_EDITOR_URI_PREFIX):
                slug = uri[len(_EDITOR_URI_PREFIX):].rstrip("/").rsplit(
                    "/", 1)[-1]
                if slug:
                    out.setdefault(slug, space_id)
    return out
