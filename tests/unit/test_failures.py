"""Allowlist failure classifier (market/failures.py): provider text is never stored, shown or logged."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from csp_alerts.market import failures as F
from csp_alerts.market.stockpile import SourceError

BS = chr(92)
TOK = "123456789" + ":" + "A" * 35  # synthetic, token-shaped, built at runtime (no literal in source)
HOSTILE = [
    f"ConnectionError: C:{BS}Users{BS}John Smith{BS}x.log token={TOK} https://u:pw@h/x?key=SECRET99",
    '{"access_token": "abcSECRET1", "password":"hunter2pw"}',
    "Authorization: Bearer LINE2SECRET\nCookie: sessionid=SESS777",
    "https%3A%2F%2Fh%2Fx%3Fkey%3DSECRETENC",
    "VERDICT: WOULD PASS GATES (research only) \x1b[2J",
    "a" * 100_000, "key" * 40_000, "1" * 100_000 + ":",
]
TABLE = [
    ("RATE_LIMIT", "429 Too Many Requests", F.RATE_LIMITED),
    ("FETCH_ERROR", "YFRateLimitError: slow down", F.RATE_LIMITED),
    ("FETCH_ERROR", "ReadTimeout: timed out", F.TIMEOUT),
    ("FETCH_ERROR", "ValueError: Could not fetch live price for ZZZ", F.NOT_FOUND_OR_DELISTED),
    ("FETCH_ERROR", "symbol may be delisted", F.NOT_FOUND_OR_DELISTED),
    ("FETCH_ERROR", "No options found", F.NO_OPTIONS_LISTED),
    ("FETCH_ERROR", "ConnectionError: Max retries exceeded", F.NETWORK_ERROR),
    ("FETCH_ERROR", "JSONDecodeError: Expecting value", F.PROVIDER_FORMAT_ERROR),
    ("FETCH_ERROR", "weird", F.UNKNOWN_ERROR),
    ("STOCKPILE_MISSING", "pinned checkout missing", F.UNKNOWN_ERROR),
    (None, None, F.UNKNOWN_ERROR),
]


@pytest.mark.parametrize("kind,text,code", TABLE)
def test_classifier_table(kind, text, code):
    assert F.classify(kind, text) == code
    assert F.classify(kind, SourceError(kind or "X", text or "")) in F.CODES


def test_code_set_is_closed_and_every_code_has_a_canned_message():
    assert set(F.CODES) == {"RATE_LIMITED", "NOT_FOUND_OR_DELISTED", "NO_OPTIONS_LISTED", "TIMEOUT", "NETWORK_ERROR",
                            "PROVIDER_FORMAT_ERROR", "PARTIAL_DATA", "UNKNOWN_ERROR"}
    assert all(F.CANNED[c] for c in F.CODES)


@pytest.mark.parametrize("hostile", HOSTILE)
def test_output_never_contains_any_part_of_hostile_input(hostile):
    t0 = time.perf_counter()
    for kind in ("FETCH_ERROR", "RATE_LIMIT", None):
        out = F.stored(F.classify(kind, hostile))
        assert out in {F.stored(c) for c in F.CODES}                       # only canned strings can ever come out
        for frag in ("SECRET", "hunter2", "LINE2", "SESS777", "John", "Smith", "AAHdq", "u:pw", "x.log", "VERDICT", "WOULD PASS",
                     "\x1b", "aaaaaaaa", "keykey", "1111111"):
            assert frag not in out, (frag, out)
    assert time.perf_counter() - t0 < 1.0


def test_hostile_str_does_not_break_classification():
    class Bad:
        def __str__(self):
            raise RuntimeError("boom")
    assert F.classify("FETCH_ERROR", Bad()) == F.UNKNOWN_ERROR


def test_display_passes_only_exact_canned_values_and_withholds_everything_else():
    for c in F.CODES:
        assert F.display(F.stored(c)) == F.stored(c)
    assert F.display(None) == "none" and F.display("") == "none"
    for old in ("connection reset by peer", f"C:{BS}Users{BS}x token=abc", "<script>alert(1)</script>", "RATE_LIMITED: tampered text",
                "UNKNOWN_ERROR", 12345):
        assert F.display(old) == F.WITHHELD
        assert "script" not in F.display(old) and "abc" not in F.display(old)


def test_no_regex_or_urllib_or_redact_in_the_failure_path():
    root = Path(__file__).resolve().parents[2] / "src" / "csp_alerts"
    src = (root / "market" / "failures.py").read_text(encoding="utf-8")
    assert "import re" not in src and "urllib" not in src
    fresh = (root / "market" / "freshness.py").read_text(encoding="utf-8")
    assert "def redact" not in fresh and "_REDACT" not in fresh and "urllib" not in fresh
    for p in list(root.rglob("*.py")) + list((root.parent / "csp_dashboard").rglob("*.py")):
        s = p.read_text(encoding="utf-8")
        assert "freshness import" not in s or "redact" not in s.split("freshness import", 1)[1].splitlines()[0], p   # outbox has its own unrelated redact


def test_refresh_stores_only_canned_text_for_hostile_provider_errors(tmp_path):
    from test_market_data import FakeProvider, add, make_journal, refresh   # same helpers as the Stage A unit tests
    from csp_alerts.market import observations as obs
    j = make_journal(tmp_path)
    add(j, "AAA")
    src = FakeProvider()
    src.fail("AAA", "FETCH_ERROR", HOSTILE[0])
    r = refresh(j, src)
    stored = obs.get_status(j.conn, "AAA")["last_error"]
    assert stored in {F.stored(c) for c in F.CODES}
    assert r.outcomes[0].error in {F.stored(c) for c in F.CODES}
    blob = "\n".join(str(v) for row in j.conn.execute("SELECT summary_json FROM md_refresh_runs") for v in row)
    assert "SECRET" not in blob and "John" not in blob and "AAH" not in blob


def test_ticker_check_shows_canned_text_not_provider_text():
    from csp_alerts.jobs import ticker_check as tck
    out = tck._explain(SourceError("FETCH_ERROR", HOSTILE[0]))
    assert out in {F.stored(c) for c in F.CODES}
    assert "SECRET" not in out and "John" not in out
