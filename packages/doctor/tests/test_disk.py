"""disk check: total data-dir size, largest DBs, acp-messages accumulation."""

from pathlib import Path

from devin_doctor.checks import disk
from devin_doctor.model import Status


def _finding(findings, needle):
    return next(f for f in findings if needle in f.message)


def test_healthy_all_pass(ctx):
    findings = disk.run(ctx)
    assert all(f.status is Status.PASS for f in findings), [
        f.message for f in findings
    ]


def test_reports_total_size(ctx):
    f = _finding(disk.run(ctx), "total")
    assert f.status is Status.PASS
    assert "files" in f.message


def test_reports_largest_stores(ctx):
    f = _finding(disk.run(ctx), "largest")
    assert f.status is Status.PASS
    assert "sessions.db" in f.message


def test_acp_accumulation_reported(ctx):
    f = _finding(disk.run(ctx), "acp-messages")
    assert "2" in f.message


def test_oversized_total_warns(ctx):
    ctx.total_warn_bytes = 1  # any non-empty dir trips it
    f = _finding(disk.run(ctx), "total")
    assert f.status is Status.WARN
    assert f.fix


def test_oversized_store_warns(ctx):
    ctx.store_warn_bytes = 1
    f = _finding(disk.run(ctx), "largest")
    assert f.status is Status.WARN


def test_acp_accumulation_warns(ctx):
    ctx.acp_warn_bytes = 1
    f = _finding(disk.run(ctx), "acp-messages")
    assert f.status is Status.WARN


def test_oversized_sessions_db_warns(ctx):
    """sessions.db over its threshold warns with path + janitor pointer."""
    ctx.sessions_db_warn_bytes = 1
    f = _finding(disk.run(ctx), "sessions.db is")
    assert f.status is Status.WARN
    assert "devin-janitor" in (f.fix or "")


def test_sessions_db_within_threshold_passes(ctx):
    f = _finding(disk.run(ctx), "sessions.db:")
    assert f.status is Status.PASS


def test_missing_data_dir_is_not_detected(tmp_path):
    """No Devin data at all — the check emits nothing and the report lists
    it under 'not detected' instead of hard-failing."""
    from devin_doctor.model import Context

    ctx = Context(data_dir=tmp_path / "nope", cwd=tmp_path)
    assert disk.run(ctx) == []


def test_uncapped_run_has_no_cap_markers(ctx):
    findings = disk.run(ctx)
    assert len(findings) == 4
    assert all("(scan capped)" not in f.message for f in findings)
    assert all("≥" not in f.message for f in findings)
    total = _finding(findings, "data dir")
    assert total.message.startswith("data dir total:")


def test_file_count_cap_marks_lower_bounds(ctx, monkeypatch):
    monkeypatch.setattr(disk, "_MAX_FILES", 2)
    findings = disk.run(ctx)
    total = _finding(findings, "data dir")
    # a capped walk can prove WARN but never PASS — the unread remainder
    # may hold the bulk, so under-threshold capped results are WARN too
    assert total.status is Status.WARN
    assert "≥" in total.message
    assert "remainder unread" in total.message
    # the marker lands on all three findings, not just the total
    assert all("(scan capped)" in f.message for f in findings)


def test_file_count_cap_still_warns_over_threshold(ctx, monkeypatch):
    monkeypatch.setattr(disk, "_MAX_FILES", 2)
    ctx.total_warn_bytes = 1
    f = _finding(disk.run(ctx), "data dir")
    assert f.status is Status.WARN
    assert "≥" in f.message
    assert "(scan capped)" in f.message
    assert f.fix


def test_time_cap_marks_lower_bounds(ctx, monkeypatch):
    monkeypatch.setattr(disk, "_MAX_SECONDS", 0.0)
    total = _finding(disk.run(ctx), "data dir")
    assert total.status is Status.WARN
    assert "≥" in total.message
    assert "(scan capped)" in total.message


def test_findings_never_restat_walked_files(ctx, monkeypatch):
    """The walk stats each file once via DirEntry; the findings must consume
    those records and never call ``Path.stat`` on a file under the data dir
    again (the old code stat'ed every file up to 5 times). The one allowed
    exception is acp-messages DBs, which get a single fresh stat so live
    growth during the walk cannot hide a threshold breach. The same
    exception applies to sessions.db, which gets its own exact finding."""
    from devin_doctor.paths import locate_stores

    known_files = {p for p in ctx.data_dir.rglob("*") if p.is_file()}
    assert known_files
    exempt = set(locate_stores(ctx.data_dir).acp_dbs)
    exempt.add(ctx.data_dir / "cli" / "sessions.db")
    seen = []
    orig = Path.stat

    def spy(self, *args, **kwargs):
        seen.append(self)
        return orig(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", spy)
    findings = disk.run(ctx)
    assert len(findings) == 4
    assert [p for p in seen if p in known_files and p not in exempt] == []


def test_acp_stats_are_fresh_not_scan_records(ctx, monkeypatch):
    """An acp DB growing after the bounded walk must still be counted:
    ``_acp_finding`` re-stats each store instead of reusing the shared
    scan records, so live growth cannot hide a threshold breach."""
    from devin_doctor.paths import human_size, locate_stores

    ctx.acp_warn_bytes = 1
    acp_db = locate_stores(ctx.data_dir).acp_dbs[0]
    expected = sum(
        p.stat().st_size for p in locate_stores(ctx.data_dir).acp_dbs
    ) + 8192
    real_walk = disk._walk_files

    def grow_after_walk(root):
        files, capped = real_walk(root)
        acp_db.write_bytes(acp_db.read_bytes() + b"x" * 8192)
        return files, capped

    monkeypatch.setattr(disk, "_walk_files", grow_after_walk)
    f = _finding(disk.run(ctx), "acp-messages")
    assert f.status is Status.WARN
    assert human_size(expected) in f.message
