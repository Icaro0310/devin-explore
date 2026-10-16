"""health-of-data check: empty sessions, orphan message_nodes, stale
sessions, locked databases."""

import sqlite3

from conftest import lock_db_exclusively
from devin_doctor.checks import health
from devin_doctor.model import Context, Status
from devin_internals.fixtures import _BASE_TS_MS


def _finding(findings, needle):
    return next(f for f in findings if needle in f.message)


def test_healthy_all_pass(ctx):
    findings = health.run(ctx)
    assert all(f.status is Status.PASS for f in findings), [
        (f.status, f.message) for f in findings
    ]


def test_empty_sessions_warn(ctx):
    con = sqlite3.connect(ctx.data_dir / "cli" / "sessions.db")
    with con:
        con.execute(
            "INSERT INTO sessions(id, working_directory, backend_type, model,"
            " agent_mode, created_at, last_activity_at)"
            " VALUES ('empty-fixture-session', '/x', 'b', 'm', 'mode', ?, ?)",
            (_BASE_TS_MS, _BASE_TS_MS),
        )
    con.close()
    findings = health.run(ctx)
    f = _finding(findings, "empty")
    assert f.status is Status.WARN
    assert "1" in f.message


def test_orphan_message_nodes_warn(ctx):
    con = sqlite3.connect(ctx.data_dir / "cli" / "sessions.db")
    with con:
        con.execute(
            "INSERT INTO message_nodes(session_id, node_id, chat_message,"
            " created_at) VALUES ('ghost-session', 1, '{}', ?)",
            (_BASE_TS_MS,),
        )
    con.close()
    findings = health.run(ctx)
    f = _finding(findings, "orphan")
    assert f.status is Status.WARN
    assert "1" in f.message


def test_stale_sessions_warn(ctx):
    ctx.now_ms = _BASE_TS_MS + 400 * 86_400_000  # ~400 days later
    findings = health.run(ctx)
    f = _finding(findings, "inactive")
    assert f.status is Status.WARN
    assert "3" in f.message


def test_no_stale_when_recent(ctx):
    findings = health.run(ctx)
    f = _finding(findings, "inactive")
    assert f.status is Status.PASS


def test_locked_db_warns(ctx):
    db = ctx.data_dir / "cli" / "sessions.db"
    holder = lock_db_exclusively(db)
    try:
        findings = health.run(ctx)
        f = _finding(findings, "SQLITE_BUSY")
        assert f.status is Status.WARN
        assert "running" in f.message.lower() or "busy" in f.message.lower()
    finally:
        holder.rollback()
        holder.close()


def test_zero_sessions_warns(tmp_path):
    from devin_internals.fixtures import create_sessions_db

    root = tmp_path / "devin"
    create_sessions_db(root / "cli" / "sessions.db", n_sessions=0)
    ctx = Context(data_dir=root, cwd=tmp_path, now_ms=_BASE_TS_MS + 10**9)
    f = _finding(health.run(ctx), "0 sessions")
    assert f.status is Status.WARN


def test_unreadable_sessions_db_skips(ctx):
    (ctx.data_dir / "cli" / "sessions.db").write_bytes(b"junk")
    findings = health.run(ctx)
    f = _finding(findings, "sessions.db")
    assert f.status is Status.WARN
    assert "skipped" in f.message.lower() or "unreadable" in f.message.lower()


def test_desktop_only_no_sessions_db_not_detected(tmp_path):
    """A data root without cli/sessions.db has no CLI data to inspect —
    no false unreadable warning (Devin Review #3). But a Desktop ``User/``
    anchor means the lock check still runs over Desktop stores."""
    root = tmp_path / "devin"
    (root / "User").mkdir(parents=True)  # Desktop marker, no CLI store
    ctx = Context(data_dir=root, cwd=tmp_path)
    findings = health.run(ctx)
    assert [f.status for f in findings] == [Status.PASS]
    assert "no locked database files" in findings[0].message


def test_desktop_only_locked_db_warns(tmp_path):
    """Desktop-only install (no cli/sessions.db) still gets the lock
    check over its own stores — a locked acp db warns (re-review
    b2dc8f0f)."""
    root = tmp_path / "devin"
    acp = root / "User" / "acp-messages"
    acp.mkdir(parents=True)
    db = acp / "s.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE app_state(key TEXT, value TEXT)")
    con.commit()
    con.close()
    holder = lock_db_exclusively(db)
    try:
        findings = health.run(Context(data_dir=root, cwd=tmp_path))
        f = _finding(findings, "locked")
        assert f.status is Status.WARN
        assert "s.db" in f.message
    finally:
        holder.rollback()
        holder.close()


def test_empty_data_dir_not_detected(tmp_path):
    """Bare shared data root — no CLI store, no Desktop anchors, nothing
    to warn about (still 'not detected')."""
    root = tmp_path / "devin"
    root.mkdir()
    ctx = Context(data_dir=root, cwd=tmp_path)
    assert health.run(ctx) == []


def test_desktop_only_data_dir_is_not_detected(tmp_path):
    """A data root without ``cli/sessions.db`` is a Desktop-only install —
    no spurious 'sessions.db unreadable' warning (devin-review on #85).
    Desktop anchors (``User/globalStorage``) mean the lock check still
    runs over Desktop stores — so the verdict is the lock-check PASS,
    not silence."""
    root = tmp_path / "devin"
    (root / "User" / "globalStorage").mkdir(parents=True)
    ctx = Context(data_dir=root, cwd=tmp_path)
    findings = health.run(ctx)
    assert [f.status for f in findings] == [Status.PASS]
    assert "no locked database files" in findings[0].message
