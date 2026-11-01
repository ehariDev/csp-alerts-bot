"""SR-08 input path: complete_assignment rejects a future-dated stock mark (same guard as add_stock_lot)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal as D

import pytest
from factories import NOW, TODAY

from csp_alerts.journal.models import PositionState as S, StockLot
from csp_alerts.journal.store import Journal, JournalError, NewPosition


def _pending(tmp_path):
    j = Journal(tmp_path / "a.sqlite3", clock=lambda: NOW)
    j.migrate()
    j.create_draft(NewPosition("T1", "EXAMPLE", TODAY + timedelta(days=5), D("40"), 100, 1, "s", "b"))
    j.transition("T1", S.PENDING)
    j.transition("T1", S.OPEN, fill_contracts=1, actual_open_credit_per_share=D("0.5"), opened_at_utc=NOW)
    j.transition("T1", S.ASSIGNED_PENDING_RECONCILIATION)
    return j


def _lot(asof):
    return StockLot("LOT-T1", "EXAMPLE", D("100"), D("40"), asof, "s", "b", "ASSIGNMENT", cost_basis_reference_usd=D("4000"))


def test_future_mark_rejected_and_nothing_changes(tmp_path):
    j = _pending(tmp_path)
    with pytest.raises(JournalError, match="dated in the future"):
        j.complete_assignment("T1", _lot(NOW + timedelta(days=365)))
    assert j.get_position("T1").state is S.ASSIGNED_PENDING_RECONCILIATION and j.list_stock_lots() == []
    j.close()


def test_mark_within_tolerance_accepted(tmp_path):
    j = _pending(tmp_path)
    j.complete_assignment("T1", _lot(NOW + timedelta(minutes=4)))
    assert j.get_position("T1").state is S.ASSIGNED_RECONCILED
    j.close()
