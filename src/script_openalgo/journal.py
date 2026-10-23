"""Trade journal: every backtested trade with the market context at its entry, for later decisions.

    script-openalgo journal build [--rebuild]                 # add trades of runs not yet in the journal (or all again)
    script-openalgo journal query [filters] [--by FEATURE]    # win rate / P&L of matching trades, optionally per bucket
    script-openalgo journal similar --strategy S --instrument I --time 11:30 --vix-pos 80 [...]   # nearest past trades
    script-openalgo journal export [--out FILE.csv]           # all journal trades as CSV

Source: the CURRENT run of each (strategy, instrument, day): the newest run made with the strategy's default settings
(the same runs the strategy stats use; nearest-expiry days and far-expiry days are both kept, flagged by `nearest`).
Features use only data up to the minute of entry (the last completed 1m candle before it): no lookahead.
The outcome columns (pnl, net, win, hold, mfe/mae, exit_reason) are what happened after entry.
Tables (workspace strategy DB): journal_trades (one row per trade), journal_days (one row per strategy/instrument/day).
"""
import argparse
import csv
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta

import pandas as pd

from script_openalgo.strategy_db import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, strategy_id INTEGER, slug TEXT, instrument TEXT, day TEXT,
  trade_no INTEGER, side TEXT, symbol TEXT, qty INTEGER, entry_time TEXT, exit_time TEXT, entry_price REAL, exit_price REAL,
  pnl REAL, net REAL, win INTEGER, hold_min REAL, exit_reason TEXT, opt_mfe REAL, opt_mae REAL,
  minute_of_day INTEGER, weekday INTEGER, dte INTEGER, nearest INTEGER,
  u_price REAL, u_from_open_pct REAL, u_day_pos REAL, u_ret15_pct REAL, u_ret30_pct REAL, u_ret60_pct REAL,
  atr5_pct REAL, gap_pct REAL, prev_range_pct REAL,
  vix REAL, vix_pos REAL, vix_chg15 REAL, vix_chg30 REAL, vix_from_open REAL,
  code_hash TEXT, built TEXT, UNIQUE(run_id, trade_no));
CREATE INDEX IF NOT EXISTS jt_strategy ON journal_trades(slug, instrument);
CREATE TABLE IF NOT EXISTS journal_days(
  run_id INTEGER PRIMARY KEY, strategy_id INTEGER, slug TEXT, instrument TEXT, day TEXT, trades INTEGER, pnl REAL, net REAL,
  day_range_pct REAL, day_change_pct REAL, gap_pct REAL, vix_open REAL, vix_close REAL, vix_range REAL, dte INTEGER, nearest INTEGER,
  built TEXT);
"""

FEATURES = ["minute_of_day", "weekday", "dte", "u_from_open_pct", "u_day_pos", "u_ret15_pct", "u_ret30_pct", "u_ret60_pct",
            "atr5_pct", "gap_pct", "prev_range_pct", "vix", "vix_pos", "vix_chg15", "vix_chg30", "vix_from_open"]
BUCKETS = {   # --by FEATURE: bucket edges (value < edge goes to that bucket)
    "minute_of_day": [600, 660, 720, 780, 840, 931], "vix_pos": [20, 40, 60, 80, 90, 101], "vix": [12, 14, 16, 18, 22, 99],
    "vix_chg15": [-0.2, -0.05, 0.05, 0.2, 99], "vix_chg30": [-0.3, -0.1, 0.1, 0.3, 99], "u_day_pos": [10, 30, 50, 70, 90, 101],
    "u_from_open_pct": [-1, -0.5, -0.2, 0.2, 0.5, 1, 99], "u_ret30_pct": [-0.4, -0.15, 0, 0.15, 0.4, 99],
    "atr5_pct": [0.08, 0.12, 0.18, 0.25, 99], "dte": [2, 5, 10, 20, 40, 999], "gap_pct": [-0.5, -0.15, 0.15, 0.5, 99],
    "weekday": [1, 2, 3, 4, 5], "side": None, "exit_reason": None, "instrument": None, "slug": None, "day": None,
}


def ensure(con):
    con.executescript(SCHEMA)


# ---------------- feature computation ----------------
def _load(cache, name, day):
    p = os.path.join(cache, f"{name}_{day}.parquet")
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)
    if "dt" in df.columns:
        df = df.set_index("dt")
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def _upto(df, ts):
    """Rows of completed 1m candles before ts (a candle stamped T is complete at T+1 min)."""
    return df.loc[: ts - pd.Timedelta(minutes=1)] if df is not None else None


def _pct(a, b):
    return round((a - b) / b * 100, 4) if a is not None and b else None


def _close_ago(sub, ts, minutes):
    s = sub.loc[: ts - pd.Timedelta(minutes=minutes + 1)]
    return float(s["close"].iloc[-1]) if len(s) else None


def _atr5(sub, n=14):
    if sub is None or len(sub) < 10:
        return None
    b = sub.resample("5min", origin=sub.index[0].normalize() + pd.Timedelta(hours=9, minutes=15), label="left", closed="left").agg(
        {"high": "max", "low": "min", "close": "last"}).dropna()
    if len(b) < 2:
        return None
    pc = b["close"].shift()
    tr = pd.concat([b["high"] - b["low"], (b["high"] - pc).abs(), (b["low"] - pc).abs()], axis=1).max(axis=1).iloc[1:]
    if not len(tr):
        return None
    a = tr.iloc[:n].mean()
    for x in tr.iloc[n:]:
        a = (a * (n - 1) + x) / n
    return float(a)


def _daily(cache, instrument):
    p = os.path.join(cache, f"daily_{instrument.upper()}.parquet")
    return pd.read_parquet(p) if os.path.exists(p) else None


def trade_features(cache, instrument, cfg, day, t, daily):
    """Context at entry time `t` (HH:MM:SS) of `day`, from data completed before that minute."""
    d = date.fromisoformat(day)
    ts = pd.Timestamp(f"{day} {t[:5]}")
    out = {"minute_of_day": ts.hour * 60 + ts.minute, "weekday": d.weekday()}
    u = _load(cache, cfg["index_symbol"], day)
    sub = _upto(u, ts)
    if sub is not None and len(sub):
        price, opn = float(sub["close"].iloc[-1]), float(sub["open"].iloc[0])
        hi, lo = float(sub["high"].max()), float(sub["low"].min())
        out.update({"u_price": price, "u_from_open_pct": _pct(price, opn),
                    "u_day_pos": round((price - lo) / (hi - lo) * 100, 2) if hi > lo else None,
                    "u_ret15_pct": _pct(price, _close_ago(sub, ts, 15)), "u_ret30_pct": _pct(price, _close_ago(sub, ts, 30)),
                    "u_ret60_pct": _pct(price, _close_ago(sub, ts, 60))})
        a = _atr5(sub)
        out["atr5_pct"] = round(a / price * 100, 4) if a else None
        if daily is not None and len(daily):
            prev = daily[daily.index < pd.Timestamp(d)]
            if len(prev):
                pc, ph, pl = float(prev["close"].iloc[-1]), float(prev["high"].iloc[-1]), float(prev["low"].iloc[-1])
                out["gap_pct"] = _pct(opn, pc)
                out["prev_range_pct"] = round((ph - pl) / pc * 100, 4) if pc else None
    v = _upto(_load(cache, "INDIAVIX", day), ts)
    if v is not None and len(v):
        last, vo = float(v["close"].iloc[-1]), float(v["open"].iloc[0])
        vh, vl = float(v["high"].max()), float(v["low"].min())
        c15, c30 = _close_ago(v, ts, 15), _close_ago(v, ts, 30)
        out.update({"vix": last, "vix_pos": round((last - vl) / (vh - vl) * 100, 2) if vh - vl >= 0.1 else None,
                    "vix_chg15": round(last - c15, 4) if c15 else None, "vix_chg30": round(last - c30, 4) if c30 else None,
                    "vix_from_open": round(last - vo, 4)})
    return out


def _excursion(cache, symbol, day, t_in, t_out, entry, qty):
    """Best / worst option P&L (rupees) while the trade was open, from the option's 1m candles."""
    o = _load(cache, symbol, day)
    if o is None:
        return None, None
    seg = o.loc[pd.Timestamp(f"{day} {t_in[:5]}"): pd.Timestamp(f"{day} {t_out[:5]}")]
    if not len(seg):
        return None, None
    return round((float(seg["high"].max()) - entry) * qty, 2), round((float(seg["low"].min()) - entry) * qty, 2)


def _exit_reasons(result_dir):
    """{(symbol, HH:MM): reason} from the SELL lines of the run's log."""
    out = {}
    p = os.path.join(result_dir, "log.txt")
    if not os.path.exists(p):
        return out
    sells, exits = [], {}
    clean = lambda s: re.sub(r"\s*\(.*?\)", "", s).strip()[:80]
    for line in open(p, encoding="utf-8", errors="replace"):
        line = line.strip()
        m = re.match(r"\S+ (\d\d:\d\d):\d\d \| SELL (\S+) .*?\| order \S+ \| (.+)$", line)
        if m:
            out.setdefault((m.group(2), m.group(1)), clean(m.group(3)))
            continue
        m = re.match(r"\S+ (\d\d:\d\d):\d\d \| SELL (\S+) ", line)             # reason on a separate EXIT line
        if m:
            sells.append((m.group(2), m.group(1)))
            continue
        m = re.match(r"\S+ (\d\d:\d\d):\d\d \| EXIT (?:ALL \(cycle \d+\): |CE |PE )?([^|]+)", line)
        if m:
            exits.setdefault(m.group(1), clean(m.group(2)))
    for sym, hm in sells:
        if (sym, hm) in out:
            continue
        t = datetime.strptime(hm, "%H:%M")
        for dm in (0, 1, -1, 2):
            r = exits.get((t + timedelta(minutes=dm)).strftime("%H:%M"))
            if r:
                out[(sym, hm)] = r
                break
    return out


def _mins(a, b):
    ta, tb = datetime.strptime(a[:8], "%H:%M:%S"), datetime.strptime(b[:8], "%H:%M:%S")
    return round((tb - ta).total_seconds() / 60, 1)


# ---------------- build ----------------
def current_runs(con):
    """Newest default-settings run per (strategy, instrument, day, executor): the runs the stats use."""
    return db.rows(con, "SELECT r.*, s.slug FROM runs r JOIN strategies s ON s.id=r.strategy_id WHERE r.id IN ("
                        "SELECT MAX(id) FROM runs WHERE COALESCE(params, '') IN ('', '{}') GROUP BY strategy_id, instrument, day, executor)")


def build(con=None, rebuild=False, cache=None, quiet=False):
    """Add journal rows for current runs not yet journaled (and drop rows of runs that are no longer current)."""
    from script_openalgo.backtest_core import instruments
    own = con is None
    con = con or db.connect()
    ensure(con)
    cache = cache or db.ws_paths()[1]
    if rebuild:
        con.execute("DELETE FROM journal_trades")
        con.execute("DELETE FROM journal_days")
    runs = current_runs(con)
    keep = {r["id"] for r in runs}
    have = {r[0] for r in con.execute("SELECT run_id FROM journal_days")}
    stale = have - keep
    for rid in stale:                                        # superseded by a newer run or deleted
        con.execute("DELETE FROM journal_trades WHERE run_id=?", (rid,))
        con.execute("DELETE FROM journal_days WHERE run_id=?", (rid,))
    added_runs = added_trades = 0
    daily_cache = {}
    for r in runs:
        if r["id"] in have and r["id"] not in stale:
            continue
        rd = r["result_dir"]
        if not rd or not os.path.isdir(rd) or r["instrument"] not in instruments.INSTRUMENTS:
            continue
        cfg = instruments.get(r["instrument"])
        daily = daily_cache.setdefault(r["instrument"], _daily(cache, r["instrument"]))
        tp = os.path.join(rd, "trades.csv")
        trades = list(csv.DictReader(open(tp, encoding="utf-8"))) if os.path.exists(tp) else []
        reasons = _exit_reasons(rd)
        now = datetime.now().isoformat(timespec="seconds")
        net_total = 0.0
        for n, t in enumerate(trades, 1):
            f = trade_features(cache, r["instrument"], cfg, r["day"], t["entry_time"], daily)
            entry, exitp, qty = float(t["entry_price"]), float(t["exit_price"]), int(float(t["qty"]))
            pnl = float(t["gross_pnl"])
            net = float(t.get("net_pnl") or pnl)
            net_total += net
            mfe, mae = _excursion(cache, t["symbol"], r["day"], t["entry_time"], t["exit_time"], entry, qty)
            row = {"run_id": r["id"], "strategy_id": r["strategy_id"], "slug": r["slug"], "instrument": r["instrument"], "day": r["day"],
                   "trade_no": n, "side": "CE" if t["symbol"].endswith("CE") else "PE", "symbol": t["symbol"], "qty": qty,
                   "entry_time": t["entry_time"], "exit_time": t["exit_time"], "entry_price": entry, "exit_price": exitp,
                   "pnl": pnl, "net": net, "win": 1 if net > 0 else 0, "hold_min": _mins(t["entry_time"], t["exit_time"]),
                   "exit_reason": reasons.get((t["symbol"], t["exit_time"][:5])) or reasons.get((t["symbol"], _plus1(t["exit_time"]))),
                   "opt_mfe": mfe, "opt_mae": mae, "dte": r.get("dte"), "nearest": r.get("nearest", 1),
                   "code_hash": r.get("code_hash"), "built": now, **f}
            cols = ",".join(row)
            con.execute(f"INSERT OR REPLACE INTO journal_trades({cols}) VALUES({','.join('?' * len(row))})", list(row.values()))
            added_trades += 1
        con.execute("INSERT OR REPLACE INTO journal_days VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], r["strategy_id"], r["slug"], r["instrument"], r["day"], len(trades), r["gross_pnl"], round(net_total, 2),
                     *_day_context(cache, cfg, r["day"], daily), r.get("dte"), r.get("nearest", 1), now))
        added_runs += 1
    con.commit()
    out = {"runs_added": added_runs, "trades_added": added_trades, "stale_removed": len(stale),
           "trades_total": con.execute("SELECT COUNT(*) FROM journal_trades").fetchone()[0],
           "days_total": con.execute("SELECT COUNT(*) FROM journal_days").fetchone()[0]}
    if own:
        con.close()
    if not quiet:
        print(f"journal: {out['runs_added']} runs / {out['trades_added']} trades added, {out['stale_removed']} superseded runs removed; "
              f"now {out['trades_total']} trades over {out['days_total']} strategy-days")
    return out


def _plus1(t):
    return (datetime.strptime(t[:5], "%H:%M") + timedelta(minutes=1)).strftime("%H:%M")


def _day_context(cache, cfg, day, daily):
    u = _load(cache, cfg["index_symbol"], day)
    v = _load(cache, "INDIAVIX", day)
    rng = chg = gap = vo = vc = vr = None
    if u is not None and len(u):
        o, c, hi, lo = float(u["open"].iloc[0]), float(u["close"].iloc[-1]), float(u["high"].max()), float(u["low"].min())
        rng, chg = round((hi - lo) / o * 100, 4), _pct(c, o)
        if daily is not None and len(daily):
            prev = daily[daily.index < pd.Timestamp(date.fromisoformat(day))]
            if len(prev):
                gap = _pct(o, float(prev["close"].iloc[-1]))
    if v is not None and len(v):
        vo, vc, vr = float(v["open"].iloc[0]), float(v["close"].iloc[-1]), round(float(v["high"].max() - v["low"].min()), 4)
    return rng, chg, gap, vo, vc, vr


# ---------------- query ----------------
def _wilson(wins, n, z=1.96):
    if not n:
        return None, None
    p = wins / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round((c - h) * 100, 1), round((c + h) * 100, 1)


def stats(rows):
    n = len(rows)
    if not n:
        return {"trades": 0}
    nets = [r["net"] or 0 for r in rows]
    w = sum(1 for x in nets if x > 0)
    gp, gl = sum(x for x in nets if x > 0), -sum(x for x in nets if x < 0)
    lo, hi = _wilson(w, n)
    return {"trades": n, "days": len({(r["slug"], r["instrument"], r["day"]) for r in rows}), "win_rate": round(w / n * 100, 1),
            "win_rate_95": [lo, hi], "avg_net": round(sum(nets) / n, 1), "total_net": round(sum(nets), 1),
            "profit_factor": round(gp / gl, 2) if gl else None, "avg_hold_min": round(sum(r["hold_min"] or 0 for r in rows) / n, 1),
            "low_sample": n < 30}


def select(con, strategy=None, instrument=None, side=None, time_from=None, time_to=None, include_far=False, **ranges):
    ensure(con)
    q, a = "SELECT * FROM journal_trades WHERE 1=1", []
    if strategy:
        q += " AND slug=?"; a.append(strategy)
    if instrument:
        q += " AND instrument=?"; a.append(instrument.upper())
    if side:
        q += " AND side=?"; a.append(side.upper())
    if time_from:
        h, m = map(int, time_from.split(":")); q += " AND minute_of_day >= ?"; a.append(h * 60 + m)
    if time_to:
        h, m = map(int, time_to.split(":")); q += " AND minute_of_day < ?"; a.append(h * 60 + m)
    if not include_far:
        q += " AND COALESCE(nearest, 1) = 1"
    for k, (lo, hi) in ranges.items():
        if lo is not None:
            q += f" AND {k} >= ?"; a.append(lo)
        if hi is not None:
            q += f" AND {k} < ?"; a.append(hi)
    return db.rows(con, q, a)


def bucketize(rows, feature):
    edges = BUCKETS.get(feature)
    groups = {}
    for r in rows:
        v = r.get(feature)
        if edges is None:
            key = str(v)
        elif v is None:
            key = "n/a"
        else:
            prev = None
            key = None
            for e in edges:
                if v < e:
                    key = f"< {e}" if prev is None else f"{prev} .. {e}"
                    break
                prev = e
            key = key or f">= {edges[-1]}"
        groups.setdefault(key, []).append(r)
    return {k: stats(v) for k, v in sorted(groups.items(), key=lambda kv: _sort_key(kv[0]))}


def _sort_key(k):
    if k.startswith("< "):
        return (0, -1e18)
    if k.startswith(">= "):
        return (0, 1e18)
    m = re.match(r"(-?[\d.]+)", k)
    return (0, float(m.group(1))) if m else (1, k)


def update_quietly(con=None):
    """Journal the runs added since the last build; never raises (called after jobs and syncs)."""
    try:
        return build(con, quiet=True)
    except Exception as e:                                   # the journal must never break a test run
        print(f"(journal update skipped: {e})")
        return None


def similar(con, k=15, strategy=None, instrument=None, side=None, **ctx):
    """The k past trades whose entry context is closest to `ctx` (feature -> value), scaled by each feature's spread."""
    rows = select(con, strategy, instrument, side)
    feats = [f for f, v in ctx.items() if v is not None and f in FEATURES]
    if not rows or not feats:
        return [], feats
    spread = {}
    for f in feats:
        vals = [r[f] for r in rows if r.get(f) is not None]
        mu = sum(vals) / len(vals) if vals else 0
        spread[f] = (sum((x - mu) ** 2 for x in vals) / len(vals)) ** 0.5 if vals else 1
    def dist(r):
        s = 0.0
        for f in feats:
            if r.get(f) is None:
                s += 4.0                                     # unknown feature counts as far
            else:
                s += ((r[f] - ctx[f]) / (spread[f] or 1)) ** 2
        return s ** 0.5
    return sorted(rows, key=dist)[:k], feats


# ---------------- CLI ----------------
def _print_stats(label, s):
    if not s.get("trades"):
        print(f"{label:28} no trades")
        return
    lo, hi = s["win_rate_95"]
    print(f"{label:28} trades {s['trades']:>4}  days {s['days']:>3}  win {s['win_rate']:>5.1f}% (95%: {lo}-{hi})  "
          f"avg net {s['avg_net']:>8.1f}  total {s['total_net']:>9.1f}  PF {s['profit_factor'] if s['profit_factor'] is not None else '-':>5}"
          f"  hold {s['avg_hold_min']:>5.1f}m" + ("  LOW SAMPLE" if s["low_sample"] else ""))


def _range_args(a):
    out = {}
    for f in ("vix", "vix_pos", "vix_chg15", "vix_chg30", "u_day_pos", "u_from_open_pct", "u_ret30_pct", "atr5_pct", "dte", "gap_pct"):
        lo, hi = getattr(a, f + "_min", None), getattr(a, f + "_max", None)
        if lo is not None or hi is not None:
            out[f] = (lo, hi)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="script-openalgo journal", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="add journal rows for new runs")
    b.add_argument("--rebuild", action="store_true", help="rebuild the whole journal")
    for name in ("query", "similar", "export"):
        p = sub.add_parser(name)
        p.add_argument("--strategy"); p.add_argument("--instrument"); p.add_argument("--side", choices=["CE", "PE"])
        p.add_argument("--json", action="store_true", help="machine-readable output")
        if name == "query":
            p.add_argument("--from", dest="time_from", help="HH:MM entry from"); p.add_argument("--to", dest="time_to", help="HH:MM entry before")
            p.add_argument("--include-far", action="store_true", help="also far-expiry days")
            p.add_argument("--by", choices=sorted(BUCKETS), help="break down by this feature")
            for f in ("vix", "vix_pos", "vix_chg15", "vix_chg30", "u_day_pos", "u_from_open_pct", "u_ret30_pct", "atr5_pct", "dte", "gap_pct"):
                p.add_argument(f"--{f.replace('_', '-')}-min", dest=f + "_min", type=float)
                p.add_argument(f"--{f.replace('_', '-')}-max", dest=f + "_max", type=float)
        if name == "similar":
            p.add_argument("-k", type=int, default=15)
            p.add_argument("--time", help="HH:MM of the signal")
            for f in FEATURES:
                if f != "minute_of_day":
                    p.add_argument(f"--{f.replace('_', '-')}", dest=f, type=float)
        if name == "export":
            p.add_argument("--out", default="journal_trades.csv")
    a = ap.parse_args(argv)
    con = db.connect()
    ensure(con)
    if a.cmd == "build":
        build(con, rebuild=a.rebuild)
    elif a.cmd == "query":
        rows = select(con, a.strategy, a.instrument, a.side, a.time_from, a.time_to, a.include_far, **_range_args(a))
        s = stats(rows)
        res = {"filters": {k: v for k, v in vars(a).items() if v not in (None, False) and k not in ("cmd", "json", "by")}, "all": s}
        if a.by:
            res["by"] = {"feature": a.by, "buckets": bucketize(rows, a.by)}
        if a.json:
            print(json.dumps(res, indent=1, default=str))
        else:
            _print_stats("ALL matching", s)
            for k, v in (res.get("by") or {}).get("buckets", {}).items():
                _print_stats(f"  {a.by} {k}", v)
            print("net = after the estimated spread (stocks); gross for indices. Backtests on cached data, paper fills. "
                  "95% = Wilson interval of the win rate; under 30 trades the numbers are LOW SAMPLE.")
    elif a.cmd == "similar":
        ctx = {f: getattr(a, f, None) for f in FEATURES}
        if a.time:
            h, m = map(int, a.time.split(":")); ctx["minute_of_day"] = h * 60 + m
        rows, feats = similar(con, a.k, a.strategy, a.instrument, a.side, **ctx)
        s = stats(rows)
        if a.json:
            print(json.dumps({"matched_on": feats, "stats": s, "trades": rows}, indent=1, default=str))
        else:
            print(f"matched on: {', '.join(feats) or 'nothing (give --time / --vix-pos / ...)'}")
            _print_stats(f"{len(rows)} most similar", s)
            for r in rows:
                print(f"  {r['day']} {r['entry_time'][:5]} {r['slug'][:24]:24} {r['instrument']:9} {r['side']} net {r['net']:>8.1f}  "
                      f"vix {r['vix']} pos {r['vix_pos']} | day pos {r['u_day_pos']} | {r['exit_reason'] or ''}")
    elif a.cmd == "export":
        rows = select(con, a.strategy, a.instrument, a.side, include_far=True)
        if rows:
            with open(a.out, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(rows[0]))
                w.writeheader(); w.writerows(rows)
        print(f"{len(rows)} trades -> {a.out}")
    con.close()


if __name__ == "__main__":
    main()
