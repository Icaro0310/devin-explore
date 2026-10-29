import csv
import sqlite3

import pytest
from conftest import BASE_MS, add_session, msg
from devin_history.audit import audit_store, classify_task, redact
from devin_history.format import audit_to_dict, audit_to_markdown, write_audit_csv
from devin_internals.parsers import SessionsStore
from devin_internals.schema import LATEST_KNOWN_SCHEMA


def test_audit_rows_cover_all_sessions(store):
    report = audit_store(store)
    assert len(report.rows) == 3
    # fixture emits the installed spec's latest schema (v17 at 0.3.1,
    # v18 at 0.3.3) — pin to the constant, not a literal
    assert report.schema_version == LATEST_KNOWN_SCHEMA
    for r in report.rows:
        assert r.user_msgs > 0
        assert r.tool_calls == 2
        assert r.status in {"completed (inferred)", "interrupted (no final reply)"}
        assert r.duration_min == 2.0


def test_audit_status_inference(db_path):
    add_session(db_path, "hidden-1", hidden=1, messages=[msg("user", "x")])
    add_session(db_path, "empty-1", messages=[])
    add_session(
        db_path, "interrupted-1",
        messages=[msg("user", "q"), msg("agent", "a"), msg("user", "again")],
        tool_calls=[({"kind": "execute"}, {"status": "completed"})],
    )
    add_session(
        db_path, "abandoned-1",
        messages=[msg("user", "lonely prompt"), msg("agent", "sure")],
    )
    add_session(
        db_path, "failing-1",
        messages=[msg("user", "fix it"), msg("agent", "done")],
        tool_calls=[
            ({"kind": "execute", "title": "run x"},
             {"status": "failed", "content": [{"content": {"text": "boom"}}]})
            for _ in range(5)
        ],
    )
    with SessionsStore(db_path) as store:
        report = audit_store(store)
    by_id = {r.id: r for r in report.rows}
    assert by_id["hidden-1"].status == "hidden/archived"
    assert by_id["empty-1"].status == "empty"
    assert by_id["interrupted-1"].status == "interrupted (no final reply)"
    assert by_id["abandoned-1"].status == "abandoned (no actions)"
    assert by_id["failing-1"].status == "completed with failures"
    assert by_id["failing-1"].failed_calls == 5
    assert by_id["failing-1"].fail_samples


def test_audit_anomalies(db_path):
    add_session(db_path, "empty-1", messages=[])
    add_session(
        db_path, "long-1",
        messages=[msg("user", "q"), msg("agent", "a")],
        created_ms=BASE_MS, last_ms=BASE_MS + 3 * 86_400_000,
    )
    con = sqlite3.connect(db_path)
    with con:
        con.execute(
            "INSERT INTO message_nodes(session_id, node_id, chat_message, created_at)"
            " VALUES ('ghost-session', 1, '{\"role\": \"user\", \"text\": \"x\"}', 1)"
        )
    con.close()
    with SessionsStore(db_path) as store:
        report = audit_store(store)

    kinds = {(a.kind, a.session_id) for a in report.anomalies}
    assert ("empty", "empty-1") in kinds
    assert ("orphan", "ghost-session") in kinds
    assert ("long-running", "long-1") in kinds


def test_audit_groupings(store):
    report = audit_store(store)
    md = audit_to_markdown(report)
    assert "## By status" in md
    assert "## By task type" in md
    assert "## By project" in md
    assert "## By month" in md
    assert "## Anomalies" in md
    data = audit_to_dict(report)
    assert data["summary"]["total"] == 3
    assert set(data["sessions"][0]) >= {
        "session_id", "status", "task_type", "tool_calls", "duration_min"}
    assert data["anomalies"] == []


def test_audit_csv(store, tmp_path):
    report = audit_store(store)
    csv_path = tmp_path / "out.csv"
    write_audit_csv(report, csv_path)
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 3
    assert {"session_id", "status", "task_type", "prompt_excerpt"} <= set(rows[0])


@pytest.mark.parametrize("title,prompt,expected", [
    ("fix broken login", "", "Bugfix"),
    ("add export command", "", "Feature / implementation"),
    ("write pytest coverage", "", "Tests"),
    ("", "", "Other"),
])
def test_classify_task(title, prompt, expected):
    assert classify_task(title, prompt) == expected


def test_redact_scrubs_secrets():
    assert "[REDACTED]" in redact("token: abc123XYZ")
    assert "[REDACTED]" in redact("key sk-abcdefghijklmnop")


def test_audit_does_not_write(store, db_path):
    import hashlib
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    audit_store(store)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_audit_prompt_excerpt(store):
    report = audit_store(store)
    for r in report.rows:
        assert isinstance(r.prompt_excerpt, str)
        assert len(r.prompt_excerpt) <= 220
