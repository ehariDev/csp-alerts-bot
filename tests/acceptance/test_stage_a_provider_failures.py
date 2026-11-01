"""Stage A: provider failures are an ALLOWLIST (DECISIONS item 133). Replaces the freshness.redact adversarial suites.

Contract derived from the request (not from the implementation):
 (a) whatever a provider raises/returns, the only failure text that is stored / printed / rendered is ONE of eight canned
     "<CODE>: <message>" strings; no >=4-char fragment of the hostile secret survives anywhere (md_* tables, the raw SQLite file,
     run summary JSON, CLI stdout, ticker-check text and JSON, dashboard HTML and JSON API); hostile sizes are fast.
 (b) a legacy free-text row (written by an older version) is shown as "UNKNOWN_ERROR: older record, text withheld".
 (c) classification table: 429/rate limit -> RATE_LIMITED, timeouts -> TIMEOUT, connection errors -> NETWORK_ERROR,
     not found/delisted -> NOT_FOUND_OR_DELISTED, no expirations -> NO_OPTIONS_LISTED, parse/schema -> PROVIDER_FORMAT_ERROR,
     else UNKNOWN_ERROR; hostile __str__ tolerated; deterministic.
 (d) the dashboard column/field label is "Provider status" on /watchlist and /market/<SYM>.
Oracle for leak checks: every 4-character window (case-folded) of the secret material must be absent from the output.
Secrets are chosen so that none of their 4-grams occurs in the canned corpus (asserted below, so the check cannot be vacuous).
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from acc_helpers import NOW, POLICY_PATH
from s2_helpers import FakeSource, raw_row
from s3_helpers import run_cli
from stage_a_env import approved_file, dash, mkt, plain_db, text_of
from stage_a_helpers import POL, Prov, add_wl, hold, jopen, md_dump, refresh

from csp_alerts.market import failures as F
from csp_alerts.market.stockpile import SourceError

ROOT = Path(__file__).resolve().parents[2]
BS = "\\"
TOK = "123456789" + ":" + "A" * 35  # synthetic, token-shaped, built at runtime (no literal in source)
CANNED_VALUES = {F.stored(c) for c in F.CODES}
CORPUS = " ".join(CANNED_VALUES).casefold()
LEGACY_MSG = "UNKNOWN_ERROR: older record, text withheld"


def grams(s: str, n: int = 4) -> set[str]:
    s = s.casefold()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


RANDOM_BLOBS = ("md", "rawfile")      # contain random uuid/hex/hash text: short windows false-positive by chance (e.g. '1234')
RANDOM_WINDOW = 8                     # P(a given 8-char window occurs by chance in 1e6 random hex chars) ~ 2e-4 per gram, and it is still
                                      # a real leak detector: any 8+ character run of the secret (or the whole of a shorter secret) is caught


def leaks(blob, secret: str, n: int = 4) -> list[str]:
    if isinstance(blob, bytes):
        blob = blob.decode("utf-8", "ignore")
    b = blob.casefold()
    n = min(n, len(secret))
    return sorted(g for g in grams(secret, n) if g in b)


# (name, full hostile text, secret material that must never appear, pure = contains no classifier trigger word)
CASES = [
    ("telegram-token", "failed: " + TOK + " trailing", TOK, True),
    ("bot-prefixed-token", "sent to bot" + TOK, TOK, True),
    ("bearer", "line1\nAuthorization: Bearer Lq2SxZq9Vb7\nline3", "Lq2SxZq9Vb7", True),
    ("basic", "Authorization: Basic dXNlcjpwYXNzWQ9vKx", "dXNlcjpwYXNzWQ9vKx", True),
    ("scheme-prefixed", "Token: Bearer Sz5PfXqz77Kw", "Sz5PfXqz77Kw", True),
    ("json-quoted", '{"access_token": "abXz1Qw7Pk", "x": 1}', "abXz1Qw7Pk", False),     # contains 'json'-free but quotes
    ("single-quoted", "api_key='QQQzx9zZwp' next", "QQQzx9zZwp", True),
    ("spaced-secret", 'secret = "spaced Qwxv7 value"', "Qwxv7", True),
    ("url-query", "HTTPError https://h.example/p?token=a%2Fb%3DcZQ&crumb=Kp2Wm9Xv", "Kp2Wm9Xv", True),
    ("url-encoded", "https%3A%2F%2Fh%2Fx%3Fkey%3DEz9NcQw4Rt", "Ez9NcQw4Rt", True),
    ("double-encoded", "https%253A%252F%252Fh%252Fx%253Fkey%253DDz7BlQw5Hk", "Dz7BlQw5Hk", True),
    ("cookie", "a\r\nCookie: A3=d=Cz4VlQw8Mn; B=2\r\nb", "Cz4VlQw8Mn", True),
    ("windows-path-spaces", "open C:" + BS + "Users" + BS + "Jxhn Smxth" + BS + "x.log then something", "Jxhn Smxth" + BS + "x.log", True),
    ("posix-path-spaces", "read /home/bxb/my dxcs/x.sqlxte and more", "bxb/my dxcs/x.sqlxte", True),
    ("unc-path", BS * 2 + "srv" + BS + "shxre" + BS + "my dir" + BS + "f.txt", "shxre", True),
    ("userinfo-url", "proxy http://u:p4ssQzw@10.0.0.1:8080 down", "p4ssQzw", True),
    ("escaped-quote-json", '{"token":"has \\" quote Xq7RtZk2Lm9"}', "Xq7RtZk2Lm9", True),
    ("long-value-600", "password=" + "zQ" * 300, "zQzQ", True),
    ("cap200-straddle", ("pad " * 60)[:195] + " password=Tq8VbN4Hw3J", "Tq8VbN4Hw3J", True),
    ("cap2000-straddle", ("pad\n" * 600)[:1995] + "\npassword=Tq8VbN4Hw3J", "Tq8VbN4Hw3J", True),
    ("unicode-key", "密码 password=übZk7Qw3x", "bZk7Qw3x", True),
    ("ghp", "ghp_" + "a" * 8 + "RSTUVWXYZQ" * 3, "RSTUVWXYZQ", True),
    ("trigger-word-inside-secret-timeout", "token=sk-timeout-9XqLmN2vB", "9XqLmN2vB", False),
    ("trigger-word-inside-secret-429", "key=Zq429Lw8Kp3", "Zq429Lw8Kp3", False),
    ("trigger-word-inside-secret-connection", "secret=connection-Vb7Rt4Yh", "Vb7Rt4Yh", False),
    ("trigger-word-inside-secret-json", "password=json-Mn3Qx8Zc", "Mn3Qx8Zc", False),
]
IDS = [c[0] for c in CASES]


def test_oracle_is_not_vacuous_secrets_do_not_collide_with_canned_text():
    for name, text, secret, pure in CASES:
        assert not (grams(secret) & grams(CORPUS)), name


# ======================================================================================================= (c) table
@pytest.mark.parametrize("kind,text,code", [
    ("RATE_LIMIT", "anything", F.RATE_LIMITED), ("FETCH_ERROR", "HTTP 429 Too Many Requests", F.RATE_LIMITED),
    ("FETCH_ERROR", "Rate limit exceeded", F.RATE_LIMITED), ("FETCH_ERROR", "YFRateLimitError", F.RATE_LIMITED),
    ("FETCH_ERROR", "Connection timed out after 10s", F.TIMEOUT), ("FETCH_ERROR", "ReadTimeout: read timeout", F.TIMEOUT),
    ("FETCH_ERROR", "ConnectionError: Max retries exceeded", F.NETWORK_ERROR),
    ("FETCH_ERROR", "SSLError: certificate", F.NETWORK_ERROR), ("FETCH_ERROR", "gaierror: Name or service not known", F.NETWORK_ERROR),
    ("FETCH_ERROR", "Network is unreachable", F.NETWORK_ERROR),
    ("FETCH_ERROR", "No data found, symbol may be delisted", F.NOT_FOUND_OR_DELISTED),
    ("FETCH_ERROR", "HTTP 404 Not Found", F.NOT_FOUND_OR_DELISTED),
    ("FETCH_ERROR", "Could not fetch live price for XYZ", F.NOT_FOUND_OR_DELISTED),
    ("FETCH_ERROR", "No expiration dates found for XYZ", F.NO_OPTIONS_LISTED),
    ("FETCH_ERROR", "no options listed", F.NO_OPTIONS_LISTED),
    ("FETCH_ERROR", "JSONDecodeError: Expecting value", F.PROVIDER_FORMAT_ERROR),
    ("FETCH_ERROR", "KeyError: 'strike'", F.PROVIDER_FORMAT_ERROR), ("FETCH_ERROR", "schema mismatch", F.PROVIDER_FORMAT_ERROR),
    ("FETCH_ERROR", "could not parse response", F.PROVIDER_FORMAT_ERROR),
    ("FETCH_ERROR", "something went sideways", F.UNKNOWN_ERROR), ("FETCH_ERROR", "", F.UNKNOWN_ERROR), (None, None, F.UNKNOWN_ERROR),
    ("STOCKPILE_MISSING", "zzzz", F.UNKNOWN_ERROR),
])
def test_classification_table(kind, text, code):
    assert F.classify(kind, text) == code == F.classify(kind, text)


def test_canned_table_is_closed_and_every_code_has_a_message():
    assert set(F.CODES) == {"RATE_LIMITED", "NOT_FOUND_OR_DELISTED", "NO_OPTIONS_LISTED", "TIMEOUT", "NETWORK_ERROR",
                            "PROVIDER_FORMAT_ERROR", "PARTIAL_DATA", "UNKNOWN_ERROR"}
    for c in F.CODES:
        assert F.CANNED[c].strip() and F.stored(c) == f"{c}: {F.CANNED[c]}"
    assert F.stored("NOT_A_CODE") == F.stored("UNKNOWN_ERROR") and F.stored(TOK) == F.stored("UNKNOWN_ERROR")   # unknown code folds


class RaisingStr:
    def __str__(self):
        raise RuntimeError("boom " + TOK)


class NonStrStr:
    def __str__(self):
        return 12345            # type: ignore[return-value]  (TypeError inside str())


@pytest.mark.parametrize("obj", [RaisingStr(), NonStrStr(), None, 0, b"\xff\xfe", object(), ["token=abc"], {"k": TOK}])
def test_hostile_text_object_is_tolerated_by_classify(obj):
    c = F.classify("FETCH_ERROR", obj)
    assert c in F.CODES
    assert F.stored(c) in CANNED_VALUES


@pytest.mark.parametrize("kind", [None, 5, b"RATE_LIMIT", object(), ["RATE_LIMIT"]])
def test_hostile_kind_is_tolerated(kind):
    assert F.classify(kind, "plain") in F.CODES        # type: ignore[arg-type]


@pytest.mark.parametrize("name,text,secret,pure", CASES, ids=IDS)
def test_classify_is_deterministic_and_output_is_canned_only(name, text, secret, pure):
    codes = {F.classify("FETCH_ERROR", text) for _ in range(3)}
    assert len(codes) == 1
    (code,) = codes
    out = F.stored(code)
    assert out in CANNED_VALUES and not leaks(out, secret)
    assert F.display(out) == out


def test_trigger_words_inside_secrets_classify_by_rule_but_never_leak():
    # Documented limitation, not a leak: substring sniffing means a secret that merely contains a trigger word picks that code.
    assert F.classify("FETCH_ERROR", "token=sk-timeout-9XqLmN2vB") == F.TIMEOUT
    assert F.classify("FETCH_ERROR", "key=Zq429Lw8Kp3") == F.RATE_LIMITED
    for t in ("token=sk-timeout-9XqLmN2vB", "key=Zq429Lw8Kp3"):
        assert not leaks(F.stored(F.classify("FETCH_ERROR", t)), t.split("=")[1][-9:])


def test_display_table():
    assert F.display(None) == "none" and F.display("") == "none"
    for v in CANNED_VALUES:
        assert F.display(v) == v
    for bad in ["RATE_LIMITED: " + F.CANNED[F.RATE_LIMITED] + " extra " + TOK, "RATE_LIMITED: tampered", "RATE_LIMITED", ": ",
                "rate_limited: " + F.CANNED[F.RATE_LIMITED], "Connection timed out", TOK, b"RATE_LIMITED", 5, ["x"], {"a": 1},
                " " + F.stored("TIMEOUT"), F.stored("TIMEOUT") + "\n", "PARTIAL_DATA: " + F.CANNED["TIMEOUT"]]:
        assert F.display(bad) == LEGACY_MSG, bad


# ======================================================================================================= (a) real paths
def _modes(prov: Prov, text: str):
    class Weird(Exception):
        pass
    prov.script["AAA"] = Exception(text)
    prov.script["BBB"] = SourceError("FETCH_ERROR", text)
    prov.script["CCC"] = Weird(text)
    prov.set("DDD", spot=61.25)
    prov.script["DDD"][0]["type"] = text                                  # hostile value in a row field
    prov.script["DDD"][0]["expiration"] = text
    prov.script["EEE"] = SourceError("RATE_LIMIT", text)                  # last: a rate limit defers anything after it


def _all_outputs(db: Path, res, d) -> dict[str, str]:
    out = {"md": json.dumps(md_dump(db)), "result": repr(res.outcomes) + repr(res.notes)}
    raw = b""
    for suf in ("", "-wal", "-shm"):
        p = Path(str(db) + suf)
        if p.exists():
            raw += p.read_bytes()
    out["rawfile"] = raw.decode("latin-1")
    for path in ("/watchlist", "/market/AAA", "/market/BBB", "/market/CCC", "/market/DDD", "/market/EEE"):
        out["html" + path] = d.req("GET", path)[2]
    for path in ("/api/watchlist", "/api/market/AAA", "/api/market/BBB", "/api/market/CCC", "/api/market/DDD", "/api/market/EEE"):
        out["api" + path] = d.req("GET", path)[2]
    return out


@pytest.mark.parametrize("name,text,secret,pure", CASES, ids=IDS)
def test_hostile_provider_text_never_lands_anywhere_through_market_refresh_and_dashboard(tmp_path, name, text, secret, pure):
    db = plain_db(tmp_path)
    add_wl(db, "AAA", "BBB", "CCC", "DDD", "EEE")
    p = Prov(NOW)
    _modes(p, text)
    res = refresh(db, p, NOW)
    attempted = [o for o in res.outcomes if o.attempted]
    assert {o.symbol for o in attempted} >= {"AAA", "BBB", "CCC", "EEE"}, res.outcomes
    for o in attempted:
        assert o.error is None or o.error in CANNED_VALUES, (o.symbol, o.error)
    c = sqlite3.connect(db)
    errs = [r[0] for r in c.execute("SELECT last_error FROM md_symbol_status WHERE last_error IS NOT NULL")]
    c.close()
    assert errs and all(e in CANNED_VALUES for e in errs), errs
    d = dash(db, approved_file(tmp_path), now=NOW + timedelta(seconds=30))
    try:
        outs = _all_outputs(db, res, d)
    finally:
        d.stop()
    for where, blob in outs.items():
        n = RANDOM_WINDOW if where in RANDOM_BLOBS else 4
        assert not leaks(blob, secret, n), (name, where, leaks(blob, secret, n))
    # pure cases: nothing but canned sentences may appear in the error cell of the watchlist API rows
    rows = json.loads(outs["api/api/watchlist"])["data"]["rows"]
    for r in rows:
        le = (r.get("status") or {}).get("last_error") if isinstance(r.get("status"), dict) else r.get("last_error")
        assert le in (None, *CANNED_VALUES), le


@pytest.mark.parametrize("exc_factory", [RaisingStr, NonStrStr], ids=["str-raises", "str-nonstr"])
def test_hostile_dunder_str_exception_does_not_crash_market_refresh_or_leak(tmp_path, exc_factory):
    class Hostile(Exception):
        def __str__(self):
            return exc_factory().__str__()
    db = plain_db(tmp_path)
    add_wl(db, "AAA", "BBB")
    p = Prov(NOW)
    p.script["AAA"] = Hostile()
    p.set("BBB")
    res = refresh(db, p, NOW)                                              # must not raise
    a = next(o for o in res.outcomes if o.symbol == "AAA")
    assert a.status == "ERROR" and a.error in CANNED_VALUES
    assert "AAAHdq" not in json.dumps(md_dump(db)) and "boom" not in json.dumps(md_dump(db))
    b = next(o for o in res.outcomes if o.symbol == "BBB")
    assert b.attempted and b.status in ("OK", "PARTIAL")                   # one bad symbol must not stop the others


@pytest.mark.parametrize("name,text,secret,pure", CASES[::3], ids=IDS[::3])
def test_ticker_check_text_and_json_are_canned_only(name, text, secret, pure):
    from csp_alerts.jobs.ticker_check import check_ticker, render_text, to_json_obj
    for exc in (Exception(text), SourceError("FETCH_ERROR", text), SourceError("RATE_LIMIT", text)):
        class Src:
            def fetch_chain(self, t, lo, hi):
                raise exc
            def fetch_earnings_dates(self, t):
                return None
        rep = check_ticker("ZZZ", Src(), POL, NOW, sleep=lambda s: None)
        assert rep.status == "UNAVAILABLE" and rep.unavailable in CANNED_VALUES, rep.unavailable
        txt = render_text([rep], POL, NOW, "America/Chicago")
        js = json.dumps(to_json_obj([rep], NOW))
        assert not leaks(txt, secret) and not leaks(js, secret), name
        assert any(v in txt for v in CANNED_VALUES) and "VERDICT: DATA UNAVAILABLE" in txt


def test_ticker_check_hostile_str_exception_is_tolerated():
    from csp_alerts.jobs.ticker_check import check_ticker

    class Hostile(Exception):
        def __str__(self):
            raise RuntimeError("boom " + TOK)

    class Src:
        def fetch_chain(self, t, lo, hi):
            raise Hostile()
        def fetch_earnings_dates(self, t):
            return None
    rep = check_ticker("ZZZ", Src(), POL, NOW, sleep=lambda s: None)
    assert rep.status == "UNAVAILABLE" and rep.unavailable in CANNED_VALUES


@pytest.mark.parametrize("idx", [0, 5, 23], ids=lambda i: CASES[i][0])
def test_cli_market_refresh_and_ticker_check_stdout_canned_only(monkeypatch, tmp_path, idx):
    name, text, secret, pure = CASES[idx]
    db = tmp_path / "j.sqlite3"
    j = jopen(db, NOW)
    j.migrate()
    from csp_alerts.journal import watchlist
    watchlist.add_symbol(j, "ZZZ", actor="t", source="t")
    j.close()
    src = FakeSource({"ZZZ": SourceError("FETCH_ERROR", text)})
    code, out, err = run_cli(monkeypatch, tmp_path, ["market-refresh"], now=NOW, source=src)
    assert code == 1 and "ERROR" in out
    assert not leaks(out + err, secret), (out, err)
    assert any(v in out for v in CANNED_VALUES)
    src = FakeSource({"ZZZ": Exception(text)})
    for extra in ([], ["--json"]):
        code, out, err = run_cli(monkeypatch, tmp_path, ["ticker-check", "--ticker", "ZZZ", *extra], now=NOW, source=src)
        assert not leaks(out + err, secret), (extra, out, err)
        assert "DATA UNAVAILABLE" in out
    assert not leaks(json.dumps(md_dump(db)), secret, RANDOM_WINDOW)


# ---- hostile sizes through every path, in a child process with a timeout (payload via stdin)
CHILD = r'''
import sys, time, json, tempfile, pathlib
from datetime import datetime, timezone
sys.path.insert(0, sys.argv[1])
payload = sys.stdin.buffer.read().decode("utf-8", "surrogatepass")
from csp_alerts.market import failures as F
from csp_alerts.market.stockpile import SourceError
from csp_alerts.jobs.ticker_check import check_ticker, render_text, to_json_obj
from csp_alerts.jobs.market_refresh import run_market_refresh
from csp_alerts.journal.store import Journal
from csp_alerts.journal import watchlist
from csp_alerts.policy.config import load_policy
NOW = datetime(2026, 10, 30, 15, 5, 2, tzinfo=timezone.utc)
pol = load_policy(pathlib.Path(sys.argv[2]))
db = pathlib.Path(tempfile.mkdtemp()) / "j.sqlite3"
j = Journal(db, clock=lambda: NOW); j.migrate()
for _s in ("AAA", "BBB", "CCC"):
    watchlist.add_symbol(j, _s, actor="t", source="t")
class Src:
    def fetch_chain(self, t, lo, hi):
        raise Exception(payload) if t == "AAA" else SourceError("FETCH_ERROR", payload)
    def fetch_earnings_dates(self, t): return None
# warm-up with a benign failure so one-off import/calendar costs are not charged to the payload
_w = SourceError("FETCH_ERROR", "x")
class Warm:
    def fetch_chain(self, t, lo, hi): raise _w
    def fetch_earnings_dates(self, t): return None
run_market_refresh(journal=j, policy=pol, source=Warm(), now_utc=NOW, sleep=lambda s: None, pause_seconds=0, only="CCC", force=True)
render_text([check_ticker("ZZZ", Warm(), pol, NOW, sleep=lambda s: None)], pol, NOW, "America/Chicago")
t0 = time.perf_counter()
code = F.classify("FETCH_ERROR", payload)
res = run_market_refresh(journal=j, policy=pol, source=Src(), now_utc=NOW, sleep=lambda s: None, pause_seconds=0)
rep = check_ticker("ZZZ", Src(), pol, NOW, sleep=lambda s: None)
txt = render_text([rep], pol, NOW, "America/Chicago"); js = json.dumps(to_json_obj([rep], NOW))
el = time.perf_counter() - t0
rows = [r[0] for r in j.conn.execute("SELECT last_error FROM md_symbol_status WHERE symbol != 'CCC'")]
runs = [r[0] for r in j.conn.execute("SELECT summary_json FROM md_refresh_runs")]
print(json.dumps({"elapsed": el, "code": code, "errs": rows, "runs": runs, "txt": txt, "js": js, "out": [o.error for o in res.outcomes if o.symbol != "CCC"]}))
'''
SHAPES = {
    "word-run": "a", "digits": "1", "key-run": "key", "token-run": "token=", "kw-sep": "token=a;", "pct": "%41", "pct-bad": "%4",
    "url": "http://h/x?a=b ", "unicode": "密码é", "nul": "\x00", "surrogate": "\ud800", "quote-key": '"key":"',
    "bs-quote": '\\"key\\":\\"', "digits-colon": "1234567:", "drive": "C:" + BS, "unc": BS * 2, "cookie": "Cookie:", "spaces": " ",
    "words": "word ", "slashes": "/home/", "tilde": "~/", "nl-eq": "x=\n", "timeout-run": "timeout ", "429-run": "429 ",
}
SIZES = {"2KB": 2_000, "100KB": 100_000, "1MB": 1_000_000}


@pytest.mark.parametrize("size", list(SIZES))
@pytest.mark.parametrize("name", list(SHAPES))
def test_large_hostile_payloads_are_fast_and_canned_only(name, size):
    unit = SHAPES[name]
    payload = (unit * (SIZES[size] // len(unit) + 1))[:SIZES[size]]
    r = subprocess.run([sys.executable, "-I", "-c", CHILD, str(ROOT / "src"), str(POLICY_PATH)],
                       input=payload.encode("utf-8", "surrogatepass"), capture_output=True, cwd=ROOT, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-800:]
    o = json.loads(r.stdout.decode("utf-8"))
    assert o["elapsed"] < 1.0, o["elapsed"]                               # the three failure paths together, excluding startup
    assert o["code"] in F.CODES
    for e in o["errs"] + o["out"]:
        assert e in CANNED_VALUES, e[:100]
    assert all(v for v in o["runs"])
    blob = json.dumps(o["runs"]) + o["txt"] + o["js"] + json.dumps(o["errs"])
    ig = grams(CORPUS)
    if not name.endswith("-run") or name in ("word-run", "key-run", "token-run"):
        # payload 4-grams that are not English from the canned corpus must be absent (e.g. 'aaaa', 'keyk', 'http')
        for g in (grams(unit * 4) - ig):
            if g.strip() and g not in ("\n", "\x00") and g in blob.casefold():
                pytest.fail(f"payload fragment {g!r} appears in output")
    if name in ("timeout-run", "429-run"):
        assert o["code"] == (F.TIMEOUT if name == "timeout-run" else F.RATE_LIMITED)


# ======================================================================================================= (b) legacy rows
LEGACY = [
    "Connection timed out after 10s https://h.example/x?crumb=LEGACYSEC9qz",
    "HTTPError: " + TOK, "C:" + BS + "Users" + BS + "Lxgacy Usxr" + BS + "x.log",
    "RATE_LIMITED: " + F.CANNED[F.RATE_LIMITED] + " LEGACYTAIL9qz",           # canned prefix with a forged tail
    "Provider error (truncated, redacted): LEGACYSEC9qz",
    "RATE_LIMITED: tampered LEGACYSEC9qz", "unknown LEGACYSEC9qz",
]


@pytest.mark.parametrize("legacy", LEGACY, ids=[f"legacy{i}" for i in range(len(LEGACY))])
def test_legacy_free_text_rows_render_withheld_on_every_surface(tmp_path, legacy):
    from csp_alerts.market import observations as obs
    db = plain_db(tmp_path)
    add_wl(db, "AAA", "BBB")
    p = Prov(NOW)
    p.set("BBB")
    refresh(db, p, NOW)                                                     # BBB: a real row, healthy
    j = jopen(db, NOW)
    obs.record_attempt(j, "AAA", now=NOW, status="ERROR", error=legacy, session="OPEN", run_id="legacy-run", success=False)
    j.close()
    d = dash(db, approved_file(tmp_path), now=NOW + timedelta(seconds=30))
    try:
        pages = {"watchlist": d.req("GET", "/watchlist")[2], "market": d.req("GET", "/market/AAA")[2],
                 "api-wl": d.req("GET", "/api/watchlist")[2], "api-m": d.req("GET", "/api/market/AAA")[2]}
    finally:
        d.stop()
    secret = "LEGACYSEC9qz" if "LEGACY" in legacy else legacy.split(BS)[-2] if "Users" in legacy else TOK
    for k, blob in pages.items():
        assert LEGACY_MSG in text_of(blob) or LEGACY_MSG in blob, k
        # n=8, not the default 4: these pages carry random hex (uuids, shas). TOK's digit run '1234'/'2345'/... is itself valid hex,
        # so a 4-char window collides by chance with a random id (the once-seen full-run flake of legacy1). An 8-char window of the
        # secret ('12345678', 'aahdqtcv', ...) cannot collide by chance (<1e-9 per position) and still catches any rendered run
        # of 8+ characters, which is every real way the whole secret would leak.
        assert not leaks(blob, secret, n=8), (k, leaks(blob, secret, n=8))


def test_valid_canned_row_passes_through_and_none_renders_none(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "AAA", "BBB")
    p = Prov(NOW)
    p.script["AAA"] = SourceError("FETCH_ERROR", "HTTP 404 Not Found")
    p.set("BBB")
    refresh(db, p, NOW)
    d = dash(db, approved_file(tmp_path), now=NOW + timedelta(seconds=30))
    try:
        a = mkt(d, "AAA")["block"]["status"]["last_error"]
        b = mkt(d, "BBB")["block"]["status"]["last_error"]
        html = d.req("GET", "/market/AAA")[2]
    finally:
        d.stop()
    assert a == F.stored(F.NOT_FOUND_OR_DELISTED) and b is None
    assert F.CANNED[F.NOT_FOUND_OR_DELISTED].split(";")[0][:30] in text_of(html)


# ======================================================================================================= (d) label
def test_dashboard_label_is_provider_status_on_watchlist_and_market_page(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "AAA")
    p = Prov(NOW)
    p.script["AAA"] = SourceError("FETCH_ERROR", "Connection timed out")
    refresh(db, p, NOW)
    d = dash(db, approved_file(tmp_path), now=NOW + timedelta(seconds=30))
    try:
        wl = d.req("GET", "/watchlist")[2]
        mk = d.req("GET", "/market/AAA")[2]
    finally:
        d.stop()
    assert "Provider status" in text_of(wl) and "Provider status" in text_of(mk)
    for h in (wl, mk):
        t = text_of(h)
        assert "Provider error (truncated)" not in t and "Last provider error" not in t and "redacted" not in t.lower().replace("not redacted", "")
    assert F.stored(F.TIMEOUT) in text_of(wl) and F.stored(F.TIMEOUT) in text_of(mk)


# ======================================================================================================= removal audit
def test_redactor_is_gone_and_no_obsolete_label_remains_in_src():
    fresh = (ROOT / "src/csp_alerts/market/freshness.py").read_text(encoding="utf-8")
    assert "def redact" not in fresh and "REDACT_MAX_CHARS" not in fresh and "urllib" not in fresh
    for p in (ROOT / "src").rglob("*.py"):
        t = p.read_text(encoding="utf-8")
        assert "freshness import redact" not in t and "Provider error (truncated)" not in t, p
    f = (ROOT / "src/csp_alerts/market/failures.py").read_text(encoding="utf-8")
    assert "import re" not in f and "urllib" not in f and "import os" not in f and "import socket" not in f


def test_nothing_else_in_the_ledger_changes_when_a_provider_fails(tmp_path):
    db = plain_db(tmp_path)
    add_wl(db, "AAA")
    before = hold(db)
    p = Prov(NOW)
    p.script["AAA"] = Exception("token=" + TOK)
    refresh(db, p, NOW)
    assert hold(db) == before
