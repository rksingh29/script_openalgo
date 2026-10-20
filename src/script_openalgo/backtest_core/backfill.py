"""Multi-day backfill: fetch the WHOLE listed life of an option contract in one call per series and split it into the normal
per-day cache files and manifests, so strategies can be backtested on many days at once.

    python -m script_openalgo.backtest_core.backfill --instrument NIFTY [--expiry 19OCT26] [--days 20] [--force]

Why: OpenAlgo returns every trading day since a contract was listed in ONE history call (about 2 s, with open interest), but
expired contracts disappear, so this must run while the contract is listed. Days on which the fetched contract was NOT the
nearest expiry are labelled (manifest: nearest=false, days_to_expiry); the multi-day runner skips them unless told otherwise.

Per day the strikes are the usual set: the day's index range (from the daily bars) +/- STRIKE_BUFFER strikes. A strike inside
the day's range that is missing or thin makes the day incomplete; a thin strike only in the outer buffer is a warning.
"""
import argparse
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from script_openalgo.backtest_core import data_loader as dl  # noqa: E402
from script_openalgo.backtest_core import instruments  # noqa: E402


def _listed_since(symbol, exchange, today):
    """First trading day the option has daily history (= when the contract started trading)."""
    code, b = dl._post("/api/v1/history", symbol=symbol, exchange=exchange, interval="D",
                       start_date=(today - timedelta(days=150)).isoformat(), end_date=today.isoformat(), source="api")
    if code != 200 or not b.get("data"):
        return None
    return min(datetime.fromtimestamp(r["timestamp"], timezone.utc).date() for r in b["data"])


def _day_strikes(cfg, low, high):
    step, buf = cfg["strike_step"], cfg["strike_buffer"]
    core_lo, core_hi = math.floor(low / step) * step, math.ceil(high / step) * step
    allk = list(range(core_lo - buf * step, core_hi + buf * step + 1, step))
    core = set(range(core_lo, core_hi + 1, step))
    return allk, core


def backfill(instrument, expiry=None, max_days=None, force=False):
    cfg = instruments.get(instrument)
    instrument = instrument.upper()
    today = date.today()
    listed = [e for e in dl.listed_expiries(instrument) if e >= today]
    if expiry is None:
        expiry = listed[0]
    elif expiry not in listed:
        raise SystemExit(f"{expiry} is not a currently listed expiry ({[e.isoformat() for e in listed[:6]]})")
    print(f"[{instrument}] backfill for expiry {expiry}")

    daily = dl.fetch_daily(instrument, today, lookback_days=150)
    daily = daily[daily.index <= pd.Timestamp(today)]
    last_complete = today if (datetime.now().hour >= 16 and today in {d.date() for d in daily.index}) else today - timedelta(days=1)
    spot = float(daily["close"].iloc[-1])
    atm = int(round(spot / cfg["strike_step"]) * cfg["strike_step"])
    since = None
    for k in (0, 1, -1, 2, -2):                            # the exact ATM strike may not be listed (irregular stock strikes)
        probe = dl.option_symbol(instrument, expiry, atm + k * cfg["strike_step"], "CE")
        since = _listed_since(probe, cfg["opt_exchange"], today)
        if since:
            break
    if since is None:
        raise SystemExit(f"no history for {probe}: is the contract listed and the broker connected?")
    days = [d.date() for d in daily.index if since <= d.date() <= last_complete and d.date() <= expiry]
    if max_days:
        days = days[-max_days:]
    print(f"[{instrument}] contract listed since {since}; {len(days)} trading days to cover: {days[0]} .. {days[-1]}")

    per_day = {}
    for d in days:
        r = daily.loc[pd.Timestamp(d)]
        per_day[d] = _day_strikes(cfg, float(r["low"]), float(r["high"]))
    all_strikes = sorted({k for allk, _ in per_day.values() for k in allk})
    print(f"[{instrument}] {len(all_strikes)} strikes x CE/PE = {2 * len(all_strikes)} series (about {2 * len(all_strikes) * 2 // 60 + 1} min)")

    # 1. index 1m candles for all days (one call)
    idx_missing = [d for d in days if force or not os.path.exists(dl.cache_path(cfg["index_symbol"], d))]
    if idx_missing:
        got = dl.fetch_candles_range(cfg["index_symbol"], cfg["index_exchange"], min(idx_missing), max(idx_missing))
        for d in idx_missing:
            if d in got and not got[d].empty:
                got[d].to_parquet(dl.cache_path(cfg["index_symbol"], d))
    # 1b. India VIX 1m for all days (one call; context for the Signals chart, not part of completeness)
    vix_missing = [d for d in days if force or not os.path.exists(dl.cache_path(dl.VIX_SYMBOL, d))]
    if vix_missing:
        got = dl.fetch_candles_range(dl.VIX_SYMBOL, dl.VIX_EXCHANGE, min(vix_missing), max(vix_missing))
        for d in vix_missing:
            if d in got and not got[d].empty:
                got[d].to_parquet(dl.cache_path(dl.VIX_SYMBOL, d))
    # 2. every option series once, split per day
    fetched = calls = 0
    for strike in all_strikes:
        for ot in ("CE", "PE"):
            sym = dl.option_symbol(instrument, expiry, strike, ot)
            need = [d for d in days if strike in per_day[d][0] and (force or not os.path.exists(dl.cache_path(sym, d)))]
            if not need:
                continue
            got = dl.fetch_candles_range(sym, cfg["opt_exchange"], min(need), max(need))
            calls += 1
            for d in need:
                if d in got and not got[d].empty:
                    got[d].to_parquet(dl.cache_path(sym, d))
                    fetched += 1
            if calls % 10 == 0:
                print(f"  ... {calls} calls, {fetched} day-files")
    print(f"[{instrument}] fetched {fetched} day-files in {calls} calls")

    # 3. manifests per day (same completeness rules as ensure_data, with the core/buffer distinction)
    code, b = dl._post("/api/v1/optionsymbol", underlying=cfg["index_symbol"], exchange=cfg["index_exchange"],
                       expiry_date=dl.fmt_expiry(expiry), offset="ATM", option_type="CE")
    lot = int(b["lotsize"]) if code == 200 and b.get("lotsize") else None
    summary = []
    stock, cov = instruments.is_stock(instrument), dl.min_coverage(instrument)
    for d in days:
        allk, core = per_day[d]
        series, issues, warnings = {}, [], []
        idx = dl.load_series(cfg["index_symbol"], d)
        idx = idx if idx is not None else pd.DataFrame()
        series[cfg["index_symbol"]] = dl.series_info(idx)
        if len(idx) < dl.min_bars(instrument):
            issues.append(f"{instrument} index has only {len(idx)} bars (need >= {dl.min_bars(instrument)})")
        unlisted = []
        for k in allk:
            for ot in ("CE", "PE"):
                sym = dl.option_symbol(instrument, expiry, k, ot)
                df = dl.load_series(sym, d)
                if df is None and sym in dl.NOT_FOUND and stock:
                    unlisted.append(sym)                 # irregular stock strike: not listed, not missing data
                    continue
                info = dl.series_info(df if df is not None else pd.DataFrame())
                series[sym] = info
                bad = info["rows"] == 0 or info["coverage"] < cov
                if bad:
                    msg = f"{sym}: " + ("no data" if info["rows"] == 0 else f"coverage {info['coverage']:.0%}")
                    (issues if k in core else warnings).append(msg + ("" if k in core else " (outer buffer strike)"))
        prior = daily[daily.index < pd.Timestamp(d)]
        series["DAILY"] = {"rows": len(prior), "last_before_day": prior.index[-1].date().isoformat() if len(prior) else None}
        if prior.empty:
            issues.append("no daily bar before this date")
        extra = {"backfilled": True, "lot_size_note": "current lot size (history of lot changes not available)"}
        if unlisted:
            extra["unlisted"] = unlisted
        m = dl._save(d, instrument, expiry, lot, dl.listed_strikes(instrument, expiry, allk, unlisted), series, issues, warnings,
                     extra=extra)
        summary.append((d, m["complete"], m["nearest"], m["days_to_expiry"], len(issues), len(warnings)))
    print(f"\n[{instrument}] day / complete / nearest expiry / days to expiry / issues / warnings")
    for r in summary:
        print("  ", r[0], "complete" if r[1] else "INCOMPLETE", "nearest" if r[2] else "far-expiry", f"dte={r[3]}", f"issues={r[4]}", f"warnings={r[5]}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="NIFTY", help="NIFTY, BANKNIFTY, a stock (RELIANCE, OFSS) or ALL")
    ap.add_argument("--expiry", help="DDMMMYY, default = nearest listed expiry")
    ap.add_argument("--days", type=int, help="only the last N trading days")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    exp = datetime.strptime(a.expiry.title(), "%d%b%y").date() if a.expiry else None
    for n in (list(instruments.INSTRUMENTS) if a.instrument.upper() == "ALL" else [a.instrument]):
        backfill(n, exp, a.days, a.force)
