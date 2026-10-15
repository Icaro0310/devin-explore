import json

import devin_search
from conftest import add_acp_db, add_session, msg
from devin_search.cli import main


def test_index_then_query_end_to_end(db_path, acp_dir, tmp_path, capsys):
    add_session(
        db_path,
        "cli-sess",
        messages=[msg("user", "remember the flibberty gibbet flag")],
    )
    add_acp_db(
        acp_dir, "gui", "gui-9", [("user", {"text": "nothing here"})]
    )
    idx = tmp_path / "search.db"

    rc = main(
        [
            "index",
            "--sessions-db", str(db_path),
            "--acp-dir", str(acp_dir),
            "--index", str(idx),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "indexed" in out and "+2 docs" in out or "docs" in out

    rc = main(
        ["query", "flibberty", "--index", str(idx)]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "flibberty" in out and "cli-sess"[:8] in out


def test_query_json_shape(db_path, tmp_path, capsys):
    add_session(
        db_path, "s1", messages=[msg("user", "xyzzy marker")]
    )
    idx = tmp_path / "search.db"
    main(["index", "--sessions-db", str(db_path), "--no-acp", "--index", str(idx)])
    capsys.readouterr()

    rc = main(["query", "xyzzy", "--index", str(idx), "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["term"] == "xyzzy"
    assert data["version"] == devin_search.__version__
    assert data["hits"][0]["session_id"] == "s1"
    assert data["hits"][0]["role"] == "user"
    assert data["hits"][0]["ref"].startswith("node:")


def test_index_json_shape(db_path, tmp_path, capsys):
    rc = main(
        [
            "index",
            "--sessions-db", str(db_path),
            "--no-acp", "--index", str(tmp_path / "s.db"),
            "--json",
        ]
    )
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["indexed"] > 0
    assert "by_source" in data and data["by_source"]["message_nodes"] > 0


def test_query_no_hits_exit_1(db_path, tmp_path, capsys):
    idx = tmp_path / "search.db"
    main(["index", "--sessions-db", str(db_path), "--no-acp",
          "--index", str(idx)])
    capsys.readouterr()
    rc = main(["query", "absent-term", "--index", str(idx)])
    assert rc == 1
    assert "no hits" in capsys.readouterr().out


def test_query_missing_index_exit_2(tmp_path, capsys):
    rc = main(["query", "x", "--index", str(tmp_path / "none.db")])
    assert rc == 2
    assert "no index" in capsys.readouterr().err


def test_bad_since_exit_2(db_path, tmp_path, capsys):
    idx = tmp_path / "search.db"
    main(["index", "--sessions-db", str(db_path), "--no-acp", "--index", str(idx)])
    capsys.readouterr()
    rc = main(["query", "x", "--since", "garbage", "--index", str(idx)])
    assert rc == 2


def test_query_log_and_misses(db_path, tmp_path, capsys):
    idx = tmp_path / "search.db"
    main(["index", "--sessions-db", str(db_path), "--no-acp",
          "--index", str(idx)])
    capsys.readouterr()
    main(["query", "absent-one", "--index", str(idx)])
    main(["query", "absent-two", "--index", str(idx)])
    main(["query", "absent-one", "--index", str(idx)])
    capsys.readouterr()

    log = tmp_path / "search.db.queries.jsonl"
    assert log.is_file()
    lines = [json.loads(l) for l in log.read_text().splitlines()]
    assert len(lines) == 3
    assert all(e["hits"] == 0 and "term" in e for e in lines)

    rc = main(["misses", "--index", str(idx), "--json"])
    assert rc == 0
    s = json.loads(capsys.readouterr().out)
    assert s["version"] == devin_search.__version__
    assert s["total_queries"] == 3 and s["zero_hit"] == 3
    assert s["zero_hit_rate"] == 1.0
    assert dict(s["top_missed_terms"]) == {"absent-one": 2, "absent-two": 1}


def test_no_log_skips_query_log(db_path, tmp_path, capsys):
    idx = tmp_path / "search.db"
    main(["index", "--sessions-db", str(db_path), "--no-acp",
          "--index", str(idx)])
    capsys.readouterr()
    main(["query", "absent", "--no-log", "--index", str(idx)])
    assert not (tmp_path / "search.db.queries.jsonl").exists()


def test_missing_sessions_db_exit_2(tmp_path, capsys):
    rc = main(
        [
            "index",
            "--sessions-db", str(tmp_path / "nope.db"),
            "--index", str(tmp_path / "s.db"),
        ]
    )
    assert rc == 2
    assert "no such file" in capsys.readouterr().err


def test_no_stores_found_exit_2(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "devin_search.cli.default_sessions_db", lambda: None
    )
    monkeypatch.setattr("devin_search.cli.default_acp_dir", lambda: None)
    rc = main(["index", "--index", str(tmp_path / "s.db")])
    assert rc == 2


def test_version(capsys):
    import pytest

    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "devin-search" in capsys.readouterr().out
