"""Install-states corpus parity — spec → findings.

Replays the vendored ``tests/install-states`` corpus — copied verbatim
from devin-internals-spec ``fixtures/install-states`` (spec PR #46) —
through the real surface checks. ``install-states-manifest.json`` binds,
per fixture, ``{surface, state, store_path, expected_findings}`` where
each finding is ``{check: rung, status, code?, note?}`` — the canonical
ladder from SURFACE-HEALTH.md §2/§4.

Each fixture dir is a home-directory surrogate: the test repoints HOME
at it so the surface's real relative paths resolve inside the fixture
(``.local/share/devin``, ``.config/Devin``, ``.copilot``, ``.codex``,
``codex-sessions`` ↔ ``$CODEX_SESSIONS_DIR``, ``.vscode-server``).

``unmodeled_states`` is honored deliberately: ``locked-or-unreadable``
is skipped because POSIX permission bits and OS/SQLite locks are not
deterministic across runners (spec §7).

Layout note: the corpus is Linux-shaped — the linux-only gate keeps the
parametrization honest on other platforms rather than faking results.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from devin_doctor import paths
from devin_doctor.checks.surfaces import (
    codex,
    copilot,
    devin_chat,
    stores_devin,
    vscode_chat,
)
from devin_doctor.model import Context

CORPUS = Path(__file__).resolve().parent / "install-states"
_MANIFEST = json.loads(
    (CORPUS / "install-states-manifest.json").read_text(encoding="utf-8")
)
_FIXTURES = _MANIFEST["fixtures"]

_RUNNERS = {
    "devin-cli": stores_devin,
    "devin-chat": devin_chat,
    "copilot-cli": copilot,
    "codex-cli": codex,
    "vscode-chat": vscode_chat,
}
_RUNGS = ("present", "readable", "schema", "sane")

_LINUX_ONLY = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="install-states corpus models the documented Linux layouts",
)


def _as_expected(finding) -> dict[str, str]:
    """The manifest shape of one produced finding — ``check`` is the
    rung; ``code``/``note`` appear only when set."""
    out = {"check": finding.rung, "status": finding.status.value}
    if finding.code is not None:
        out["code"] = finding.code
    if finding.note is not None:
        out["note"] = finding.note
    return out


def test_manifest_shape_and_coverage():
    """Every manifest fixture is exercised; the corpus binds exactly the
    five surfaces and stays self-describing about deliberate gaps."""
    assert _MANIFEST["spec"] == "docs/SURFACE-HEALTH.md"
    assert len(_FIXTURES) == 32
    assert {f["surface"] for f in _FIXTURES} == set(_RUNNERS)
    for fixture in _FIXTURES:
        assert (CORPUS / fixture["store_path"]).is_dir(), fixture
        rungs = [f["check"] for f in fixture["expected_findings"]]
        # one finding per rung, emitted in ladder order
        assert rungs == sorted(rungs, key=_RUNGS.index)
        assert len(set(rungs)) == len(rungs)
    # locked-or-unreadable is intentionally absent — locks and POSIX
    # permission behavior are not deterministic across runners (§7)
    assert "locked-or-unreadable" in _MANIFEST["unmodeled_states"]
    assert not any(
        f["state"] == "locked-or-unreadable" for f in _FIXTURES
    )


@_LINUX_ONLY
@pytest.mark.parametrize(
    "fixture",
    _FIXTURES,
    ids=[f"{f['surface']}/{f['state']}" for f in _FIXTURES],
)
def test_fixture_parity(fixture, tmp_path, monkeypatch):
    root = (CORPUS / fixture["store_path"]).resolve()
    # HOME surrogate — every surface root resolves under it; env vars
    # for the other surfaces stay cleared by the autouse fixture.
    monkeypatch.setenv("HOME", str(root))
    sessions_dir = root / "codex-sessions"
    if sessions_dir.is_dir():
        monkeypatch.setenv("CODEX_SESSIONS_DIR", str(sessions_dir))
    else:
        monkeypatch.delenv("CODEX_SESSIONS_DIR", raising=False)

    ctx = Context(
        data_dir=paths.default_data_dir(),
        config_dir=paths.default_config_dir(),
        cwd=tmp_path,
    )
    findings = _RUNNERS[fixture["surface"]].run(ctx)

    assert all(f.check == fixture["surface"] for f in findings)
    assert all(f.rung in _RUNGS for f in findings)
    assert [_as_expected(f) for f in findings] == fixture[
        "expected_findings"
    ], (
        f"{fixture['surface']}/{fixture['state']}: "
        f"{[_as_expected(f) for f in findings]}"
    )
