"""``devin-pm digest``: watermark filtering, the ``pm-digest`` cursor
(``--incremental``), and the incrementality envelope — atomic report
write, cursor commit after the work, fail-open on corrupt state,
``DEVIN_CURSOR_DIR`` override.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest
from conftest import BASE_TS_MS, insert_session
from devin_internals.cursor import Cursor
from devin_pm.cli import main
from devin_pm.digest import (
    CURSOR_NAME,
    SLACK_SAFE_LIMIT,
    digest_filename,
    parse_since,
    render_slack_digest,
    run_digest,
)
from devin_pm.projects import group_sessions, load_sessions

# fixture activity offsets (created_at + 120_000 default):
# a-1 BASE+120_000 · a-2 BASE+3_720_000 · a-3 BASE+7_320_000 ·
# b-1 BASE+10_920_000 · b-2 BASE+14_520_000
MAX_ACTIVITY = BASE_TS_MS + 14_520_000


@pytest.fixture
def db_flag(sessions_db):
    return ["--sessions-db", str(sessions_db)]


@pytest.fixture
def cursor_dir(tmp_path, monkeypatch):
    """Point ``DEVIN_CURSOR_DIR`` at a tmp dir; return it."""
    d = tmp_path / "cursors"
    monkeypatch.setenv("DEVIN_CURSOR_DIR", str(d))
    return d


def _state_file(cursor_dir):
    return cursor_dir / f"{CURSOR_NAME}-state.json"


def _digests(out_dir):
    return sorted(out_dir.glob("pm-digest-*.md"))


# ---------------------------------------------------------------------------
# parse_since
# ---------------------------------------------------------------------------


def test_parse_since_epoch_ms_passthrough():
    assert parse_since("1780000000000") == 1_780_000_000_000


def test_parse_since_epoch_seconds_scaled():
    assert parse_since("1780000000") == 1_780_000_000_000


def test_parse_since_zero_means_full():
    assert parse_since("0") == 0


def test_parse_since_iso_date_and_datetime():
    expected = int(
        datetime(2026, 5, 19, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert parse_since("2026-05-19") == expected
    assert parse_since("2026-05-19T00:00:00Z") == expected
    aware = parse_since("2026-05-19T02:00:00+02:00")
    assert aware == expected


def test_parse_since_naive_is_utc():
    dt = parse_since("2026-05-19T10:30:00")
    assert dt == int(
        datetime(2026, 5, 19, 10, 30, tzinfo=timezone.utc).timestamp()
        * 1000
    )


@pytest.mark.parametrize("bad", ["garbage", "", "2026-13-45", "1.2.3"])
def test_parse_since_rejects_junk(bad):
    with pytest.raises(ValueError):
        parse_since(bad)


def test_cli_since_junk_exits_2(db_flag, capsys):
    assert main(["digest", *db_flag, "--since", "bogus"]) == 2
    assert "bogus" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# filename / rendering
# ---------------------------------------------------------------------------


def test_digest_filename_iso_week():
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    assert digest_filename(now) == "pm-digest-2026-W43.md"


def test_digest_writes_week_file(sessions_db, db_flag, tmp_path, capsys):
    assert (
        main(["digest", *db_flag, "--since", "0", "--out-dir",
              str(tmp_path)])
        == 0
    )
    files = _digests(tmp_path)
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert text.startswith("# devin-pm weekly digest — ")
    assert "since 1970-01-01" in text  # baseline 0 renders as epoch
    assert "## alpha" in text and "## beta" in text
    assert "`a-1`" in text and "`b-2`" in text
    # a-2 is the hidden `milestone:` session → done, in-window
    assert "- [x] Alpha scaffold _(this period)_" in text
    assert "- [ ] Beta MVP _(this period)_" in text
    assert "wrote" in capsys.readouterr().out
    # atomic write: no tmp residue
    assert not list(tmp_path.glob("*.tmp"))


def test_digest_since_filters_window(sessions_db, db_flag, tmp_path):
    # only b-2 is newer than b-1's activity mark
    assert (
        main(
            [
                "digest",
                *db_flag,
                "--since",
                str(BASE_TS_MS + 11_000_000),
                "--out-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    text = _digests(tmp_path)[0].read_text(encoding="utf-8")
    assert "## beta" in text and "## alpha" not in text
    assert "`b-2`" in text and "`b-1`" not in text


def test_digest_since_iso_accepted(sessions_db, db_flag, tmp_path):
    assert (
        main(
            ["digest", *db_flag, "--since", "2020-01-01", "--out-dir",
             str(tmp_path)]
        )
        == 0
    )
    assert _digests(tmp_path)


def test_digest_far_future_since_nothing_new(db_flag, tmp_path, capsys):
    assert (
        main(
            ["digest", *db_flag, "--since", "9999999999999",
             "--out-dir", str(tmp_path)]
        )
        == 0
    )
    assert "no new project activity" in capsys.readouterr().out
    assert _digests(tmp_path) == []


def test_digest_default_window_is_seven_days(sessions_db, db_flag,
                                             tmp_path, capsys):
    """Bare ``digest``: fixture activity is months old → nothing new;
    a session active right now enters the window alone."""
    now_ms = int(time.time() * 1000)
    insert_session(
        sessions_db,
        sid="a-new",
        working_directory=str(sessions_db.parent / "alpha"),
        title="alpha: today",
        created_at=now_ms - 60_000,
        last_activity_at=now_ms,
    )
    assert main(["digest", *db_flag, "--out-dir", str(tmp_path)]) == 0
    text = _digests(tmp_path)[0].read_text(encoding="utf-8")
    assert "`a-new`" in text
    assert "`a-1`" not in text and "## beta" not in text


def test_digest_all_old_activity_nothing_new(sessions_db, db_flag,
                                             tmp_path, capsys):
    """Same bare run without a fresh session → no report, no file."""
    assert main(["digest", *db_flag, "--out-dir", str(tmp_path)]) == 0
    assert "no new project activity" in capsys.readouterr().out
    assert _digests(tmp_path) == []


# ---------------------------------------------------------------------------
# stdout mode
# ---------------------------------------------------------------------------


def test_digest_stdout_prints_no_file(sessions_db, db_flag, tmp_path,
                                      monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert (
        main(["digest", *db_flag, "--since", "0", "--stdout"]) == 0
    )
    out = capsys.readouterr().out
    assert out.startswith("# devin-pm weekly digest")
    assert "## alpha" in out
    assert _digests(tmp_path) == []


def test_digest_stdout_incremental_still_commits(
    sessions_db, db_flag, cursor_dir, capsys
):
    """``--stdout`` is a real run: the cursor still advances."""
    assert (
        main(["digest", *db_flag, "--incremental", "--stdout"]) == 0
    )
    capsys.readouterr()
    committed = json.loads(_state_file(cursor_dir).read_text())
    assert committed["last_activity_at"] == MAX_ACTIVITY


# ---------------------------------------------------------------------------
# --slack (mrkdwn, verbatim-postable)
# ---------------------------------------------------------------------------


def test_digest_slack_format(sessions_db, db_flag, capsys):
    """``--slack`` prints mrkdwn: bold headers, bullets, no md tables."""
    assert main(["digest", *db_flag, "--since", "0", "--slack"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("*devin-pm weekly digest — ")
    assert "|---|" not in out and "| id |" not in out
    assert "### Sessions" not in out
    assert "*alpha*" in out and "*beta*" in out
    assert "• `a-1`" in out and "• `b-2`" in out
    # milestones render as checkbox bullets, not a section header
    assert "• [x] Alpha scaffold _(this period)_" in out


def test_digest_slack_writes_no_file(sessions_db, db_flag, tmp_path,
                                     monkeypatch, capsys):
    """``--slack`` implies stdout — the week file is not written."""
    monkeypatch.chdir(tmp_path)
    assert main(["digest", *db_flag, "--since", "0", "--slack"]) == 0
    capsys.readouterr()
    assert _digests(tmp_path) == []


def test_digest_slack_nothing_new(sessions_db, db_flag, capsys):
    assert (
        main(["digest", *db_flag, "--since", "9999999999999", "--slack"])
        == 0
    )
    assert "no new project activity" in capsys.readouterr().out


def test_digest_slack_incremental_still_commits(
    sessions_db, db_flag, cursor_dir, capsys
):
    """``--slack`` is a real run too: the cursor still advances."""
    assert (
        main(["digest", *db_flag, "--incremental", "--slack"]) == 0
    )
    capsys.readouterr()
    committed = json.loads(_state_file(cursor_dir).read_text())
    assert committed["last_activity_at"] == MAX_ACTIVITY


def test_slack_digest_char_cap_truncates(sessions_db):
    """Over-cap output drops whole lines and says how many."""
    projects = group_sessions(load_sessions(sessions_db))
    out = render_slack_digest(projects, {}, since_ms=0, max_chars=200)
    assert len(out) <= 200
    assert "truncated" in out and "lines dropped" in out
    assert out.startswith("*devin-pm weekly digest")


def test_slack_digest_default_cap(sessions_db):
    projects = group_sessions(load_sessions(sessions_db))
    out = render_slack_digest(projects, {}, since_ms=0)
    assert len(out) <= SLACK_SAFE_LIMIT


# ---------------------------------------------------------------------------
# incremental cursor mode
# ---------------------------------------------------------------------------


def test_incremental_first_run_full_then_nothing_new(
    sessions_db, db_flag, cursor_dir, tmp_path, capsys
):
    out_dir = tmp_path / "digests"
    assert (
        main(["digest", *db_flag, "--incremental",
              "--out-dir", str(out_dir)])
        == 0
    )
    files = _digests(out_dir)
    assert len(files) == 1
    first_bytes = files[0].read_bytes()
    assert "## alpha" in first_bytes.decode()

    # watermark committed AFTER the report, at max last_activity_at
    committed = json.loads(_state_file(cursor_dir).read_text())
    assert committed["last_activity_at"] == MAX_ACTIVITY
    assert "generated_at" in committed

    # second run: no new activity → message, file untouched
    assert (
        main(["digest", *db_flag, "--incremental",
              "--out-dir", str(out_dir)])
        == 0
    )
    assert "no new project activity" in capsys.readouterr().out
    assert _digests(out_dir) == files
    assert files[0].read_bytes() == first_bytes


def test_incremental_picks_up_new_session(
    sessions_db, db_flag, cursor_dir, tmp_path, capsys
):
    out_dir = tmp_path / "digests"
    assert (
        main(["digest", *db_flag, "--incremental",
              "--out-dir", str(out_dir)])
        == 0
    )
    capsys.readouterr()

    insert_session(
        sessions_db,
        sid="a-4",
        working_directory=str(sessions_db.parent / "alpha"),
        title="alpha: post-digest work",
        created_at=BASE_TS_MS + 20_000_000,
        last_activity_at=BASE_TS_MS + 20_120_000,
    )
    assert (
        main(["digest", *db_flag, "--incremental", "--stdout"])
        == 0
    )
    out = capsys.readouterr().out
    assert "`a-4`" in out
    assert "`a-1`" not in out and "## beta" not in out

    # watermark advanced to the new session's activity
    committed = json.loads(_state_file(cursor_dir).read_text())
    assert committed["last_activity_at"] == BASE_TS_MS + 20_120_000


def test_incremental_corrupt_state_fails_open(
    sessions_db, db_flag, cursor_dir, tmp_path
):
    """Garbage in the state file → full digest, exit 0, state healed."""
    cursor_dir.mkdir(parents=True)
    _state_file(cursor_dir).write_text("{not json at all",
                                       encoding="utf-8")
    assert (
        main(["digest", *db_flag, "--incremental",
              "--out-dir", str(tmp_path)])
        == 0
    )
    text = _digests(tmp_path)[0].read_text(encoding="utf-8")
    assert "## alpha" in text and "## beta" in text
    assert "`a-1`" in text and "`b-2`" in text  # full run
    committed = json.loads(_state_file(cursor_dir).read_text())
    assert committed["last_activity_at"] == MAX_ACTIVITY


def test_incremental_garbage_value_fails_open(
    sessions_db, db_flag, cursor_dir, tmp_path
):
    """Valid JSON, garbage watermark value → same fail-open path."""
    cursor_dir.mkdir(parents=True)
    _state_file(cursor_dir).write_text(
        json.dumps({"last_activity_at": "watermark??"}),
        encoding="utf-8",
    )
    assert (
        main(["digest", *db_flag, "--incremental",
              "--out-dir", str(tmp_path)])
        == 0
    )
    assert _digests(tmp_path)


def test_cursor_dir_env_honored(sessions_db, db_flag, cursor_dir,
                              tmp_path, monkeypatch):
    """State lands under DEVIN_CURSOR_DIR, never in ``./.devin``."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert (
        main(["digest", *db_flag, "--incremental",
              "--out-dir", str(tmp_path)])
        == 0
    )
    assert _state_file(cursor_dir).is_file()
    assert not (cwd / ".devin").exists()


def test_since_flag_overrides_cursor(
    sessions_db, db_flag, cursor_dir, tmp_path
):
    """Explicit ``--since`` wins as baseline; cursor still advances."""
    out_dir = tmp_path / "digests"
    # seed a high watermark, then ask for an older baseline explicitly
    c = Cursor(CURSOR_NAME, dir=cursor_dir)
    c.set("last_activity_at", MAX_ACTIVITY)
    c.commit()
    assert (
        main(["digest", *db_flag, "--incremental", "--since",
              str(BASE_TS_MS + 8_000_000), "--out-dir", str(out_dir)])
        == 0
    )
    text = _digests(out_dir)[0].read_text(encoding="utf-8")
    assert "## beta" in text and "## alpha" not in text
    assert "`b-1`" in text and "`b-2`" in text and "`a-3`" not in text


# ---------------------------------------------------------------------------
# core-level + vscdb
# ---------------------------------------------------------------------------


def test_run_digest_core_returns_report(sessions_db, tmp_path):
    projects = group_sessions(load_sessions(sessions_db))
    cur_dir = tmp_path / "cursors"
    c = Cursor(CURSOR_NAME, dir=cur_dir)
    result = run_digest(projects, cursor=c, out_dir=tmp_path)
    assert result.report and result.out_path.is_file()
    assert result.watermark == MAX_ACTIVITY
    assert {p.name for p in result.projects} == {"alpha", "beta"}

    c2 = Cursor(CURSOR_NAME, dir=cur_dir)
    again = run_digest(projects, cursor=c2, out_dir=tmp_path)
    assert again.report is None and again.out_path is None
    assert again.projects == []


def test_digest_includes_gui_sessions(
    sessions_db, state_vscdb, db_flag, tmp_path
):
    """``--vscdb`` GUI bindings participate; gui row carries its mark."""
    assert (
        main(
            [
                "digest",
                *db_flag,
                "--vscdb",
                str(state_vscdb),
                "--since",
                "0",
                "--out-dir",
                str(tmp_path),
            ]
        )
        == 0
    )
    text = _digests(tmp_path)[0].read_text(encoding="utf-8")
    assert "## gamma" in text
    assert "| gui |" in text or " gui |" in text


def test_digest_never_writes_to_sessions_db(sessions_db, db_flag,
                                            cursor_dir, tmp_path):
    before = sessions_db.read_bytes()
    main(["digest", *db_flag, "--incremental",
          "--out-dir", str(tmp_path)])
    assert sessions_db.read_bytes() == before
