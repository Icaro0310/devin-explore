"""CLI contract: status / report / milestones / registry + exit codes."""

from __future__ import annotations

import json

import pytest
from devin_pm.cli import main
from devin_internals.schema import LATEST_KNOWN_SCHEMA


@pytest.fixture
def db_flag(sessions_db):
    return ["--sessions-db", str(sessions_db)]


def test_status_table(db_flag, capsys):
    assert main(["status", *db_flag]) == 0
    out = capsys.readouterr().out
    assert "PROJECT" in out and "SESSIONS" in out
    assert "alpha" in out and "beta" in out


def test_status_json(db_flag, capsys):
    assert main(["status", *db_flag, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    by_name = {p["name"]: p for p in data}
    assert by_name["alpha"]["sessions"] == 3
    assert by_name["alpha"]["status"]["hidden"] == 1
    assert by_name["beta"]["cost"] is None


def test_report_project(db_flag, capsys):
    assert main(["report", *db_flag, "--project", "alpha"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Project: alpha")
    assert "## Sessions" in out
    assert "- [x] Alpha scaffold" in out


def test_report_project_case_insensitive(db_flag, capsys):
    assert main(["report", *db_flag, "--project", "ALPHA"]) == 0
    assert "# Project: alpha" in capsys.readouterr().out


def test_report_global_when_no_project(db_flag, capsys):
    assert main(["report", *db_flag]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# devin-pm status report")
    assert "## alpha" in out and "## beta" in out


def test_report_out_file(db_flag, capsys, tmp_path):
    out_file = tmp_path / "alpha.md"
    assert (
        main(["report", *db_flag, "--project", "alpha", "--out", str(out_file)])
        == 0
    )
    assert out_file.read_text(encoding="utf-8").startswith("# Project: alpha")
    assert "wrote" in capsys.readouterr().out.lower()


def test_report_unknown_project(db_flag, capsys):
    assert main(["report", *db_flag, "--project", "nope"]) == 2
    err = capsys.readouterr().err
    assert "nope" in err and "alpha" in err  # lists known projects


def test_milestones_listing(db_flag, capsys):
    assert main(["milestones", *db_flag, "--project", "alpha"]) == 0
    out = capsys.readouterr().out
    assert "- [x] Alpha scaffold" in out
    assert "Done: 1/1 (100%)" in out


def test_milestones_json(db_flag, capsys):
    assert main(["milestones", *db_flag, "--project", "beta", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["project"] == "beta"
    assert data["total"] == 1 and data["done"] == 0
    assert data["milestones"][0]["name"] == "Beta MVP"


def test_milestones_unknown_project(db_flag, capsys):
    assert main(["milestones", *db_flag, "--project", "nope"]) == 2


def test_milestones_malformed_file(db_flag, capsys, sessions_db):
    # sessions_db fixture lives in workdir; corrupt alpha's file
    alpha_dir = sessions_db.parent / "alpha"
    (alpha_dir / "milestones.json").write_text("{bad", encoding="utf-8")
    assert main(["milestones", *db_flag, "--project", "alpha"]) == 1
    assert "invalid" in capsys.readouterr().err.lower()


def test_registry_stdout(db_flag, capsys):
    assert main(["registry", *db_flag]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["version"] == 1
    assert data["totals"]["projects"] == 2
    names = {p["name"] for p in data["projects"]}
    assert names == {"alpha", "beta"}


def test_registry_out_file(db_flag, tmp_path):
    out_file = tmp_path / "registry.json"
    assert main(["registry", *db_flag, "--out", str(out_file)]) == 0
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["source"]["sessions_db"].endswith("sessions.db")
    # fixture emits the installed spec's latest schema (v17 at 0.3.1,
    # v18 at 0.3.3) — pin to the constant, not a literal
    assert data["source"]["schema_version"] == LATEST_KNOWN_SCHEMA


def test_missing_db_exits_2(tmp_path, capsys):
    missing = tmp_path / "no.db"
    assert main(["status", "--sessions-db", str(missing)]) == 2
    assert "no sessions.db" in capsys.readouterr().err.lower()


def test_not_a_sessions_db(tmp_path, capsys):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not sqlite at all")
    assert main(["status", "--sessions-db", str(junk)]) == 1


def test_readonly_never_writes(sessions_db, db_flag):
    """The CLI must open the DB read-only — size/mtime stay untouched."""
    before = sessions_db.read_bytes()
    main(["status", *db_flag])
    main(["registry", *db_flag])
    assert sessions_db.read_bytes() == before
