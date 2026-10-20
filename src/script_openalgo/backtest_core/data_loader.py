"""
Data layer for backtests: fetch candles from OpenAlgo (:5000, source="api"),
clean them and cache to data_cache/. The backtest reads only the cache.

CLI:  python -m script_openalgo.backtest_core.data_loader [--instrument NIFTY|BANKNIFTY|ALL] [--date YYYY-MM-DD] [--force]
Exit code 0 = manifest(s) complete, 2 = incomplete (see manifest "issues").
"""
import argparse
import glob
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

from . import instruments

from script_openalgo import config  # noqa: E402
THROTTLE_SEC = 0.4
RETRIES = 3
SESSION_START = "09:15"
SESSION_END = "15:29"
MIN_BARS_INDEX = 370        # of 375
MIN_COVERAGE_OPTION = 0.90  # fraction of 375 minutes present
CALENDAR_INSTRUMENT = "NIFTY"   # used to decide whether a date was a trading day

IST = timezone(timedelta(hours=5, minutes=30))
CACHE_DIR = config.data_cache_dir()
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


NOT_FOUND = set()          # symbols OpenAlgo answered "not found" for (e.g. a strike that is not listed for a stock)


def _not_found(code, msg):
    return code == 400 and "not found" in str(msg or "").lower()


def min_bars(instrument):
    """Minimum 1m bars of the underlying for a usable day (stocks end at 15:14, indices at 15:29)."""
    return instruments.get(instrument).get("min_underlying_bars", MIN_BARS_INDEX)


def min_coverage(instrument):
    return instruments.get(instrument).get("min_option_coverage", MIN_COVERAGE_OPTION)


def _key():
    k = config.api_key()
    if not k:
        raise SystemExit("OpenAlgo API key not found: set OPENALGO_API_KEY or put OPENALGO_API_KEY=... in the workspace .env file.")
    return k


def _post(path, **body):
    body["apikey"] = _key()
    r = requests.post(f"{config.host()}{path}", json=body, timeout=60)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"status": "error", "message": r.text[:200]}


def fetch_candles(symbol, exchange, day):
    """1m candles for one date as a DataFrame indexed by naive IST minute. Empty if none."""
    d = day.isoformat()
    last = None
    for attempt in range(RETRIES):
        time.sleep(THROTTLE_SEC)
        code, b = _post("/api/v1/history", symbol=symbol, exchange=exchange, interval="1m",
                        start_date=d, end_date=d, source="api")
        if code == 200 and isinstance(b.get("data"), list):
            return clean(b["data"], day)
        last = (code, b.get("message"))
        if _not_found(code, b.get("message")):
            NOT_FOUND.add(symbol)
            return pd.DataFrame()
        if code in (400, 404):      # symbol unknown / no data: retrying will not help
            break
        time.sleep(1 + attempt)
    print(f"  fetch failed {symbol}: {last}")
    return pd.DataFrame()


def clean(rows, day):
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["dt"] = (pd.to_datetime(df["timestamp"], unit="s", utc=True)
                .dt.tz_convert(IST).dt.tz_localize(None))
    df = df[df["dt"].dt.date == day]                       # end_date can return extra days
    df = df.set_index("dt").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    df = df.between_time(SESSION_START, SESSION_END)       # drop bars after 15:29
    return df[["open", "high", "low", "close", "volume", "oi"]]


def previous_trading_day(today=None):
    """Latest date before `today` on which the index has candles (skips weekends/holidays)."""
    cfg = instruments.get(CALENDAR_INSTRUMENT)
    d = (today or date.today()) - timedelta(days=1)
    for _ in range(10):
        if d.weekday() < 5:
            cached = load_series(cfg["index_symbol"], d)           # a day already in the cache needs no API call
            if (cached is not None and len(cached) >= MIN_BARS_INDEX) or (
                    len(fetch_candles(cfg["index_symbol"], cfg["index_exchange"], d)) > 0):
                return d
        d -= timedelta(days=1)
    raise RuntimeError("no trading day found in the last 10 days")


def listed_expiries(instrument):
    cfg = instruments.get(instrument)
    code, b = _post("/api/v1/expiry", symbol=cfg["index_symbol"], exchange=cfg["opt_exchange"],
                    instrumenttype="options")
    if code != 200 or not b.get("data"):
        raise RuntimeError(f"expiry lookup failed: {b}")
    return [datetime.strptime(e.title(), "%d-%b-%y").date() for e in b["data"]]


def fmt_expiry(d):
    return f"{d.day:02d}{MONTHS[d.month - 1]}{d.year % 100:02d}"


def option_symbol(instrument, expiry, strike, opt_type):
    return f"{instruments.get(instrument)['index_symbol']}{fmt_expiry(expiry)}{int(strike)}{opt_type}"


def candidate_strikes(instrument, low, high):
    cfg = instruments.get(instrument)
    step, buf = cfg["strike_step"], cfg["strike_buffer"]
    lo = math.floor(low / step) * step - buf * step
    hi = math.ceil(high / step) * step + buf * step
    return list(range(lo, hi + step, step))


def previous_expiry(instrument, expiry):
    """The expiry BEFORE `expiry` for this instrument's cycle (heuristic: holiday shifts are ignored).
    weekly = 7 days earlier; monthly = the last same-weekday of the previous month."""
    if instruments.get(instrument).get("expiry_cycle") == "weekly":
        return expiry - timedelta(days=7)
    y, m = (expiry.year, expiry.month - 1) if expiry.month > 1 else (expiry.year - 1, 12)
    last = date(y, m, 28)
    while True:                                   # walk forward to the last day of that month
        nxt = last + timedelta(days=1)
        if nxt.month != m:
            break
        last = nxt
    while last.weekday() != expiry.weekday():
        last -= timedelta(days=1)
    return last


def expiry_info(instrument, day, expiry):
    """days_to_expiry and whether `expiry` was the NEAREST expiry on `day` (day falls after the previous expiry)."""
    prev = previous_expiry(instrument, expiry)
    return {"days_to_expiry": (expiry - day).days, "nearest": prev < day <= expiry, "previous_expiry": prev.isoformat()}


def fetch_candles_range(symbol, exchange, start_day, end_day):
    """ONE history call for many days (the broker returns every day in the range). Returns {date: cleaned DataFrame}."""
    last = None
    for attempt in range(RETRIES):
        time.sleep(THROTTLE_SEC)
        code, b = _post("/api/v1/history", symbol=symbol, exchange=exchange, interval="1m",
                        start_date=start_day.isoformat(), end_date=end_day.isoformat(), source="api")
        if code == 200 and isinstance(b.get("data"), list):
            rows = b["data"]
            days = sorted({datetime.fromtimestamp(r["timestamp"], IST).date() for r in rows})
            return {d: clean(rows, d) for d in days}
        last = (code, b.get("message"))
        if _not_found(code, b.get("message")):
            NOT_FOUND.add(symbol)
            return {}
        if code in (400, 404):
            break
        time.sleep(1 + attempt)
    print(f"  range fetch failed {symbol}: {last}")
    return {}


def cache_path(name, day):
    return os.path.join(CACHE_DIR, f"{name}_{day.isoformat()}.parquet")


def manifest_path(day, instrument="NIFTY"):
    return os.path.join(CACHE_DIR, f"manifest_{instrument.upper()}_{day.isoformat()}.json")


def series_info(df):
    if df.empty:
        return {"rows": 0, "first": None, "last": None, "coverage": 0.0, "gaps": []}
    full = pd.date_range(df.index[0].normalize() + pd.Timedelta(hours=9, minutes=15),
                         periods=375, freq="min")
    missing = full.difference(df.index)
    return {"rows": len(df), "first": df.index[0].strftime("%H:%M"), "last": df.index[-1].strftime("%H:%M"),
            "coverage": round(len(df) / 375, 3), "gaps": [m.strftime("%H:%M") for m in missing][:60]}


def _load_or_fetch(name, symbol, exchange, day, force):
    p = cache_path(name, day)
    if os.path.exists(p) and not force:
        return pd.read_parquet(p), False
    df = fetch_candles(symbol, exchange, day)
    if not df.empty:
        os.makedirs(CACHE_DIR, exist_ok=True)
        df.to_parquet(p)
    return df, True



# ---------------- daily bars (for CPR / previous-day levels) and option-chain snapshots (PCR) ----------------
DAILY_LOOKBACK_DAYS = 45


def daily_path(instrument):
    return os.path.join(CACHE_DIR, f"daily_{instrument.upper()}.parquet")


def fetch_daily(instrument, upto_day, lookback_days=DAILY_LOOKBACK_DAYS):
    """Daily OHLC bars of the index from OpenAlgo (history interval 'D'); merged into data_cache/daily_<INSTRUMENT>.parquet."""
    cfg = instruments.get(instrument)
    start = (upto_day - timedelta(days=lookback_days)).isoformat()
    time.sleep(THROTTLE_SEC)
    code, b = _post("/api/v1/history", symbol=cfg["index_symbol"], exchange=cfg["index_exchange"], interval="D",
                    start_date=start, end_date=upto_day.isoformat(), source="api")
    if code != 200 or not isinstance(b.get("data"), list) or not b["data"]:
        print(f"  daily fetch failed {instrument}: {code} {b.get('message')}")
        return load_daily(instrument)
    df = pd.DataFrame(b["data"])
    # daily bars are stamped at 00:00 UTC of the trading date
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_localize(None).dt.normalize()
    df = df.set_index("dt").sort_index()[["open", "high", "low", "close", "volume", "oi"]]
    old = load_daily(instrument)
    if old is not None and not old.empty:
        df = pd.concat([old[~old.index.isin(df.index)], df]).sort_index()
    os.makedirs(CACHE_DIR, exist_ok=True)
    df.to_parquet(daily_path(instrument))
    return df


def load_daily(instrument):
    p = daily_path(instrument)
    return pd.read_parquet(p) if os.path.exists(p) else None


def snapshot_chain(instrument, expiry=None, strike_count=None):
    """Save the CURRENT full option chain (with open interest) from OpenAlgo to data_cache/chain_<INSTR>_<date>_<HHMM>.json.
    OpenAlgo gives live data only, so snapshots are the only way to get real full-chain PCR in a later backtest:
    take them during market hours (e.g. 09:20, 12:00, 15:20) from now on."""
    cfg = instruments.get(instrument)
    if expiry is None:
        expiry = [e for e in listed_expiries(instrument) if e >= date.today()][0]
    body = {"underlying": cfg["index_symbol"], "exchange": cfg["index_exchange"], "expiry_date": fmt_expiry(expiry)}
    if strike_count:
        body["strike_count"] = int(strike_count)
    code, b = _post("/api/v1/optionchain", **body)
    if code != 200 or b.get("status") != "success":
        raise RuntimeError(f"optionchain failed: {code} {b.get('message')}")
    now_t = datetime.now()
    path = os.path.join(CACHE_DIR, f"chain_{instrument.upper()}_{now_t.date().isoformat()}_{now_t:%H%M}.json")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(b, f)
    print(f"[{instrument}] chain snapshot saved: {os.path.basename(path)} ({len(b['chain'])} strikes)")
    return path


def chain_snapshots(instrument, day):
    """[(datetime, path)] of snapshots saved for `day`, oldest first."""
    out = []
    for p in sorted(glob.glob(os.path.join(CACHE_DIR, f"chain_{instrument.upper()}_{day.isoformat()}_*.json"))):
        m = re.search(r"_(\d{4})\.json$", p)
        if m:
            out.append((datetime.combine(day, datetime.strptime(m.group(1), "%H%M").time()), p))
    return out


def ensure_data(day, instrument="NIFTY", force=False):
    """Fetch/verify everything needed to backtest `instrument` on `day`. Returns the manifest dict (also saved)."""
    cfg = instruments.get(instrument)
    instrument = instrument.upper()
    os.makedirs(CACHE_DIR, exist_ok=True)
    issues = []
    series = {}
    print(f"[{instrument}] preparing data for {day} ...")

    idx, _ = _load_or_fetch(cfg["index_symbol"], cfg["index_symbol"], cfg["index_exchange"], day, force)
    series[cfg["index_symbol"]] = series_info(idx)
    if len(idx) < min_bars(instrument):
        issues.append(f"{instrument} index has only {len(idx)} bars (need >= {min_bars(instrument)}); not a trading day or data missing")
        return _save(day, instrument, None, None, [], series, issues)

    daily = fetch_daily(instrument, day)
    prior = daily[daily.index < pd.Timestamp(day)] if daily is not None else None
    series["DAILY"] = {"rows": 0 if prior is None else len(prior), "last_before_day": (prior.index[-1].date().isoformat() if prior is not None and len(prior) else None)}
    if prior is None or prior.empty:
        issues.append("no daily bar before this date (needed for CPR / previous-day levels)")

    # expiry valid on that date = first listed expiry on/after it
    expiries = [e for e in listed_expiries(instrument) if e >= day]
    if not expiries:
        issues.append("no listed expiry on/after the date")
        return _save(day, instrument, None, None, [], series, issues)
    expiry = expiries[0]
    if (expiry - day).days > cfg["max_expiry_gap_days"]:
        issues.append(f"nearest listed expiry {expiry} is {(expiry - day).days} days after {day}: "
                      "the expiry actually used that day has probably expired and is no longer available")

    code, b = _post("/api/v1/optionsymbol", underlying=cfg["index_symbol"], exchange=cfg["index_exchange"],
                    expiry_date=fmt_expiry(expiry), offset="ATM", option_type="CE")
    lot = int(b["lotsize"]) if code == 200 and b.get("lotsize") else None
    if lot is None:
        issues.append(f"lot size lookup failed: {b}")

    strikes = candidate_strikes(instrument, float(idx["low"].min()), float(idx["high"].max()))
    print(f"[{instrument}] range {idx['low'].min():.0f}-{idx['high'].max():.0f} | expiry {expiry} | "
          f"{len(strikes)} strikes x CE/PE | lot {lot}")
    unlisted, cov = [], min_coverage(instrument)
    for strike in strikes:
        for ot in ("CE", "PE"):
            sym = option_symbol(instrument, expiry, strike, ot)
            df, _ = _load_or_fetch(sym, sym, cfg["opt_exchange"], day, force)
            if df.empty and sym in NOT_FOUND and instruments.is_stock(instrument):
                # stock strikes are irregular: a strike OpenAlgo does not know is not listed, not missing data.
                # (indices keep "no data": there "not found" usually means an expired contract)
                unlisted.append(sym)
                continue
            info = series_info(df)
            series[sym] = info
            if info["rows"] == 0:
                issues.append(f"{sym}: no data")
            elif info["coverage"] < cov:
                issues.append(f"{sym}: coverage {info['coverage']:.0%} below {cov:.0%}")
    listed = listed_strikes(instrument, expiry, strikes, unlisted)
    return _save(day, instrument, expiry, lot, listed, series, issues, extra={"unlisted": unlisted} if unlisted else None)


def listed_strikes(instrument, expiry, strikes, unlisted):
    """Strikes with at least one of CE/PE listed (a strike whose CE and PE are both unknown to OpenAlgo is dropped)."""
    gone = set(unlisted)
    return [k for k in strikes if not (option_symbol(instrument, expiry, k, "CE") in gone
                                       and option_symbol(instrument, expiry, k, "PE") in gone)]


def _save(day, instrument, expiry, lot, strikes, series, issues, warnings=None, extra=None):
    m = {"instrument": instrument, "date": day.isoformat(), "expiry": expiry.isoformat() if expiry else None,
         "lot_size": lot, "strikes": strikes, "series": series, "issues": issues, "warnings": warnings or [],
         "complete": not issues, "created": datetime.now().isoformat(timespec="seconds")}
    if expiry:
        m.update(expiry_info(instrument, day, expiry))
    if extra:
        m.update(extra)
    with open(manifest_path(day, instrument), "w") as f:
        json.dump(m, f, indent=1)
    print(f"[{instrument}] manifest: complete={m['complete']} ({len(series)} series, {len(issues)} issues)")
    for i in issues[:15]:
        print("  ISSUE:", i)
    return m


def load_manifest(day, instrument="NIFTY"):
    p = manifest_path(day, instrument)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def load_series(name, day):
    p = cache_path(name, day)
    return pd.read_parquet(p) if os.path.exists(p) else None


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="NIFTY", help="NIFTY, BANKNIFTY or ALL")
    ap.add_argument("--date", help="YYYY-MM-DD (default: previous trading day)")
    ap.add_argument("--force", action="store_true", help="refetch even if cached")
    ap.add_argument("--chain", action="store_true", help="save a snapshot of the CURRENT option chain (for PCR) instead of fetching candles")
    a = ap.parse_args()
    d = date.fromisoformat(a.date) if a.date else previous_trading_day()
    names = list(instruments.INSTRUMENTS) if a.instrument.upper() == "ALL" else [a.instrument]
    if a.chain:
        for n in names:
            snapshot_chain(n)
        sys.exit(0)
    ok = all([ensure_data(d, n, force=a.force)["complete"] for n in names])
    sys.exit(0 if ok else 2)
