from devin_search.fmt import (
    fmt_ts,
    hits_table,
    hits_to_dicts,
    stats_line,
    stats_to_dict,
)
from devin_search.index import IndexStats
from devin_search.query import Hit


def _hit(**kw):
    base = {

        "session_id": "abcdef01-2345-6789-abcd-ef0123456789",
        "role": "user",
        "ts": 1_780_000_000_000,
        "project": "/projects/alpha",
        "session_title": "alpha",
        "source": "devin-cli",
        "ref": "node:7",
        "snippet": "deploy with «kubectl»",
        "rank": -1.5,
    }
    base.update(kw)
    return Hit(**base)


def test_hits_table_columns():
    table = hits_table([_hit()])
    lines = table.splitlines()
    assert "WHEN" in lines[0] and "SNIPPET" in lines[0]
    assert "user" in table and "alpha" in table
    assert "abcdef01" in table
    assert "«kubectl»" in table


def test_hits_table_empty():
    assert hits_table([]) == "no hits"


def test_hits_to_dicts_shape():
    (d,) = hits_to_dicts([_hit()])
    assert set(d) == {
        "session_id", "role", "ts", "project", "session_title",
        "source", "ref", "snippet", "rank", "history_note",
    }
    assert d["ref"] == "node:7"
    assert d["rank"] == -1.5
    assert d["history_note"] is None


def test_hits_table_shows_history_note():
    note = "/exports/2026-05-28_abcdef01-2345-6789-abcd-ef0123456789.md"
    table = hits_table([_hit(history_note=note)])
    lines = table.splitlines()
    assert any(f"note: {note}" in line for line in lines)


def test_hits_table_omits_note_when_absent():
    table = hits_table([_hit()])
    assert "note:" not in table


def test_fmt_ts():
    assert fmt_ts(0) == "-"
    assert fmt_ts(1_780_000_000_000) == "2026-05-28 20:26"


def test_stats_line_and_dict():
    stats = IndexStats(
        index_path="/tmp/search.db",
        message_nodes=3,
        prompt_history=1,
        tool_calls=2,
        devin_chat_messages=4,
        removed=1,
        sources=["devin-cli", "devin-chat"],
    )
    line = stats_line(stats)
    assert "+10 docs" in line and "removed 1" in line
    d = stats_to_dict(stats)
    assert d["indexed"] == 10
    assert d["by_source"]["devin_chat_messages"] == 4
    assert d["sources"] == ["devin-cli", "devin-chat"]
