"""Stage B2 section F: alerts page (WT-04 'show generation/delivery state on the page ... Expose the active threshold values and
policy version'; telegram-alerts skill: acknowledgment is not proof of reading; unknown ACK stays ambiguous, never auto-resent).

Expected mapping, derived from the outbox status vocabulary in the telegram-alerts skill / spec (PENDING, SENDING, RETRY, SENT,
UNKNOWN_ACK, FAILED_FINAL, DROPPED_*) and the task brief:
  PENDING -> queued | SENDING -> sending | RETRY -> retry | SENT -> acknowledged | UNKNOWN_ACK -> unknown acknowledgment
  FAILED_FINAL -> failed | DROPPED_EXPIRED / DROPPED_STALE -> dropped
Thresholds oracle = config/policy.yaml read directly (profit 0.60, close review DTE 21, loss multiple 2.00, delta 0.30, assignment 7).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
import yaml
from b1_env import B1, text_of
from web_helpers import fingerprint

REPO = Path(__file__).resolve().parents[2]
STATES = [("PENDING", "queued"), ("SENDING", "sending"), ("RETRY", "retry"), ("SENT", "acknowledged"), ("UNKNOWN_ACK", "unknown acknowledgment"),
          ("FAILED_FINAL", "failed"), ("DROPPED_EXPIRED", "dropped"), ("DROPPED_STALE", "dropped")]
TOKEN = "123456789:AAH-fake_token_value_0123456789abcdefghijk"


def put_alert(db, aid, status, *, cls="PROFIT_EXIT", sev="WARN", trade="OPN", ev=None, text="synthetic alert text", created="2026-10-30T14:00:00Z",
              nxt=None, attempts=0, mid=None):
    c = sqlite3.connect(db)
    evidence = {"revalidate": {"trade_id": trade, "trigger": "PROFIT_EXIT", "ticker": "AAA", "fingerprint": "fp-" + aid}, **(ev or {})}
    c.execute("INSERT INTO alert_outbox (alert_id, dedup_key, class, severity, status, created_at_utc, next_attempt_at_utc, attempts, "
              "telegram_message_id, content_text, evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (aid, "dk-" + aid, cls, sev, status, created, nxt, attempts, mid, text, json.dumps(evidence)))
    c.commit()
    c.close()


@pytest.fixture()
def w(tmp_path):
    x = B1(tmp_path)
    for i, (st, _) in enumerate(STATES):
        put_alert(x.db, f"AL-{i}", st, created=f"2026-10-30T14:0{i}:00Z", attempts=i, nxt="2026-10-30T15:30:00Z" if st == "RETRY" else None,
                  mid="42" if st == "SENT" else None,
                  ev={"last_error": "HTTP 500 from Telegram"} if st in ("FAILED_FINAL", "DROPPED_STALE") else None)
    yield x
    x.stop()


def rows_text(w):
    h = w.get("/alerts")[2]
    return h, text_of(h)


def row_for(h, aid):
    m = re.search(rf'<tr>(?:(?!</tr>).)*?/alerts/{aid}\b.*?</tr>', h, re.S)
    assert m, aid
    return text_of(m.group(0))


@pytest.mark.parametrize("i,st,word", [(i, s, wd) for i, (s, wd) in enumerate(STATES)])
def test_each_outbox_status_maps_to_the_right_delivery_label(w, i, st, word):
    h, _ = rows_text(w)
    r = row_for(h, f"AL-{i}").lower()
    assert word in r, (st, r)
    # never claim more than the Bot API said
    for bad in ("delivered to you", "read", "seen", "received by you", "confirmed delivered"):
        assert bad not in re.sub(r"does not prove you read", "", r), (st, bad)
    if st == "UNKNOWN_ACK":
        assert "never auto-resent" in r or "may or may not" in r
    if st == "RETRY":
        assert "2026-10-30 10:30" in r or "15:30" in r                           # next attempt shown (CDT 10:30 = 15:30Z)
    if st in ("DROPPED_EXPIRED", "DROPPED_STALE"):
        assert "dropped" in r and "acknowledged" not in r


def test_sent_is_not_presented_as_read_or_acted_and_the_disclaimer_is_visible(w):
    _, t = rows_text(w)
    assert "Telegram acknowledgment does not prove you read or acted." in t
    low = t.lower()
    assert "delivered and read" not in low and "you have read" not in low


def test_ids_are_stable_links_and_the_detail_page_exists(w):
    h1, h2 = w.get("/alerts")[2], w.get("/alerts")[2]
    ids1 = re.findall(r'href="/alerts/([^"?]+)', h1)
    assert ids1 == re.findall(r'href="/alerts/([^"?]+)', h2) and {f"AL-{i}" for i in range(8)} <= set(ids1)
    st, _, d = w.get("/alerts/AL-3")
    assert st == 200 and "AL-3" in d
    assert w.get("/alerts/NOPE")[0] == 404


def test_no_resend_cancel_or_trade_controls_anywhere_on_the_alert_pages(w):
    before = fingerprint(w.db)
    for p in ("/alerts", "/alerts/AL-0", "/alerts/AL-4", "/alerts?status=RETRY"):
        h = w.get(p)[2]
        forms = re.findall(r"<form\b[^>]*>", h)
        assert all('action="/logout"' in f or 'method="get"' in f.lower() for f in forms), (p, forms)
        low = text_of(h).lower().replace("cannot resend", "")
        for bad in ("resend", "send again", "retry now", "re-send", "place order", "cancel order", "sell now", "buy now"):
            assert bad not in low, (p, bad)
    assert fingerprint(w.db) == before
    for p in ("/alerts/resend", "/alerts/AL-4/resend", "/alerts/AL-4/retry"):
        assert w.post(p, {"alert": "AL-4"})[0] in (404, 405)


def test_position_page_links_its_alerts(w):
    h = w.get("/positions/OPN")[2]
    links = set(re.findall(r'href="/alerts/([^"?]+)', h))
    assert {f"AL-{i}" for i in range(8)} <= links


def test_thresholds_and_policy_identity_match_config_policy_yaml(w):
    pol = yaml.safe_load((REPO / "config" / "policy.yaml").read_text(encoding="utf-8"))
    ex = pol["exits"]
    for page in ("/alerts", "/positions/OPN"):
        t = text_of(w.get(page)[2])
        assert "provisional" in t.lower() and "owner decides" in t.lower()
        assert pol["policy_id"] in t and str(pol["policy_version"]) in t, page
        for v in (ex["profit_captured_fraction"], str(ex["close_review_at_dte_calendar"]), ex["loss_buyback_ask_multiple"],
                  ex["delta_review_abs"], str(ex["assignment_warning_dte"])):
            assert v in t, (page, v)
    # the page must not claim the values are final / approved
    assert "approved optimization" in text_of(w.get("/alerts")[2]) or "provisional" in text_of(w.get("/alerts")[2]).lower()


def test_hostile_alert_text_is_escaped_on_every_alert_page(w):
    put_alert(w.db, "AL-X", "FAILED_FINAL", cls="DATA_ISSUE", text="<script>alert(1)</script>",
              ev={"last_error": "<script>alert(2)</script> ‮",
                  "revalidate": {"trade_id": "<img src=x onerror=alert(1)>", "trigger": "<b>x</b>", "ticker": "‮AAA"}})
    for p in ("/alerts", "/alerts/AL-X", "/positions/OPN"):
        h = w.get(p)[2]
        assert "<script>alert" not in h and "<img src=x" not in h and "<b>x</b>" not in h, p
        assert "‮" not in h, p


def test_token_shaped_text_stored_in_last_error_is_never_rendered(w):
    put_alert(w.db, "AL-X", "FAILED_FINAL", ev={"last_error": f"HTTPError https://api.telegram.org/bot{TOKEN}/sendMessage"})
    for p in ("/alerts", "/alerts/AL-X"):
        h = w.get(p)[2]
        assert TOKEN not in h and "AAH-fake_token" not in h, p


def test_huge_outbox_is_paginated_and_the_page_stays_bounded(tmp_path):
    x = B1(tmp_path)
    try:
        c = sqlite3.connect(x.db)
        c.executemany("INSERT INTO alert_outbox (alert_id, dedup_key, class, severity, status, created_at_utc, attempts, content_text, evidence_json) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      [(f"BULK-{i:05d}", f"bk{i}", "PROFIT_EXIT", "WARN", "SENT", "2026-10-30T14:00:00Z", 1, "t", "{}") for i in range(3000)])
        c.commit()
        c.close()
        t0 = time.monotonic()
        st, _, h = x.get("/alerts")
        dt = time.monotonic() - t0
        n = len(re.findall(r'href="/alerts/BULK-', h))
        assert st == 200 and 0 < n <= 200 and len(h) < 600_000 and dt < 5, (n, len(h), dt)
        assert "3000" in text_of(h) or "3,000" in text_of(h)                      # total is stated, not silently capped
        nxt = re.findall(r'href="(/alerts\?[^"]*(?:before|after)=[^"]+)"', h)
        assert nxt, "no pagination link"
        seen = set(re.findall(r'href="/alerts/(BULK-\d+)', h))
        st2, _, h2 = x.get(nxt[-1].replace("&amp;", "&"))
        seen2 = set(re.findall(r'href="/alerts/(BULK-\d+)', h2))
        assert st2 == 200 and seen2 and not (seen & seen2)
    finally:
        x.stop()


def test_filters_by_state_work_and_reject_junk_without_error(w):
    t = text_of(w.get("/alerts?status=RETRY")[2])
    assert "AL-2" in t and "AL-0" not in t
    for junk in ("status=%3Cscript%3E", "status=" + "A" * 300, "severity=%00", "type=..%5C..", "cursor=-1", "before=abc"):
        st, _, h = w.get("/alerts?" + junk)
        assert st in (200, 400) and "Traceback" not in h
