"""B2 cosmetics: singular/plural in mitigation text, one negative-money format, no negative zero (web, Telegram templates, CLI previews)."""
from __future__ import annotations

from decimal import Decimal as D

import pytest
from csp_alerts.money import usd2


def _entry(unit, used, limit, label="Active contracts"):
    return {"unit": unit, "used": D(used), "limit": D(limit), "room": D(limit) - D(used), "breached": D(used) > D(limit),
            "exhausted": D(used) == D(limit), "label": label, "reason": "CONTRACT_COUNT_LIMIT", "binding": False}


@pytest.mark.parametrize("unit,used,limit,want", [
    ("contracts", "5", "4", "over its limit by 1 contract."),        # 5 - 4 = 1 -> singular
    ("contracts", "6", "4", "over its limit by 2 contracts."),       # 2 -> plural
    ("names", "3", "2", "over its limit by 1 name."),
    ("names", "4", "2", "over its limit by 2 names."),
])
def test_mitigation_singular_plural(unit, used, limit, want):
    from csp_dashboard.views import mitigation_lines
    txt = " ".join(mitigation_lines([_entry(unit, used, limit)], []))
    assert want in txt, txt
    assert "(s)" not in txt


@pytest.mark.parametrize("v,want", [("-1500", "-$1,500.00"), ("1500", "$1,500.00"), ("-0.004", "$0.00"), ("-0", "$0.00"), ("0.005", "$0.00"),
                                    ("-0.015", "-$0.02"), ("-1234567.891", "-$1,234,567.89")])
def test_usd2_has_one_negative_format_and_no_negative_zero(v, want):
    # half-even on the cent: 0.005 -> 0.00, 0.015 -> 0.02 (nearest even); a result of zero never carries a sign
    out = usd2(D(v))
    assert out == want and "$-" not in out


def test_web_trade_preview_negative_money_uses_the_one_format(tmp_path):
    """Net premium = credit 0.45*100 - fees: with credit 0.00 and fees 0.65 net is -0.65 -> '-$0.65' (never '$-0.65')."""
    from b1_env import B1, sell, text_of
    x = B1(tmp_path)
    try:
        html = x.preview(sell("BBB", "30", credit="0.00"))[2]
        t = text_of(html)
        assert "$-" not in t
    finally:
        x.stop()


def test_telegram_templates_usd_has_no_negative_zero():
    from csp_alerts.alerts.templates import usd
    assert usd(D("-1500")) in ("-$1,500", "-$1,500.00")
    assert usd(D("-0.001")) == "$0.00" or usd(D("-0.001")) == "$0"
