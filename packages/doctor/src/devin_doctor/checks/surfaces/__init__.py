"""Agent session surfaces — one check module per surface.

Each module runs the same ladder (present → readable → schema → sane) and
uses the canonical surface names from SESSION-SURFACES.md §1 as its
``CHECK_ID`` — that id is what ``--surface`` scopes and what the report
groups under 'surfaces'. A surface whose roots are absent emits no
findings: an uninstalled agent is a valid environment, never a failure.
"""

from devin_doctor.checks.surfaces import (
    codex,
    copilot,
    devin_chat,
    stores_devin,
    vscode_chat,
)

# Report order: the Devin surfaces first, then the rest of the machine.
SURFACES = [stores_devin, devin_chat, copilot, codex, vscode_chat]

SURFACE_NAMES = [m.CHECK_ID for m in SURFACES]

__all__ = [
    "SURFACES",
    "SURFACE_NAMES",
    "codex",
    "copilot",
    "devin_chat",
    "stores_devin",
    "vscode_chat",
]
