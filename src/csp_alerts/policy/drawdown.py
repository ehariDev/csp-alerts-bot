"""Drawdown rules (spec 4.2). Pure.

drawdown = max(0, (peak - latest) / peak) over manually confirmed snapshots, chronologically.
  >= pause_new_entries_at (3%)        -> PAUSE
  >= reduce_exposure_review_at (5%)   -> REDUCE_REVIEW
  >= hard_halt_at (8%)                -> HARD_HALT
EVERY level LATCHES (owner decision): the highest level reached stays until the owner records a manual
risk reset (Journal.record_risk_reset); a rebound never re-enables entries. Monitoring keeps running.
Peak = max(owner-confirmed baseline equity, confirmed snapshots), shifted by logged external cash flows. The baseline is an explicit audited
journal event (Journal.confirm_baseline); without one entries are blocked (reconcile NO_BASELINE).
With no confirmed snapshot the state is UNKNOWN. After a reset only snapshots at/after the reset cut-off
count and the original baseline no longer seeds the peak.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from csp_alerts.journal.models import AccountSnapshot
from csp_alerts.policy.config import Policy


class DrawdownLevel(StrEnum):
    UNKNOWN = "UNKNOWN"
    NONE = "NONE"
    PAUSE = "PAUSE"
    REDUCE_REVIEW = "REDUCE_REVIEW"
    HARD_HALT = "HARD_HALT"


_RANK = {DrawdownLevel.UNKNOWN: -1, DrawdownLevel.NONE: 0, DrawdownLevel.PAUSE: 1,
         DrawdownLevel.REDUCE_REVIEW: 2, DrawdownLevel.HARD_HALT: 3}


@dataclass(frozen=True)
class DrawdownState:
    level: DrawdownLevel
    drawdown: Decimal | None
    peak_equity_usd: Decimal | None
    latest_equity_usd: Decimal | None
    latest_asof_utc: datetime | None
    halt_latched: bool
    latched: bool = False  # any level >= PAUSE reached since the last manual reset
    current_level: DrawdownLevel = DrawdownLevel.NONE  # level from latest equity alone (informational)

    @property
    def entries_blocked(self) -> bool:
        return self.level in (DrawdownLevel.PAUSE, DrawdownLevel.REDUCE_REVIEW, DrawdownLevel.HARD_HALT)


def _level(peak: Decimal, equity: Decimal, policy: Policy) -> DrawdownLevel:
    """Cross-multiplied comparison so threshold boundaries are exact (no division rounding)."""
    d = policy.drawdown
    loss = peak - equity
    if loss >= d.hard_halt_at * peak:
        return DrawdownLevel.HARD_HALT
    if loss >= d.reduce_exposure_review_at * peak:
        return DrawdownLevel.REDUCE_REVIEW
    if loss >= d.pause_new_entries_at * peak:
        return DrawdownLevel.PAUSE
    return DrawdownLevel.NONE


def _flow_first(flow: tuple, snap: AccountSnapshot) -> bool:
    """True when the cash flow precedes the snapshot in event order (timestamp, then journal sequence)."""
    if flow[0] != snap.asof_utc:
        return flow[0] < snap.asof_utc
    seq = flow[2] if len(flow) > 2 else 0
    return not (seq and snap.seq and seq > snap.seq)


def compute_drawdown(
    snapshots: list[AccountSnapshot] | tuple[AccountSnapshot, ...],
    policy: Policy,
    *,
    baseline_asof_utc: datetime | None = None,
    baseline_equity_usd: Decimal | None = None,
    cash_flows: tuple[tuple, ...] | list = (),
) -> DrawdownState:
    """Latched drawdown level.

    peak = max(owner-confirmed baseline equity, all confirmed snapshots in scope), running chronologically.
    The reported level is the HIGHEST level reached at any snapshot in scope (every level latches until
    the owner records a manual risk reset). `baseline_asof_utc` is the last risk-reset cut-off: after a
    reset only snapshots at/after it count and the original baseline equity no longer seeds the peak.
    """
    valid = sorted(
        (s for s in snapshots if s.manual_confirmed and s.equity_usd > 0
         and (baseline_asof_utc is None or s.asof_utc >= baseline_asof_utc)),
        key=lambda s: (s.asof_utc, s.seq),
    )
    if not valid:
        return DrawdownState(DrawdownLevel.UNKNOWN, None, None, None, None, False)

    peak = baseline_equity_usd if (baseline_equity_usd is not None and baseline_asof_utc is None) else Decimal("0")
    highest = DrawdownLevel.NONE
    flows = sorted((f for f in cash_flows if baseline_asof_utc is None or f[0] >= baseline_asof_utc),
                   key=lambda f: (f[0], f[2] if len(f) > 2 else 0))
    fi = 0
    for s in valid:
        # IT-008: an external cash flow shifts the peak by the same amount (a withdrawal is not a loss, a deposit
        # does not hide one). `highest` only ever increases, so a flow can never clear or downgrade a latch.
        # SR-06: events are ordered by (timestamp, journal sequence). A flow recorded strictly earlier applies first;
        # on an exact timestamp tie it applies first unless BOTH sequence numbers are known and the snapshot came first.
        while fi < len(flows) and _flow_first(flows[fi], s):
            peak = max(peak + flows[fi][1], Decimal("0"))
            fi += 1
        peak = max(peak, s.equity_usd)
        lvl = _level(peak, s.equity_usd, policy)
        if _RANK[lvl] > _RANK[highest]:
            highest = lvl
    latest = valid[-1]
    dd = max(Decimal("0"), (peak - latest.equity_usd) / peak)
    return DrawdownState(highest, dd, peak, latest.equity_usd, latest.asof_utc,
                         highest is DrawdownLevel.HARD_HALT, latched=highest is not DrawdownLevel.NONE,
                         current_level=_level(peak, latest.equity_usd, policy))


def escalated(prev: DrawdownLevel, new: DrawdownLevel) -> bool:
    """True when `new` is a higher severity than `prev` (alert policy must emit a fresh alert)."""
    return _RANK[new] > _RANK[prev]
