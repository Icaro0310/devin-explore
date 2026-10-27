"""GUI transcript export: acp-messages correlation + devin-redact."""
import json
import sqlite3
from pathlib import Path

import pytest
from devin_history.cli import main
from devin_history.export import export_gui_sessions
from devin_history.transcripts import (
    RedactionError,
    load_dir,
    load_transcript,
    match_transcript,
    redact,
    require_redactor,
)

ACP_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE messages (position INTEGER PRIMARY KEY, kind TEXT NOT NULL,
 payload TEXT NOT NULL);
"""

GH_TOKEN = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"


def create_acp_db(path, *, session_id=None, title=None, messages=(),
                  truncated="0"):
    """Synthetic ``acp-messages/*.db``: meta + ordered payload rows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = [
        ("schema_version", "6"),
        ("info", json.dumps({"title": title} if title else {})),
        ("message_count", str(len(messages))),
        ("truncated", truncated),
    ]
    if session_id is not None:
        meta.append(("fixture.session_id", session_id))
    con = sqlite3.connect(path)
    with con:
        con.executescript(ACP_DDL)
        con.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", meta)
        con.executemany(
            "INSERT INTO messages(position, kind, payload) VALUES (?, ?, ?)",
            [(i, kind, json.dumps(payload))
             for i, (kind, payload) in enumerate(messages)],
        )
    con.close()
    return path


@pytest.fixture
def acp_dir(tmp_path):
    d = tmp_path / "acp-messages"
    create_acp_db(
        d / "canyon-newspaper.db",
        session_id="canyon-newspaper",
        title="Fix flaky test",
        messages=[
            ("user_message", {"role": "user",
                              "content": "Fix the flaky login test"}),
            ("agent_message", {"role": "assistant",
                               "content": "I found a race in setUp."}),
            ("agent_thought", {"content": "thinking about retries"}),
            ("tool_call", {"tool": "bash", "content": "pytest -q"}),
        ],
    )
    return d


def test_load_transcript_decodes_roles_and_meta(acp_dir):
    t = load_transcript(acp_dir / "canyon-newspaper.db")
    assert t is not None
    assert t.session_id == "canyon-newspaper"
    assert t.title == "Fix flaky test"
    assert t.marker > 0
    roles = [m.role for m in t.messages]
    assert roles == ["user", "assistant", "thought", "tool"]
    assert "flaky login" in t.messages[0].text


def test_load_transcript_rejects_non_acp_db(tmp_path):
    bad = tmp_path / "random.db"
    sqlite3.connect(bad).close()
    assert load_transcript(bad) is None
    assert load_transcript(tmp_path / "missing.db") is None


def test_load_transcript_falls_back_to_stem_and_walk(tmp_path):
    p = create_acp_db(
        tmp_path / "acp-messages" / "orphan-file.db",
        messages=[("user_message", {"nested": {"deep": {"s": "hi there"}}})],
    )
    t = load_transcript(p)
    assert t.session_id == "orphan-file"  # no session_id meta → stem
    assert t.messages[0].text == "hi there"


def test_load_dir_bounds_and_skips_bad(tmp_path):
    d = tmp_path / "acp-messages"
    create_acp_db(d / "a.db", messages=[("user_message", {"text": "x"})])
    sqlite3.connect(d / "b.db").close()  # unreadable → skipped
    got = load_dir(d)
    assert [t.path.name for t in got] == ["a.db"]
    assert load_dir(tmp_path / "nope") == []


def test_match_transcript_order(acp_dir):
    loaded = load_dir(acp_dir)
    assert match_transcript("canyon-newspaper", None, loaded) is loaded[0]
    # label fallback
    stem_only = create_acp_db(
        acp_dir / "zzz.db", title="Deploy runbook",
        messages=[("user_message", {"text": "hi"})],
    )
    loaded2 = load_dir(acp_dir)
    t = match_transcript("river-otter", "Deploy runbook", loaded2)
    assert t is not None and t.path == stem_only
    assert match_transcript("nope", None, loaded2) is None


def test_redact_scrubs_real_secret():
    redact_text = require_redactor()
    edits = []
    out = redact(f"my token is {GH_TOKEN} ok", redact_text, edits)
    assert GH_TOKEN not in out
    assert "<REDACTED:" in out
    assert edits


def test_redact_fail_closed_on_engine_error():
    def boom(text):
        raise RuntimeError("engine down")

    with pytest.raises(RedactionError):
        redact("anything", boom)


def test_export_embeds_matched_transcript(vstore, acp_dir, tmp_path):
    out = tmp_path / "o"
    res = export_gui_sessions(vstore, out, acp_dir=acp_dir)
    assert res.transcripts == 1
    name = next(n for n in res.written if "canyon-newspaper" in n)
    note = (out / name).read_text()
    assert "## Transcript" in note
    assert "flaky login test" in note
    assert "race in setUp" in note
    assert "redacted with" in note
    # unmatched binding stays metadata-only
    other = next(n for n in res.written if "river-otter" in n)
    assert "## Transcript" not in (out / other).read_text()


def test_export_redacts_transcript_content(vstore, tmp_path):
    d = tmp_path / "acp-messages"
    create_acp_db(
        d / "canyon-newspaper.db", session_id="canyon-newspaper",
        messages=[("user_message",
                   {"content": f"use {GH_TOKEN} please"})],
    )
    out = tmp_path / "o"
    res = export_gui_sessions(vstore, out, acp_dir=d)
    assert res.redactions >= 1
    for f in out.glob("*.md"):
        assert GH_TOKEN not in f.read_text()
    name = next(n for n in res.written if "canyon-newspaper" in n)
    assert "<REDACTED:" in (out / name).read_text()


def test_export_transcript_only_note(vstore, tmp_path):
    d = tmp_path / "acp-messages"
    create_acp_db(
        d / "unbound-session.db", session_id="unbound-session",
        title="Ghost chat",
        messages=[("user_message", {"text": "still here?"})],
    )
    out = tmp_path / "o"
    res = export_gui_sessions(vstore, out, acp_dir=d)
    files = [f.name for f in out.glob("*unbound-session*.md")]
    assert files, "transcript-only note was not written"
    note = (out / files[0]).read_text()
    assert "no `state.vscdb` binding" in note
    assert "still here?" in note
    entry = next(e for e in res.index_entries if e.slug == "unbound-session")
    assert entry.backend == "acp-messages"
    assert entry.surface == "devin-chat" and entry.native_id == "unbound-session"


def test_export_fail_closed_redaction_error(vstore, acp_dir, tmp_path,
                                            monkeypatch):
    def boom(text):
        raise RuntimeError("engine down")

    monkeypatch.setattr("devin_redact.engine.redact_text", boom)
    out = tmp_path / "o"
    res = export_gui_sessions(vstore, out, acp_dir=acp_dir)
    assert res.redaction_failed == ["canyon-newspaper"]
    # fail closed: NO note and NO partial output for that session
    assert not list(out.glob("*canyon-newspaper*.md"))
    # the unmatched metadata-only binding still exports
    assert any("river-otter" in n for n in res.written)


def test_export_no_transcripts_flag_is_metadata_only(vstore, acp_dir,
                                                     tmp_path):
    out = tmp_path / "o"
    res = export_gui_sessions(vstore, out, acp_dir=acp_dir,
                              transcripts=False)
    assert res.transcripts == 0
    name = next(n for n in res.written if "canyon-newspaper" in n)
    assert "## Transcript" not in (out / name).read_text()


def test_export_transcripts_idempotent(vstore, acp_dir, tmp_path):
    out = tmp_path / "o"
    export_gui_sessions(vstore, out, acp_dir=acp_dir)
    res2 = export_gui_sessions(vstore, out, acp_dir=acp_dir)
    assert res2.written == []
    assert "canyon-newspaper" in res2.skipped_unchanged


def test_export_transcripts_dry_run_writes_nothing(vstore, acp_dir,
                                                   tmp_path):
    out = tmp_path / "o"
    res = export_gui_sessions(vstore, out, acp_dir=acp_dir, dry_run=True)
    assert res.written
    assert not out.exists()


def test_export_store_none_transcript_only(acp_dir, tmp_path):
    out = tmp_path / "o"
    res = export_gui_sessions(None, out, acp_dir=acp_dir)
    assert any("canyon-newspaper" in f for f in res.written)


def test_cli_export_gui_end_to_end(vscdb_path, acp_dir, tmp_path, capsys):
    out = tmp_path / "o"
    rc = main(["export-gui", "--vscdb", str(vscdb_path),
               "--acp-dir", str(acp_dir), "--out", str(out)])
    assert rc == 0
    note = next(out.glob("*canyon-newspaper*.md")).read_text()
    assert "## Transcript" in note


def test_cli_export_gui_vscdb_none_transcripts_only(acp_dir, tmp_path):
    out = tmp_path / "o"
    rc = main(["export-gui", "--vscdb", "none",
               "--acp-dir", str(acp_dir), "--out", str(out)])
    assert rc == 0
    assert next(out.glob("*canyon-newspaper*.md"))


def test_cli_export_gui_redaction_failure_exit_code(vscdb_path, acp_dir,
                                                    tmp_path, monkeypatch,
                                                    capsys):
    def boom(text):
        raise RuntimeError("engine down")

    monkeypatch.setattr("devin_redact.engine.redact_text", boom)
    out = tmp_path / "o"
    rc = main(["export-gui", "--vscdb", str(vscdb_path),
               "--acp-dir", str(acp_dir), "--out", str(out)])
    assert rc == 1
    assert "redaction failed" in capsys.readouterr().err


def test_cli_export_gui_no_transcripts_flag(vscdb_path, acp_dir, tmp_path):
    out = tmp_path / "o"
    rc = main(["export-gui", "--vscdb", str(vscdb_path),
               "--acp-dir", str(acp_dir), "--no-transcripts",
               "--out", str(out)])
    assert rc == 0
    assert "## Transcript" not in next(
        out.glob("*canyon-newspaper*.md")).read_text()
