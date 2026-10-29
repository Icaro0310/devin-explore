"""registry.json — the machine-consumable project registry."""

from __future__ import annotations

import json

import pytest
from devin_pm.milestones import collect_milestones
from devin_pm.projects import group_sessions, load_sessions
from devin_pm.registry import build_registry, write_registry
from devin_internals.schema import LATEST_KNOWN_SCHEMA


@pytest.fixture
def projects(sessions_db):
    return group_sessions(load_sessions(sessions_db))


@pytest.fixture
def registry(projects, sessions_db):
    return build_registry(
        projects,
        milestones={p.name: collect_milestones(p) for p in projects},
        sessions_db=sessions_db,
        schema_version=LATEST_KNOWN_SCHEMA,
        generated="2026-09-29T00:00:00Z",
    )


def test_top_level_shape(registry):
    assert registry["version"] == 1
    assert registry["generated"] == "2026-09-29T00:00:00Z"
    # fixture emits the installed spec's latest schema (v17 at 0.3.1,
    # v18 at 0.3.3) — pin to the constant, not a literal
    assert registry["source"]["schema_version"] == LATEST_KNOWN_SCHEMA
    assert registry["source"]["sessions_db"].endswith("sessions.db")
    assert isinstance(registry["projects"], list)
    assert isinstance(registry["totals"], dict)


def test_project_entry_shape(registry):
    entry = {p["name"]: p for p in registry["projects"]}
    alpha = entry["alpha"]
    assert alpha["sessions"] == 3
    assert sorted(alpha["session_ids"]) == ["a-1", "a-2", "a-3"]
    assert alpha["status"] == {"active": 2, "hidden": 1}
    assert alpha["cost"] == 0.75
    assert alpha["milestones"] == {"total": 1, "done": 1, "pending": 0}
    assert alpha["last_activity"].endswith("Z")
    assert alpha["working_directory"].endswith("alpha")

    beta = entry["beta"]
    assert beta["cost"] is None
    assert beta["milestones"] == {"total": 1, "done": 0, "pending": 1}


def test_totals(registry):
    assert registry["totals"]["projects"] == 2
    assert registry["totals"]["sessions"] == 5
    assert registry["totals"]["cost"] == 0.75


def test_serializable(registry, tmp_path):
    text = json.dumps(registry, indent=2)
    assert json.loads(text)["version"] == 1


def test_write_registry(registry, tmp_path):
    out = tmp_path / "registry.json"
    write_registry(registry, out)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["totals"]["projects"] == 2


def test_milestones_optional(projects):
    registry = build_registry(projects, generated="2026-09-29T00:00:00Z")
    alpha = {p["name"]: p for p in registry["projects"]}["alpha"]
    assert "milestones" not in alpha
    assert "sessions_db" not in registry["source"]
