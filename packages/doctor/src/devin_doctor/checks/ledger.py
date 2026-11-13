"""Check — task ledger: store health via ``devin_memory.ledger``.

Optional integration (``devin-doctor[ledger]``). Degrades honestly:
no ``devin-memory`` + no ledger dir → PASS note=not-installed; dep
missing but a store exists → WARN with the install hint; dep present →
real ``integrations.check`` verdicts.
"""

from __future__ import annotations

from devin_doctor.ledger_paths import ledger_store_dir
from devin_doctor.model import Context, Finding, Status

CHECK_ID = "ledger"


def run(ctx: Context) -> list[Finding]:
    store = ledger_store_dir()
    try:
        from devin_memory.ledger import Ledger, integrations
    except ImportError:
        if store.is_dir() and (store / "ledger.jsonl").exists():
            return [Finding(
                CHECK_ID, Status.WARN,
                f"task ledger exists at {store} but devin-memory is not installed",
                fix="pip install 'devin-doctor[ledger]'",
            )]
        return [Finding(
            CHECK_ID, Status.PASS,
            "task ledger not installed",
            note="not-installed",
        )]

    if not store.is_dir():
        return [Finding(
            CHECK_ID, Status.PASS,
            "task ledger store not initialized",
            note="not-installed",
        )]

    ok, problems = integrations.check(Ledger(store))
    if ok:
        return [Finding(CHECK_ID, Status.PASS, "task ledger healthy")]
    return [Finding(
        CHECK_ID, Status.FAIL,
        f"task ledger problems ({len(problems)}): " + "; ".join(problems[:3]),
        fix="devin-memory ledger doctor",
    )]
