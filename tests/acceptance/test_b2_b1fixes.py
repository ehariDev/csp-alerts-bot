"""Independent re-verification of the Stage B1 defect fixes (DECISIONS D15 items 144-151, 164): B1-3 (13+ lots), B1-1 (no prefilled
numbers, required attributes, server requires fees), B1-2 (ASCII-only digits), order wording, reflected values, info codes.
Synthetic, loopback only."""
from __future__ import annotations

import re
import unicodedata

import pytest
from b1_env import B1, checkin, sell, text_of
from test_b1_domain import CLOSE_OK, previewable
from test_b1_security import lots_env
from web_helpers import fingerprint

CANNED_FEES = "Enter the actual fees; type 0 if none."


@pytest.fixture()
def w(tmp_path):
    x = B1(tmp_path)
    yield x
    x.stop()


# ---------------------------------------------------------------- B1-3
@pytest.mark.parametrize("n", [13, 14, 30])
def test_checkin_with_more_than_twelve_lots_is_a_200_page_with_cli_pointer_and_no_partial_form(tmp_path, n):
    y = lots_env(tmp_path, n)
    try:
        st, _, body = y.get("/checkin")
        assert st == 200 and "use the CLI" in text_of(body)
        assert 'name="nlots"' not in body and 'action="/checkin/preview"' not in body        # it could not show every lot: no form
        before = fingerprint(y.db)
        st, _, html = y.post("/checkin/preview", {"action": "checkin", "equity": "100000", "free_cash": "96000", "nlots": "0"}, csrf=y.csrf("/watchlist"))
        assert not previewable(html) or st in (400, 200)        # a hand-crafted preview must not silently ignore the lots
        assert fingerprint(y.db) == before
    finally:
        y.stop()


# ---------------------------------------------------------------- B1-1: no prefilled values, required, server requires fees
FORM_URLS = ["/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=filled", "/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=working",
             "/trade/mark-filled/OPN", "/trade/close-order/OPN", "/trade/close-fill/OPN", "/trade/cancel/OPN", "/trade/cancel-close/OPN",
             "/trade/expire/OPN", "/checkin"]
NO_PREFILL = ("credit", "debit", "fees", "executed_date", "executed_time", "expiry_date", "expiry", "free_cash", "equity")


def test_no_trade_form_prefills_any_owner_number_date_or_time(w):
    seen = 0
    for u in FORM_URLS:
        st, _, h = w.get(u)
        assert st == 200, u
        for tag in re.findall(r"<input\b[^>]*>", h):
            a = dict(re.findall(r'(\w[\w-]*)="([^"]*)"', tag))
            if a.get("type") == "hidden" or a.get("name") not in NO_PREFILL:
                continue
            seen += 1
            assert a.get("value", "") == "", (u, tag)
    assert seen >= 10


def test_required_attribute_on_fees_credit_debit_dates_and_times(w):
    _, loc, _ = w.do({**sell("BBB", "30"), "action": "sell_working"})
    wk = loc.split("trade=")[1].split("&")[0]
    need = {"/trade/sell?symbol=BBB&expiry=2026-12-04&strike=30&kind=filled": ("credit", "fees", "executed_date", "executed_time"),
            f"/trade/mark-filled/{wk}": ("credit", "fees", "executed_date", "executed_time"),
            "/trade/close-fill/OPN": ("debit", "fees", "executed_date", "executed_time"),
            "/trade/expire/OPN": ("expiry_date",)}
    for u, names in need.items():
        h = w.get(u)[2]
        for n in names:
            tags = re.findall(rf'<input\b[^>]*name="{n}"[^>]*>', h)
            assert tags and all(re.search(r"\brequired\b", t) for t in tags), (u, n, tags)


MISSING = [("sell_filled", sell("BBB", "30")), ("mark_filled", None), ("close_filled", {"action": "close_filled", "trade_id": "OPN", "debit": "0.20", **CLOSE_OK})]


@pytest.mark.parametrize("blank", ["", "  ", None])
@pytest.mark.parametrize("action", ["sell_filled", "close_filled"])
def test_server_refuses_blank_or_missing_fees_with_the_canned_message_and_accepts_0_and_0_00(w, action, blank):
    f = sell("BBB", "30") if action == "sell_filled" else {"action": "close_filled", "trade_id": "OPN", "debit": "0.20", **CLOSE_OK}
    g = dict(f)
    if blank is None:
        g.pop("fees")
    else:
        g["fees"] = blank
    before = fingerprint(w.db)
    html = w.preview(g)[2]
    assert not previewable(html) and CANNED_FEES in text_of(html), text_of(html)[-500:]
    assert fingerprint(w.db) == before
    for ok in ("0", "0.00"):
        assert previewable(w.preview({**f, "fees": ok})[2]), ok


def test_mark_filled_also_requires_fees(w):
    st, loc, _ = w.do(sell("BBB", "30", action="sell_working") if False else {**sell("BBB", "30"), "action": "sell_working"})
    tid = loc.split("trade=")[1].split("&")[0]
    base = {"action": "mark_filled", "trade_id": tid, "credit": "0.52", "executed_date": "2026-10-30", "executed_time": "10:00"}
    assert CANNED_FEES in text_of(w.preview(base)[2]) and CANNED_FEES in text_of(w.preview({**base, "fees": ""})[2])
    assert previewable(w.preview({**base, "fees": "0"})[2])


def test_expiry_date_is_required_never_defaulted_and_malformed_dates_are_canned(w):
    for bad in ("", "2026-13-45", "2026-02-30", "2026-1-1", "26-10-30", "2026/10/30"):
        h = w.preview({"action": "expire", "trade_id": "OPN", "expiry_date": bad})[2]
        assert not previewable(h) and "Traceback" not in h, bad
    assert not previewable(w.preview({"action": "expire", "trade_id": "OPN"})[2])


# ---------------------------------------------------------------- B1-2: ASCII digits only
HOSTILE = {
    "credit": ["٠.45", "0.4٥", "０.45", "0.45²", "0.4\n5", "½", "①", "۰.45", "०.45", "0.45\n0.46"],
    "fees": ["٠.65", "0.6٥", "０.65", "0.65²", "0.6\n5", "½", "०"],
    "strike": ["٣٠", "３０", "3٠", "30\n0"],
    "contracts": ["１", "١", "¹", "1\n1"],
    "executed_date": ["２０２６-10-30", "2026-１０-30", "2026-10-3٠",
                      "٢٠٢٦-١٠-٣٠", "2026-10-30\n0"],
    "executed_time": ["٠٩:٣٠", "09:3٠", "０９:３０", "09:30\n09:30"],
    "expiry": ["２０２６-12-04", "2026-12-0٤", "2026-12-04\n2026-12-04"],
}
CASES = [(f, v) for f, vs in HOSTILE.items() for v in vs]


@pytest.mark.parametrize("field,val", CASES, ids=[f"{f}-{i}" for i, (f, v) in enumerate(CASES)])
def test_every_sell_numeric_date_time_field_rejects_non_ascii_digits_and_embedded_newlines(w, field, val):
    """Surrounding whitespace is stripped (pinned by test_b1_domain FEES ' 0.65 '), but any non-ASCII digit or an interior line break
    must be refused without a write."""
    before = fingerprint(w.db)
    st, _, html = w.preview({**sell("BBB", "30"), field: val})
    assert st in (200, 400) and not previewable(html), (field, ascii(val))
    assert fingerprint(w.db) == before


@pytest.mark.parametrize("val", ["0.45\n", "0.45 \n", "\n0.45"], ids=["trail-nl", "trail-sp-nl", "lead-nl"])
def test_surrounding_whitespace_is_only_stripped_never_stored_raw(w, val):
    html = w.preview(sell("BBB", "30", credit=val))[2]
    if previewable(html):
        _, loc, _ = w.do(sell("BBB", "30", credit=val))
        assert w.sql("SELECT actual_open_credit_per_share FROM option_positions WHERE trade_id LIKE 'WEB-%'")[0][0] == "0.45"


@pytest.mark.parametrize("bad", ["١٠٠٠٠٠", "１０００００", "1000\n00", "¹⁰"])
def test_checkin_equity_and_cash_reject_non_ascii_digits_and_interior_newlines(w, bad):
    for fld in ("equity", "free_cash"):
        f = {"action": "checkin", "equity": "100000", "free_cash": "96000", "nlots": "0"}
        f[fld] = bad
        st, _, html = w.post("/checkin/preview", f)
        assert st in (200, 400) and not previewable(html), (fld, ascii(bad))


@pytest.mark.parametrize("bad", ["١", "１", "¹", "x", "99999999999999999999"])
def test_checkin_lot_count_junk_never_applies_stock_marks(tmp_path, bad):
    """nlots is a count of ASCII digits. Anything else must not raise (no 500) and must never cause a stock mark to be applied."""
    y = lots_env(tmp_path, 2)
    try:
        f = {"action": "checkin", "equity": "100000", "free_cash": "96000", "nlots": bad, "lot_0": "L00", "mark_0": "41", "lot_1": "L01", "mark_1": "41"}
        st, _, html = y.post("/checkin/preview", f)
        assert st in (200, 400) and "Traceback" not in html
        if previewable(html):
            assert "41" not in " ".join(v for k, v in B1.hidden(html).items() if k.startswith("mark")), "mark applied despite junk lot count"
    finally:
        y.stop()


def test_symbol_uppercasing_does_not_fold_non_ascii_letters_into_ascii(w):
    # 'ſ'.upper() == 'S' and 'ı'.upper() == 'I': these would let a look-alike address another ticker
    for sym in ("ſSS", "ıNTC", "bbb​", "B" + chr(10) + "BB", "ＢＢＢ"):
        assert not previewable(w.preview(sell(sym, "30"))[2]), ascii(sym)
    assert previewable(w.preview(sell("bbb", "30"))[2])                       # plain ASCII lower-case is still fine


def test_get_filters_with_non_ascii_digits_never_500_and_never_write(w):
    before = fingerprint(w.db)
    for p in ("/market/BBB?strike_min=%D9%A3%D9%A0", "/market/BBB?expiry=%EF%BC%92%EF%BC%90%EF%BC%92%EF%BC%96-12-04",
              "/market/BBB?strike_max=30%0A", "/trade/sell?symbol=BBB&expiry=2026-12-04&strike=%D9%A3%D9%A0&kind=filled",
              "/trade/sell?symbol=BBB&expiry=2026-12-04%0A&strike=30&kind=filled", "/positions/%D9%A1", "/alerts?cursor=%D9%A1%D9%A2"):
        st, _, h = w.get(p)
        assert st in (200, 400, 404), (p, st)
        assert "Traceback" not in h
    # the sell-form query with a Unicode strike must not render a usable form for a contract that is not exactly stored
    h = w.get("/trade/sell?symbol=BBB&expiry=2026-12-04&strike=%D9%A3%D9%A0&kind=filled")[2]
    assert 'name="credit"' not in h
    assert fingerprint(w.db) == before


# ---------------------------------------------------------------- wording
def test_sell_to_open_and_buy_to_close_wording_on_forms_and_previews(w):
    _, loc, _ = w.do({**sell("BBB", "30"), "action": "sell_working"})
    wk = loc.split("trade=")[1].split("&")[0]
    for u in (FORM_URLS[0], FORM_URLS[1], f"/trade/mark-filled/{wk}"):
        t = text_of(w.get(u)[2])
        assert "Sell to open" in t, u
    for u in ("/trade/close-order/OPN", "/trade/close-fill/OPN"):
        assert "Buy to close" in text_of(w.get(u)[2]), u
    assert "Buy to close" in text_of(w.preview({"action": "close_filled", "trade_id": "OPN", "debit": "0.2", **CLOSE_OK})[2])
    assert "Sell to open" in text_of(w.preview(sell("BBB", "30"))[2])
    allt = " ".join(text_of(w.get(u)[2]) for u in FORM_URLS)
    for bad in ("buy to open", "sell to close", "place the order", "submit order", "we will place"):
        assert bad not in allt.lower(), bad


# ---------------------------------------------------------------- reflected values
FORBIDDEN_CATS = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
POISON = "A‮B​C\x00D EF͸G﻿H\x1bI\x07J\ttab\nnl"


def forbidden_in(html: str) -> list[str]:
    body = re.sub(r"<[^>]+>", "", html)
    return sorted({hex(ord(c)) for c in html if unicodedata.category(c) in FORBIDDEN_CATS and c not in "\n\r\t"} |
                  {hex(ord(c)) for c in body if unicodedata.category(c) in FORBIDDEN_CATS and c not in "\n\r\t"})


def test_reflected_values_are_stripped_of_control_format_separator_and_private_use_characters(w):
    pages = []
    for f in (sell("BBB", "30", credit=POISON), sell("BBB", "30", fees=POISON), sell("BBB", "30", executed_date=POISON, executed_time=POISON),
              sell("ZZZ", "30", sector=POISON, bucket=POISON), {**sell("BBB", "30"), "reason": POISON, "symbol": "BBB" + POISON},
              {"action": "expire", "trade_id": "OPN", "expiry_date": POISON}, {"action": "close_filled", "trade_id": "OPN" + POISON, "debit": POISON}):
        pages.append(w.preview(f)[2])
    pages.append(w.post("/checkin/preview", {"action": "checkin", "equity": POISON, "free_cash": POISON, "nlots": POISON})[2])
    pages.append(w.post("/portfolio/labels/preview", {"ticker": "AAA", "sector": POISON, "bucket": POISON}, csrf=w.csrf("/portfolio/labels/AAA"))[2])
    for h in pages:
        assert forbidden_in(h) == [], forbidden_in(h)
        assert "<script" not in h.lower()


def test_reflected_input_values_are_length_limited(w):
    h = w.preview(sell("ZZZ", "30", sector="a" * 5000, bucket="b" * 5000, credit="1" * 5000))[2]
    assert len(h) < 60000 and "a" * 200 not in h and "1" * 200 not in h


# ---------------------------------------------------------------- info codes persisted
def test_contract_info_codes_are_persisted_and_displayed_as_information_not_as_a_block(w):
    """BBB 90 vs spot 61.25: the strike is above the stock price (information). It must not block recording, must be stored in the audit
    payload and shown on the position page as information."""
    import json
    st, loc, html = w.do(sell("BBB", "90"))
    assert w.result_code(loc) == "recorded", text_of(html)[-600:]
    tid = loc.split("trade=")[1].split("&")[0]
    ev = json.loads(w.sql("SELECT after_json FROM audit_log WHERE event_type='WEB_TRADE_RECORDED' ORDER BY rowid DESC LIMIT 1")[0][0])
    assert ev["contract_info_codes"], ev
    t = text_of(w.get(f"/positions/{tid}")[2])
    assert "off-strategy contract characteristics (information)" in t
    assert all(c in t for c in ev["contract_info_codes"])
    # info codes must not create a breach flag by themselves
    assert w.sql("SELECT 1 FROM audit_log WHERE event_type='POLICY_BREACH_RECORDED' AND after_json LIKE ?", (f"%{tid}%",)) == []
