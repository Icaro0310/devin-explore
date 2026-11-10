"""Static assertions for .github/workflows/auto-approve.yml.

The caller is thin by design: triggers plus a pin to the reusable
``pr-approve`` gate in devin-powerups. These tests pin the v1.16 caller
contract — the gate version, the ``pull_request_review_comment``
trigger that re-evaluates the gate when a fix-commit reply lands, and
the scoped job permissions (``contents: write`` is still required by
the v1.16 ``arm-auto-merge`` job inside the reusable) — without running
the workflow.
"""

from pathlib import Path

import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "auto-approve.yml"
)
GATE_PIN = "Icaro0310/devin-powerups/.github/workflows/pr-approve.yml@"


def _load() -> dict:
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # YAML 1.1 parses the literal key `on` as boolean True.
    wf["on"] = wf.pop(True)
    return wf


def test_gate_pin_is_v1_16() -> None:
    """v1.16 splits the auto-merge arm into a dedicated job so the
    approve path runs read-only again; the pin must be exact so a
    silent downgrade is caught here."""
    uses = _load()["jobs"]["approve"]["uses"]
    assert uses == GATE_PIN + "v1.16"


def test_review_comment_trigger_re_evaluates_gate() -> None:
    """A reply citing fix commits re-runs the gate on the same head."""
    assert _load()["on"]["pull_request_review_comment"]["types"] == ["created"]


def test_gate_permissions_stay_scoped() -> None:
    """contents:write is required by the v1.16 ``arm-auto-merge`` job
    inside the reusable (`gh pr merge --auto`); its absence was the
    old safe state and is now the regression this contract guards."""
    perms = _load()["jobs"]["approve"]["permissions"]
    assert perms == {"contents": "write", "pull-requests": "write"}
