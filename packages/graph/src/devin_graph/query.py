"""Canned queries over a built ``graph.db``.

Matching is forgiving: exact key first, then case-insensitive, then suffix
(so ``src/app.py`` finds ``/repo/alpha/src/app.py``). All functions return
plain JSON-ready dicts.
"""

from __future__ import annotations

from itertools import combinations

from devin_graph.extract import normalize_path


def _session_dict(gs, node) -> dict:
    project = None
    for e in gs.out_edges(node.id):
        if e.kind == "runs_in":
            project = e.dst.split(":", 1)[1]
    return {
        "id": node.key,
        "title": node.attrs.get("title"),
        "project": project,
        "last_activity_at": node.attrs.get("last_activity_at"),
    }


def _match_file_keys(gs, query: str) -> list[str]:
    q = normalize_path(query) or query.strip()
    keys = [n.key for n in gs.nodes("file")]
    exact = [k for k in keys if k == q]
    if exact:
        return sorted(exact)
    ci = [k for k in keys if k.casefold() == q.casefold()]
    if ci:
        return sorted(ci)
    return sorted(k for k in keys if k.endswith("/" + q) or k == q)


def _match_project_keys(gs, query: str) -> list[str]:
    q = normalize_path(query) or query.strip()
    nodes = gs.nodes("project")
    exact = [n for n in nodes if n.key == q]
    if exact:
        return sorted(n.key for n in exact)
    named = [n for n in nodes if n.attrs.get("name") == query.strip()]
    ci = [n for n in nodes
          if n.key.casefold() == q.casefold()
          or (n.attrs.get("name") or "").casefold() == query.strip().casefold()]
    found = {n.key for n in named + ci}
    if found:
        return sorted(found)
    return sorted(n.key for n in nodes
                  if n.key.endswith("/" + q) or n.key == q)


def _match_tool_keys(gs, query: str) -> list[str]:
    q = query.strip()
    return sorted(n.key for n in gs.nodes("tool")
                  if n.key == q or n.key.casefold() == q.casefold())


def _sessions_of_project(gs, project_key: str) -> list[str]:
    return sorted(
        e.src.split(":", 1)[1]
        for e in gs.in_edges(f"project:{project_key}")
        if e.kind == "runs_in"
    )


def sessions_for_file(gs, path: str) -> dict:
    """Which sessions touched ``path``, via which tool calls."""
    file_keys = _match_file_keys(gs, path)
    tool_calls = []
    session_ids: set[str] = set()
    for fk in file_keys:
        for e in gs.in_edges(f"file:{fk}"):
            if e.kind != "file_touched":
                continue
            tool_calls.append({
                "id": e.src.split(":", 1)[1],
                "session_id": e.owner,
                "file": fk,
            })
            if e.owner:
                session_ids.add(e.owner)
    tool_calls.sort(key=lambda t: t["id"])
    sessions = [
        _session_dict(gs, gs.node(f"session:{sid}"))
        for sid in sorted(session_ids)
        if gs.node(f"session:{sid}") is not None
    ]
    return {"query": path, "files": file_keys, "sessions": sessions,
            "tool_calls": tool_calls}


def sessions_for_tool(gs, name: str) -> dict:
    """Which sessions/projects used tool ``name``."""
    session_ids: set[str] = set()
    projects: set[str] = set()
    for key in _match_tool_keys(gs, name):
        for e in gs.in_edges(f"tool:{key}"):
            if e.kind == "tool_used" and e.owner:
                session_ids.add(e.owner)
    sessions = [
        _session_dict(gs, gs.node(f"session:{sid}"))
        for sid in sorted(session_ids)
        if gs.node(f"session:{sid}") is not None
    ]
    for s in sessions:
        if s["project"]:
            projects.add(s["project"])
    return {"tool": name, "sessions": sessions,
            "projects": sorted(projects)}


def tools_for_project(gs, query: str) -> dict:
    """Tools used by sessions running in project ``query``."""
    project_keys = _match_project_keys(gs, query)
    session_ids = sorted({
        sid for pk in project_keys for sid in _sessions_of_project(gs, pk)
    })
    tools: set[str] = set()
    for sid in session_ids:
        for e in gs.out_edges(f"session:{sid}"):
            if e.kind == "tool_used":
                tools.add(e.dst.split(":", 1)[1])
    sessions = [
        _session_dict(gs, gs.node(f"session:{sid}"))
        for sid in session_ids
        if gs.node(f"session:{sid}") is not None
    ]
    return {"query": query, "projects": project_keys, "tools": sorted(tools),
            "sessions": sessions}


def project_detail(gs, query: str) -> dict:
    """One project: its sessions, the tools they used, the files they touched."""
    keys = _match_project_keys(gs, query)
    if not keys:
        return {"project": None, "sessions": [], "tools": [], "files": []}
    project_key = keys[0]
    base = tools_for_project(gs, project_key)
    session_ids = {s["id"] for s in base["sessions"]}
    files = sorted({
        e.dst.split(":", 1)[1]
        for e in gs.edges("file_touched")
        if e.owner in session_ids
    })
    return {"project": project_key, "sessions": base["sessions"],
            "tools": base["tools"], "files": files}


def projects_graph(gs) -> dict:
    """Project-to-project adjacency for visualization (D3-style).

    Two projects link when they share a tool (both used it) or a file (both
    touched it); ``weight`` = number of shared entities.
    """
    projects = gs.nodes("project")
    tools_by_project: dict[str, set[str]] = {p.key: set() for p in projects}
    files_by_project: dict[str, set[str]] = {p.key: set() for p in projects}

    for e in gs.edges("tool_used"):
        proj = _project_of_session(gs, e.src.split(":", 1)[1])
        if proj in tools_by_project:
            tools_by_project[proj].add(e.dst.split(":", 1)[1])
    for e in gs.edges("file_touched"):
        proj = _project_of_session(gs, e.owner)
        if proj in files_by_project:
            files_by_project[proj].add(e.dst.split(":", 1)[1])

    nodes = [{
        "id": p.key,
        "name": p.attrs.get("name"),
        "sessions": _sessions_of_project(gs, p.key),
    } for p in projects]

    links = []
    for a, b in combinations(sorted(tools_by_project), 2):
        shared_tools = sorted(tools_by_project[a] & tools_by_project[b])
        shared_files = sorted(files_by_project[a] & files_by_project[b])
        if shared_tools or shared_files:
            links.append({
                "source": a, "target": b,
                "weight": len(shared_tools) + len(shared_files),
                "shared_tools": shared_tools,
                "shared_files": shared_files,
            })
    return {"nodes": nodes, "links": links}


def _project_of_session(gs, session_id: str | None) -> str | None:
    if session_id is None:
        return None
    for e in gs.out_edges(f"session:{session_id}"):
        if e.kind == "runs_in":
            return e.dst.split(":", 1)[1]
    return None


def shared_files(gs) -> dict:
    """Files touched by two or more distinct projects.

    A file counts when ``file_touched`` edges link it to tool calls owned by
    sessions running in different projects (``working_directory``). Each
    entry carries the sorted project list and the session ids that touched
    the file — useful both for collision audits and D3-style highlighting.
    """
    projects_of_file: dict[str, set[str]] = {}
    sessions_of_file: dict[str, set[str]] = {}
    for e in gs.edges("file_touched"):
        if not e.owner:
            continue
        project = _project_of_session(gs, e.owner)
        if project is None:
            continue
        file_key = e.dst.split(":", 1)[1]
        projects_of_file.setdefault(file_key, set()).add(project)
        sessions_of_file.setdefault(file_key, set()).add(e.owner)

    files = []
    for file_key in sorted(projects_of_file):
        projects = sorted(projects_of_file[file_key])
        if len(projects) < 2:
            continue
        node = gs.node(f"file:{file_key}")
        files.append({
            "file": file_key,
            "name": node.attrs.get("name") if node else None,
            "projects": projects,
            "sessions": sorted(sessions_of_file[file_key]),
        })
    return {"count": len(files), "files": files}


def orphaned_nodes(gs) -> dict:
    """Nodes with no incident edges — report-only, never deletes.

    ``prunable`` marks the subset the builder's orphan prune would drop
    on the next run (shared nodes — ``owner IS NULL`` — with no edges);
    owned orphans (e.g. a session with no tool calls and no project)
    carry provenance through their owner and persist until that owner
    is re-extracted or removed.
    """
    endpoints = {e.src for e in gs.edges()} | {e.dst for e in gs.edges()}
    nodes = [
        {
            "id": n.id,
            "kind": n.kind,
            "key": n.key,
            "owner": n.owner,
            "name": n.attrs.get("name") or n.attrs.get("title"),
            "prunable": n.owner is None,
        }
        for n in gs.nodes()
        if n.id not in endpoints
    ]
    return {"count": len(nodes), "nodes": nodes}


def blame_session(gs, query: str) -> dict:
    """What graph content session ``query`` produced or touched.

    ``produced`` = nodes and edges the extraction stamped with this
    session as ``owner`` (the session node itself, its tool_call nodes,
    and every edge it emitted); ``touched`` = the shared entities those
    edges reach — files, tools, projects, commits. ``session`` is
    ``None`` when ``query`` matches nothing or is an ambiguous prefix —
    ``candidates`` then lists session keys for the CLI to hint at.
    """
    node = gs.node(f"session:{query}")
    candidates: list[str] = []
    if node is None:
        candidates = sorted(
            n.key for n in gs.nodes("session") if n.key.startswith(query)
        )
        if len(candidates) == 1:
            node = gs.node(f"session:{candidates[0]}")
    if node is None:
        return {
            "query": query,
            "session": None,
            "candidates": candidates[:20],
        }
    sid = node.key
    owned_nodes = [n for n in gs.nodes() if n.owner == sid]
    owned_edges = [e for e in gs.edges() if e.owner == sid]
    files = sorted(
        {e.dst.split(":", 1)[1] for e in owned_edges
         if e.kind == "file_touched"}
    )
    tools = sorted(
        {e.dst.split(":", 1)[1] for e in owned_edges
         if e.kind == "tool_used"}
    )
    projects = sorted(
        {e.dst.split(":", 1)[1] for e in owned_edges
         if e.kind == "runs_in"}
    )
    commits = [
        {"sha": sha, "relation": rel}
        for sha, rel in sorted(
            {
                (e.dst.split(":", 1)[1], e.kind)  # produced | referenced
                for e in owned_edges
                if e.kind in ("produced", "referenced")
            }
        )
    ]
    return {
        "session_id": sid,
        "session": _session_dict(gs, node),
        "extracted_at": gs.extracted_at(sid),
        "projects": projects,
        "files": files,
        "tools": tools,
        "commits": commits,
        "nodes_owned": [
            {"id": n.id, "kind": n.kind, "key": n.key}
            for n in owned_nodes
        ],
        "edges_owned": [
            {"kind": e.kind, "src": e.src, "dst": e.dst}
            for e in owned_edges
        ],
        "counts": {
            "nodes": len(owned_nodes),
            "edges": len(owned_edges),
            "files": len(files),
            "tools": len(tools),
            "commits": len(commits),
        },
    }
