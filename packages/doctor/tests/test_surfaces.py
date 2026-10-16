"""Surface checks — one ladder (present → readable → schema → sane) per
agent session surface: devin-cli, devin-chat, copilot-cli, codex-cli,
vscode-chat. Absence is never a failure: a missing root emits no findings
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
    assert stores_devin.run(ctx) == []


def test_devin_cli_missing_db_warns(tmp_path):
    root = tmp_path / "devin"
    root.mkdir()  # data dir exists — the store was never created
    ctx = Context(data_dir=root, cwd=tmp_path)
    findings = stores_devin.run(ctx)
    assert len(findings) == 1
    assert findings[0].status is Status.WARN
    assert "missing" in findings[0].message


def test_devin_cli_zero_byte_warns(ctx):
    db = ctx.data_dir / "cli" / "sessions.db"
    db.write_bytes(b"")
    f = stores_devin.run(ctx)[0]
    assert f.status is Status.WARN
    assert "empty" in f.message


def test_devin_cli_corrupt_fails(ctx):
    (ctx.data_dir / "cli" / "sessions.db").write_bytes(b"junk junk junk")
    findings = stores_devin.run(ctx)
    assert findings[0].status is Status.FAIL
    assert "unreadable" in findings[0].message


def test_devin_cli_schema_old_warns(ctx):
    create_sessions_db(ctx.data_dir / "cli" / "sessions.db", schema_version=14)
    f = _finding(stores_devin.run(ctx), "schema v14")
    assert f.status is Status.WARN
    assert "unsupported" in f.message


def test_devin_cli_schema_unknown_warns(ctx):
    create_sessions_db(ctx.data_dir / "cli" / "sessions.db", schema_version=99)
    findings = stores_devin.run(ctx)
    f = _finding(findings, "schema v99")
    assert f.status is Status.WARN
    assert "unknown" in f.message
    # counts still run — unknown schema is not a parse refusal for COUNT(*)
    assert _finding(findings, "3 sessions")


def test_devin_cli_zero_sessions_warns(tmp_path):
    root = tmp_path / "devin"
    create_sessions_db(root / "cli" / "sessions.db", n_sessions=0)
    ctx = Context(data_dir=root, cwd=tmp_path)
    f = _finding(stores_devin.run(ctx), "0 sessions")
    assert f.status is Status.WARN
    assert "installed but unused" in f.message


def test_devin_cli_missing_ledger_fails(tmp_path):
    root = tmp_path / "devin"
    db = root / "cli" / "sessions.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE something_else (x INTEGER)")
    con.close()
    ctx = Context(data_dir=root, cwd=tmp_path)
    f = _finding(stores_devin.run(ctx), "schema detection")
    assert f.status is Status.FAIL


# --- devin-chat --------------------------------------------------------------


def test_devin_chat_healthy(ctx):
    findings = devin_chat.run(ctx)
    assert all(f.check == "devin-chat" for f in findings)
    assert all(f.status is Status.PASS for f in findings)
    assert "2 db(s)" in _finding(findings, "acp-messages").message
    assert "state.vscdb" in _finding(findings, "state.vscdb").message


def test_devin_chat_not_detected(tmp_path):
    ctx = Context(data_dir=tmp_path / "nope", cwd=tmp_path)
    assert devin_chat.run(ctx) == []


def test_devin_chat_empty_acp_dir_warns(ctx):
    acp_dir = ctx.data_dir / "User" / "acp-messages"
    for db in acp_dir.glob("*.db"):
        db.unlink()
    f = _finding(devin_chat.run(ctx), "0 db files")
    assert f.status is Status.WARN
    assert "installed but unused" in f.message


def test_devin_chat_wrong_shape_fails(ctx):
    bad = ctx.data_dir / "User" / "acp-messages" / "bogus.db"
    con = sqlite3.connect(bad)
    con.execute("CREATE TABLE something_else (x INTEGER)")
    con.close()
    f = _finding(devin_chat.run(ctx), "bogus.db")
    assert f.status is Status.FAIL


def test_devin_chat_unknown_schema_version_warns(ctx):
    db = create_acp_messages_db(
        ctx.data_dir / "User" / "acp-messages" / "future.db", seed=7
    )
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE meta SET value = '42' WHERE key = 'schema_version'"
    )
    con.commit()
    con.close()
    f = _finding(devin_chat.run(ctx), "unknown")
    assert f.status is Status.WARN
    assert "schema_version" in f.message


def test_devin_chat_missing_both_warns(tmp_path):
    root = tmp_path / "devin"
    root.mkdir()
    ctx = Context(data_dir=root, cwd=tmp_path)
    findings = devin_chat.run(ctx)
    assert {f.status for f in findings} == {Status.WARN}
    assert _finding(findings, "acp-messages dir missing")
    assert _finding(findings, "state.vscdb missing")


def test_devin_chat_detected_by_vscdb_alone(tmp_path):
    root = tmp_path / "devin"
    create_state_vscdb(root / "User" / "globalStorage" / "state.vscdb")
    ctx = Context(data_dir=root, cwd=tmp_path)
    findings = devin_chat.run(ctx)
    assert _finding(findings, "state.vscdb present").status is Status.PASS
    # the install is detected — the missing acp dir is WARN, not absent
    assert _finding(findings, "acp-messages").status is Status.WARN


# --- copilot-cli -------------------------------------------------------------


def _copilot_root(home: Path) -> Path:
    return home / ".copilot"


def test_copilot_not_detected():
    assert copilot.run(Context(data_dir=Path("/x"), cwd=Path("/x"))) == []


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
    _copilot_root(_hermetic_surfaces).mkdir(parents=True)
    f = copilot.run(Context(data_dir=Path("/x"), cwd=Path("/x")))[0]
    assert f.status is Status.WARN
    assert "installed but unused" in f.message


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
    assert codex.run(Context(data_dir=Path("/x"), cwd=Path("/x"))) == []


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
    assert "2 threads" in _finding(findings, "threads").message
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


def test_codex_corrupt_state_fails(_hermetic_surfaces):
    root = _hermetic_surfaces / ".codex"
    root.mkdir()
    (root / "state_4.sqlite").write_bytes(b"garbage not sqlite")
    f = codex.run(Context(data_dir=Path("/x"), cwd=Path("/x")))[0]
    assert f.status is Status.FAIL
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
    f = _finding(
        codex.run(Context(data_dir=Path("/x"), cwd=Path("/x"))),
        "state_4.sqlite",
    )
    assert "3 threads" in f.message


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


# --- vscode-chat -------------------------------------------------------------


def _vscode_user_dir() -> Path:
    """First candidate user dir — platform-correct under the fake HOME."""
    return paths.vscode_chat_userdir_candidates()[0]


def test_vscode_chat_not_detected():
    assert vscode_chat.run(Context(data_dir=Path("/x"), cwd=Path("/x"))) == []


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
