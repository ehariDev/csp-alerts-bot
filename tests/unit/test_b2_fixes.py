"""Stage B2 tester defects B2-1 (one asset-type resolver), B2-2 (stored alert text is allowlisted) and two cosmetic items."""
from __future__ import annotations

from decimal import Decimal as D

import pytest
from factories import approval
from test_journal import j, newpos  # noqa: F401  (j is a fixture)

from csp_alerts.policy.alert_reasons import CANNED_REASONS, WITHHELD, display_reason
from csp_alerts.journal.models import PositionState as S
from csp_alerts.money import usd2
from csp_alerts.policy.approved import (ASSET_UNKNOWN, ASSET_UNKNOWN_LABEL, SRC_FILE, SRC_JOURNAL, SRC_NONE, asset_type_label,
                                        asset_type_map, resolve_asset_type)
from csp_alerts.policy.portfolio import company_names, is_company
from csp_dashboard import views

TOKEN = "123456789" + ":" + "AAH-fake_token_value_" + "0123456789abcd"      # built in pieces: the repo secret scan must not match


# ---------------------------------------------------------------- B2-1
def test_resolver_order_journal_then_file_then_unknown():
    assert resolve_asset_type("E", {"E": "ETF"}, {"E": "STOCK"}) == ("ETF", SRC_JOURNAL)
    assert resolve_asset_type("E", {}, {"E": "ETF"}) == ("ETF", SRC_FILE)
    assert resolve_asset_type("E", None, None) == (ASSET_UNKNOWN, SRC_NONE)
    assert asset_type_label(ASSET_UNKNOWN) == ASSET_UNKNOWN_LABEL == "UNKNOWN (not in approvals)"
    assert asset_type_label("ETF") == "ETF"


def test_only_a_known_etf_escapes_the_company_name_cap():
    assert is_company("STOCK") and is_company(ASSET_UNKNOWN) and is_company("anything else") and not is_company("ETF")


def _held(j):
    j.create_draft(newpos("P1", ticker="EEE"))
    j.transition("P1", S.PENDING)


def test_file_only_approval_gives_the_book_the_etf_type_and_no_company_name(j):
    _held(j)
    assert j.journal_asset_types() == {}                                  # the scan has not synced it
    book = j.load_book([approval(ticker="EEE", asset_type="ETF")])
    assert book.positions[0].asset_type == "ETF" and company_names(book) == ()
    n = j.conn.execute("SELECT COUNT(*) FROM approved_underlyings").fetchone()[0]
    assert n == 0                                                          # read-only: nothing was written to the journal


def test_no_approval_anywhere_is_unknown_and_counted_as_a_company(j):
    _held(j)
    book = j.load_book([])
    assert book.positions[0].asset_type == ASSET_UNKNOWN and company_names(book) == ("EEE",)
    assert j.load_book().positions[0].asset_type == ASSET_UNKNOWN          # unreadable approvals (None) behave the same


def test_journal_table_wins_over_the_file(j):
    _held(j)
    j.upsert_approved(approval(ticker="EEE", asset_type="STOCK"))
    book = j.load_book([approval(ticker="EEE", asset_type="ETF")])
    assert book.positions[0].asset_type == "STOCK"


def test_asset_type_map_tolerates_none():
    assert asset_type_map(None) == {} and asset_type_map([approval()]) == {"EXAMPLE": "STOCK"}


# ---------------------------------------------------------------- B2-2
def test_only_application_reasons_are_displayed():
    for r in CANNED_REASONS:
        assert display_reason(r) == r
    assert display_reason(None) is None and display_reason("") is None
    for bad in (f"HTTPError https://api.telegram.org/bot{TOKEN}/sendMessage", TOKEN, "400 Bad Request: chat not found",
                "entries now blocked: " + TOKEN, "entries now blocked: ", 42, ["x"], "expired before send "):
        assert display_reason(bad) == WITHHELD, bad


def test_safe_evidence_withholds_every_unlisted_string():
    ev = {"policy_sha": "a" * 64, "parse_mode": "HTML", "expires_at_utc": "2026-10-30T14:00:00Z", "part": 1, "parts": 1,
          "scope": "paper_primary", "last_error": f"HTTPError https://api.telegram.org/bot{TOKEN}/x",
          "revalidate": {"trade_id": "T1", "ticker": "AAA", "approval_sha": TOKEN, "ledger_marker": "2026-10-30T14:00:00+00:00|s1|7",
                         "extra": TOKEN}, "surprise": TOKEN}
    out = views.safe_evidence(ev)
    assert "AAH-fake" not in repr(out) and "123456789" not in repr(out)
    assert out["last_error"] == WITHHELD and out["policy_sha"] == "a" * 64 and out["parts"] == 1
    assert out["revalidate"]["trade_id"] == "T1" and out["revalidate"]["approval_sha"] == views.FIELD_WITHHELD
    assert out["revalidate"]["ledger_marker"].endswith("|7") and "extra" not in out["revalidate"]
    assert out["other_keys_withheld"] == 1


def test_safe_field_rejects_token_shaped_ids():
    assert views._safe_field("trade_id", "WEB-AAA-20261120-ab12") == "WEB-AAA-20261120-ab12"
    assert views._safe_field("trade_id", TOKEN) == views.FIELD_WITHHELD
    assert views._safe_field("ticker", "aaa") == views.FIELD_WITHHELD


# ---------------------------------------------------------------- cosmetic
@pytest.mark.parametrize("v,s", [("-1500", "-$1,500.00"), ("1500", "$1,500.00"), ("0", "$0.00"), ("-0.004", "$0.00"),
                                 ("-12.345", "-$12.34"), ("1234567.5", "$1,234,567.50")])
def test_negative_money_has_one_format(v, s):
    assert usd2(D(v)) == s and views._usd(D(v)) == s


def test_mitigation_text_singular_and_plural():
    def entry(unit, used, limit):
        return {"scope": "portfolio", "label": "L", "unit": unit, "limit": D(limit), "used": D(used), "room": D(limit) - D(used),
                "breached": True, "exhausted": False, "reason": "R", "binding": True}
    one = views.mitigation_lines([entry("contracts", 5, 4)], [])[0]
    many = views.mitigation_lines([entry("contracts", 6, 4)], [])[0]
    names = views.mitigation_lines([entry("names", 3, 2)], [])[0]
    usd = views.mitigation_lines([entry("USD", 16000, 15000)], [])[0]
    assert "over its limit by 1 contract." in one and "1 contracts" not in one and "used 5 contracts of 4 contracts" in one
    assert "over its limit by 2 contracts." in many and "over its limit by 1 name." in names
    assert "over its limit by $1,000.00." in usd


# ---------------------------------------------------------------- B2-2b / B2-3
def test_trigger_and_state_are_closed_enums():
    sf = views._safe_field
    for t in ("PROFIT_EXIT", "DATA_ISSUE", "ASSIGNED_PENDING"):
        assert sf("trigger", t) == t
    for s in ("OPEN", "ASSIGNED_PENDING_RECONCILIATION"):
        assert sf("state", s) == s
    for k, bad in [("trigger", "expired before send ZQX9"), ("state", "expired before send ZQX9"),
                   ("trigger", "NOT_A_TRIGGER"), ("state", "open"), ("trigger", "PROFIT_EXIT "), ("state", "BOGUS_STATE")]:
        assert sf(k, bad) == views.FIELD_WITHHELD


def test_telegram_usd_no_negative_zero_and_whole_dollar_format_kept():
    from csp_alerts.alerts.templates import usd
    assert usd(D("-0.001")) == "$0.00"
    assert usd(D("-0.004")) == "$0.00"
    assert usd(D("0")) == "$0"
    assert usd(D("-0.01")) == "-$0.01"
    assert usd(D("-1500")) == "-$1,500"
    assert usd(D("1234.5")) == "$1,234.50"
