"""Find instruments with listed options on the OpenAlgo platform and work out their settings for instruments.py.

    search("HDFC")          -> underlyings whose name matches and that have CE/PE contracts on NFO
    describe("HDFCBANK")    -> a ready instrument entry (exchange, strike step, expiry cycle, bars per day, lot size)

Everything comes from OpenAlgo itself: `search` on NFO lists every option contract with its underlying (`name`), strike,
expiry and lot size; `search` on NSE / NSE_INDEX tells whether the underlying is a stock or an index; daily history gives the
last close (to measure the strike step near the money) and one day of 1m history gives the bars per session.
"""
from collections import Counter
from datetime import date, datetime, timedelta

from script_openalgo.backtest_core import data_loader as dl
from script_openalgo.backtest_core import instruments

MAX_RESULTS = 25


def _search(query, exchange):
    code, b = dl._post("/api/v1/search", query=query, exchange=exchange)
    if code != 200 or b.get("status") != "success":
        raise RuntimeError(f"OpenAlgo search failed ({code}): {b.get('message')}")
    return b.get("data") or []


def _exp(s):
    try:
        return datetime.strptime(str(s).title(), "%d-%b-%y").date()
    except ValueError:
        return None


MAX_CHECKS = 15          # candidates checked for options per search (one exact NFO search each, ~0.3 s)


def _options_of(u):
    """CE/PE contracts whose underlying is exactly `u` (an exact query stays under OpenAlgo's 500-result cap)."""
    return [r for r in _search(u, "NFO") if r.get("instrumenttype") in ("CE", "PE") and (r.get("name") or "").upper() == u]


def search(query):
    """Underlyings matching `query` that have options on NFO: [{symbol, name, kind, exchange, lot_size, expiries, added}].
    OpenAlgo's search returns at most 500 rows, so a broad NFO query can be filled by one underlying's contracts (HDFC ->
    HDFCAMC only). Candidates therefore come from NSE (stocks), NSE_INDEX (indices) and the NFO rows' underlyings, and each
    candidate is confirmed with an exact NFO query."""
    q = (query or "").strip().upper()
    if len(q) < 2:
        return []
    eq = {r["symbol"].upper(): r for r in _search(q, "NSE") if r.get("instrumenttype") == "EQ"}
    idx = {r["symbol"].upper() for r in _search(q, "NSE_INDEX")}
    fno = {(r.get("name") or "").upper() for r in _search(q, "NFO")} - {""}
    cands = sorted(fno | set(eq) | idx, key=lambda u: (not u.startswith(q), u not in fno, len(u), u))
    out = []
    for u in cands[:MAX_CHECKS]:
        opts = _options_of(u)
        if not opts:
            continue
        stock = u in eq
        exps = sorted({_exp(r["expiry"]) for r in opts if _exp(r.get("expiry"))})
        out.append({"symbol": u, "name": eq[u]["name"] if stock else u, "kind": "stock" if stock else "index",
                    "exchange": "NSE" if stock else "NSE_INDEX", "lot_size": opts[0].get("lotsize"),
                    "expiries": [x.isoformat() for x in exps if x >= date.today()][:3],
                    "added": u in instruments.INSTRUMENTS})
        if len(out) >= MAX_RESULTS:
            break
    return out


def _last_close(symbol, exchange):
    end = date.today()
    code, b = dl._post("/api/v1/history", symbol=symbol, exchange=exchange, interval="D",
                       start_date=(end - timedelta(days=15)).isoformat(), end_date=end.isoformat(), source="api")
    rows = b.get("data") or [] if code == 200 else []
    return float(rows[-1]["close"]) if rows else None


def _bars_per_day(symbol, exchange):
    """1m bars of the underlying on the latest full session (stocks end at 15:14, indices at 15:29)."""
    d = date.today() - timedelta(days=1)
    for _ in range(8):
        if d.weekday() < 5:
            df = dl.fetch_candles(symbol, exchange, d)
            if len(df) > 200:
                return len(df), d
        d -= timedelta(days=1)
    return None, None


def describe(symbol):
    """Settings for `symbol`, derived from its listed option contracts. Raises if it has no options on NFO."""
    u = symbol.strip().upper()
    opts = _options_of(u)
    if not opts:
        raise RuntimeError(f"{u} has no option contracts on NFO in OpenAlgo (not an F&O underlying, or the master contract is not loaded)")
    expiries = sorted({_exp(r["expiry"]) for r in opts if _exp(r.get("expiry"))})
    upcoming = [e for e in expiries if e >= date.today()] or expiries
    nearest = upcoming[0]
    gaps = [(b - a).days for a, b in zip(upcoming, upcoming[1:])]
    weekly = bool(gaps) and min(gaps) <= 8
    eq = [r for r in _search(u, "NSE") if r.get("instrumenttype") == "EQ" and r["symbol"].upper() == u]
    stock = bool(eq)
    exchange = "NSE" if stock else "NSE_INDEX"
    strikes = sorted({float(r["strike"]) for r in opts if _exp(r.get("expiry")) == nearest and r.get("strike") and r["strike"] > 0})
    close = _last_close(u, exchange)
    step = None
    if strikes:
        near = sorted(strikes, key=lambda k: abs(k - close))[:12] if close else strikes
        near.sort()
        diffs = [round(b - a, 2) for a, b in zip(near, near[1:]) if b > a]
        step = Counter(diffs).most_common(1)[0][0] if diffs else None
    bars, bars_day = _bars_per_day(u, exchange)
    lot = opts[0].get("lotsize")
    cfg = {
        "kind": "stock" if stock else "index",
        "name": eq[0]["name"] if stock else u,
        "index_symbol": u,
        "index_exchange": exchange,
        "opt_exchange": "NFO",
        "strike_step": int(step) if step and float(step).is_integer() else step,
        "strike_buffer": 3 if stock else 2,
        "expiry_cycle": "weekly" if weekly else "monthly",
        "max_expiry_gap_days": 8 if weekly else 35,
        "min_underlying_bars": max(300, bars - 5) if bars else (355 if stock else 370),
        "min_option_coverage": 0.80 if stock else 0.90,
        "slippage_pct": 1.0 if stock else 0.0,
        "strategy_overrides": {},
        "lot_size_seen": lot,
        "added": datetime.now().isoformat(timespec="seconds"),
        "note": (f"auto: nearest expiry {nearest}, {len(strikes)} strikes listed, last close {close}, "
                 f"{bars} bars on {bars_day}; slippage_pct and min_option_coverage are defaults, adjust after a backfill"),
    }
    if not cfg["strike_step"]:
        raise RuntimeError(f"could not work out the strike step of {u} (no strikes near the last close {close})")
    return cfg


def add(symbol, **overrides):
    """describe() + optional user values (min_option_coverage, slippage_pct, daily_max_loss) -> saved instrument."""
    cfg = describe(symbol)
    for k in ("min_option_coverage", "slippage_pct", "strike_buffer"):
        if overrides.get(k) not in (None, ""):
            cfg[k] = float(overrides[k]) if k != "strike_buffer" else int(overrides[k])
    if overrides.get("daily_max_loss") not in (None, ""):
        cfg["strategy_overrides"] = dict(cfg["strategy_overrides"], DAILY_MAX_LOSS=float(overrides["daily_max_loss"]))
    return instruments.save_user(symbol, cfg)
