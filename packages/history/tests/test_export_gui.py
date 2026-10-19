import hashlib
import json
import sqlite3

import pytest
from conftest import BASE_MS, create_vscdb, gui_items
from devin_history.cli import main
from devin_history.export import export_gui_sessions
from devin_history.paths import default_state_vscdb, state_vscdb_candidates
from devin_history.vscdb import gui_sessions
from devin_internals.parsers import StateVscdbStore


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_gui_sessions_parses_slugs_backends_and_accessed(vstore):
    sessions = {g.slug: g for g in gui_sessions(vstore)}
    assert set(sessions) == {"canyon-newspaper", "river-otter", "broken-json"}

    canyon = sessions["canyon-newspaper"]
    assert canyon.backend == "acp"
    assert canyon.label == "Fix flaky test"
    assert canyon.workspace_id == "/work/alpha"
    assert canyon.folders == ("/work/alpha", "/work/shared")
    assert canyon.last_updated == BASE_MS
    # lastAccessed derived via resourceToSpace (space→slug) + metadata
    assert canyon.space_id == "space-1"
    assert canyon.last_accessed == BASE_MS + 5_000

    otter = sessions["river-otter"]
    assert otter.backend == "ssh-remote"
    assert otter.space_id is None
    assert otter.last_accessed is None

    # malformed JSON degrades to empty metadata, never fatal
    broken = sessions["broken-json"]
    assert broken.label is None
    assert broken.folders == ()


def test_export_gui_one_note_per_session_plus_index(vstore, tmp_path):
    out = tmp_path / "out"
    res = export_gui_sessions(vstore, out)

    assert len(res.written) == 3
    assert res.skipped_unchanged == []
    notes = sorted(p.name for p in out.glob("*.md"))
    assert notes == sorted(res.written)
    assert (out / "index.json").exists()
    # filename carries the session slug
    assert any("canyon-newspaper" in n for n in res.written)
    assert any("river-otter" in n for n in res.written)


def test_export_gui_note_frontmatter_and_body(vstore, tmp_path):
    out = tmp_path / "out"
    res = export_gui_sessions(vstore, out)
    name = next(n for n in res.written if "canyon-newspaper" in n)
    body = (out / name).read_text(encoding="utf-8")

    assert body.startswith("---\n")
    assert "session_id: canyon-newspaper" in body
    assert "source: gui" in body
    assert "backend: acp" in body
    assert f"last_activity: {BASE_MS}" in body
    assert f"last_accessed: {BASE_MS + 5000}" in body
    assert "machine_id:" in body
    assert "profile:" in body
    assert "tags: [session, devin, history, gui]" in body
    assert "# Fix flaky test" in body
    assert "`/work/alpha`" in body
    assert "`/work/shared`" in body
    assert "no local transcript" in body

    otter = next(n for n in res.written if "river-otter" in n)
    body = (out / otter).read_text(encoding="utf-8")
    assert "last_accessed:" not in body  # not derivable → omitted


def test_export_gui_notes_carry_evidence_anchors(vstore, tmp_path):
    """SESSION-EVIDENCE anchors on every GUI note (spec §1 + SURFACES §4)."""
    out = tmp_path / "out"
    res = export_gui_sessions(vstore, out)

    name = next(n for n in res.written if "canyon-newspaper" in n)
    body = (out / name).read_text(encoding="utf-8")
    ws_hash = hashlib.sha256(b"/work/alpha").hexdigest()[:16]
    assert "surface: devin-chat" in body
    assert "native_id: canyon-newspaper" in body
    assert f"ref: devin-chat:{ws_hash}:native:canyon-newspaper" in body

    otter = next(n for n in res.written if "river-otter" in n)
    body = (out / otter).read_text(encoding="utf-8")
    ws_hash = hashlib.sha256(b"/srv/beta").hexdigest()[:16]
    assert "surface: devin-chat" in body
    assert "native_id: river-otter" in body
    assert f"ref: devin-chat:{ws_hash}:native:river-otter" in body


def test_export_gui_evidence_ref_ephemeral_without_workspace(vstore, tmp_path):
    """A binding with no workspaceId keeps a hashless native id → the ref is
    the ephemeral display key (SURFACES §4), never a canonical-looking id."""
    out = tmp_path / "out"
    res = export_gui_sessions(vstore, out)
    name = next(n for n in res.written if "broken-json" in n)
    body = (out / name).read_text(encoding="utf-8")
    assert "surface: devin-chat" in body
    assert "native_id: broken-json" in body
    # broken-json has no timestamps → sorts first in the export listing
    assert "ref: devin-chat:ephemeral:0" in body


def test_export_gui_index_json_has_provenance_and_stats(vstore, tmp_path):
    out = tmp_path / "out"
    res = export_gui_sessions(vstore, out)
    index = json.loads((out / "index.json").read_text(encoding="utf-8"))

    assert index["provenance"]["machine_id"]
    assert index["provenance"]["profile"] in ("corporate", "personal")
    assert index["source"] == "state.vscdb"
    stats = index["stats"]
    assert stats["total"] == 3
    assert stats["backends"] == {"acp": 2, "ssh-remote": 1}
    assert set(stats["projects"]) == {"alpha", "beta", "?"}
    assert stats["date_first"] <= stats["date_last"]

    rows = {s["slug"]: s for s in index["sessions"]}
    assert rows["canyon-newspaper"]["last_accessed"] == BASE_MS + 5000
    assert rows["canyon-newspaper"]["workspace_id"] == "/work/alpha"
    assert rows["river-otter"]["last_accessed"] is None
    assert len(index["sessions"]) == len(res.index_entries)


def test_export_gui_rerun_is_idempotent(vstore, tmp_path):
    out = tmp_path / "out"
    export_gui_sessions(vstore, out)
    before = {p.name: p.stat().st_mtime_ns for p in out.iterdir()}

    res = export_gui_sessions(vstore, out)
    assert res.written == []
    assert set(res.skipped_unchanged) == {
        "canyon-newspaper", "river-otter", "broken-json"}
    assert {p.name: p.stat().st_mtime_ns for p in out.iterdir()} == before


def test_export_gui_force_rewrites(vstore, tmp_path):
    out = tmp_path / "out"
    export_gui_sessions(vstore, out)
    res = export_gui_sessions(vstore, out, force=True)
    assert len(res.written) == 3
    assert res.skipped_unchanged == []


def test_export_gui_dry_run_writes_nothing(vstore, tmp_path):
    out = tmp_path / "out"
    res = export_gui_sessions(vstore, out, dry_run=True)
    assert len(res.written) == 3
    assert not out.exists() or list(out.iterdir()) == []


def test_export_gui_does_not_modify_source(vscdb_path, tmp_path):
    before = _sha256(vscdb_path)
    with StateVscdbStore(vscdb_path) as store:
        export_gui_sessions(store, tmp_path / "out")
    assert _sha256(vscdb_path) == before
    assert not list(vscdb_path.parent.glob("state.vscdb-*"))


def test_export_gui_empty_store_yields_empty_index(tmp_path):
    path = create_vscdb(tmp_path / "empty.vscdb", {"unrelated.key": "{}"})
    with StateVscdbStore(path) as store:
        res = export_gui_sessions(store, tmp_path / "out")
    assert res.written == [] and res.index_entries == []
    index = json.loads((tmp_path / "out" / "index.json").read_text())
    assert index["stats"]["total"] == 0
    assert index["sessions"] == []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_export_gui(vscdb_path, tmp_path, capsys):
    out = tmp_path / "notes"
    assert main(["export-gui", "--vscdb", str(vscdb_path),
                 "--out", str(out)]) == 0
    assert len(list(out.glob("*.md"))) == 3
    assert (out / "index.json").exists()
    assert "written: 3" in capsys.readouterr().out


def test_cli_export_gui_json_output(vscdb_path, tmp_path, capsys):
    out = tmp_path / "notes"
    assert main(["export-gui", "--vscdb", str(vscdb_path),
                 "--out", str(out), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["written"]) == 3
    assert data["indexed"] == 3


def test_cli_export_gui_missing_file_fails(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["export-gui", "--vscdb", str(tmp_path / "nope.vscdb"),
              "--out", str(tmp_path / "o")])
    assert exc.value.code == 2
    assert "no such file" in capsys.readouterr().err


def test_cli_export_gui_unreadable_file_fails(tmp_path, capsys):
    bad = tmp_path / "bad.vscdb"
    bad.write_text("not a sqlite file", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["export-gui", "--vscdb", str(bad),
              "--out", str(tmp_path / "o")])
    assert exc.value.code == 2
    assert capsys.readouterr().err


def test_cli_export_gui_missing_itemtable_fails(tmp_path, capsys):
    wrong = tmp_path / "wrong.vscdb"
    con = sqlite3.connect(wrong)
    con.execute("CREATE TABLE other (x)")
    con.close()
    with pytest.raises(SystemExit) as exc:
        main(["export-gui", "--vscdb", str(wrong),
              "--out", str(tmp_path / "o")])
    assert exc.value.code == 2
    assert "ItemTable" in capsys.readouterr().err


def test_cli_export_gui_no_default_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "empty-appdata"))
    monkeypatch.setattr(
        "devin_history.paths.Path.home", lambda: tmp_path / "nohome")
    with pytest.raises(SystemExit) as exc:
        main(["export-gui", "--out", str(tmp_path / "o")])
    assert exc.value.code == 2
    assert "no state.vscdb found" in capsys.readouterr().err


def test_cli_export_gui_auto_detects_default(monkeypatch, tmp_path, capsys):
    cfg = tmp_path / "config"
    path = cfg / "devin" / "User" / "globalStorage" / "state.vscdb"
    create_vscdb(path, gui_items())
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg))
    # Windows auto-detect reads %APPDATA% (XDG is ignored there); point both
    # at the synthetic config root so the lookup succeeds on either platform
    monkeypatch.setenv("APPDATA", str(cfg))
    monkeypatch.setattr(
        "devin_history.paths.Path.home", lambda: tmp_path / "nohome")

    out = tmp_path / "notes"
    assert main(["export-gui", "--out", str(out)]) == 0
    assert len(list(out.glob("*.md"))) == 3


def test_vscdb_candidates_linux(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    cands = state_vscdb_candidates(environ=env, platform="linux")
    assert cands[0] == (
        tmp_path / "config" / "Devin" / "User" / "globalStorage"
        / "state.vscdb"
    )
    assert default_state_vscdb(environ=env, platform="linux") is None
    cands[0].parent.mkdir(parents=True)
    cands[0].touch()
    assert default_state_vscdb(environ=env, platform="linux") == cands[0]


# -- HI-2: incremental export via hook --------------------------------------


def test_export_only_session(tmp_path, store):
    from devin_history.export import export_sessions
    s = store.sessions()[0]
    res = export_sessions(store, tmp_path / "out", only_session=s.id[:12])
    assert len(res.written) <= 1


def test_export_only_session_no_match(tmp_path, store):
    import pytest
    from devin_history.export import export_sessions
    with pytest.raises(ValueError, match="no session"):
        export_sessions(store, tmp_path / "o", only_session="nope")


def test_from_hook_stdin(tmp_path, store, monkeypatch, capsys):
    import io
    import json
    import sys

    from devin_history.cli import main
    s = store.sessions()[0]
    fake = io.StringIO(json.dumps({"session_id": s.id}))
    monkeypatch.setattr(fake, "isatty", lambda: False)
    monkeypatch.setattr(sys, "stdin", fake)
    db = store.path
    rc = main(["export", "--sessions-db", str(db), "--out",
               str(tmp_path / "o"), "--from-hook", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["indexed"] >= 1
