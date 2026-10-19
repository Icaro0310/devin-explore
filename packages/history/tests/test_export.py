import hashlib
import json
import re

from conftest import add_session, msg
from devin_history.export import export_sessions
from devin_history.format import evidence_ref
from devin_internals.parsers import SessionsStore


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ws_hash(seed):
    """The §4 workspace hash: sha256(normalized seed)[:16]."""
    return hashlib.sha256(seed.strip().encode()).hexdigest()[:16]


def test_export_one_note_per_session_plus_index(store, tmp_path):
    out = tmp_path / "out"
    res = export_sessions(store, out)

    assert len(res.written) == 3
    assert res.skipped_empty == []
    notes = sorted(p.name for p in out.glob("*.md") if p.name != "index.md")
    assert notes == sorted(res.written)
    assert (out / "index.md").exists()
    for name in res.written:
        body = (out / name).read_text(encoding="utf-8")
        assert "session_id:" in body
        assert "last_activity:" in body
        assert "## Conversation" in body


def test_export_rerun_is_idempotent(store, tmp_path):
    out = tmp_path / "out"
    export_sessions(store, out)
    before = {p.name: p.stat().st_mtime_ns for p in out.iterdir()}

    res = export_sessions(store, out)
    assert res.written == []
    assert len(res.skipped_unchanged) == 3
    after = {p.name: p.stat().st_mtime_ns for p in out.iterdir()}
    assert before == after


def test_export_force_rewrites(store, tmp_path):
    out = tmp_path / "out"
    export_sessions(store, out)
    res = export_sessions(store, out, force=True)
    assert len(res.written) == 3
    assert res.skipped_unchanged == []


def test_export_skips_empty_and_no_user_sessions(db_path, tmp_path):
    add_session(db_path, "empty-1", messages=[])
    add_session(db_path, "agent-only", messages=[msg("agent", "hi"), msg("agent", "yo")])
    with SessionsStore(db_path) as store:
        res = export_sessions(store, tmp_path / "out")
    assert set(res.skipped_empty) == {"empty-1", "agent-only"}
    assert len(res.index_entries) == 3


def test_export_dry_run_writes_nothing(store, tmp_path):
    out = tmp_path / "out"
    res = export_sessions(store, out, dry_run=True)
    assert len(res.written) == 3
    assert not out.exists() or list(out.iterdir()) == []


def test_export_index_md_has_stats_block(store, tmp_path):
    out = tmp_path / "out"
    res = export_sessions(store, out)
    index = (out / "index.md").read_text(encoding="utf-8")

    assert index.startswith("---\ntags:")
    assert "## Stats" in index
    assert "| Project | Sessions | User | Assistant | Tool |" in index
    assert f"| **Total** | **{len(res.index_entries)}** |" in index
    assert "**Span:**" in index and "**Formats:** md:" in index

    total_user = sum(e.user_msgs for e in res.index_entries)
    assert total_user > 0
    assert f"**{total_user}**" in index  # bolded in the Total row
    for project in {e.project for e in res.index_entries}:
        assert f"| {project} | " in index


def test_export_index_json_has_stats(store, tmp_path):
    out = tmp_path / "json"
    res = export_sessions(store, out, fmt="json")
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))

    stats = index["stats"]
    assert stats["total"] == 3
    assert stats["formats"] == {"json": 3}
    assert stats["date_first"] <= stats["date_last"]
    msgs = stats["messages"]
    assert msgs["user"] == sum(e.user_msgs for e in res.index_entries)
    assert msgs["assistant"] == sum(
        e.assistant_msgs for e in res.index_entries)
    assert msgs["tool"] == sum(e.tool_msgs for e in res.index_entries)
    assert set(stats["projects"]) == {e.project for e in res.index_entries}


def test_export_index_stats_include_unchanged_sessions(store, tmp_path):
    out = tmp_path / "out"
    export_sessions(store, out)
    res = export_sessions(store, out)  # all unchanged this time
    index = (out / "index.md").read_text(encoding="utf-8")
    assert f"| **Total** | **{len(res.index_entries)}** |" in index
    stats_total = sum(e.user_msgs for e in res.index_entries)
    assert stats_total > 0


def test_export_json_dump(store, tmp_path):
    out = tmp_path / "json"
    res = export_sessions(store, out, fmt="json")
    assert len(res.written) == 3
    assert (out / "index.json").exists()
    data = json.loads((out / res.written[0]).read_text(encoding="utf-8"))
    assert data["session_id"]
    assert data["messages"]
    assert data["last_activity"]
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    assert len(index["sessions"]) == 3


# -- SESSION-EVIDENCE anchors (devin-internals-spec docs/SESSION-EVIDENCE.md)


def test_evidence_ref_canonical_and_ephemeral():
    # §4 canonical id: <surface>:<workspace-hash>:native:<id>
    ref = evidence_ref("devin-cli", "sess-1", "/fixture/workspace/00")
    assert ref == f"devin-cli:{_ws_hash('/fixture/workspace/00')}:native:sess-1"
    # components encode `%` then `:` so separators stay unambiguous
    assert evidence_ref("devin-cli", "a:b%c", "/ws").endswith(
        ":native:a%3Ab%25c")
    # seed normalization: trailing separators stripped before hashing
    assert evidence_ref("devin-cli", "s", "/ws/") == \
        evidence_ref("devin-cli", "s", "/ws")
    # hashless native id → ephemeral display key, never a canonical id
    assert evidence_ref("devin-cli", "sess-1", "") == "devin-cli:ephemeral:0"
    assert evidence_ref("devin-cli", "sess-1", None, ephemeral_index=7) == \
        "devin-cli:ephemeral:7"
    assert evidence_ref("devin-cli", "sess-1", "  ") == \
        "devin-cli:ephemeral:0"


def test_export_notes_carry_evidence_anchors(store, tmp_path):
    out = tmp_path / "out"
    res = export_sessions(store, out)
    sessions = {s.id: s for s in store.sessions()}
    for name in res.written:
        body = (out / name).read_text(encoding="utf-8")
        sid = name[:-3].split("_", 1)[1]
        session = sessions[sid]
        assert f"session_id: {sid}" in body
        assert "surface: devin-cli" in body
        assert f"native_id: {sid}" in body
        expected = (
            f"devin-cli:{_ws_hash(session.working_directory)}:native:{sid}")
        assert f"ref: {expected}" in body


def test_export_json_carries_evidence_anchors(store, tmp_path):
    out = tmp_path / "json"
    res = export_sessions(store, out, fmt="json")
    sessions = {s.id: s for s in store.sessions()}
    for name in res.written:
        data = json.loads((out / name).read_text(encoding="utf-8"))
        sid = data["session_id"]
        session = sessions[sid]
        assert data["surface"] == "devin-cli"
        assert data["native_id"] == sid
        assert data["ref"] == (
            f"devin-cli:{_ws_hash(session.working_directory)}:native:{sid}")


def test_export_evidence_ref_ephemeral_without_workspace(db_path, tmp_path):
    add_session(
        db_path, "no-ws", working_directory="",
        messages=[msg("user", "hi"), msg("assistant", "yo")])
    with SessionsStore(db_path) as store:
        res = export_sessions(store, tmp_path / "out")
    name = next(n for n in res.written if "no-ws" in n)
    body = (tmp_path / "out" / name).read_text(encoding="utf-8")
    assert "surface: devin-cli" in body
    assert "native_id: no-ws" in body
    assert re.search(r"^ref: devin-cli:ephemeral:\d+$", body, re.MULTILINE)

    with SessionsStore(db_path) as store:
        res = export_sessions(store, tmp_path / "json", fmt="json")
    name = next(n for n in res.written if "no-ws" in n)
    data = json.loads((tmp_path / "json" / name).read_text(encoding="utf-8"))
    assert data["native_id"] == "no-ws"
    assert data["ref"].startswith("devin-cli:ephemeral:")


def test_export_ephemeral_refs_stable_across_filters(db_path, tmp_path):
    """Filtered exports must not renumber ephemeral anchors: a workspace-less
    session keeps its position in the full ordered listing, so two sessions
    exported under --session-id never collapse onto the same ref."""
    add_session(
        db_path, "no-ws-a", working_directory="",
        messages=[msg("user", "hi"), msg("assistant", "yo")])
    add_session(
        db_path, "no-ws-b", working_directory="",
        messages=[msg("user", "hey"), msg("assistant", "ya")])
    with SessionsStore(db_path) as store:
        pos = {
            s.id: i
            for i, s in enumerate(
                sorted(store.sessions(), key=lambda s: s.created_at))
        }
        ra = export_sessions(store, tmp_path / "a", only_session="no-ws-a")
        rb = export_sessions(store, tmp_path / "b", only_session="no-ws-b")

    body_a = (tmp_path / "a" / ra.written[0]).read_text(encoding="utf-8")
    body_b = (tmp_path / "b" / rb.written[0]).read_text(encoding="utf-8")
    ref_a = next(
        ln for ln in body_a.splitlines() if ln.startswith("ref:"))
    ref_b = next(
        ln for ln in body_b.splitlines() if ln.startswith("ref:"))
    assert ref_a != ref_b
    assert ref_a == f"ref: devin-cli:ephemeral:{pos['no-ws-a']}"
    assert ref_b == f"ref: devin-cli:ephemeral:{pos['no-ws-b']}"


def test_export_index_json_entries_carry_anchors(store, tmp_path):
    out = tmp_path / "json"
    export_sessions(store, out, fmt="json")
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))
    sessions = {s.id: s for s in store.sessions()}
    assert len(index["sessions"]) == 3
    for entry in index["sessions"]:
        sid = entry["session_id"]
        assert entry["surface"] == "devin-cli"
        assert entry["native_id"] == sid
        assert entry["ref"] == (
            f"devin-cli:{_ws_hash(sessions[sid].working_directory)}"
            f":native:{sid}")


def test_export_heals_pre_anchor_notes(store, tmp_path):
    """A note written before the anchors existed must be re-rendered on the
    next run — the unchanged-activity marker alone is not up-to-date."""
    out = tmp_path / "out"
    res = export_sessions(store, out)
    for name in res.written:  # strip the anchor lines → pre-upgrade note
        p = out / name
        body = p.read_text(encoding="utf-8")
        body = "\n".join(
            ln for ln in body.splitlines()
            if not ln.startswith(("surface:", "native_id:", "ref:"))
        ) + "\n"
        p.write_text(body, encoding="utf-8")

    res2 = export_sessions(store, out)
    assert len(res2.written) == 3
    assert res2.skipped_unchanged == []
    for name in res2.written:
        body = (out / name).read_text(encoding="utf-8")
        assert "surface: devin-cli" in body
        assert re.search(r"^native_id: \S+$", body, re.MULTILINE)
        assert re.search(r"^ref: devin-cli:\S+$", body, re.MULTILINE)

    res3 = export_sessions(store, out)  # healed notes skip normally again
    assert res3.written == []
    assert len(res3.skipped_unchanged) == 3


def test_export_json_heals_pre_anchor_records(store, tmp_path):
    out = tmp_path / "json"
    res = export_sessions(store, out, fmt="json")
    for name in res.written:  # drop the anchor keys → pre-upgrade record
        p = out / name
        data = json.loads(p.read_text(encoding="utf-8"))
        for key in ("surface", "native_id", "ref"):
            del data[key]
        p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    res2 = export_sessions(store, out, fmt="json")
    assert len(res2.written) == 3
    assert res2.skipped_unchanged == []
    for name in res2.written:
        data = json.loads((out / name).read_text(encoding="utf-8"))
        assert data["surface"] == "devin-cli"
        assert data["native_id"]
        assert data["ref"].startswith("devin-cli:")


def test_export_json_rerun_idempotent(store, tmp_path):
    out = tmp_path / "json"
    export_sessions(store, out, fmt="json")
    res = export_sessions(store, out, fmt="json")
    assert res.written == []
    assert len(res.skipped_unchanged) == 3


def test_export_does_not_modify_source_db(db_path, tmp_path):
    before = _sha256(db_path)
    with SessionsStore(db_path) as store:
        export_sessions(store, tmp_path / "out")
        export_sessions(store, tmp_path / "json", fmt="json")
    assert _sha256(db_path) == before
    assert not list(db_path.parent.glob("sessions.db-*"))


def test_export_since_filters_by_last_activity(store, tmp_path):
    from conftest import BASE_MS

    res_all = export_sessions(store, tmp_path / "out2", since=0)
    res_none = export_sessions(store, tmp_path / "out3")
    assert (len(res_all.written), len(res_all.skipped_empty)) == (
        len(res_none.written), len(res_none.skipped_empty))

    res_far = export_sessions(
        store, tmp_path / "out4", since=2_000_000_000_000)
    assert res_far.written == [] and res_far.index_entries == []

    # fixture last_activity_at: +2min, +62min, +122min — a 30-min
    # watermark drops only the first session
    res_mid = export_sessions(
        store, tmp_path / "out5", since=BASE_MS + 1_800_000)
    assert len(res_mid.written) == 2
    res_mid2 = export_sessions(
        store, tmp_path / "out6", since=BASE_MS + 4_200_000)
    assert len(res_mid2.written) == 1


def test_export_incremental_cursor_watermark(db_path, store, tmp_path):
    from conftest import BASE_MS
    from devin_internals.cursor import Cursor

    cur_dir = tmp_path / "cursors"
    c = Cursor("devin-history-export", dir=cur_dir)
    out = tmp_path / "out"
    r1 = export_sessions(store, out, cursor=c)
    assert len(r1.written) == 3
    # watermark committed at max last_activity_at across the store
    committed = json.loads(
        (cur_dir / "devin-history-export-state.json").read_text())
    assert committed["last_activity_at"] >= BASE_MS

    # second incremental run: nothing newer → nothing written
    c2 = Cursor("devin-history-export", dir=cur_dir)
    r2 = export_sessions(store, out, cursor=c2)
    assert r2.written == [] and r2.index_entries == []

    # a new session with later activity is picked up
    add_session(
        db_path, "sess-later",
        messages=[msg("user", "hi"), msg("assistant", "yo"),
                  msg("user", "more"), msg("assistant", "ok")],
        last_ms=BASE_MS + 86_400_000 * 2)
    c3 = Cursor("devin-history-export", dir=cur_dir)
    r3 = export_sessions(store, out, cursor=c3)
    assert len(r3.written) == 1
    assert "sess-later" in r3.written[0]


def test_export_incremental_dry_run_does_not_commit(store, tmp_path):
    from devin_internals.cursor import Cursor

    cur_dir = tmp_path / "cursors"
    c = Cursor("devin-history-export", dir=cur_dir)
    export_sessions(store, tmp_path / "out", cursor=c, dry_run=True)
    assert not (cur_dir / "devin-history-export-state.json").exists()
