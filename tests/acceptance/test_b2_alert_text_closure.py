"""B2-2 confirmation: hostile values stored DIRECTLY in alert_outbox rows must never render raw on /alerts, /alerts/<id> or the JSON API.
Every hostile value contains the canary ZQX9 plus characters no closed pattern (hex digest, ISO time, identifier, upper-case ticker,
exact canned reason) can accept, so ANY appearance of ZQX9 in a response is a leak."""
from __future__ import annotations

import json

import pytest
from b1_env import B1
from test_b2_alerts import TOKEN, put_alert

CANARY = "ZQX9"
BS = chr(92)
HOSTILE = {
    "token_url": f"{CANARY} HTTPError https://api.telegram.org/bot{TOKEN}/sendMessage",
    "token_bare": f"{CANARY}{TOKEN}",
    "path": CANARY + " C:" + BS + "Users" + BS + "ehari" + BS + "workspace" + BS + ".env",
    "html": f"{CANARY}<script>alert(1)</script><img src=x onerror=alert(2)><b>x</b>",
    "ctrl": f"{CANARY}\x00\x1b[31m\x07\r\n\t end",
    "bidi": f"{CANARY}\u202e\u2066 evil\u200b\ufeff",
    "unicode": f"{CANARY} \uff21\uff21\uff21 \u0410\u0410\u0410 \U0001f600 caf\u00e9",
    "big": CANARY + "A" * 100_000 + " " + CANARY,
    "sql_quote": f"{CANARY}'; DROP TABLE alert_outbox;--",
    "near_canned": "expired before send " + CANARY,        # a canned reason with a suffix must not pass
    "blocked_unknown_code": f"entries now blocked: NOT_APPROVED,{CANARY}",
}
FIELDS = ["last_error", "trade_id", "trigger", "ticker", "policy_sha", "decision_ref", "expires_at_utc", "fingerprint", "parse_mode", "state"]


def _urls(aid):
    return ["/alerts", f"/alerts/{aid}", "/api/alerts", f"/api/alerts/{aid}"]


@pytest.fixture()
def w(tmp_path):
    x = B1(tmp_path)
    yield x
    x.stop()


def _cases():
    out = []
    for field in FIELDS:
        for hk in HOSTILE:
            out.append((field, hk))
    return out


@pytest.mark.parametrize("field,hk", _cases())
def test_hostile_value_in_any_evidence_field_never_renders(w, field, hk):
    val = HOSTILE[hk]
    aid = f"AL-{field}-{hk}".replace("_", "-")
    ev = {"last_error": None} if field != "last_error" else {}
    if field == "last_error":
        ev = {"last_error": val}
    else:
        ev = {"revalidate": {"trade_id": "OPN", "trigger": "PROFIT_EXIT", "ticker": "AAA", field: val}, field: val}
    put_alert(w.db, aid, "FAILED_FINAL", ev=ev)
    seen = 0
    for p in _urls(aid):
        st, _, body = w.get(p)
        if st == 404:
            continue
        seen += 1
        assert st == 200, (p, st)
        assert CANARY not in body, (p, field, hk, body[body.find(CANARY) - 80: body.find(CANARY) + 120])
        assert TOKEN not in body and "AAH-fake_token" not in body, (p, field, hk)
        assert "<script>alert" not in body and "onerror=alert" not in body and "<b>x</b>" not in body, (p, field, hk)
        assert ("C:" + BS + "Users") not in body and "C:" + BS + BS + "Users" not in body, (p, field, hk)
        for ch in ("\x00", "\x1b", "\x07", "\u202e", "\u2066"):
            assert ch not in body, (p, field, hk, repr(ch))
    assert seen >= 2


def test_unknown_evidence_keys_and_nested_values_are_not_shown(w):
    put_alert(w.db, "AL-UK", "FAILED_FINAL", ev={f"{CANARY}_key": f"{CANARY}_val", "nested": {"a": [f"{CANARY}x", {"b": TOKEN}]},
                                                  "revalidate": {"zzz": f"{CANARY}", "trade_id": "OPN", "trigger": "PROFIT_EXIT", "ticker": "AAA"}})
    for p in _urls("AL-UK"):
        st, _, body = w.get(p)
        if st == 404:
            continue
        assert CANARY not in body and TOKEN not in body, p


def test_valid_closed_values_still_render_and_label_is_the_new_one(w):
    put_alert(w.db, "AL-OK", "FAILED_FINAL", ev={"last_error": "expired before send"})
    put_alert(w.db, "AL-OLD", "FAILED_FINAL", ev={"last_error": "400 Bad Request: chat not found"})
    put_alert(w.db, "AL-BLK", "DROPPED_STALE", ev={"last_error": "entries now blocked: APPROVAL_EXPIRED,NOT_APPROVED"})
    ok = w.get("/alerts/AL-OK")[2]
    old = w.get("/alerts/AL-OLD")[2]
    blk = w.get("/alerts/AL-BLK")[2]
    assert "expired before send" in ok and "Last delivery reason" in ok
    assert "older record, text withheld" in old and "chat not found" not in old
    assert "entries now blocked: APPROVAL_EXPIRED,NOT_APPROVED" in blk
    for h in (ok, old, blk, w.get("/alerts")[2]):
        assert "redacted" not in h.lower() and "Last error" not in h


def test_dashboard_import_closure_has_no_telegram_transport_and_reasons_module_is_outside_alerts():
    import subprocess
    import sys
    code = ("import sys, csp_dashboard.server, csp_dashboard.views, csp_alerts.policy.alert_reasons;"
            "bad=[m for m in sys.modules if m.startswith('csp_alerts.alerts') or m.endswith('telegram')];"
            "print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    import csp_alerts.policy.alert_reasons as ar
    assert "alerts" not in ar.__name__.split(".")[1:-1]
