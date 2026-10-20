"""Check registry — one module per check, each exposing ``run(ctx)``.

The agent session surfaces (``checks/surfaces/``) run first and group
under 'surfaces' in the report; the Devin-environment checks diagnose the
local Devin install itself and group under 'devin environment'. Both
lists are ordered the way they appear in the report.
"""

from devin_doctor.checks import (
    automation,
    config,
    disk,
    health,
    hooks_windows,
    path_check,
    publish,
    updates,
)
from devin_doctor.checks.surfaces import SURFACE_NAMES, SURFACES

# Devin-context checks — they diagnose the Devin install, not a session
# surface, and report 'not detected' rather than FAIL when no Devin store
# exists at all.
DEVIN_CHECKS = [
    health,
    config,
    hooks_windows,
    disk,
    updates,
    automation,
    path_check,
    publish,
]

CHECKS = [*SURFACES, *DEVIN_CHECKS]

__all__ = [
    "CHECKS",
    "DEVIN_CHECKS",
    "SURFACES",
    "SURFACE_NAMES",
    "automation",
    "config",
    "disk",
    "health",
    "hooks_windows",
    "path_check",
    "publish",
    "updates",
]
