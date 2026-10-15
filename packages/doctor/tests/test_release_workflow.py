"""Static assertions for .github/workflows/release.yml.

The release fan-out has no unit-testable code, so these tests pin the
gates that keep a mistagged tag push, a manual dispatch, or a partial
fanout failure from publishing or stranding artifacts.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "release.yml"
TAG_PUSH_ONLY = "github.event_name == 'push' && github.ref_type == 'tag'"


def _load() -> dict:
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # YAML 1.1 parses the literal key `on` as boolean True.
    wf["on"] = wf.pop(True)
    return wf


def test_release_and_fanout_tag_push_only():
    jobs = _load()["jobs"]
    assert jobs["release"]["if"].startswith("github.event_name == 'push'")
    assert jobs["fanout"]["if"] == TAG_PUSH_ONLY
    assert sorted(jobs["fanout"]["needs"]) == ["release", "resolve"]


def test_release_creation_is_idempotent():
    """A rerun after a partial failure must not die on gh release create."""
    steps = _load()["jobs"]["release"]["steps"]
    runs = "\n".join(s.get("run", "") for s in steps)
    assert "gh release view" in runs
    assert "gh release upload" in runs


def test_attach_vsix_survives_fanout_failure():
    """The vsix asset attaches whenever the release minted — a failed
    registry leg must not strand it (needs.release.result status check
    drops the implicit success() over fanout)."""
    attach = _load()["jobs"]["attach-vsix"]
    assert sorted(attach["needs"]) == ["fanout", "release", "resolve"]
    assert "needs.release.result == 'success'" in attach["if"]
    assert "needs.resolve.outputs.vsix-dir != ''" in attach["if"]


def test_fanout_uses_reusable_with_scoped_secrets():
    fanout = _load()["jobs"]["fanout"]
    assert fanout["uses"] == "Icaro0310/devin-powerups/.github/workflows/release.yml@v1"
    assert set(fanout["secrets"]) == {"OVSX_PAT"}
