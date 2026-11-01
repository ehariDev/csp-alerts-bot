"""Persisted market-data observation store (Stage A): stock price and option quotes as SEPARATE observations.

Used by the `market-refresh` job, by the monitor (it persists the quotes it fetches) and, read-only, by the dashboard.
Nothing here fetches data, decides eligibility, writes ledger records, snapshots or outbox rows, or sends anything.

Rules (each covered by tests/unit/test_market_data.py):
  * Unknown stays unknown: a missing provider market timestamp is stored as NULL, never replaced by the retrieval
    time. Retrieval time is stored separately and never proves exchange freshness.
  * Old evidence is never made current: a failed fetch writes NO observation (the prior rows keep their ORIGINAL
    retrieved_at); a re-fetch that returns an unusable quote (no two-sided bid/ask) for a contract that had a usable
    one leaves the prior row untouched ("never overwrite good data with nulls") and is counted as `retained`.
  * Stock price is only ever the provider's underlying price; it is never copied into an option bid/ask or vice versa.
  * Bounded: stock history keeps STOCK_HISTORY_KEEP rows per symbol; option history keeps OPTION_HISTORY_KEEP rows per
    contract and gains a row only when bid/ask/volume/open interest/delta changed; contracts expired more than 7 days
    are pruned; at most MAX_EXPIRIES_PER_SYMBOL nearest expiries (plus any held expiry) are stored per symbol.
  * Option identity: strike (canonical plain decimal string), expiration, right, full OCC symbol. The OCC symbol comes
    from the provider row when present, otherwise it is DERIVED (root + YYMMDD + P + strike*1000) and labelled so;
    a derived symbol is not evidence of a standard deliverable.
  * Latest-attempt failures are remembered (DECISIONS D-RV01/RV02): when the NEWEST fetch could not supply a valid stock
    price (missing or conflicting underlying across rows) or a valid two-sided quote for a contract that has a retained prior
    quote, the failure is recorded (per stock / per exact contract, with the attempt time and a canned reason) in
    md_symbol_status.detail_json["latest_evidence"]. The retained value keeps its ORIGINAL timestamp; readers treat it as
    retained/unusable for current gates and numeric advice until a valid observation clears the mark. No schema change.
Formulas: OCC strike field = strike * 1000 as an 8-digit integer; quote is "usable" when bid > 0, ask > 0, ask >= bid.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from csp_alerts.journal.models import from_iso, to_iso
from csp_alerts.journal.reconcile import exchange_date
from csp_alerts.journal.store import Journal
from csp_alerts.market.freshness import (
    MAX_EXPIRIES_PER_SYMBOL,
    OPTION_HISTORY_KEEP,
    REQUEST_EXPIRES_SECONDS,
    REQUEST_MIN_SPACING_SECONDS,
    REQUESTS_PER_HOUR,
    STOCK_HISTORY_KEEP,
    backoff_seconds,
)
from csp_alerts.market.normalize import PROVIDER, NormalizedChain, NormalizedRow, raw_sha256, standard_symbol, to_decimal
from csp_alerts.market.models import TIER_R

PRICE_TYPE = "LAST"          # the Stockpile/Yahoo path returns fast_info lastPrice / regularMarketPrice (unverified which)
PRICE_TYPE_NOTE = ("price type as labelled for this provider path (Yahoo lastPrice/regularMarketPrice); the provider "
                   "does not say whether it is an intraday last trade or the previous close")
EXPIRED_KEEP_DAYS = 7
RUN_HISTORY_KEEP = 500


def canon_strike(d: Decimal) -> str:
    """Canonical plain decimal string with no exponent or trailing zeros ('95', '37.5')."""
    return format(d.normalize(), "f")


def occ_symbol(ticker: str, expiration: date, strike: Decimal) -> str:
    root = re.sub(r"[.\-/]", "", ticker.upper())
    return f"{root}{expiration:%y%m%d}P{int(strike * 1000):08d}"


def usable_quote(bid: Decimal | None, ask: Decimal | None) -> bool:
    return bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid


def _d(v: Decimal | None, places: int = 4) -> str | None:
    """Plain decimal string rounded half-even to `places` (Yahoo values arrive as binary floats such as 60.13999938964844;
    that digit noise is not information and is not displayed). Prices/quotes: 4 places; delta and IV: 6."""
    if v is None or not v.is_finite():
        return None
    q = v.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)
    return format(q.normalize(), "f")


def _iso_opt(v: Any) -> str | None:
    if v is None or v == "":
        return None
    try:
        return to_iso(v) if isinstance(v, datetime) else to_iso(from_iso(str(v)))
    except (ValueError, TypeError):
        return None


@dataclass
class StoreResult:
    symbol: str
    stock_written: bool = False
    stock_price: Decimal | None = None
    stock_note: str = ""
    options_written: int = 0
    options_retained: int = 0
    options_dropped_by_cap: int = 0
    options_older_ignored: int = 0
    notes: list[str] = field(default_factory=list)


LATEST_KEY = "latest_evidence"
ABSENT_REASON = "contract absent from the newest successful chain; the prior quote is retained but not current"
QUOTE_FAILURE_REASON = "newest quote attempt had no valid two-sided bid/ask; the prior quote is retained but not current"


def contract_key(expiration: str, strike: str) -> str:
    return f"{expiration}|{strike}"


def parse_at(v: Any) -> datetime:
    """Parse a stored evidence time (sub-second precision is kept in latest_evidence marks)."""
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc)


def mark_active(f: dict[str, Any] | None, row_retrieved_iso: str | None, row_run_id: str | None) -> bool:
    """A failure/absence mark qualifies a retained observation while (a) it was made AGAINST that exact retained row
    (`row_run` equals the row's run id: this removes the one-second tie, because stored row times are truncated to seconds) or
    (b) its precise attempt time is later than the row's retrieval time. Valid newer data replaces the row and clears the mark."""
    if not f:
        return False
    try:
        if f.get("row_run") and row_run_id and f["row_run"] == row_run_id:
            return True
        return row_retrieved_iso is not None and parse_at(f["at"]) > from_iso(row_retrieved_iso)
    except Exception:  # noqa: BLE001
        return False


def _ev_load(c, symbol: str) -> dict[str, Any]:
    r = c.execute("SELECT detail_json FROM md_symbol_status WHERE symbol=?", (symbol,)).fetchone()
    if r is None:
        return {}
    try:
        ev = (json.loads(r["detail_json"] or "{}") or {}).get(LATEST_KEY) or {}
    except (ValueError, TypeError):
        return {}
    return ev if isinstance(ev, dict) else {}


def _ev_save(c, symbol: str, ev: dict[str, Any], before: dict[str, Any], now_iso: str) -> None:
    """Merge `ev` into the symbol's detail_json (creating a minimal status row only when a failure must be remembered)."""
    if ev == before:
        return
    r = c.execute("SELECT detail_json FROM md_symbol_status WHERE symbol=?", (symbol,)).fetchone()
    if r is None:
        if not ev:
            return
        c.execute("INSERT INTO md_symbol_status VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (symbol, "PARTIAL", None, None, None, 0, None, None, "", json.dumps({LATEST_KEY: ev}, sort_keys=True)))
        return
    try:
        d = json.loads(r["detail_json"] or "{}") or {}
    except (ValueError, TypeError):
        d = {}
    if ev:
        d[LATEST_KEY] = ev
    else:
        d.pop(LATEST_KEY, None)
    c.execute("UPDATE md_symbol_status SET detail_json=? WHERE symbol=?", (json.dumps(d, sort_keys=True, default=str), symbol))


RUN_AT_KEY = "run_at"


def _runat_load(c, symbol: str) -> dict[str, str]:
    r = c.execute("SELECT detail_json FROM md_symbol_status WHERE symbol=?", (symbol,)).fetchone()
    try:
        v = (json.loads(r["detail_json"] or "{}") or {}).get(RUN_AT_KEY) if r else None
    except (ValueError, TypeError):
        return {}
    return dict(v) if isinstance(v, dict) else {}


def _runat_save(c, symbol: str, run_at: dict[str, str]) -> None:
    r = c.execute("SELECT detail_json FROM md_symbol_status WHERE symbol=?", (symbol,)).fetchone()
    if r is None:                    # minimal row (like _ev_save); the status writers that follow keep this detail
        c.execute("INSERT INTO md_symbol_status VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (symbol, "PARTIAL", None, None, None, 0, None, None, "", json.dumps({RUN_AT_KEY: run_at}, sort_keys=True)))
        return
    try:
        d = json.loads(r["detail_json"] or "{}") or {}
    except (ValueError, TypeError):
        d = {}
    d[RUN_AT_KEY] = run_at
    c.execute("UPDATE md_symbol_status SET detail_json=? WHERE symbol=?", (json.dumps(d, sort_keys=True, default=str), symbol))


def row_precise_time(c, symbol: str, row_retrieved_iso: str, run_id: str | None) -> datetime:
    """Precise retrieval time of a stored row: the run's recorded sub-second time when known, else the whole-second time."""
    try:
        if run_id:
            v = _runat_load(c, symbol).get(run_id)
            if v:
                return parse_at(v)
    except Exception:  # noqa: BLE001
        pass
    return from_iso(row_retrieved_iso)


def latest_evidence(c, symbol: str) -> dict[str, Any]:
    """{"stock": {"at","reason"}?, "contracts": {"<expiration>|<strike>": {"at","reason"}}} (read-only; {} when none)."""
    try:
        return _ev_load(c, symbol)
    except Exception:  # noqa: BLE001 - unreadable bookkeeping is "no extra information", the dashboard still fails closed on data
        return {}


def stock_price_from(rows: list[NormalizedRow]) -> tuple[Decimal | None, str, str | None]:
    """(price, note, provider_market_time_iso). One consistent positive `spot` across rows, else unavailable."""
    spots = {r.observation.spot for r in rows if r.observation.spot is not None and r.observation.spot > 0}
    if not spots:
        return None, "provider rows carried no valid underlying price", None
    if len(spots) > 1:
        return None, "provider rows disagree on the underlying price; none stored", None
    times = {_iso_opt(r.raw.get("spot_asof_utc")) for r in rows}
    times.discard(None)
    return Decimal(_d(next(iter(spots)))), "", (next(iter(times)) if len(times) == 1 else None)


def store_chain(j: Journal, *, symbol: str, run_id: str, source: str, retrieved_at: datetime, norm: NormalizedChain,
                keep_expiries: frozenset[date] = frozenset(), max_expiries: int = MAX_EXPIRIES_PER_SYMBOL) -> StoreResult:
    """Persist one successful fetch for `symbol` in ONE transaction (market-data tables only)."""
    res = StoreResult(symbol)
    today = exchange_date(retrieved_at)
    rows = [r for r in norm.rows if r.observation.expiration is not None and r.observation.strike is not None
            and r.observation.expiration >= today]
    expiries = sorted({r.observation.expiration for r in rows})
    allowed = set(expiries[:max_expiries]) | (set(expiries) & set(keep_expiries))
    res.options_dropped_by_cap = sum(1 for r in rows if r.observation.expiration not in allowed)
    rows = [r for r in rows if r.observation.expiration in allowed]
    price, note, mtime = stock_price_from(norm.rows)
    res.stock_note = note
    rt = to_iso(retrieved_at)
    at_precise = retrieved_at.astimezone(timezone.utc).isoformat()      # marks keep sub-second order
    with j.tx():
        c = j.conn
        ev_before = _ev_load(c, symbol)
        ev: dict[str, Any] = json.loads(json.dumps(ev_before))
        ev_contracts: dict[str, Any] = ev.setdefault("contracts", {})
        # Ordering (RV-20261031-04): evidence older than what is already stored (a delayed writer) is never applied, never
        # regresses a time and never clears or sets a newer failure mark.
        inc = retrieved_at.astimezone(timezone.utc)
        run_at: dict[str, str] = _runat_load(c, symbol)     # run id -> precise retrieval time (stored times are whole seconds)

        def row_time(iso: str, rid: str | None) -> datetime:
            try:
                return parse_at(run_at[rid]) if rid in run_at else from_iso(iso)
            except Exception:  # noqa: BLE001
                return from_iso(iso)
        srow = c.execute("SELECT retrieved_at_utc, run_id FROM md_stock_latest WHERE symbol=?", (symbol,)).fetchone()
        smark = ev.get("stock")
        stock_older = bool((srow is not None and row_time(srow["retrieved_at_utc"], srow["run_id"]) > inc)
                           or (smark and parse_at(smark["at"]) > inc))
        if stock_older:
            res.notes.append("stock evidence older than the stored observation or failure: not applied")
        elif price is None:
            ev["stock"] = {"at": at_precise, "reason": note, "row_run": srow["run_id"] if srow else None}   # prior row keeps ORIGINAL time
        else:
            ev.pop("stock", None)
        if price is not None and not stock_older:
            c.execute("INSERT OR REPLACE INTO md_stock_latest VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (symbol, _d(price), PRICE_TYPE, PROVIDER, TIER_R, mtime, rt, source, run_id, None))
            c.execute("INSERT INTO md_stock_history(symbol, price, price_type, provider, tier, market_time_utc, retrieved_at_utc, "
                      "source, run_id) VALUES (?,?,?,?,?,?,?,?,?)",
                      (symbol, _d(price), PRICE_TYPE, PROVIDER, TIER_R, mtime, rt, source, run_id))
            c.execute("DELETE FROM md_stock_history WHERE symbol=? AND obs_id NOT IN "
                      "(SELECT obs_id FROM md_stock_history WHERE symbol=? ORDER BY obs_id DESC LIMIT ?)",
                      (symbol, symbol, STOCK_HISTORY_KEEP))
            res.stock_written, res.stock_price = True, price
        for r in rows:
            o = r.observation
            strike = canon_strike(o.strike)
            exp = o.expiration.isoformat()
            prior = c.execute("SELECT bid, ask, volume, open_interest, delta, retrieved_at_utc, run_id FROM md_option_latest "
                              "WHERE symbol=? AND expiration=? AND option_right='P' AND strike=?", (symbol, exp, strike)).fetchone()
            cmark = ev_contracts.get(contract_key(exp, strike))
            if (prior is not None and row_time(prior["retrieved_at_utc"], prior["run_id"]) > inc) or (
                    cmark and parse_at(cmark["at"]) > inc):
                res.options_older_ignored += 1       # delayed older writer: neither replaces the row nor touches the mark
                continue
            if not usable_quote(o.bid, o.ask) and prior is not None:
                pb, pa = to_decimal(prior["bid"]), to_decimal(prior["ask"])
                if usable_quote(pb, pa):
                    res.options_retained += 1      # keep the prior usable quote with its ORIGINAL timestamp
                    ev_contracts[contract_key(exp, strike)] = {"at": at_precise, "reason": QUOTE_FAILURE_REASON,
                                                               "row_run": prior["run_id"]}
                    continue
            provided = r.raw.get("contractSymbol") or r.raw.get("contract_symbol")
            if provided and str(provided).strip():
                occ, occ_src = str(provided).strip().upper()[:32], "PROVIDER"
            else:
                occ, occ_src = occ_symbol(symbol, o.expiration, o.strike), "DERIVED"
            note_id = ("provider contract symbol conflicts with the row's expiry/strike/root"
                       if occ_src == "PROVIDER" and standard_symbol(occ, symbol, o.expiration, o.strike) is False else None)
            last = to_decimal(r.raw.get("last"))
            vals = (symbol, exp, "P", strike, occ, occ_src, _d(o.bid), _d(o.ask), _d(last) if last and last > 0 else None,
                    o.daily_volume, o.open_interest, _d(o.delta, 6), _d(o.implied_volatility, 6), _d(o.spot),
                    _iso_opt(o.last_option_trade_at_utc), _iso_opt(r.raw.get("quote_asof_utc")), PROVIDER, TIER_R, rt, source,
                    run_id, r.raw_sha256, note_id)
            c.execute("INSERT OR REPLACE INTO md_option_latest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
            ev_contracts.pop(contract_key(exp, strike), None)     # the row now reflects the newest attempt
            res.options_written += 1
            changed = prior is None or (prior["bid"], prior["ask"], prior["volume"], prior["open_interest"], prior["delta"]) != (
                vals[6], vals[7], vals[9], vals[10], vals[11])
            if changed:
                c.execute("INSERT INTO md_option_history(symbol, expiration, option_right, strike, occ_symbol, bid, ask, last, volume, "
                          "open_interest, delta, provider, retrieved_at_utc, source, run_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (symbol, exp, "P", strike, occ, vals[6], vals[7], vals[8], vals[9], vals[10], vals[11], PROVIDER, rt, source, run_id))
                c.execute("DELETE FROM md_option_history WHERE symbol=? AND expiration=? AND option_right='P' AND strike=? AND obs_id NOT IN "
                          "(SELECT obs_id FROM md_option_history WHERE symbol=? AND expiration=? AND option_right='P' AND strike=? "
                          "ORDER BY obs_id DESC LIMIT ?)", (symbol, exp, strike, symbol, exp, strike, OPTION_HISTORY_KEEP))
        # RV-ACC-05: for every expiry this successful fetch DID return, a stored strike missing from it is absent (not current).
        seen = {(r.observation.expiration.isoformat(), canon_strike(r.observation.strike)) for r in rows}
        for exp_s in {e for e, _ in seen}:
            for st_s, prt, prun in c.execute("SELECT strike, retrieved_at_utc, run_id FROM md_option_latest "
                                             "WHERE symbol=? AND expiration=? AND option_right='P'", (symbol, exp_s)).fetchall():
                m = ev_contracts.get(contract_key(exp_s, st_s))
                if (exp_s, st_s) not in seen and row_time(prt, prun) <= inc and not (m and parse_at(m["at"]) > inc):
                    ev_contracts[contract_key(exp_s, st_s)] = {"at": at_precise, "reason": ABSENT_REASON, "row_run": prun}
        cutoff = (today - timedelta(days=EXPIRED_KEEP_DAYS)).isoformat()
        c.execute("DELETE FROM md_option_latest WHERE symbol=? AND expiration < ?", (symbol, cutoff))
        c.execute("DELETE FROM md_option_history WHERE symbol=? AND expiration < ?", (symbol, cutoff))
        for k in [k for k in ev_contracts if k.split("|", 1)[0] < cutoff]:
            del ev_contracts[k]
        if not ev_contracts:
            ev.pop("contracts", None)
        if res.stock_written or res.options_written:
            run_at[run_id] = at_precise
            if len(run_at) > 50:             # evict the OLDEST precise times (json keys are stored sorted, so order is not age)
                def _age(kv):
                    try:
                        return parse_at(kv[1])
                    except Exception:  # noqa: BLE001
                        return datetime.min.replace(tzinfo=timezone.utc)
                for k, _ in sorted(run_at.items(), key=_age)[:len(run_at) - 50]:
                    del run_at[k]
        fm = ev.get("fetch")
        if fm and inc >= parse_at(fm["at"]) and (res.stock_written or res.options_written):
            del ev["fetch"]                      # newer valid evidence supersedes a recorded whole-fetch failure
        _ev_save(c, symbol, ev, ev_before, rt)
        _runat_save(c, symbol, run_at)
    return res


# ---------------------------------------------------------------- per-symbol status
def get_status(c, symbol: str) -> dict[str, Any] | None:
    r = c.execute("SELECT * FROM md_symbol_status WHERE symbol=?", (symbol,)).fetchone()
    return None if r is None else dict(r)


def record_attempt(j: Journal, symbol: str, *, now: datetime, status: str, error: str | None, session: str, run_id: str,
                   success: bool, detail: dict[str, Any] | None = None) -> None:
    """Upsert the per-symbol status after a fetch attempt. A failure never clears last_success; a success clears the
    failure counter and any backoff."""
    with j.tx():
        prev = get_status(j.conn, symbol)
        fails = 0 if success else (prev["consecutive_failures"] if prev else 0) + 1
        last_success = to_iso(now) if success else (prev["last_success_at_utc"] if prev else None)
        nxt = None if success else to_iso(now + timedelta(seconds=backoff_seconds(status, fails)))
        detail = dict(detail or {})
        kept = _ev_load(j.conn, symbol)          # latest-attempt failures survive the status upsert (cleared only by valid data)
        if kept:
            detail[LATEST_KEY] = kept
        ra = _runat_load(j.conn, symbol)
        if ra:
            detail[RUN_AT_KEY] = ra
        j.conn.execute("INSERT OR REPLACE INTO md_symbol_status VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (symbol, status, to_iso(now), last_success, None if success else error, fails, nxt, session, run_id,
                        json.dumps(detail, sort_keys=True, default=str)))


def touch_success(j: Journal, symbol: str, now: datetime, run_id: str) -> None:
    """The monitor fetched this symbol successfully: record the success without pretending a refresh attempt happened.
    Never moves last_success backwards (a delayed older writer), and never reports OK / clears failure and backoff when a
    later refresh attempt is the newest evidence (RV-ACC-06)."""
    with j.tx():
        prev = get_status(j.conn, symbol)
        new_iso = to_iso(now)
        if prev is None:
            j.conn.execute("INSERT OR REPLACE INTO md_symbol_status VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (symbol, "OK", None, new_iso, None, 0, None, None, run_id, "{}"))
            return
        old = prev["last_success_at_utc"]
        if old and old >= new_iso:
            return                                         # older (or same-second) success: nothing to record
        att = prev["last_attempt_at_utc"]
        if att and att > new_iso:                          # a newer attempt exists: keep its status, error and backoff
            j.conn.execute("UPDATE md_symbol_status SET last_success_at_utc=? WHERE symbol=?", (new_iso, symbol))
            return
        j.conn.execute("INSERT OR REPLACE INTO md_symbol_status VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (symbol, "OK", att, new_iso, None, 0, None, prev["session_at_attempt"], run_id, prev["detail_json"]))


def record_monitor_fetch_failure(j: Journal, symbol: str, now: datetime, reason: str) -> None:
    """The monitor's own fetch failed (RV-ACC-07). Recorded as a whole-fetch mark in latest_evidence so position views stop
    treating the retained quote as current. It deliberately does NOT touch last_attempt / failure counter / backoff: those
    drive the refresh job's cadence, and a monitor error must neither delay nor accelerate it. No send path."""
    with j.tx():
        before = _ev_load(j.conn, symbol)
        ev = json.loads(json.dumps(before))
        at = now.astimezone(timezone.utc)
        old = ev.get("fetch")
        if old and parse_at(old["at"]) > at:
            return
        ev["fetch"] = {"at": at.isoformat(), "reason": reason, "source": "MONITOR"}
        _ev_save(j.conn, symbol, ev, before, to_iso(now))


# ---------------------------------------------------------------- run records and owner requests
def record_run(j: Journal, run_id: str, started: datetime, finished: datetime, trigger: str, session: str, status: str,
               symbols: list[str], summary: dict[str, Any]) -> None:
    with j.tx():
        j.conn.execute("INSERT OR REPLACE INTO md_refresh_runs VALUES (?,?,?,?,?,?,?,?)",
                       (run_id, to_iso(started), to_iso(finished), trigger, session, status, json.dumps(symbols),
                        json.dumps(summary, sort_keys=True, default=str)))
        j.conn.execute("DELETE FROM md_refresh_runs WHERE run_id NOT IN (SELECT run_id FROM md_refresh_runs "
                       "ORDER BY started_at_utc DESC, rowid DESC LIMIT ?)", (RUN_HISTORY_KEEP,))


def request_refresh(j: Journal, scope: str, requested_by: str, tracked_symbols: set[str]) -> tuple[str, str | None]:
    """Owner 'refresh now'. Writes ONE request row; the next market-refresh run picks it up. No fetch happens here.
    Returns (code, request_id): queued | pending | rate | unknown_symbol."""
    now = j.clock()
    if scope != "ALL" and scope not in tracked_symbols:
        return "unknown_symbol", None
    with j.tx():
        c = j.conn
        c.execute("UPDATE md_refresh_requests SET status='EXPIRED' WHERE status='PENDING' AND requested_at_utc < ?",
                  (to_iso(now - timedelta(seconds=REQUEST_EXPIRES_SECONDS)),))
        pend = [r[0] for r in c.execute("SELECT scope FROM md_refresh_requests WHERE status='PENDING'")]
        if "ALL" in pend or scope in pend:
            return "pending", None
        last = c.execute("SELECT MAX(requested_at_utc) FROM md_refresh_requests").fetchone()[0]
        if last and (now - from_iso(last)).total_seconds() < REQUEST_MIN_SPACING_SECONDS:
            return "rate", None
        n = c.execute("SELECT COUNT(*) FROM md_refresh_requests WHERE requested_at_utc >= ?",
                      (to_iso(now - timedelta(hours=1)),)).fetchone()[0]
        if n >= REQUESTS_PER_HOUR:
            return "rate", None
        rid = str(uuid.uuid4())
        c.execute("INSERT INTO md_refresh_requests(request_id, requested_at_utc, requested_by, scope, status) VALUES (?,?,?,?,?)",
                  (rid, to_iso(now), requested_by, scope, "PENDING"))
        j._audit("MARKET_REFRESH_REQUESTED", requested_by, None, {"request_id": rid, "scope": scope}, "web")
        return "queued", rid


def claim_requests(j: Journal, run_id: str, now: datetime) -> list[str]:
    """Job side: expire stale requests, mark the pending ones picked up by `run_id`, return their scopes."""
    with j.tx():
        c = j.conn
        c.execute("UPDATE md_refresh_requests SET status='EXPIRED', note='not picked up in time' WHERE status='PENDING' AND "
                  "requested_at_utc < ?", (to_iso(now - timedelta(seconds=REQUEST_EXPIRES_SECONDS)),))
        rows = c.execute("SELECT request_id, scope FROM md_refresh_requests WHERE status='PENDING' ORDER BY requested_at_utc").fetchall()
        for r in rows:
            c.execute("UPDATE md_refresh_requests SET status='DONE', picked_up_at_utc=?, run_id=? WHERE request_id=?",
                      (to_iso(now), run_id, r["request_id"]))
        return [r["scope"] for r in rows]


def recent_requests(c, limit: int = 5) -> list[dict[str, Any]]:
    return [dict(r) for r in c.execute("SELECT * FROM md_refresh_requests ORDER BY requested_at_utc DESC, rowid DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- read helpers (SELECT only; used by the dashboard)
def latest_stock(c, symbol: str) -> dict[str, Any] | None:
    r = c.execute("SELECT * FROM md_stock_latest WHERE symbol=?", (symbol,)).fetchone()
    return None if r is None else dict(r)


def _annotate(rows: list[dict[str, Any]], ev: dict[str, Any]) -> list[dict[str, Any]]:
    """Add `retained_unusable` (canned reason) to a row whose contract has a NEWER failed/absent attempt. Values and the
    original retrieved_at_utc are untouched. Readers that build observations (policy.browse) honour the key."""
    marks = ev.get("contracts") or {}
    for r in rows:
        f = marks.get(contract_key(r["expiration"], r["strike"]))
        if mark_active(f, r["retrieved_at_utc"], r.get("run_id")):
            r["retained_unusable"] = str(f.get("reason") or QUOTE_FAILURE_REASON)[:200]
            r["latest_failure_at_utc"] = parse_at(f["at"]).isoformat()
    return rows


def latest_options(c, symbol: str) -> list[dict[str, Any]]:
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM md_option_latest WHERE symbol=? ORDER BY expiration, CAST(strike AS NUMERIC)", (symbol,))]
    return _annotate(rows, latest_evidence(c, symbol)) if rows else rows


def option_for_contract(c, symbol: str, expiration: date, strike: Decimal) -> dict[str, Any] | None:
    r = c.execute("SELECT * FROM md_option_latest WHERE symbol=? AND expiration=? AND option_right='P' AND strike=?",
                  (symbol, expiration.isoformat(), canon_strike(strike))).fetchone()
    return None if r is None else _annotate([dict(r)], latest_evidence(c, symbol))[0]


def last_runs(c, limit: int = 3) -> list[dict[str, Any]]:
    return [dict(r) for r in c.execute("SELECT * FROM md_refresh_runs ORDER BY started_at_utc DESC, rowid DESC LIMIT ?", (limit,))]


def has_tables(c) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='md_option_latest'").fetchone() is not None


__all__ = ["StoreResult", "store_chain", "record_attempt", "touch_success", "record_run", "request_refresh", "claim_requests",
           "recent_requests", "latest_stock", "latest_options", "option_for_contract", "last_runs", "get_status", "has_tables",
           "record_monitor_fetch_failure", "row_precise_time", "latest_evidence", "mark_active", "parse_at", "ABSENT_REASON", "contract_key", "LATEST_KEY", "canon_strike", "occ_symbol", "usable_quote", "raw_sha256", "PRICE_TYPE", "PRICE_TYPE_NOTE"]
