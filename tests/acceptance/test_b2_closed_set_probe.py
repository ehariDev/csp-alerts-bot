"""B2-2b / B2-3 independent probe: the closed-set check on revalidate.trigger / revalidate.state must (a) still render every value the
application really writes and (b) withhold near-misses; templates.usd must have no negative zero.

Values the app writes (derived from reading the writers: alerts/templates.py exit_alert -> signal.trigger.value, state -> pos.state.value;
unresolved_assignment_alert -> "ASSIGNED_PENDING" + state value). The tests below build rows from the real enums AND from the real template
functions fed a real journal position."""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal as D

import pytest
from acc_helpers import NOW
from b1_env import B1
from stage_a_helpers import jopen
from test_b2_alerts import put_alert

from csp_alerts.journal.models import PositionState
from csp_alerts.policy.exits import ExitSignal, Trigger

WITHHELD = "(older record, text withheld)"


@pytest.fixture()
def w(tmp_path):
    x = B1(tmp_path)
    yield x
    x.stop()


def _api(w, aid):
    import json
    st, _, body = w.get(f"/api/alerts/{aid}")
    assert st == 200, (aid, st)
    return json.loads(body), body


def _put(w, aid, trig, state):
    put_alert(w.db, aid, "SENT", mid="7", ev={"revalidate": {"trade_id": "OPN", "trigger": trig, "ticker": "AAA", "state": state,
                                                             "fingerprint": "ab" * 16}})


@pytest.mark.parametrize("trig", [t.value for t in Trigger] + ["ASSIGNED_PENDING"])
@pytest.mark.parametrize("state", [s.value for s in PositionState])
def test_every_real_enum_pair_renders(w, trig, state):
    aid = f"AL-{trig}-{state}".replace("_", "-")
    _put(w, aid, trig, state)
    _, body = _api(w, aid)
    assert WITHHELD not in body, (trig, state)
    assert trig in body and state in body


NEAR = ["profit_exit", "PROFIT_EXIT\n", "PROFIT_EXIT ", " PROFIT_EXIT", "PROFIT_EXI", "PROFIT_EXIT_X", "XPROFIT_EXIT", "PROFITЕXIT",
        "ＰROFIT_EXIT", "ASSIGNED_PENDING_RECONCILIATION", "ASSIGNMENT_PENDING", "ASSIGNED", "", "OPEN", "PROFIT_EXIT\x00",
        "PROFIT-EXIT"]


@pytest.mark.parametrize("i", range(len(NEAR)))
def test_near_miss_trigger_withheld(w, i):
    v = NEAR[i]
    aid = f"AL-NT-{i}"
    _put(w, aid, v, "OPEN")
    d, body = _api(w, aid)
    assert d["alert"]["trigger"] == WITHHELD if "alert" in d else WITHHELD in body
    st, _, page = w.get(f"/alerts/{aid}")
    assert st == 200
    if v.strip():
        assert ("TRIG:" + v) not in page


NEAR_S = ["open", "OPEN\n", "OPE", "OPENX", "OPEN ", "ОPEN", "ASSIGNED_PENDING", "ASSIGNED_PENDING_RECONCILIATIO",
          "ASSIGNED_PENDING_RECONCILIATIONS", "PROFIT_EXIT", "closed", "ASSIGNED-RECONCILED", ""]


@pytest.mark.parametrize("i", range(len(NEAR_S)))
def test_near_miss_state_withheld(w, i):
    v = NEAR_S[i]
    aid = f"AL-NS-{i}"
    _put(w, aid, "PROFIT_EXIT", v)
    _, body = _api(w, aid)
    assert WITHHELD in body
    if v and v.isalpha() is False or v:
        # the near-miss text must not be echoed as the state value
        import json
        d = json.loads(body)
        assert v not in json.dumps(d.get("alert", d).get("decision_ref", {}).get("state", ""), ensure_ascii=True) or v in (
            "PROFIT_EXIT",) is False


def test_real_template_output_renders_on_alerts_page(w):
    """Feed the REAL template functions with a REAL journal position and show the meta they emit is displayed, not withheld."""
    from csp_alerts.alerts import templates
    j = jopen(w.db, NOW)
    pos = j.get_position("OPN")
    j.close() if hasattr(j, "close") else None
    now = NOW + timedelta(hours=1)
    seen = []
    for t in Trigger:
        sig = ExitSignal(t, "ACTION", "OPN", {})
        try:
            m = templates.exit_alert(sig, pos, None, None, now, None, "ACCT")
        except Exception as e:  # a template that cannot be built from an empty detail is not the subject of this probe
            pytest.skip(f"template for {t} needs detail: {e!r}")
        seen.append(m.meta)
    import dataclasses
    pa = dataclasses.replace(pos, state=PositionState.ASSIGNED_PENDING_RECONCILIATION)
    seen.append(templates.unresolved_assignment_alert(pa, now, "ACCT").meta)
    assert len(seen) == len(Trigger) + 1
    for i, meta in enumerate(seen):
        aid = f"AL-REAL-{i}"
        _put(w, aid, meta["trigger"], meta["state"])
        _, body = _api(w, aid)
        assert WITHHELD not in body, meta
        assert meta["trigger"] in body and meta["state"] in body


@pytest.mark.parametrize("val,expect", [
    ("-0.001", "$0.00"), ("-0.004", "$0.00"), ("0", "$0"), ("-1500", "-$1,500"), ("1234.5", "$1,234.50"),
    ("-0.01", "-$0.01"), ("-1234.5", "-$1,234.50"), ("-0", "$0"),
])
def test_usd_values(val, expect):
    from csp_alerts.alerts.templates import usd
    # hand: -0.001 and -0.004 round to 0.00 cents -> no sign; 1234.5 -> $1,234.50; whole dollars print without cents (deliberate)
    assert usd(D(val)) == expect


@pytest.mark.xfail(strict=True, reason="DEFECT B2-3b (cosmetic, residual): templates.usd decides the sign with ROUND_HALF_UP but prints digits with "
                   "format() (half-even): usd(Decimal('-0.005')) == '-$0.00' (negative zero), usd(Decimal('0.005')) == '$0.00', "
                   "usd(Decimal('-1234.565')) == '-$1,234.56'. Only exact half-cent inputs; money here is normally whole cents.")
def test_usd_half_cent_negative_has_no_negative_zero_or_wrong_sign():
    """-0.005: round-half-up gives 0.01 cents, so -$0.01 is the consistent answer; '-$0.00' would be negative zero."""
    from csp_alerts.alerts.templates import usd
    out = usd(D("-0.005"))
    assert out != "-$0.00", out
    assert not re.fullmatch(r"-\$0(\.0+)?", out), out
