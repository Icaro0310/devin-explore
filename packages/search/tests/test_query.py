import pytest
from conftest import BASE_MS, add_session, msg
from devin_search.index import build_index
from devin_search.query import Hit, parse_since, search, to_fts_query


@pytest.fixture
def rich_db(db_path):
    add_session(
        db_path,
        "sess-alpha",
        title="alpha",
        working_directory="/projects/alpha",
        created_ms=BASE_MS,
        messages=[
            msg("user", "deploy with kubectl delete pod --force"),
            msg("assistant", "done, no errors"),
        ],
        prompts=[("kubectl delete pod --force", 1)],
    )
    add_session(
        db_path,
        "sess-beta",
        title="beta",
        working_directory="/projects/beta",
        created_ms=BASE_MS + 86_400_000,  # +1 day
        messages=[
            msg("user", "kubectl kubectl kubectl cheatsheet please"),
        ],
    )
    return db_path


@pytest.fixture
def index_path(rich_db, tmp_path):
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=rich_db)
    return idx


def test_search_finds_terms_across_sessions(index_path):
    hits = search(index_path, "kubectl")
    sids = {h.session_id for h in hits}
    assert {"sess-alpha", "sess-beta"} <= sids
    assert all(isinstance(h, Hit) for h in hits)
    assert all("«" in h.snippet and "»" in h.snippet for h in hits)


def test_bm25_ranks_higher_tf_first(index_path):
    hits = search(
        index_path, "kubectl", role="user", project="projects/", limit=10
    )
    node_hits = [h for h in hits if h.ref.startswith("node:")]
    assert node_hits[0].session_id == "sess-beta"  # 3x term beats 1x
    assert node_hits[0].rank < node_hits[1].rank


def test_role_filter(index_path):
    hits = search(index_path, "kubectl", role="user")
    assert hits
    assert all(h.role == "user" for h in hits)
    assert search(index_path, "kubectl", role="system") == []


def test_shell_prompts_have_shell_role(index_path):
    hits = search(index_path, "kubectl", role="shell")
    assert hits
    assert all(h.ref.startswith("prompt:") for h in hits)


def test_project_filter(index_path):
    hits = search(index_path, "kubectl", project="alpha")
    assert hits
    assert all("alpha" in h.project for h in hits)
    assert {h.session_id for h in hits} == {"sess-alpha"}


def test_since_filter(index_path):
    hits = search(index_path, "kubectl", since=BASE_MS + 3_600_000)
    assert hits
    assert all(h.ts >= BASE_MS + 3_600_000 for h in hits)
    assert {h.session_id for h in hits} == {"sess-beta"}


def test_limit(index_path):
    hits = search(index_path, "fixture", limit=2)
    assert len(hits) == 2


def test_no_match_returns_empty(index_path):
    assert search(index_path, "nonexistent-term-zzz") == []


def test_tool_filter(db_path, tmp_path):
    """``tool`` keeps only docs whose tool-call payload named that tool;
    combined with ``role`` it narrows further, and absent it nothing is
    filtered out."""
    add_session(
        db_path,
        "tool-sess",
        messages=[msg("user", "quasar maintenance")],
        tool_calls=[
            ({"name": "shell", "command": "quasar restart"},
             {"output": "quasar restarted"}),
            ({"name": "read_file", "path": "/etc/quasar.conf"}, None),
        ],
    )
    idx = tmp_path / "search.db"
    build_index(idx, sessions_db=db_path)

    hits = search(idx, "quasar", tool="shell")
    assert hits
    assert all(h.tool == "shell" for h in hits)
    assert {h.ref for h in hits} == {"tool:tool-sess/tool-sess-tc0"}

    hits = search(idx, "quasar", role="tool", tool="read_file")
    assert hits
    assert all(h.tool == "read_file" and h.role == "tool" for h in hits)
    assert {h.ref for h in hits} == {"tool:tool-sess/tool-sess-tc1"}

    # A doc cannot satisfy both sides of a contradictory pair.
    assert search(idx, "quasar", role="user", tool="shell") == []
    # Unknown tool name → no hits.
    assert search(idx, "quasar", tool="nonexistent-tool") == []
    # Blank means absent — never an exact match on "".
    assert search(idx, "quasar", tool="") == search(idx, "quasar")
    assert search(idx, "quasar", tool="  ") == search(idx, "quasar")
    # No filter → user message + both tool-call docs.
    assert len(search(idx, "quasar")) == 3


def test_fts_operators_in_term_are_safe(index_path):
    assert search(index_path, 'kubectl OR "unbalanced') == []
    assert to_fts_query('a "b" NEAR') == '"a" """b""" "NEAR"'


def test_missing_index_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        search(tmp_path / "nope.db", "x")


def test_parse_since():
    assert parse_since("1780000000000") == 1_780_000_000_000
    assert parse_since("2026-05-25") == 1_779_667_200_000
    with pytest.raises(ValueError):
        parse_since("not-a-date")
