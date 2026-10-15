import json

import devin_search
from conftest import (
    add_acp_db,
    add_codex_home,
    add_copilot_session,
    add_session,
    add_vscode_chat,
    codex_record,
    copilot_event,
    msg,
    vscode_request,
)
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


def test_index_and_query_each_new_surface(tmp_path, capsys):
    """--copilot-dir/--codex-dir/--vscode-chat-dir index their stores;
    hits land in query output."""
    copilot_root = tmp_path / "copilot"
    add_copilot_session(
        copilot_root,
        "cp-1",
        [copilot_event("user.message", {"content": "cli copilot zephyr"})],
    )
    codex_root = add_codex_home(
        tmp_path / "codex",
        rollouts=[
            (
                "sessions/x/rollout-cli-1.jsonl",
                [
                    codex_record(
                        "session_meta",
                        {"id": "cx-1", "timestamp": "2026-05-28T20:26:40Z",
                         "cwd": "/w"},
                    ),
                    codex_record(
                        "event_msg",
                        {"type": "user_message", "message": "cli codex zephyr"},
                    ),
                ],
            )
        ],
    )
    vscode_user = tmp_path / "Code" / "User"
    add_vscode_chat(
        vscode_user,
        "vs-1",
        doc={
            "version": 3,
            "requests": [vscode_request("cli vscode zephyr", "resp")],
        },
        ws_dir="h",
    )
    idx = tmp_path / "search.db"

    rc = main(
        [
            "index",
            "--copilot-dir", str(copilot_root),
            "--codex-dir", str(codex_root),
            "--vscode-chat-dir", str(vscode_user),
            "--index", str(idx),
        ]
    )
    assert rc == 0
    capsys.readouterr()

    rc = main(["query", "zephyr", "--index", str(idx), "--json"])
    assert rc == 0
    hits = json.loads(capsys.readouterr().out)["hits"]
    assert {h["source"] for h in hits} == {
        "copilot-cli",
        "codex-cli",
        "vscode-chat",
    }


def test_no_surface_flags_exclude_sources(tmp_path, capsys):
    copilot_root = tmp_path / "copilot"
    add_copilot_session(
        copilot_root,
        "cp-2",
        [copilot_event("user.message", {"content": "solo copilot term"})],
    )
    codex_root = add_codex_home(
        tmp_path / "codex",
        state_n=4,
        threads=[{"id": "t-1", "first_user_message": "solo codex term"}],
    )
    idx = tmp_path / "search.db"

    rc = main(
        [
            "index",
            "--copilot-dir", str(copilot_root),
            "--no-codex", "--no-vscode-chat", "--no-acp",
            "--index", str(idx),
        ]
    )
    assert rc == 0
    capsys.readouterr()

    rc = main(["query", "solo", "--index", str(idx), "--json"])
    assert rc == 0
    hits = json.loads(capsys.readouterr().out)["hits"]
    assert {h["source"] for h in hits} == {"copilot-cli"}
    assert codex_root.exists()  # passed dir untouched but unused here


def test_query_stale_index_is_store_error_not_traceback(db_path, tmp_path, capsys):
    """A v1 index queried after upgrade exits 2 with a reindex hint —
    the documented store-error contract, not a Python traceback."""
    import sqlite3

    add_session(db_path, "s1", messages=[msg("user", "xyzzy marker")])
    idx = tmp_path / "search.db"
    main(["index", "--sessions-db", str(db_path), "--no-acp", "--index", str(idx)])
    con = sqlite3.connect(idx)
    try:
        con.execute(
            "UPDATE index_meta SET value = '1' WHERE key = 'schema_version'"
        )
        con.commit()
    finally:
        con.close()
    capsys.readouterr()

    rc = main(["query", "xyzzy", "--index", str(idx)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "stale" in err and "rebuild" in err and "Traceback" not in err
