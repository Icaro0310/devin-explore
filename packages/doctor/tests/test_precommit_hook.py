"""Contract test for the repo-level pre-commit hook manifest.

``.pre-commit-hooks.yaml`` at the repo root is what pre-commit reads when
this repo is used as a hook source (``repo:`` in a consumer's
``.pre-commit-config.yaml``). The pre-commit package itself is not a dev
dependency, so these tests pin the manifest fields that make the gate
behave correctly and then execute the entry's argv through ``cli.main``:

- ``entry`` must run ``devin-doctor check --offline`` — the updates check
  is the only network caller, and a commit gate must stay fully local
  (``entry`` cannot export ``DEVIN_DOCTOR_OFFLINE`` portably on Windows).
- ``verbose: true`` keeps WARN findings visible: pre-commit hides the
  output of passing hooks, and doctor exits 0 on WARN-only runs.
- exit semantics: 0 on PASS/WARN, 1 on FAIL (blocks the commit).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
from devin_doctor.cli import main

HOOK_FILE = Path(__file__).resolve().parents[3] / ".pre-commit-hooks.yaml"

pytestmark = pytest.mark.skipif(
    not HOOK_FILE.exists(),
    reason="repo-level .pre-commit-hooks.yaml not present (sdist layout)",
)


def _manifest_fields() -> dict[str, str]:
    """Extract the single hook's scalar fields without a YAML dependency.

    The manifest is deliberately flat — one ``- id:`` list item whose
    fields are ``key: value`` scalars at two-space indent, plus a folded
    ``description`` block (``>-``) whose continuation lines are deeper and
    therefore never match the scalar pattern.
    """
    fields: dict[str, str] = {}
    for line in HOOK_FILE.read_text(encoding="utf-8").splitlines():
        item = re.match(r"^-\s+([a-z_]+):\s*(.+?)\s*$", line)
        if item:
            fields[item.group(1)] = item.group(2)
            continue
        scalar = re.match(r"^  ([a-z_]+):\s*(.+?)\s*$", line)
        if scalar:
            fields[scalar.group(1)] = scalar.group(2)
    return fields


def test_hook_manifest_contract():
    fields = _manifest_fields()
    assert fields["id"] == "devin-doctor-gate"
    assert fields["language"] == "system"
    # filenames are never appended — doctor inspects the data dir, not the
    # staged files
    assert fields["pass_filenames"] == "false"
    # a health gate must run even when nothing was staged for its fileset
    assert fields["always_run"] == "true"
    # pre-commit hides output of passing hooks; without verbose the WARN
    # findings on a green run would never reach the contributor
    assert fields["verbose"] == "true"


def test_entry_runs_check_offline(ctx, capsys, monkeypatch):
    """The entry argv drives ``cli.main`` and stays off the network."""
    monkeypatch.delenv("DEVIN_DOCTOR_OFFLINE", raising=False)
    argv = shlex.split(_manifest_fields()["entry"])
    assert argv[0] == "devin-doctor"
    assert argv[1] == "check"
    assert "--offline" in argv, "commit gate must not fetch the manifest"
    rc = main(
        argv[1:]
        + ["--data-dir", str(ctx.data_dir), "--cwd", str(ctx.cwd)]
    )
    out = capsys.readouterr().out
    assert rc == 0
    # --offline took effect inside the process
    assert "update check skipped" in out


def test_entry_blocks_on_failure(broken_ctx, capsys):
    """A FAIL finding exits 1 — that is what blocks the commit."""
    argv = shlex.split(_manifest_fields()["entry"])
    rc = main(
        argv[1:]
        + [
            "--data-dir",
            str(broken_ctx.data_dir),
            "--cwd",
            str(broken_ctx.cwd),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL" in out
