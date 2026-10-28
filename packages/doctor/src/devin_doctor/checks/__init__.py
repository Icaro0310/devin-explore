"""Check registry — one module per check, each exposing ``run(ctx)``.

The agent session surfaces (``checks/surfaces/``) run first and group
under 'surfaces' in the report; the Devin-environment checks diagnose the
local Devin install itself and group under 'devin environment'. Both
lists are ordered the way they appear in the report.
"""

from devin_doctor.checks import (
    config,
    disk,
    health,
    hooks_windows,
    integrations,
    live,
    updates,
)
from devin_doctor.checks.surfaces import SURFACE_NAMES, SURFACES

# Devin-context checks — they diagnose the Devin install, not a session
# surface, and report 'not detected' rather than FAIL when no Devin store
# exists at all.
DEVIN_CHECKS = [health, config, hooks_windows, disk, updates]

# Opt-in checks — they only run when the matching CLI flag passes their
# CHECK_ID via ``run_all(optional=...)``: ``integrations`` needs the
# ``gh`` CLI plus GitHub API access; ``live`` authenticates against
# Slack/Ollama. Keeping them out of CHECKS preserves the hermetic
# default run (and the --offline pre-commit gate).
OPTIONAL_CHECKS = [integrations, live]

CHECKS = [*SURFACES, *DEVIN_CHECKS]

__all__ = [
    "CHECKS",
    "DEVIN_CHECKS",
    "OPTIONAL_CHECKS",
    "SURFACES",
    "SURFACE_NAMES",
    "config",
    "disk",
    "health",
    "hooks_windows",
    "integrations",
    "live",
    "updates",
]
