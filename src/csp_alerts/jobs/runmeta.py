"""Run-record identity (WD-003). Stored inside the existing `run_status.summary_json` column, so no schema change.

Every completed or failed scan/monitor/skip records: scope, policy id/version/sha256, the ledger revision it saw
and the code identity. Older rows simply lack these keys; readers show them as "not recorded", never as zero.
"""
from __future__ import annotations

from typing import Any

from csp_alerts.journal.store import Journal
from csp_alerts.policy.config import Policy
from csp_alerts.version import code_identity


def run_context(journal: Journal, policy: Policy) -> dict[str, Any]:
    ident = code_identity()
    try:
        ledger_revision: int | None = journal.ledger_seqs()[1]
    except Exception:  # noqa: BLE001 - identity is best effort; a run record must never be lost because of it
        ledger_revision = None
    return {
        "scope": policy.account_scope, "policy_id": policy.policy_id, "policy_version": policy.policy_version,
        "policy_sha256": policy.sha256, "ledger_revision": ledger_revision,
        "code_revision": ident.label, "code_manifest_sha256": ident.manifest_sha256,
    }
