"""``devin-search stats`` — persistent index health, read-only.

``build_index`` persists run counters in ``index_meta`` (``run:*`` /
``total:*``) so stats can report doc counts, watermarks and
reconciliation history without re-deriving any of it.
"""

import json

from conftest import add_acp_db
from devin_search.cli import main
from devin_search.index import build_index, index_doc_count
from devin_search.stats import collect, render_text


def test_collect_reports_health(db_path, tmp_path):
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)
    h = collect(idx)
    assert h.docs == index_doc_count(idx) > 0
    assert h.schema_version == 4
    assert "devin-cli" in h.by_source
    assert h.by_source["devin-cli"]["docs"] == h.docs
    assert h.by_source["devin-cli"]["latest_ts"] == h.latest_doc_ts
    # table watermarks recorded after the run
    wm = h.watermarks["devin-cli"]
    assert isinstance(wm["message_nodes"], int) and wm["message_nodes"] > 0
    assert "content_digest" in wm
    # run counters persisted for health reporting
    assert h.run["last_indexed"] == h.docs
    assert h.run["last_removed"] == 0
    assert h.run["last_swept"] == 0
    assert h.run["last_run_at"] > 0
    assert h.totals == {"indexed": h.docs, "removed": 0, "swept": 0}


def test_collect_file_source_watermarks(acp_dir, tmp_path):
    idx = tmp_path / "search.db"
    add_acp_db(acp_dir, "one", "s-1", [("user", {"text": "alpha"})])
    add_acp_db(acp_dir, "two", "s-2", [("user", {"text": "beta"})])
    build_index(idx, acp_dir=acp_dir)
    h = collect(idx)
    wm = h.watermarks["devin-chat"]
    assert wm["files"] == 2
    assert set(wm["positions"]) == {"one.db", "two.db"}
    assert all(isinstance(v, int) for v in wm["positions"].values())
    assert "devin-chat" in h.by_source


def test_collect_removed_and_swept(acp_dir, tmp_path):
    idx = tmp_path / "search.db"
    keep = add_acp_db(
        acp_dir, "keep", "s-keep", [("user", {"text": "stay"})]
    )
    gone = add_acp_db(
        acp_dir, "gone", "s-gone", [("user", {"text": "depart"})]
    )
    build_index(idx, acp_dir=acp_dir)
    gone.unlink()
    build_index(idx, acp_dir=acp_dir)
    h = collect(idx)
    assert h.run["last_removed"] == 1
    assert h.run["last_swept"] == 1
    assert h.totals["removed"] == 1 and h.totals["swept"] == 1
    # the vanished file's marker is gone from the watermarks
    assert set(h.watermarks["devin-chat"]["positions"]) == {
        keep.name
    }


def test_totals_accumulate_across_runs(db_path, tmp_path):
    idx = tmp_path / "search.db"
    first = build_index(idx, sessions_db=db_path)
    second = build_index(idx, sessions_db=db_path)  # incremental no-op
    assert second.indexed == 0
    h = collect(idx)
    assert h.run["last_indexed"] == 0
    assert h.totals["indexed"] == first.indexed


def test_collect_missing_index(tmp_path):
    idx = tmp_path / "none.db"
    try:
        collect(idx)
    except FileNotFoundError as exc:
        assert "index not found" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")
    # read-only contract: stats never creates the file
    assert not idx.exists()


def test_collect_empty_index_file(tmp_path):
    idx = tmp_path / "empty.db"
    idx.write_bytes(b"")
    h = collect(idx)
    assert h.docs == 0
    assert h.schema_version is None
    assert h.run == {} and h.totals == {}


def test_render_text(db_path, tmp_path):
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)
    out = render_text(collect(idx))
    assert "index:" in out and "documents:" in out
    assert "devin-cli" in out
    assert "watermarks:" in out and "reconciliation:" in out
    assert "last run" in out and "totals:" in out


def test_cli_stats_text(db_path, tmp_path, capsys):
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)
    rc = main(["stats", "--index", str(idx)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "documents:" in out and "devin-cli" in out


def test_cli_stats_json(db_path, tmp_path, capsys):
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)
    rc = main(["stats", "--index", str(idx), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["index"] == str(idx)
    assert data["docs"] > 0
    assert data["schema_version"] == 4
    assert data["by_source"]["devin-cli"]["docs"] == data["docs"]
    assert data["watermarks"]["devin-cli"]["message_nodes"] > 0
    assert data["run"]["last_indexed"] == data["docs"]
    assert data["totals"]["indexed"] == data["docs"]
    assert data["version"]


def test_cli_stats_missing_index(tmp_path, capsys):
    idx = tmp_path / "none.db"
    rc = main(["stats", "--index", str(idx)])
    assert rc == 2
    assert "index not found" in capsys.readouterr().err
    assert not idx.exists()  # stats never creates it
