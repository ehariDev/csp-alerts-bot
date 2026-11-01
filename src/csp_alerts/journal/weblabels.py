"""Owner-confirmed label correction for the local web page (Stage B2, WT-05): PLAN (preview) and RECORD (confirm).

What it changes: the sector and correlation-bucket labels stored on the rows the exposure rules read for ONE ticker (live put
positions and stock lots, `Journal.label_rows`). That is the existing label store; there is no parallel one. It writes one
audited LABEL_CORRECTED event with every previous value. It never touches approvals: an approval carries event-clearance and
deliverable attestations that stay CLI-only (`journal approve`). For an APPROVED ticker the only correction accepted is the one
that makes the ledger rows equal the approval's own labels. Asset type is not editable here: the backend reads it only from the
approval record (unapproved names count as individual companies, the conservative default), so correcting it would need a new
store or a fake approval; both are deliberately not done.

Formulas: none of its own. The before/after exposure comes from `policy.portfolio.exposure_groups` on the current book and on a
hypothetical book with the new labels (same function, same figures the entry gate compares).

Flow mirrors webtrade: build_plan() writes nothing; record() re-plans inside ONE write transaction, refuses a stale revision or
changed labels, is idempotent per key, writes the labels + audit, then re-checks recorded policy-breach flags.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Mapping

from csp_alerts.journal import webtrade as wt
from csp_alerts.journal.models import COLLATERAL_STATES, PositionState, norm_label
from csp_alerts.journal.store import Journal, JournalError
from csp_alerts.policy.approved import ApprovedUnderlying
from csp_alerts.policy.config import Policy
from csp_alerts.policy.portfolio import UNKNOWN_LABEL, GroupRow, exposure_groups

ACTION = "label_correct"
ACTOR, SOURCE = wt.ACTOR, wt.SOURCE
_LABEL = re.compile(r"[A-Za-z0-9 _&.\-]{1,40}\Z")      # ASCII only; the normalized form is strip+casefold

MESSAGES = {
    "label_recorded": "Labels corrected and audited. Exposure is now grouped under the new labels.",
    "duplicate": "That confirmation was already recorded; nothing was written a second time.",
    "stale": "The ledger or the labels changed since this preview, so it was rejected. Nothing was written; preview again.",
    "expired": wt.MESSAGES["expired"], "token": wt.MESSAGES["token"], "invalid": wt.MESSAGES["invalid"],
    "reason_required": wt.MESSAGES["reason_required"], "ack_required": wt.MESSAGES["ack_required"],
    "unavailable": wt.MESSAGES["unavailable"], "rejected": wt.MESSAGES["rejected"],
    "csrf": "The form expired or was not issued by this page. Nothing was written; reload the page and try again.",
    "bad_request": "Bad request.",
}
RESULT_CODES = frozenset(MESSAGES)
ASSET_TYPE_NOTE = ("Asset type is not editable here. The backend reads it only from your approval record; a name without one is "
                   "counted as an individual company (the conservative default). Changing it is a CLI approval step.")


@dataclass
class LabelPlan:
    fields: dict[str, str] = field(default_factory=dict)          # normalized values bound by the preview token
    errors: list[str] = field(default_factory=list)
    ticker: str = ""
    current: list[dict[str, str]] = field(default_factory=list)   # kind, id, sector, bucket (as stored now)
    before: list[GroupRow] = field(default_factory=list)          # sector/bucket groups touched, now
    after: list[GroupRow] = field(default_factory=list)           # the same keys after the correction
    after_breached: list[str] = field(default_factory=list)
    approval_labels: tuple[str, str] | None = None
    notes: list[str] = field(default_factory=list)
    checks: list[wt.Check] = field(default_factory=list)
    ledger_revision: int = 0
    policy_sha: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class LabelResult:
    code: str
    ticker: str | None = None

    @property
    def ok(self) -> bool:
        return self.code in ("label_recorded", "duplicate")

    @property
    def message(self) -> str:
        return MESSAGES.get(self.code, MESSAGES["rejected"])


def _digest(pos: list[dict], lots: list[dict]) -> str:
    """Canonical fingerprint of the labels about to be replaced (compare-and-set at confirm time)."""
    body = json.dumps([[r["id"], r["sector"], r["bucket"]] for r in pos] + [["lot", r["id"], r["sector"], r["bucket"]] for r in lots],
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:32]


def _norm(raw: str, what: str, errs: list[str]) -> str:
    if not _LABEL.match(raw or ""):
        errs.append(f"{what} must be 1-40 characters: letters, digits, spaces and _ & . - only.")
        return ""
    v = norm_label(raw)
    if not v or v == UNKNOWN_LABEL:
        errs.append(f"{what} cannot be blank or 'unknown': a correction must state a real label (unknown labels block concentration checks).")
        return ""
    return v


def _hypothetical(book, ticker: str, sector: str, bucket: str):
    pos = tuple(replace(p, sector=sector, correlation_bucket=bucket)
                if p.ticker == ticker and (p.state in COLLATERAL_STATES or p.state is PositionState.ASSIGNED_PENDING_RECONCILIATION) else p
                for p in book.positions)
    lots = tuple(replace(l, sector=sector, correlation_bucket=bucket) if l.ticker == ticker else l for l in book.stock_lots)
    return replace(book, positions=pos, stock_lots=lots)


def build_plan(j: Journal, policy: Policy, approved: list[ApprovedUnderlying] | None, form: Mapping[str, str]) -> LabelPlan:
    """Validate `form` and describe exactly what the correction would change. Reads only; never writes."""
    plan = LabelPlan(policy_sha=policy.sha256, ledger_revision=j.ledger_seqs()[1])
    errs = plan.errors
    if wt._get(form, "action") != ACTION:
        errs.append("Unknown action.")
        return plan
    idem = wt._get(form, "idem")
    if not wt._IDEM.match(idem):
        errs.append("The form is missing its idempotency key; reload the form page.")
        return plan
    ticker = wt._get(form, "ticker")
    ticker = ticker if ticker.isascii() and wt._SYM.match(ticker) else ""
    if not ticker:
        errs.append("The ticker is not a valid symbol.")
        return plan
    plan.ticker = ticker
    if approved is None:
        errs.append("Your approval file cannot be read, so label corrections are refused (nothing is assumed).")
        return plan
    pos, lots = j.label_rows(ticker)
    if not pos and not lots:
        errs.append("No live put position or stock lot of that ticker carries labels to correct.")
        return plan
    plan.current = [{"kind": "put", **r} for r in pos] + [{"kind": "stock lot", **r} for r in lots]
    sector = _norm(wt._get(form, "sector"), "Sector", errs)
    bucket = _norm(wt._get(form, "bucket"), "Correlation bucket", errs)
    ap = next((a for a in approved if a.ticker == ticker), None)
    if ap is not None:
        plan.approval_labels = (ap.sector, ap.correlation_bucket)
        plan.notes.append(f"{ticker} is on your approved list with sector '{ap.sector}' and bucket '{ap.correlation_bucket}'. "
                          "Approval labels are attested through the CLI and are not changed here; the only correction accepted is the one "
                          "that makes the ledger rows equal them.")
        if sector and bucket and (sector, bucket) != (ap.sector, ap.correlation_bucket):
            errs.append("For an approved ticker the ledger labels must equal the approval's labels. To change the approval itself use the "
                        "CLI `journal approve` (RUNBOOK section 1).")
    if sector and bucket and all(r["sector"] == sector and r["bucket"] == bucket for r in pos + lots):
        errs.append("Those labels are already stored on every row of this ticker; there is nothing to correct.")
    plan.fields = {"action": ACTION, "idem": idem, "ticker": ticker, "sector": sector, "bucket": bucket,
                   "from_labels": _digest(pos, lots)}
    if errs:
        return plan
    book = j.load_book(approved)
    old_keys = {r["sector"] for r in pos + lots} | {r["bucket"] for r in pos + lots}
    new_keys = {sector, bucket}
    touched = lambda g: g.dimension in ("sector", "bucket") and g.key in old_keys | new_keys        # noqa: E731
    plan.before = [g for g in exposure_groups(book, policy) if touched(g)]
    hyp = exposure_groups(_hypothetical(book, ticker, sector, bucket), policy)
    plan.after = [g for g in hyp if touched(g) and (g.key in new_keys or g.key in old_keys)]
    plan.after_breached = [f"{g.dimension} {g.key}" for g in plan.after if g.breached]
    plan.notes.append(ASSET_TYPE_NOTE)
    plan.checks.append(wt.Check("ack_labels", f"I confirm that sector '{sector}' and correlation bucket '{bucket}' are the correct labels "
                                              f"for {ticker}. They decide which concentration limits count this holding."))
    return plan


def record(j: Journal, policy: Policy, approved: list[ApprovedUnderlying] | None, form: Mapping[str, str], *, now: datetime,
           bound_revision: int, bound_policy_sha: str, reason: str, acks: frozenset[str]) -> LabelResult:
    """Write the correction described by `form`, or nothing (one transaction; idempotent; stale-checked)."""
    reason = wt.clean_text(reason, wt.MAX_REASON)
    try:
        with j.tx():
            idem = wt._get(form, "idem")
            if wt._IDEM.match(idem):
                done = j.audit_find("WEB_LABEL_CORRECTED", "idempotency_key", idem)
                if done is not None:
                    return LabelResult("duplicate", done.get("ticker"))
            if policy.sha256 != bound_policy_sha or j.ledger_seqs()[1] != bound_revision:
                return LabelResult("stale")
            plan = build_plan(j, policy, approved, form)
            if plan.fields and plan.fields.get("from_labels") != wt._get(form, "from_labels"):
                return LabelResult("stale")
            if plan.errors:
                return LabelResult("invalid", plan.ticker or None)
            if not reason:
                return LabelResult("reason_required", plan.ticker)
            if any(c.name not in acks for c in plan.checks):
                return LabelResult("ack_required", plan.ticker)
            f = plan.fields
            j.correct_labels(plan.ticker, f["sector"], f["bucket"], actor=ACTOR, source=SOURCE, reason=reason, idempotency_key=idem)
            wt.sweep_breaches(j, policy, approved, now, reason="re-checked after a label correction")
            j.audit_event("WEB_LABEL_CORRECTED", ACTOR, {"idempotency_key": idem, "ticker": plan.ticker, "sector": f["sector"],
                                                         "correlation_bucket": f["bucket"]}, SOURCE)
            return LabelResult("label_recorded", plan.ticker)
    except (JournalError, ValueError):
        return LabelResult("rejected")
