"""Surface checks — one ladder (present → readable → schema → sane) per
agent session surface: devin-cli, devin-chat, copilot-cli, codex-cli,
vscode-chat. Absence is never a failure: a missing root emits the
canonical ``present PASS / not-installed`` finding (SURFACE-HEALTH §3.1)
and the surface groups under 'not detected'."""

import json
import sqlite3
from pathlib import Path

import pytest
from conftest import (
    BASE_MS,
    add_codex_home,
    add_copilot_session,
    add_vscode_chat,
    codex_record,
    copilot_event,
    lock_db_exclusively,
)
from devin_doctor import paths
from devin_doctor.checks.surfaces import (
    SURFACE_NAMES,
    codex,
    copilot,
    devin_chat,
    stores_devin,
    vscode_chat,
)
from devin_doctor.model import Context, Status
from devin_internals.fixtures import (
    create_acp_messages_db,
    create_sessions_db,
    create_state_vscdb,
)


def _finding(findings, needle):
    return next(f for f in findings if needle in f.message)


def _assert_not_installed(findings):
    """The canonical absent-surface finding: present PASS +
    ``note: not-installed`` (SURFACE-HEALTH §3.1)."""
    assert len(findings) == 1
    f = findings[0]
    assert f.status is Status.PASS
    assert f.rung == "present"
    assert f.note == "not-installed"
    return f


# --- registry ---------------------------------------------------------------


def test_surface_names_match_spec_vocabulary():
    assert SURFACE_NAMES == [
        "devin-cli",
        "devin-chat",
        "copilot-cli",
        "codex-cli",
        "vscode-chat",
    ]


# --- devin-cli --------------------------------------------------------------


def test_devin_cli_healthy(ctx):
    findings = stores_devin.run(ctx)
    assert all(f.check == "devin-cli" for f in findings)
    assert all(f.status is Status.PASS for f in findings)
    assert "sessions.db present" in findings[0].message
    assert "v17" in _finding(findings, "schema").message
    assert "3 sessions" in _finding(findings, "3 sessions").message


def test_devin_cli_not_detected(tmp_path):
    ctx = Context(data_dir=tmp_path / "nope", cwd=tmp_path)
    _assert_not_installed(stores_devin.run(ctx))


def test_devin_cli_missing_db_warns(tmp_path):
    root = tmp_path / "devin"
    (root / "cli").mkdir(parents=True)  # CLI ran once — the store was never created
    ctx = Context(data_dir=root, cwd=tmp_path)
    findings = stores_devin.run(ctx)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "missing" in findings[0].message
    assert findings[0].rung == "present"
    assert findings[0].code == "store-missing"


def test_devin_cli_bare_root_not_detected(tmp_path):
    """A bare data root has no CLI anchor — on case-insensitive
    filesystems it may just be the unified Desktop config root."""
    root = tmp_path / "devin"
    (root / "User").mkdir(parents=True)  # Desktop-only anchors
    ctx = Context(data_dir=root, cwd=tmp_path)
    _assert_not_installed(stores_devin.run(ctx))


def test_devin_cli_zero_byte_warns(ctx):
    db = ctx.data_dir / "cli" / "sessions.db"
    db.write_bytes(b"")
    f = _finding(stores_devin.run(ctx), "empty")
    assert f.status is Status.WARN
    assert f.rung == "sane"
    assert f.code == "empty-store"


def test_devin_cli_corrupt_fails(ctx):
    (ctx.data_dir / "cli" / "sessions.db").write_bytes(b"junk junk junk")
    findings = stores_devin.run(ctx)
    f = _finding(findings, "unreadable")
    assert f.status is Status.FAIL
    assert f.rung == "readable"
    assert f.code == "store-corrupt"


def test_devin_cli_locked_db_warns_not_corrupt(ctx):
    """SQLITE_BUSY is a transient lock — Devin is running — not corruption.
    The surface ladder must agree with health-of-data's WARN, not FAIL the
    run with ``store-corrupt``."""
    holder = lock_db_exclusively(ctx.data_dir / "cli" / "sessions.db")
    try:
        findings = stores_devin.run(ctx)
    finally:
        holder.rollback()
        holder.close()
    assert not any(f.status is Status.FAIL for f in findings)
    f = _finding(findings, "locked")
    assert f.status is Status.WARN
    assert f.code == "unreadable"
    assert "closed" in (f.fix or "")


def test_devin_cli_schema_old_warns(ctx):
    create_sessions_db(ctx.data_dir / "cli" / "sessions.db", schema_version=14)
    f = _finding(stores_devin.run(ctx), "schema v14")
    assert f.status is Status.WARN
    assert "below the supported floor" in f.message
    assert f.code == "schema-old"


def test_devin_cli_schema_unknown_warns(ctx):
    create_sessions_db(ctx.data_dir / "cli" / "sessions.db", schema_version=99)
    findings = stores_devin.run(ctx)
    f = _finding(findings, "schema v99")
    assert f.status is Status.WARN
    assert "unknown" in f.message
    assert f.code == "schema-unknown"
    # counts still run — unknown schema is not a parse refusal for COUNT(*)
    assert _finding(findings, "3 sessions")


def test_devin_cli_zero_sessions_warns(tmp_path):
    root = tmp_path / "devin"
    create_sessions_db(root / "cli" / "sessions.db", n_sessions=0)
    ctx = Context(data_dir=root, cwd=tmp_path)
    f = _finding(stores_devin.run(ctx), "0 sessions")
    assert f.status is Status.WARN
    assert "installed but unused" in f.message
    assert f.code == "store-empty"


def test_devin_cli_missing_ledger_warns_schema_unknown(tmp_path):
    """§4.1: a store without the migration ledger is schema-unknown —
    WARN, never a FAIL (the file opened fine)."""
    root = tmp_path / "devin"
    db = root / "cli" / "sessions.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE something_else (x INTEGER)")
    con.close()
    ctx = Context(data_dir=root, cwd=tmp_path)
    findings = stores_devin.run(ctx)
    f = _finding(findings, "no readable schema version")
    assert f.status is Status.WARN
    assert f.rung == "schema"
    assert f.code == "schema-unknown"
    assert not any(f.status is Status.FAIL for f in findings)


# --- devin-chat --------------------------------------------------------------


def test_devin_chat_healthy(ctx):
    findings = devin_chat.run(ctx)
    assert all(f.check == "devin-chat" for f in findings)
    assert all(f.status is Status.PASS for f in findings)
    assert "2 db file(s)" in _finding(findings, "acp-messages").message
    assert "state.vscdb" in _finding(findings, "state.vscdb").message


def test_devin_chat_not_detected(tmp_path):
    ctx = Context(data_dir=tmp_path / "nope", cwd=tmp_path)
    _assert_not_installed(devin_chat.run(ctx))


def test_devin_chat_empty_acp_dir_warns(ctx):
    acp_dir = ctx.data_dir / "User" / "acp-messages"
    for db in acp_dir.glob("*.db"):
        db.unlink()
    f = _finding(devin_chat.run(ctx), "0 db files")
    assert f.status is Status.WARN
    assert "installed but unused" in f.message
    assert f.code == "store-empty"


def test_devin_chat_wrong_shape_warns_schema_unknown(ctx):
    """A db that opens but lacks the meta+messages shape is
    schema-unknown WARN, not corrupt — it parsed fine (§4.2)."""
    bad = ctx.data_dir / "User" / "acp-messages" / "bogus.db"
    con = sqlite3.connect(bad)
    con.execute("CREATE TABLE something_else (x INTEGER)")
    con.close()
    findings = devin_chat.run(ctx)
    f = _finding(findings, "bogus.db")
    assert f.status is Status.WARN
    assert f.rung == "schema"
    assert f.code == "schema-unknown"
    assert not any(f.status is Status.FAIL for f in findings)


def test_devin_chat_unknown_schema_version_still_passes(ctx):
    """§4.2's shape check is meta+messages presence only — an unknown
    schema_version value does not WARN (no version marker is
    known-good here; only the table shape is)."""
    db = create_acp_messages_db(
        ctx.data_dir / "User" / "acp-messages" / "future.db", seed=7
    )
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE meta SET value = '42' WHERE key = 'schema_version'"
    )
    con.commit()
    con.close()
    findings = devin_chat.run(ctx)
    assert not any(f.status is Status.FAIL for f in findings)
    assert all(f.code != "schema-unknown" for f in findings)


def test_devin_chat_cli_only_root_not_detected(tmp_path):
    """A bare Devin data root is shared by CLI and Desktop — it is NOT
    evidence of a Desktop install (Devin Review #2)."""
    root = tmp_path / "devin"
    root.mkdir()
    create_sessions_db(root / "cli" / "sessions.db")
    ctx = Context(data_dir=root, cwd=tmp_path)
    _assert_not_installed(devin_chat.run(ctx))


def test_devin_chat_missing_stores_warns(tmp_path):
    """A Desktop ``User/`` marker with no acp-messages container →
    present WARN store-missing (§4.2 anchor = the container)."""
    root = tmp_path / "devin"
    (root / "User").mkdir(parents=True)
    ctx = Context(data_dir=root, cwd=tmp_path)
    findings = devin_chat.run(ctx)
    assert {f.status for f in findings} == {Status.WARN}
    f = _finding(findings, "acp-messages dir missing")
    assert f.rung == "present"
    assert f.code == "store-missing"


def test_devin_chat_zero_byte_db_warns(ctx):
    """A 0 B acp db must not hide behind a healthy sibling's counts
    (Devin Review #5 — sane verdict is WARN empty-store)."""
    (ctx.data_dir / "User" / "acp-messages" / "empty.db").write_bytes(b"")
    findings = devin_chat.run(ctx)
    f = _finding(findings, "empty-store")
    assert f.status is Status.WARN
    assert "zero-byte" in f.message
    assert f.rung == "sane"
    assert f.code == "empty-store"


def test_devin_chat_corrupt_vscdb_fails(tmp_path):
    """A state.vscdb whose pages can't be read is store-corrupt FAIL —
    a damaged store, not a foreign shape (Devin Review). The file keeps
    a readable sqlite_master (page 1 intact) so the failure surfaces
    while the spec store reads its tables, exercising the
    sqlite3.Error → corrupt reclassification."""
    root = tmp_path / "devin"
    root.mkdir()
    donor = root / "donor.db"
    con = sqlite3.connect(donor)
    con.execute("CREATE TABLE ItemTable(key TEXT PRIMARY KEY, value BLOB)")
    con.executemany(
        "INSERT INTO ItemTable VALUES (?, ?)",
        [(f"k{i}", b"x" * 4000) for i in range(50)],
    )
    con.commit()
    con.close()
    data = bytearray(donor.read_bytes())
    data[8192:12288] = b"\xde\xad" * 2048  # corrupt a mid-file page
    path = root / "User" / "globalStorage" / "state.vscdb"
    path.parent.mkdir(parents=True)
    path.write_bytes(bytes(data))
    ctx = Context(data_dir=root, cwd=tmp_path)
    findings = devin_chat.run(ctx)
    f = _finding(findings, "state.vscdb")
    assert f.status is Status.FAIL
    assert f.code == "store-corrupt"
    assert f.rung == "readable"


def test_devin_chat_detected_by_vscdb_alone(tmp_path):
    root = tmp_path / "devin"
    create_state_vscdb(root / "User" / "globalStorage" / "state.vscdb")
    ctx = Context(data_dir=root, cwd=tmp_path)
    findings = devin_chat.run(ctx)
    assert _finding(findings, "state.vscdb present").status is Status.PASS
    # the install is detected — the missing acp dir is WARN, not absent
    assert _finding(findings, "acp-messages").status is Status.WARN


def test_devin_chat_locked_state_vscdb_warns_not_corrupt(tmp_path):
    """A locked state.vscdb means the Desktop app is running — WARN, not a
    corruption FAIL."""
    root = tmp_path / "devin"
    path = root / "User" / "globalStorage" / "state.vscdb"
    create_state_vscdb(path)
    holder = sqlite3.connect(path)
    holder.execute("PRAGMA journal_mode=DELETE")
    holder.execute("BEGIN EXCLUSIVE")
    holder.execute("INSERT INTO ItemTable(key, value) VALUES ('qa.lock', '1')")
    try:
        findings = devin_chat.run(Context(data_dir=root, cwd=tmp_path))
    finally:
        holder.rollback()
        holder.close()
    assert not any(f.status is Status.FAIL for f in findings)
    f = _finding(findings, "state.vscdb")
    assert f.status is Status.WARN
    assert "locked" in f.message
    assert f.code == "unreadable"


# --- copilot-cli -------------------------------------------------------------


def _copilot_root(home: Path) -> Path:
    return home / ".copilot"


def test_copilot_not_detected():
    _assert_not_installed(
        copilot.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    )


def test_copilot_healthy(_hermetic_surfaces):
    add_copilot_session(
        _copilot_root(_hermetic_surfaces),
        "sess-1",
        [
            copilot_event("session.start", {"version": "0.1"}, idx=0),
            copilot_event("user.message", {"content": "hi"}, idx=1),
        ],
        workspace={"git_root": "/fixture/workspace/copilot"},
    )
    findings = copilot.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    assert all(f.status is Status.PASS for f in findings)
    assert "1 session log(s)" in _finding(findings, "session log").message
    assert "2 events" in _finding(findings, "events").message


def test_copilot_env_var_root(_hermetic_surfaces, monkeypatch):
    root = _hermetic_surfaces / "custom-copilot"
    add_copilot_session(
        root, "s", [copilot_event("session.start", {}, idx=0)]
    )
    monkeypatch.setenv("COPILOT_DIR", str(root))
    findings = copilot.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    assert "1 session log(s)" in _finding(findings, "session log").message


def test_copilot_root_without_sessions_warns(_hermetic_surfaces):
    """A root holding config but no session-state container is
    store-missing at present — not 'empty' (§4.3)."""
    _copilot_root(_hermetic_surfaces).mkdir(parents=True)
    f = copilot.run(Context(data_dir=Path("/x"), cwd=Path("/x")))[0]
    assert f.status is Status.WARN
    assert f.rung == "present"
    assert f.code == "store-missing"


def test_copilot_corrupt_log_fails(_hermetic_surfaces):
    root = _copilot_root(_hermetic_surfaces)
    add_copilot_session(
        root, "ok", [copilot_event("session.start", {}, idx=0)]
    )
    bad = add_copilot_session(
        root, "bad", [copilot_event("session.start", {}, idx=0)]
    )
    bad.write_text("{not json\n")
    f = _finding(copilot.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
                 "unreadable")
    assert f.status is Status.FAIL
    assert "events.jsonl" in f.message


def test_copilot_malformed_tail_fails(_hermetic_surfaces):
    """Valid head + malformed later record → store-corrupt FAIL — the
    readable rung scans every line, not just the first (Devin Review #1)."""
    log = add_copilot_session(
        _copilot_root(_hermetic_surfaces),
        "damaged",
        [copilot_event("session.start", {}, idx=0)],
    )
    with log.open("a", encoding="utf-8") as fh:
        fh.write("{broken line\n")
    f = _finding(
        copilot.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
        "unreadable",
    )
    assert f.status is Status.FAIL
    assert "events.jsonl" in f.message


def test_copilot_unterminated_tail_is_not_corrupt(_hermetic_surfaces):
    """A final line without a trailing newline that fails to parse is a
    transient in-progress append — excluded, not corruption."""
    log = add_copilot_session(
        _copilot_root(_hermetic_surfaces),
        "live",
        [copilot_event("session.start", {}, idx=0)],
    )
    with log.open("a", encoding="utf-8") as fh:
        fh.write('{"id": "partial')  # writer mid-append — no newline
    findings = copilot.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    assert all(f.status is Status.PASS for f in findings)
    assert "1 events" in _finding(findings, "events").message


def test_copilot_null_record_fails(_hermetic_surfaces):
    """``null`` is valid JSON but never a valid record — a leading null
    must not let a later record become 'first' (re-review b2dc8f0f);
    null records anywhere are malformed → store-corrupt FAIL."""
    add_copilot_session(
        _copilot_root(_hermetic_surfaces),
        "nulled",
        ["null", copilot_event("session.start", {}, idx=1)],
    )
    f = _finding(
        copilot.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
        "unreadable",
    )
    assert f.status is Status.FAIL
    assert "events.jsonl" in f.message


def test_copilot_unknown_envelope_warns(_hermetic_surfaces):
    add_copilot_session(
        _copilot_root(_hermetic_surfaces),
        "odd",
        [{"unexpected": "envelope"}],
    )
    f = _finding(
        copilot.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
        "schema-unknown",
    )
    assert f.status is Status.WARN


def test_copilot_legacy_layout_seen(_hermetic_surfaces):
    add_copilot_session(
        _copilot_root(_hermetic_surfaces),
        "old",
        [copilot_event("session.start", {}, idx=0)],
        layout="legacy-flat",
    )
    f = _finding(
        copilot.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
        "session log",
    )
    assert f.status is Status.PASS


# --- codex-cli ---------------------------------------------------------------


def test_codex_not_detected():
    _assert_not_installed(
        codex.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    )


def test_codex_healthy(_hermetic_surfaces):
    add_codex_home(
        _hermetic_surfaces / ".codex",
        state_n=4,
        threads=[{"id": "t-1"}, {"id": "t-2"}],
        rollouts=[
            (
                "sessions/rollout-a.jsonl",
                [codex_record("session_meta", {"id": "r-1"})],
            )
        ],
    )
    findings = codex.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    assert all(f.status is Status.PASS for f in findings)
    assert "2 threads" in _finding(findings, "2 threads").message
    assert "1 rollout" in _finding(findings, "rollout").message


def test_codex_env_var_root(_hermetic_surfaces, monkeypatch):
    root = add_codex_home(
        _hermetic_surfaces / "custom-codex",
        state_n=1,
        threads=[{"id": "t"}],
    )
    monkeypatch.setenv("CODEX_HOME", str(root))
    f = _finding(
        codex.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
        "state_1.sqlite",
    )
    assert f.status is Status.PASS


def test_codex_root_without_stores_warns(_hermetic_surfaces):
    (_hermetic_surfaces / ".codex").mkdir()
    f = codex.run(Context(data_dir=Path("/x"), cwd=Path("/x")))[0]
    assert f.status is Status.WARN
    assert "installed but unused" in f.message
    assert f.code == "store-missing"


def test_codex_corrupt_state_fails(_hermetic_surfaces):
    root = _hermetic_surfaces / ".codex"
    root.mkdir()
    (root / "state_4.sqlite").write_bytes(b"garbage not sqlite")
    findings = codex.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    f = _finding(findings, "state_4.sqlite")
    assert f.status is Status.FAIL
    assert f.code == "store-corrupt"
    assert "magic" in f.message or "unreadable" in f.message


def test_codex_state_without_threads_warns(_hermetic_surfaces):
    root = _hermetic_surfaces / ".codex"
    root.mkdir()
    con = sqlite3.connect(root / "state_2.sqlite")
    con.execute("CREATE TABLE other (x INTEGER)")
    con.close()
    f = _finding(
        codex.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
        "threads",
    )
    assert f.status is Status.WARN
    assert "schema-unknown" in f.message


def test_codex_largest_n_wins(_hermetic_surfaces):
    root = _hermetic_surfaces / ".codex"
    root.mkdir()
    for n, count in ((2, 1), (4, 3)):
        con = sqlite3.connect(root / f"state_{n}.sqlite")
        con.execute(
            "CREATE TABLE threads(id TEXT PRIMARY KEY, created_at INTEGER,"
            " updated_at INTEGER, cwd TEXT, has_user_event INTEGER)"
        )
        for i in range(count):
            con.execute("INSERT INTO threads VALUES (?, 0, 0, '', 0)", (f"t{i}",))
        con.commit()
        con.close()
    findings = codex.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    f = _finding(findings, "3 threads")
    assert "state_4.sqlite" in f.message


def test_codex_nonnumeric_state_warns_schema_unknown(_hermetic_surfaces):
    """A ``state_*.sqlite`` without a numeric suffix is a foreign
    artifact — schema-unknown WARN, even when a numeric db is selected
    (Devin Review: precedence must not silence it)."""
    root = add_codex_home(
        _hermetic_surfaces / ".codex",
        state_n=4,
        threads=[{"id": "t-1"}],
    )
    con = sqlite3.connect(root / "state_foo.sqlite")
    con.execute("CREATE TABLE threads (id TEXT PRIMARY KEY)")
    con.close()
    findings = codex.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    # the numeric db is still selected for the ladder
    assert "state_4.sqlite" in _finding(findings, "opens read-only").message
    f = _finding(findings, "state_foo")
    assert f.status is Status.WARN
    assert f.rung == "schema"
    assert f.code == "schema-unknown"


def test_codex_rollouts_without_state(_hermetic_surfaces):
    add_codex_home(
        _hermetic_surfaces / ".codex",
        rollouts=[
            (
                "archived_sessions/rollout-old.jsonl",
                [codex_record("session_meta", {"id": "r"})],
            )
        ],
    )
    findings = codex.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    assert "1 rollout" in _finding(findings, "rollout").message


def test_codex_zero_byte_rollout_warns(_hermetic_surfaces):
    """A 0 B rollout is empty-store, not 'readable' (Devin Review #6)."""
    root = _hermetic_surfaces / ".codex"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / "rollout-empty.jsonl").write_bytes(b"")
    findings = codex.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    f = _finding(findings, "empty-store")
    assert f.status is Status.WARN
    # and it is not counted as a readable rollout
    assert all("readable" not in f.message for f in findings)


def test_codex_zero_byte_rollout_with_state_warns(_hermetic_surfaces):
    """A healthy state db must not silence an empty rollout — the sane
    verdict is WARN empty-store, not PASS (Devin Review)."""
    add_codex_home(
        _hermetic_surfaces / ".codex",
        state_n=4,
        threads=[{"id": "t-1"}],
        rollouts=[("sessions/rollout-dead.jsonl", [])],
    )
    findings = codex.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    f = _finding(findings, "empty-store")
    assert f.status is Status.WARN
    assert f.rung == "sane"
    assert f.code == "empty-store"
    # the sane rung emits exactly one verdict — no hidden PASS beside it
    sane = [g for g in findings if g.rung == "sane"]
    assert sane == [f]


def test_codex_whitespace_rollout_warns(_hermetic_surfaces):
    """A nonzero rollout with only whitespace decodes zero records —
    not 'readable': WARN empty-store (re-review b2dc8f0f)."""
    root = _hermetic_surfaces / ".codex"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / "rollout-blank.jsonl").write_text("   \n\n  \n")
    findings = codex.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    assert _finding(findings, "empty-store").status is Status.WARN
    assert all("readable" not in f.message for f in findings)


def test_codex_unterminated_only_rollout_warns(_hermetic_surfaces):
    """A rollout whose whole content is one unterminated unparseable
    line — indistinguishable from a writer mid-append — is WARN
    empty-store, not FAIL (re-review b2dc8f0f)."""
    root = _hermetic_surfaces / ".codex"
    (root / "sessions").mkdir(parents=True)
    (root / "sessions" / "rollout-part.jsonl").write_text("{partial")
    findings = codex.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    assert _finding(findings, "empty-store").status is Status.WARN
    assert all(f.status is not Status.FAIL for f in findings)


def test_codex_malformed_rollout_tail_fails(_hermetic_surfaces):
    """Valid head + malformed terminated line → FAIL (Devin Review #1)."""
    add_codex_home(
        _hermetic_surfaces / ".codex",
        rollouts=[
            (
                "sessions/rollout-bad.jsonl",
                [codex_record("session_meta", {"id": "r"}), "{nope"],
            )
        ],
    )
    f = _finding(
        codex.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
        "unreadable",
    )
    assert f.status is Status.FAIL


# --- vscode-chat -------------------------------------------------------------


def _vscode_user_dir() -> Path:
    """First candidate user dir — platform-correct under the fake HOME."""
    return paths.vscode_chat_userdir_candidates()[0]


def test_vscode_chat_not_detected():
    _assert_not_installed(
        vscode_chat.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    )


def test_vscode_chat_healthy():
    user_dir = _vscode_user_dir()
    add_vscode_chat(
        user_dir,
        "sess-1",
        doc={
            "version": 3,
            "sessionId": "sess-1",
            "creationDate": BASE_MS,
            "requests": [{"requestId": "r1", "timestamp": BASE_MS}],
        },
    )
    findings = vscode_chat.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    assert all(f.status is Status.PASS for f in findings)
    assert "1 chat session file(s)" in _finding(
        findings, "chat session"
    ).message


def test_vscode_chat_user_dir_without_sessions_warns():
    _vscode_user_dir().mkdir(parents=True)
    f = vscode_chat.run(Context(data_dir=Path("/x"), cwd=Path("/x")))[0]
    assert f.status is Status.WARN
    assert "installed but unused" in f.message


def test_vscode_chat_mutation_log_passes():
    add_vscode_chat(
        _vscode_user_dir(),
        "log-1",
        mutations=[
            {"kind": 0, "k": [], "v": {"version": 3, "requests": []}},
            {"kind": 1, "k": ["lastMessageDate"], "v": BASE_MS},
        ],
    )
    findings = vscode_chat.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    assert all(f.status is Status.PASS for f in findings)


def test_vscode_chat_corrupt_snapshot_fails():
    user_dir = _vscode_user_dir()
    path = add_vscode_chat(
        user_dir, "bad", doc={"version": 3, "requests": []}
    )
    path.write_text("{broken json")
    f = _finding(
        vscode_chat.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
        "unreadable",
    )
    assert f.status is Status.FAIL


def test_vscode_chat_unknown_shape_warns():
    add_vscode_chat(
        _vscode_user_dir(),
        "odd",
        doc={"completely": "unrelated", "data": 1},
    )
    f = _finding(
        vscode_chat.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
        "schema-unknown",
    )
    assert f.status is Status.WARN


def test_vscode_chat_unknown_mutation_kind_warns():
    add_vscode_chat(
        _vscode_user_dir(),
        "weird",
        mutations=[{"kind": 9, "k": [], "v": {}}],
    )
    f = _finding(
        vscode_chat.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
        "schema-unknown",
    )
    assert f.status is Status.WARN


def test_vscode_chat_non_int_kind_warns_not_crashes():
    """``kind`` as array/object used to crash the membership test —
    now WARN schema-unknown, never FAIL (Devin Review #7)."""
    add_vscode_chat(
        _vscode_user_dir(),
        "weird2",
        mutations=[{"kind": [1, 2], "k": [], "v": {}}],
    )
    findings = vscode_chat.run(Context(data_dir=Path("/x"), cwd=Path("/x")))
    assert all(f.status is not Status.FAIL for f in findings)
    assert _finding(findings, "schema-unknown").status is Status.WARN


def test_vscode_chat_malformed_tail_fails():
    """Valid head + malformed terminated mutation line → FAIL
    (Devin Review #1)."""
    path = add_vscode_chat(
        _vscode_user_dir(),
        "damaged",
        mutations=[{"kind": 0, "k": [], "v": {}}],
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{oops\n")
    f = _finding(
        vscode_chat.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
        "unreadable",
    )
    assert f.status is Status.FAIL


# --- runner scoping + grouping ------------------------------------------------


def test_run_all_scopes_surfaces(ctx):
    from devin_doctor import doctor

    report = doctor.run_all(ctx, surfaces=["devin-cli"])
    assert report.surfaces_checked == ["devin-cli"]
    assert {f.check for f in report.findings} >= {"devin-cli", "config"}
    assert not any(f.check == "devin-chat" for f in report.findings)


def test_run_all_rejects_unknown_surface(ctx):
    from devin_doctor import doctor

    with pytest.raises(ValueError, match="unknown surface"):
        doctor.run_all(ctx, surfaces=["not-a-surface"])


def test_render_text_groups_sections(ctx):
    from devin_doctor import doctor

    report = doctor.run_all(ctx)
    out = doctor.render_text(report)
    assert "surfaces" in out
    assert "devin environment" in out
    assert "devin-cli — PASS" in out
    assert "not detected: copilot-cli, codex-cli, vscode-chat" in out


def test_render_json_surfaces_block(ctx):
    from devin_doctor import doctor

    payload = json.loads(doctor.render_json(doctor.run_all(ctx), ctx))
    surfaces = payload["surfaces"]
    assert surfaces["detected"] == {"devin-cli": "PASS", "devin-chat": "PASS"}
    assert surfaces["not_detected"] == [
        "copilot-cli",
        "codex-cli",
        "vscode-chat",
    ]


def test_no_devin_nothing_hard_fails(tmp_path):
    """A machine without any Devin store is a valid environment: devin
    checks report 'not detected' instead of FAIL — exit 0."""
    from devin_doctor import doctor

    ctx = Context(
        data_dir=tmp_path / "no-devin",
        cwd=tmp_path,
        config_dir=tmp_path / "no-config",
    )
    report = doctor.run_all(ctx)
    assert not any(f.status is Status.FAIL for f in report.findings)
    out = doctor.render_text(report)
    assert "not detected" in out
    assert doctor.exit_code(report) == 0


def test_two_surfaces_smoke(tmp_path, _hermetic_surfaces):
    """Synthetic tmp tree with two surfaces: both report, the rest group
    under 'not detected', devin env stays silent."""
    from devin_doctor import doctor

    add_copilot_session(
        _hermetic_surfaces / ".copilot",
        "s",
        [copilot_event("session.start", {}, idx=0)],
    )
    add_codex_home(
        _hermetic_surfaces / ".codex",
        state_n=1,
        threads=[{"id": "t"}],
    )
    ctx = Context(data_dir=tmp_path / "absent", cwd=tmp_path)
    report = doctor.run_all(ctx)
    detected = {f.check for f in report.findings}
    assert {"copilot-cli", "codex-cli"} <= detected
    out = doctor.render_text(report)
    assert "copilot-cli — PASS" in out
    assert "codex-cli — PASS" in out
    assert "not detected: devin-cli, devin-chat, vscode-chat" in out
