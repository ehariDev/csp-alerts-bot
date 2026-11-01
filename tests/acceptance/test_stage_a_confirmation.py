"""Stage A confirmation pass (DECISIONS item 130): D5 CLI lookalikes, D2 clock-seam source audit, D4 /api POST.

Oracles are hand-derived:
 - D5: 'ſpy' (U+017F) and 'ıbm' (U+0131) contain non-ASCII; str.upper() would map them to 'S'/'I'. They must be rejected, never folded.
 - D2: the only allowed wall-clock reads in src/ are the named injectable defaults and the provider-retrieval timestamp.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from acc_helpers import NOW, REPO
from s2_helpers import FakeSource
from s3_helpers import run_cli

from csp_alerts.jobs import ticker_check as tck
from csp_alerts.journal import watchlist as wl

BS = chr(92)


# ----------------------------------------------------------------------------------------------------- D5
@pytest.mark.parametrize("raw", ["\u017fpy", "\u0131bm", "SPY\u017f", "\uff33\uff30\uff39", "\u212aO", "\u0410APL"])
def test_normalize_symbol_rejects_non_ascii_lookalikes_in_both_normalizers(raw):
    assert tck.normalize_symbol(raw) is None
    with pytest.raises(wl.WatchlistError):
        wl.normalize_symbol(raw)


@pytest.mark.parametrize("raw", ["spy", " Spy ", "brk-b", "BRK.B"])
def test_normalize_symbol_still_accepts_ascii_case_variants(raw):
    assert tck.normalize_symbol(raw) == raw.strip().upper() == wl.normalize_symbol(raw)


@pytest.mark.parametrize("raw", ["\u017fpy", "\u0131bm"])
def test_ticker_check_cli_rejects_lookalikes_without_fetching(monkeypatch, tmp_path, raw):
    src = FakeSource({"SPY": [], "IBM": []}, {}, NOW)
    code, out, err = run_cli(monkeypatch, tmp_path, ["ticker-check", "--ticker", raw], source=src)
    blob = out + err
    assert code != 0, blob
    assert src.calls == [] and src.earn_calls == [], "provider must not be called for an invalid symbol"
    assert "STATUS: INVALID" in blob and "VERDICT: DATA UNAVAILABLE" in blob and "VERDICT: PASS" not in blob


def test_no_fallback_uppercasing_precedes_the_ascii_check():
    """Source audit: in both normalizers the isascii() test precedes .upper()."""
    for rel in ("src/csp_alerts/jobs/ticker_check.py", "src/csp_alerts/journal/watchlist.py"):
        t = (REPO / rel).read_text(encoding="utf-8")
        body = t[t.index("def normalize_symbol"):]
        body = body[:body.index("\ndef ", 10)] if "\ndef " in body[10:] else body
        assert body.index("isascii()") < body.index(".upper()"), rel


# D7 (redaction) tests were removed with the redactor (DECISIONS item 133); see test_stage_a_provider_failures.py.


# ----------------------------------------------------------------------------------------------------- D2 audit
ALLOWED_WALL_CLOCK = {
    "src/csp_alerts/cli.py": 1,                 # def _now() (the seam)
    "src/csp_alerts/journal/store.py": 1,       # Journal default clock
    "src/csp_alerts/journal/webwrites.py": 1,   # default clock of the dashboard write helper
    "src/csp_alerts/market/stockpile.py": 1,    # provider retrieval timestamp (adapter only)
    "src/csp_dashboard/readonly.py": 1,         # default clock of the read-only view
    "src/csp_dashboard/views.py": 1,            # default clock field
}


def test_no_unexpected_direct_wall_clock_reads_in_src():
    found = {}
    for p in (REPO / "src").rglob("*.py"):
        n = len(re.findall(r"datetime\.(?:now|utcnow)\(|date\.today\(|\btime\.time\(", p.read_text(encoding="utf-8")))
        if n:
            found[p.relative_to(REPO).as_posix()] = n
    assert found == ALLOWED_WALL_CLOCK, found
