"""Market-structure helpers for strategies: CPR (Central Pivot Range), classic pivots, PCR from an OpenAlgo option chain,
and a combined market-bias read. Pure functions (no I/O, no clock), so the same code runs live and in a backtest.

Data a strategy needs (both are normal OpenAlgo client calls, supported by the backtest FakeClient):
    daily  = client.history(symbol="NIFTY", exchange="NSE_INDEX", interval="D", start_date=..., end_date=..., source="api")
             -> DataFrame of COMPLETED daily bars (today's bar is never included in a backtest)
    chain  = client.optionchain(underlying="NIFTY", exchange="NSE_INDEX", expiry_date="19OCT26")
             -> {"status", "underlying_ltp", "atm_strike", "chain": [{"strike", "ce": {...oi...}, "pe": {...oi...}}]}

Import from a strategy file with:
    from script_openalgo.strategy_lib import levels

The thresholds below are common trader conventions used as DEFAULTS, not proven edges. Test them, do not trust them.
"""

NARROW_CPR_PCT = 0.15     # CPR width below this % of the pivot is "narrow" (often read as: trending day expected)
WIDE_CPR_PCT = 0.40       # above this % it is "wide" (often read as: range-bound day expected)
PCR_BULLISH = 1.2         # PCR (put OI / call OI) above this: more puts written = support = mildly bullish
PCR_BEARISH = 0.8         # below this: more calls written = resistance = mildly bearish


def cpr(high, low, close):
    """Central Pivot Range and classic pivot levels from the PREVIOUS day's high, low, close.
    pivot = (H+L+C)/3, bc = (H+L)/2, tc = 2*pivot - bc. tc/bc are returned so that tc >= bc."""
    h, l, c = float(high), float(low), float(close)
    pivot = (h + l + c) / 3.0
    bc = (h + l) / 2.0
    tc = 2.0 * pivot - bc
    if tc < bc:
        tc, bc = bc, tc
    rng = h - l
    return {
        "pivot": pivot, "tc": tc, "bc": bc,
        "width": tc - bc, "width_pct": (tc - bc) / pivot * 100.0,
        "r1": 2 * pivot - l, "s1": 2 * pivot - h,
        "r2": pivot + rng, "s2": pivot - rng,
        "r3": h + 2 * (pivot - l), "s3": l - 2 * (h - pivot),
        "prev_high": h, "prev_low": l, "prev_close": c,
    }


def cpr_from_daily(daily):
    """CPR for 'today' from a DataFrame of completed daily bars (the last row is the previous trading day)."""
    if daily is None or len(daily) == 0:
        return None
    last = daily.iloc[-1]
    return cpr(last["high"], last["low"], last["close"])


def cpr_width_class(width_pct, narrow=NARROW_CPR_PCT, wide=WIDE_CPR_PCT):
    return "narrow" if width_pct < narrow else ("wide" if width_pct > wide else "normal")


def cpr_relation(today, yesterday):
    """How today's CPR sits against the previous day's CPR: 'higher', 'lower' or 'overlapping' (a value-area shift)."""
    if not today or not yesterday:
        return None
    if today["bc"] > yesterday["tc"]:
        return "higher"
    if today["tc"] < yesterday["bc"]:
        return "lower"
    return "overlapping"


def cpr_bias(price, c):
    """Where price is relative to today's CPR: above TC = bullish, below BC = bearish, inside = neutral."""
    if c is None:
        return "neutral"
    if price > c["tc"]:
        return "bullish"
    if price < c["bc"]:
        return "bearish"
    return "neutral"


def pcr_from_chain(chain_response, within_points=None, by="oi"):
    """Put-Call Ratio from an OpenAlgo optionchain() response. by = 'oi' (open interest) or 'volume'.
    within_points limits it to strikes within +/- that many index points of ATM (None = every strike returned).
    Returns None if there is no call data."""
    if not chain_response or chain_response.get("status") != "success":
        return None
    atm = chain_response.get("atm_strike")
    ce = pe = 0.0
    for row in chain_response.get("chain", []):
        if within_points is not None and atm is not None and abs(row["strike"] - atm) > within_points:
            continue
        ce += float((row.get("ce") or {}).get(by) or 0)
        pe += float((row.get("pe") or {}).get(by) or 0)
    return None if ce <= 0 else pe / ce


def pcr_bias(pcr, bullish=PCR_BULLISH, bearish=PCR_BEARISH):
    if pcr is None:
        return "neutral"
    return "bullish" if pcr >= bullish else ("bearish" if pcr <= bearish else "neutral")


def market_bias(price, c, pcr=None, prev_c=None):
    """Combine CPR position, CPR width, CPR shift vs yesterday and PCR into one read.
    Returns {"bias": "bullish"|"bearish"|"neutral", "score": int, "day_type": "trending"|"range"|"normal", "reasons": [...]}.
    score: each of price-vs-CPR, CPR shift and PCR adds +1 (bullish) or -1 (bearish); bias needs |score| >= 2."""
    reasons, score = [], 0
    pb = cpr_bias(price, c)
    if pb != "neutral":
        score += 1 if pb == "bullish" else -1
        reasons.append(f"price {'above TC' if pb == 'bullish' else 'below BC'}")
    else:
        reasons.append("price inside CPR")
    rel = cpr_relation(c, prev_c)
    if rel in ("higher", "lower"):
        score += 1 if rel == "higher" else -1
        reasons.append(f"CPR {rel} than yesterday")
    if pcr is not None:
        qb = pcr_bias(pcr)
        if qb != "neutral":
            score += 1 if qb == "bullish" else -1
        reasons.append(f"PCR {pcr:.2f} ({qb})")
    wc = cpr_width_class(c["width_pct"]) if c else "normal"
    day_type = {"narrow": "trending", "wide": "range", "normal": "normal"}[wc]
    reasons.append(f"CPR {wc} ({c['width_pct']:.2f}%)" if c else "no CPR")
    bias = "bullish" if score >= 2 else ("bearish" if score <= -2 else "neutral")
    return {"bias": bias, "score": score, "day_type": day_type, "reasons": reasons}
