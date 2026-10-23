"""Trade journal: every backtested trade with the market context at its entry, for later decisions and models.

    script-openalgo journal build [--rebuild]               # journal new runs; recompute rows whose feature version is old
    script-openalgo journal features                        # list the registered features
    script-openalgo journal query [filters] [--by FEATURE]  # win rate / P&L of matching trades, optionally per bucket
    script-openalgo journal similar --strategy S --time 11:30 --feature vix_pos=80 ...   # nearest past trades
    script-openalgo journal edges [--strategy S]            # candidate conditions, with multiple-comparison control
    script-openalgo journal export [--out FILE.csv]         # all trades with all features as CSV (model training data)

Source: the CURRENT run of each (strategy, instrument, day): the newest run made with the strategy's default settings (the
runs the strategy stats use). Features use only data completed before the entry minute (no lookahead); outcome columns
(pnl, net, win, hold_min, exit_reason, opt_mfe, opt_mae) describe what happened after entry.

Features are PLUGGABLE: a group is a function registered with @feature_group(name, version, {feature: description}).
Its values are stored in the `features` JSON column, and every row records the FEATURE_VERSION it was built with, so
adding or changing a group makes `journal build` recompute the history from the cached candles. To add one: write a
function taking a Ctx and returning {feature: value}, decorate it, bump its version when its meaning changes.
"""
import argparse
import csv
import hashlib
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
  pnl REAL, net REAL, win INTEGER, hold_min REAL, exit_reason TEXT, opt_mfe REAL, opt_mae REAL, dte INTEGER, nearest INTEGER,
  code_hash TEXT, features TEXT, feature_version TEXT, built TEXT, UNIQUE(run_id, trade_no));
CREATE INDEX IF NOT EXISTS jt_strategy ON journal_trades(slug, instrument);
CREATE TABLE IF NOT EXISTS journal_days(
  run_id INTEGER PRIMARY KEY, strategy_id INTEGER, slug TEXT, instrument TEXT, day TEXT, trades INTEGER, pnl REAL, net REAL,
  day_range_pct REAL, day_change_pct REAL, gap_pct REAL, vix_open REAL, vix_close REAL, vix_range REAL, dte INTEGER, nearest INTEGER,
  feature_version TEXT, built TEXT);
"""
OUTCOME = ["pnl", "net", "win", "hold_min", "exit_reason", "opt_mfe", "opt_mae"]


# ---------------- feature registry ----------------
GROUPS = []          # [(name, version, {feature: description}, function)]


def feature_group(name, version, features):
    def deco(fn):
        GROUPS.append((name, version, features, fn))
        return fn
    return deco


def feature_names():
    return [f for _, _, feats, _ in GROUPS for f in feats]


def feature_version():
    sig = ";".join(f"{n}:{v}:{','.join(f)}" for n, v, f, _ in GROUPS)
    return hashlib.sha1(sig.encode()).hexdigest()[:10]


class Ctx:
    """What a feature function may look at: candles of `day` up to (not including) the entry minute, and the trade."""

    def __init__(self, cache, instrument, cfg, day, entry_time, trade, run, loader):
        self.cache, self.instrument, self.cfg, self.day = cache, instrument, cfg, day
        self.d = date.fromisoformat(day)
        self.ts = pd.Timestamp(f"{day} {entry_time[:5]}")
        self.trade, self.run, self._load = trade, run, loader

    def series(self, name):
        return self._load(name, self.day)

    def upto(self, name):
        """Completed 1m candles of `name` before the entry minute (a candle stamped T is complete at T+1 min)."""
        df = self.series(name)
        return df.loc[: self.ts - pd.Timedelta(minutes=1)] if df is not None else None

    def daily(self):
        return self._load("daily_" + self.instrument.upper(), None)

    def manifest(self):
        return self._load("manifest", self.day, instrument=self.instrument)

    def option(self, strike, ot):
        from script_openalgo.backtest_core import data_loader as dl
        m = self.manifest() or {}
        if not m.get("expiry"):
            return None
        return dl.option_symbol(self.instrument, date.fromisoformat(m["expiry"]), strike, ot)


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


def _rel15(sub, col="volume"):
    """Last 15 minutes of `col` against the average 15-minute amount so far today (needs 45+ minutes)."""
    if sub is None or len(sub) < 45 or col not in sub:
        return None
    v = sub[col].astype(float)
    last = float(v.iloc[-15:].sum())
    avg = float(v.sum()) / len(v) * 15
    return round(last / avg, 3) if avg > 0 else None


@feature_group("time", 1, {"minute_of_day": "minutes since midnight at entry (09:15 = 555)", "weekday": "0 = Monday",
                           "dte": "days to expiry of the traded contract"})
def f_time(c):
    return {"minute_of_day": c.ts.hour * 60 + c.ts.minute, "weekday": c.d.weekday(), "dte": c.run.get("dte")}


@feature_group("underlying", 1, {
    "u_price": "underlying price at entry", "u_from_open_pct": "% move since the open",
    "u_day_pos": "position in the day's range so far (0 = low, 100 = high)", "u_ret15_pct": "% move, last 15 min",
    "u_ret30_pct": "% move, last 30 min", "u_ret60_pct": "% move, last 60 min",
    "atr5_pct": "ATR(14) of 5m candles, % of price", "gap_pct": "open vs previous close, %",
    "prev_range_pct": "previous day's high-low range, % of its close"})
def f_underlying(c):
    sub = c.upto(c.cfg["index_symbol"])
    out = {}
    if sub is None or not len(sub):
        return out
    price, opn = float(sub["close"].iloc[-1]), float(sub["open"].iloc[0])
    hi, lo = float(sub["high"].max()), float(sub["low"].min())
    out.update({"u_price": price, "u_from_open_pct": _pct(price, opn),
                "u_day_pos": round((price - lo) / (hi - lo) * 100, 2) if hi > lo else None,
                "u_ret15_pct": _pct(price, _close_ago(sub, c.ts, 15)), "u_ret30_pct": _pct(price, _close_ago(sub, c.ts, 30)),
                "u_ret60_pct": _pct(price, _close_ago(sub, c.ts, 60))})
    a = _atr5(sub)
    out["atr5_pct"] = round(a / price * 100, 4) if a else None
    dly = c.daily()
    if dly is not None and len(dly):
        prev = dly[dly.index < pd.Timestamp(c.d)]
        if len(prev):
            pc, ph, pl = float(prev["close"].iloc[-1]), float(prev["high"].iloc[-1]), float(prev["low"].iloc[-1])
            out["gap_pct"] = _pct(opn, pc)
            out["prev_range_pct"] = round((ph - pl) / pc * 100, 4) if pc else None
    return out


@feature_group("vix", 1, {"vix": "India VIX at entry", "vix_pos": "VIX position in its range so far today (0-100)",
                          "vix_chg15": "VIX change, last 15 min (points)", "vix_chg30": "VIX change, last 30 min (points)",
                          "vix_from_open": "VIX change since the open (points)"})
def f_vix(c):
    v = c.upto("INDIAVIX")
    if v is None or not len(v):
        return {}
    last, vo = float(v["close"].iloc[-1]), float(v["open"].iloc[0])
    vh, vl = float(v["high"].max()), float(v["low"].min())
    c15, c30 = _close_ago(v, c.ts, 15), _close_ago(v, c.ts, 30)
    return {"vix": last, "vix_pos": round((last - vl) / (vh - vl) * 100, 2) if vh - vl >= 0.1 else None,
            "vix_chg15": round(last - c15, 4) if c15 else None, "vix_chg30": round(last - c30, 4) if c30 else None,
            "vix_from_open": round(last - vo, 4)}


@feature_group("volume", 1, {"u_vol_rel15": "underlying volume, last 15 min vs its average 15 min so far today",
                             "opt_vol_rel15": "traded option's volume, last 15 min vs its average so far today",
                             "opt_vol15": "traded option's volume in the last 15 min (units)"})
def f_volume(c):
    out = {"u_vol_rel15": _rel15(c.upto(c.cfg["index_symbol"]))}
    o = c.upto(c.trade["symbol"])
    if o is not None and len(o):
        out["opt_vol_rel15"] = _rel15(o)
        out["opt_vol15"] = float(o["volume"].iloc[-15:].sum())
    return out


def _atm(c, price):
    m = c.manifest() or {}
    ks = [k for k in m.get("strikes", []) if c.series(c.option(k, "CE")) is not None and c.series(c.option(k, "PE")) is not None]
    return min(ks, key=lambda k: abs(k - price)) if ks else None


def _last(df, col="close"):
    return float(df[col].iloc[-1]) if df is not None and len(df) else None


@feature_group("oi", 1, {"pcr_oi": "put/call ratio of open interest, strikes within 3 steps of ATM",
                         "atm_ce_oi_chg30_pct": "ATM call OI change, last 30 min, %", "atm_pe_oi_chg30_pct": "ATM put OI change, last 30 min, %",
                         "opt_oi_chg30_pct": "traded option's OI change, last 30 min, %"})
def f_oi(c):
    sub = c.upto(c.cfg["index_symbol"])
    price = _last(sub)
    if price is None:
        return {}
    m = c.manifest() or {}
    strikes = sorted(m.get("strikes", []))
    atm = _atm(c, price)
    out = {}
    if atm is not None:
        i = strikes.index(atm)
        near = strikes[max(0, i - 3): i + 4]
        ce = sum(_last(c.upto(c.option(k, "CE")), "oi") or 0 for k in near)
        pe = sum(_last(c.upto(c.option(k, "PE")), "oi") or 0 for k in near)
        out["pcr_oi"] = round(pe / ce, 4) if ce else None
        for ot in ("CE", "PE"):
            df = c.upto(c.option(atm, ot))
            if df is not None and len(df) > 31:
                a, b = float(df["oi"].iloc[-1]), float(df["oi"].iloc[-31])
                out[f"atm_{ot.lower()}_oi_chg30_pct"] = _pct(a, b) if b else None
    o = c.upto(c.trade["symbol"])
    if o is not None and len(o) > 31 and float(o["oi"].iloc[-31]):
        out["opt_oi_chg30_pct"] = _pct(float(o["oi"].iloc[-1]), float(o["oi"].iloc[-31]))
    return out


@feature_group("premium", 1, {"opt_prem_pct": "traded option's price before entry, % of the underlying",
                              "straddle_pct": "ATM call + put price, % of the underlying (implied-volatility proxy)",
                              "opt_ret15_pct": "traded option's % change, last 15 min"})
def f_premium(c):
    price = _last(c.upto(c.cfg["index_symbol"]))
    if price is None:
        return {}
    out = {}
    o = c.upto(c.trade["symbol"])
    if o is not None and len(o):
        p = float(o["close"].iloc[-1])
        out["opt_prem_pct"] = round(p / price * 100, 4)
        out["opt_ret15_pct"] = _pct(p, _close_ago(o, c.ts, 15))
    atm = _atm(c, price)
    if atm is not None:
        ce, pe = _last(c.upto(c.option(atm, "CE"))), _last(c.upto(c.option(atm, "PE")))
        if ce and pe:
            out["straddle_pct"] = round((ce + pe) / price * 100, 4)
    return out


# ---------------- data loading (one build reads each file once) ----------------
def make_loader(cache):
    memo = {}

    def load(name, day, instrument=None):
        key = (name, day, instrument)
        if key in memo:
            return memo[key]
        val = None
        if name == "manifest":
            p = os.path.join(cache, f"manifest_{instrument.upper()}_{day}.json")
            val = json.load(open(p)) if os.path.exists(p) else None
        elif name and name.startswith("daily_"):
            p = os.path.join(cache, f"{name}.parquet")
            val = pd.read_parquet(p) if os.path.exists(p) else None
        elif name:
            p = os.path.join(cache, f"{name}_{day}.parquet")
            if os.path.exists(p):
                df = pd.read_parquet(p)
                if "dt" in df.columns:
                    df = df.set_index("dt")
                df.index = pd.to_datetime(df.index)
                val = df.sort_index()
        if len(memo) > 4000:                                 # keep memory bounded on big rebuilds
            memo.clear()
        memo[key] = val
        return val
    return load


def compute_features(ctx):
    out = {}
    for name, _, _, fn in GROUPS:
        try:
            out.update({k: v for k, v in (fn(ctx) or {}).items() if v is not None and not (isinstance(v, float) and math.isnan(v))})
        except Exception as e:                               # one broken feature must not stop the journal
            out[f"_error_{name}"] = str(e)[:120]
    return out


# ---------------- build ----------------
def ensure(con):
    cols = {r[1] for r in con.execute("PRAGMA table_info(journal_trades)")}
    if cols and "features" not in cols:                      # v1 layout (one column per feature): derived data, rebuild
        con.execute("DROP TABLE journal_trades")
        con.execute("DROP TABLE IF EXISTS journal_days")
    con.executescript(SCHEMA)


def current_runs(con):
    """Newest default-settings run per (strategy, instrument, day, executor): the runs the stats use."""
    return db.rows(con, "SELECT r.*, s.slug FROM runs r JOIN strategies s ON s.id=r.strategy_id WHERE r.id IN ("
                        "SELECT MAX(id) FROM runs WHERE COALESCE(params, '') IN ('', '{}') GROUP BY strategy_id, instrument, day, executor)")


def _exit_reasons(result_dir):
    """{(symbol, HH:MM): reason} from the run's log (SELL lines, or a separate EXIT line in the same minutes)."""
    out, sells, exits = {}, [], {}
    p = os.path.join(result_dir, "log.txt")
    if not os.path.exists(p):
        return out
    clean = lambda s: re.sub(r"\s*\(.*?\)", "", s).strip()[:80]
    for line in open(p, encoding="utf-8", errors="replace"):
        line = line.strip()
        m = re.match(r"\S+ (\d\d:\d\d):\d\d \| SELL (\S+) .*?\| order \S+ \| (.+)$", line)
        if m:
            out.setdefault((m.group(2), m.group(1)), clean(m.group(3)))
            continue
        m = re.match(r"\S+ (\d\d:\d\d):\d\d \| SELL (\S+) ", line)
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
    return round((datetime.strptime(b[:8], "%H:%M:%S") - datetime.strptime(a[:8], "%H:%M:%S")).total_seconds() / 60, 1)


def _plus1(t):
    return (datetime.strptime(t[:5], "%H:%M") + timedelta(minutes=1)).strftime("%H:%M")


def _excursion(load, symbol, day, t_in, t_out, entry, qty):
    o = load(symbol, day)
    if o is None:
        return None, None
    seg = o.loc[pd.Timestamp(f"{day} {t_in[:5]}"): pd.Timestamp(f"{day} {t_out[:5]}")]
    if not len(seg):
        return None, None
    return round((float(seg["high"].max()) - entry) * qty, 2), round((float(seg["low"].min()) - entry) * qty, 2)


def _day_context(load, cfg, instrument, day):
    u, v = load(cfg["index_symbol"], day), load("INDIAVIX", day)
    rng = chg = gap = vo = vc = vr = None
    if u is not None and len(u):
        o, cl, hi, lo = float(u["open"].iloc[0]), float(u["close"].iloc[-1]), float(u["high"].max()), float(u["low"].min())
        rng, chg = round((hi - lo) / o * 100, 4), _pct(cl, o)
        dly = load("daily_" + instrument.upper(), None)
        if dly is not None and len(dly):
            prev = dly[dly.index < pd.Timestamp(date.fromisoformat(day))]
            if len(prev):
                gap = _pct(o, float(prev["close"].iloc[-1]))
    if v is not None and len(v):
        vo, vc, vr = float(v["open"].iloc[0]), float(v["close"].iloc[-1]), round(float(v["high"].max() - v["low"].min()), 4)
    return rng, chg, gap, vo, vc, vr


def build(con=None, rebuild=False, cache=None, quiet=False):
    """Journal current runs that are new or were built with an older feature version; drop superseded runs."""
    from script_openalgo.backtest_core import instruments
    own = con is None
    con = con or db.connect()
    ensure(con)
    cache = cache or db.ws_paths()[1]
    fv = feature_version()
    if rebuild:
        con.execute("DELETE FROM journal_trades")
        con.execute("DELETE FROM journal_days")
    runs = current_runs(con)
    keep = {r["id"] for r in runs}
    have = {r[0]: r[1] for r in con.execute("SELECT run_id, feature_version FROM journal_days")}
    stale = [rid for rid in have if rid not in keep]
    for rid in stale:
        con.execute("DELETE FROM journal_trades WHERE run_id=?", (rid,))
        con.execute("DELETE FROM journal_days WHERE run_id=?", (rid,))
    load = make_loader(cache)
    added_runs = added_trades = refreshed = 0
    for r in runs:
        if have.get(r["id"]) == fv:
            continue
        rd = r["result_dir"]
        if not rd or not os.path.isdir(rd) or r["instrument"] not in instruments.INSTRUMENTS:
            continue
        if r["id"] in have:
            refreshed += 1
            con.execute("DELETE FROM journal_trades WHERE run_id=?", (r["id"],))
        cfg = instruments.get(r["instrument"])
        tp = os.path.join(rd, "trades.csv")
        trades = list(csv.DictReader(open(tp, encoding="utf-8"))) if os.path.exists(tp) else []
        reasons = _exit_reasons(rd)
        now = datetime.now().isoformat(timespec="seconds")
        net_total = 0.0
        for n, t in enumerate(trades, 1):
            ctx = Ctx(cache, r["instrument"], cfg, r["day"], t["entry_time"], t, r, load)
            feats = compute_features(ctx)
            entry, exitp, qty = float(t["entry_price"]), float(t["exit_price"]), int(float(t["qty"]))
            pnl = float(t["gross_pnl"])
            net = float(t.get("net_pnl") or pnl)
            net_total += net
            mfe, mae = _excursion(load, t["symbol"], r["day"], t["entry_time"], t["exit_time"], entry, qty)
            row = {"run_id": r["id"], "strategy_id": r["strategy_id"], "slug": r["slug"], "instrument": r["instrument"], "day": r["day"],
                   "trade_no": n, "side": "CE" if t["symbol"].endswith("CE") else "PE", "symbol": t["symbol"], "qty": qty,
                   "entry_time": t["entry_time"], "exit_time": t["exit_time"], "entry_price": entry, "exit_price": exitp,
                   "pnl": pnl, "net": net, "win": 1 if net > 0 else 0, "hold_min": _mins(t["entry_time"], t["exit_time"]),
                   "exit_reason": reasons.get((t["symbol"], t["exit_time"][:5])) or reasons.get((t["symbol"], _plus1(t["exit_time"]))),
                   "opt_mfe": mfe, "opt_mae": mae, "dte": r.get("dte"), "nearest": r.get("nearest", 1), "code_hash": r.get("code_hash"),
                   "features": json.dumps(feats, sort_keys=True), "feature_version": fv, "built": now}
            con.execute(f"INSERT OR REPLACE INTO journal_trades({','.join(row)}) VALUES({','.join('?' * len(row))})", list(row.values()))
            added_trades += 1
        con.execute("INSERT OR REPLACE INTO journal_days VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], r["strategy_id"], r["slug"], r["instrument"], r["day"], len(trades), r["gross_pnl"], round(net_total, 2),
                     *_day_context(load, cfg, r["instrument"], r["day"]), r.get("dte"), r.get("nearest", 1), fv, now))
        added_runs += 1
    con.commit()
    out = {"runs_added": added_runs - refreshed, "runs_refreshed": refreshed, "trades_added": added_trades, "stale_removed": len(stale),
           "trades_total": con.execute("SELECT COUNT(*) FROM journal_trades").fetchone()[0],
           "days_total": con.execute("SELECT COUNT(*) FROM journal_days").fetchone()[0], "feature_version": fv}
    if own:
        con.close()
    if not quiet:
        print(f"journal: {out['runs_added']} new runs, {out['runs_refreshed']} recomputed (feature version {fv}), {out['trades_added']} trades "
              f"written, {out['stale_removed']} superseded runs removed; now {out['trades_total']} trades over {out['days_total']} strategy-days")
    return out


def update_quietly(con=None):
    """Journal the runs added since the last build; never raises (called after jobs and syncs)."""
    try:
        return build(con, quiet=True)
    except Exception as e:                                   # the journal must never break a test run
        print(f"(journal update skipped: {e})")
        return None


# ---------------- reading ----------------
def load_rows(con, strategy=None, instrument=None, side=None, include_far=False):
    ensure(con)
    q, a = "SELECT * FROM journal_trades WHERE 1=1", []
    for col, val in (("slug", strategy), ("instrument", instrument.upper() if instrument else None), ("side", side.upper() if side else None)):
        if val:
            q += f" AND {col}=?"
            a.append(val)
    if not include_far:
        q += " AND COALESCE(nearest, 1) = 1"
    rows = []
    for r in db.rows(con, q, a):
        f = json.loads(r.pop("features") or "{}")
        rows.append({**f, **r})
    return rows


FILTER_RE = re.compile(r"^\s*([a-z_0-9]+)\s*(<=|>=|!=|=|<|>)\s*(.+?)\s*$")


def parse_filter(text):
    m = FILTER_RE.match(text)
    if not m:
        raise SystemExit(f"bad filter {text!r}: use e.g. vix_pos<80 or side=PE")
    k, op, v = m.groups()
    try:
        v = float(v)
    except ValueError:
        pass
    return k, op, v


def apply_filters(rows, filters):
    ops = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
           "=": lambda a, b: a == b, "!=": lambda a, b: a != b}
    out = []
    for r in rows:
        ok = True
        for k, op, v in filters:
            x = r.get(k)
            if x is None or (isinstance(v, float) and not isinstance(x, (int, float))):
                ok = False
                break
            if not ops[op](x, v):
                ok = False
                break
        if ok:
            out.append(r)
    return out


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


def quantile_edges(values, k):
    v = sorted(values)
    return [v[int(len(v) * i / k)] for i in range(1, k)] if len(v) >= k else []


def bucketize(rows, feature, k=4):
    vals = [r.get(feature) for r in rows if isinstance(r.get(feature), (int, float))]
    groups = {}
    if vals and len(vals) >= len([r for r in rows if r.get(feature) is not None]) * 0.9 and len(set(vals)) > k:
        edges = quantile_edges(vals, k)
        labels = [f"< {edges[0]:g}"] + [f"{a:g} .. {b:g}" for a, b in zip(edges, edges[1:])] + [f">= {edges[-1]:g}"]
        for r in rows:
            x = r.get(feature)
            if not isinstance(x, (int, float)):
                groups.setdefault("n/a", []).append(r)
                continue
            i = sum(1 for e in edges if x >= e)
            groups.setdefault((i, labels[i]), []).append(r)
        return {lab[1] if isinstance(lab, tuple) else lab: stats(g) for lab, g in sorted(groups.items(), key=lambda kv: kv[0] if isinstance(kv[0], tuple) else (99, "")) }
    for r in rows:
        groups.setdefault(str(r.get(feature)), []).append(r)
    return {k2: stats(g) for k2, g in sorted(groups.items())}


def similar(rows, ctx, k=15):
    """The k trades whose features are closest to `ctx` (feature -> value), each feature scaled by its spread."""
    feats = [f for f, v in ctx.items() if isinstance(v, (int, float))]
    if not rows or not feats:
        return [], feats
    spread = {}
    for f in feats:
        vals = [r[f] for r in rows if isinstance(r.get(f), (int, float))]
        mu = sum(vals) / len(vals) if vals else 0
        spread[f] = (sum((x - mu) ** 2 for x in vals) / len(vals)) ** 0.5 if vals else 1
    def dist(r):
        return sum(4.0 if not isinstance(r.get(f), (int, float)) else ((r[f] - ctx[f]) / (spread[f] or 1)) ** 2 for f in feats) ** 0.5
    return sorted(rows, key=dist)[:k], feats


# ---------------- edge report ----------------
def _bh(pvals):
    """Benjamini-Hochberg q-values (false discovery rate) for a list of p-values."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [0.0] * m
    prev = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        prev = min(prev, pvals[i] * m / rank)
        q[i] = prev
    return q


def edges(rows, min_trades=8, min_days=5, perms=2000, group_by=("slug", "instrument"), seed=7):
    """For every (strategy, instrument) and feature: split the trades into thirds by the feature and compare each third with
    the other trades of that strategy (average net, shuffle test; win rate shown too). All p-values together get
    Benjamini-Hochberg q-values. Returns (tests, summary)."""
    import numpy as np
    rng = np.random.default_rng(seed)
    groups = {}
    for r in rows:
        groups.setdefault(tuple(r[g] for g in group_by), []).append(r)
    tests = []
    names = feature_names()
    for key, g in groups.items():
        if len(g) < 2 * min_trades:
            continue
        nets_all = np.array([x["net"] or 0 for x in g], dtype=float)
        for f in names:
            vals = [(i, x.get(f)) for i, x in enumerate(g) if isinstance(x.get(f), (int, float))]
            if len(vals) < 2 * min_trades or len({v for _, v in vals}) < 3:
                continue
            e = quantile_edges([v for _, v in vals], 3)
            if len(set(e)) < 2:
                continue
            for b, (lo, hi, label) in enumerate([(None, e[0], f"{f} < {e[0]:g}"), (e[0], e[1], f"{e[0]:g} <= {f} < {e[1]:g}"),
                                                 (e[1], None, f"{f} >= {e[1]:g}")]):
                idx = [i for i, v in vals if (lo is None or v >= lo) and (hi is None or v < hi)]
                rest = [i for i, _ in vals if i not in set(idx)]
                days = len({g[i]["day"] for i in idx})
                if len(idx) < min_trades or len(rest) < min_trades or days < min_days:
                    continue
                a, rr = nets_all[idx], nets_all[rest]
                obs = a.mean() - rr.mean()
                pool = np.concatenate([a, rr])
                cnt = 0
                for _ in range(perms):
                    rng.shuffle(pool)
                    if abs(pool[:len(a)].mean() - pool[len(a):].mean()) >= abs(obs) - 1e-9:
                        cnt += 1
                p = (cnt + 1) / (perms + 1)
                wa, wr = float((a > 0).mean() * 100), float((rr > 0).mean() * 100)
                tests.append({"group": dict(zip(group_by, key)), "feature": f, "condition": label, "trades": len(idx), "days": days,
                              "avg_net": round(float(a.mean()), 1), "avg_net_rest": round(float(rr.mean()), 1), "diff": round(float(obs), 1),
                              "win_rate": round(wa, 1), "win_rate_rest": round(wr, 1), "p": round(p, 4),
                              "total_net": round(float(a.sum()), 1)})
    for t, q in zip(tests, _bh([t["p"] for t in tests]) if tests else []):
        t["q"] = round(q, 4)
    m = len(tests)
    summary = {"tests": m, "expected_false_at_p05": round(0.05 * m, 1), "p_below_05": sum(1 for t in tests if t["p"] < 0.05),
               "candidates_q10": sum(1 for t in tests if t["q"] < 0.10), "min_trades": min_trades, "min_days": min_days,
               "trades": len(rows), "days": len({r["day"] for r in rows})}
    return sorted(tests, key=lambda t: (t["q"], t["p"])), summary


def edges_text(tests, summary, top=20):
    s = summary
    lines = ["# Journal edge report", "",
             f"{s['trades']} trades on {s['days']} distinct days. {s['tests']} comparisons were made (each strategy x instrument x feature, "
             f"split in thirds, each third against the other trades; min {s['min_trades']} trades and {s['min_days']} days per third).", "",
             f"**Multiple comparisons:** with {s['tests']} tests, about **{s['expected_false_at_p05']}** would show p < 0.05 by chance alone; "
             f"{s['p_below_05']} did. q = Benjamini-Hochberg false-discovery-rate value: only rows with q < 0.10 are candidates "
             f"({s['candidates_q10']} here). A candidate is a HYPOTHESIS: make it an optional strategy filter (default off) and confirm it "
             "on days it was not found on (multi-day run on newer days) before using it. Trades on the same day are not independent, "
             "so also look at the day count.", "",
             "| q | p | strategy | instrument | condition | trades | days | avg net | others | win % | others |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in tests[:top]:
        flag = " **candidate**" if t["q"] < 0.10 else ""
        g = t["group"]
        lines.append(f"| {t['q']}{flag} | {t['p']} | {g.get('slug', '')} | {g.get('instrument', 'all')} | `{t['condition']}` | {t['trades']} | {t['days']} | "
                     f"{t['avg_net']} | {t['avg_net_rest']} | {t['win_rate']} | {t['win_rate_rest']} |")
    if not tests:
        lines.append("| - | - | not enough trades per strategy yet | | | | | | | | |")
    return "\n".join(lines)


# ---------------- CLI ----------------
def _print_stats(label, s):
    if not s.get("trades"):
        print(f"{label:34} no trades")
        return
    lo, hi = s["win_rate_95"]
    print(f"{label:34} trades {s['trades']:>4}  days {s['days']:>3}  win {s['win_rate']:>5.1f}% (95%: {lo}-{hi})  "
          f"avg net {s['avg_net']:>8.1f}  total {s['total_net']:>9.1f}  PF {s['profit_factor'] if s['profit_factor'] is not None else '-':>5}"
          f"  hold {s['avg_hold_min']:>5.1f}m" + ("  LOW SAMPLE" if s["low_sample"] else ""))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="script-openalgo journal", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="journal new runs, recompute rows with an old feature version")
    b.add_argument("--rebuild", action="store_true", help="rebuild the whole journal")
    sub.add_parser("features", help="list the registered features")
    for name in ("query", "similar", "edges", "export"):
        p = sub.add_parser(name)
        p.add_argument("--strategy"); p.add_argument("--instrument"); p.add_argument("--side", choices=["CE", "PE"])
        p.add_argument("--include-far", action="store_true", help="also far-expiry days")
        p.add_argument("--json", action="store_true", help="machine-readable output")
        p.add_argument("--filter", action="append", default=[], help="e.g. vix_pos<80, minute_of_day>=600, side=PE (repeatable)")
        if name == "query":
            p.add_argument("--by", help="break down by this feature (numeric features: quartiles)")
        if name == "similar":
            p.add_argument("-k", type=int, default=15)
            p.add_argument("--time", help="HH:MM of the signal")
            p.add_argument("--feature", action="append", default=[], help="NAME=value of the current situation (repeatable)")
        if name == "edges":
            p.add_argument("--min-trades", type=int, default=8); p.add_argument("--min-days", type=int, default=5)
            p.add_argument("--pool", action="store_true", help="per strategy over all instruments (instead of per instrument)")
            p.add_argument("--top", type=int, default=20)
        if name == "export":
            p.add_argument("--out", default="journal_trades.csv")
    a = ap.parse_args(argv)
    con = db.connect()
    ensure(con)
    if a.cmd == "build":
        build(con, rebuild=a.rebuild)
        con.close()
        return
    if a.cmd == "features":
        print(f"feature version {feature_version()}")
        for gname, ver, feats, _ in GROUPS:
            print(f"[{gname} v{ver}]")
            for f, d in feats.items():
                print(f"  {f:22} {d}")
        con.close()
        return
    rows = apply_filters(load_rows(con, a.strategy, a.instrument, a.side, a.include_far), [parse_filter(f) for f in a.filter])
    if a.cmd == "query":
        s = stats(rows)
        res = {"all": s, "by": {"feature": a.by, "buckets": bucketize(rows, a.by)} if a.by else None}
        if a.json:
            print(json.dumps(res, indent=1, default=str))
        else:
            _print_stats("ALL matching", s)
            for k, v in (res["by"] or {}).get("buckets", {}).items():
                _print_stats(f"  {a.by} {k}", v)
            print("net = after the estimated spread (stocks); gross for indices. Backtests on cached data, paper fills. "
                  "95% = Wilson interval of the win rate; under 30 trades = LOW SAMPLE.")
    elif a.cmd == "similar":
        ctx = {}
        for f in a.feature:
            k, _, v = parse_filter(f)
            ctx[k] = v
        if a.time:
            h, m = map(int, a.time.split(":")); ctx["minute_of_day"] = h * 60 + m
        top, feats = similar(rows, ctx, a.k)
        s = stats(top)
        if a.json:
            print(json.dumps({"matched_on": feats, "stats": s, "trades": top}, indent=1, default=str))
        else:
            print(f"matched on: {', '.join(feats) or 'nothing (give --time / --feature NAME=value)'}")
            _print_stats(f"{len(top)} most similar", s)
            for r in top:
                print(f"  {r['day']} {r['entry_time'][:5]} {r['slug'][:24]:24} {r['instrument']:9} {r['side']} net {r['net']:>8.1f} | "
                      + " ".join(f"{f}={r.get(f)}" for f in feats) + f" | {r['exit_reason'] or ''}")
    elif a.cmd == "edges":
        tests, summary = edges(rows, a.min_trades, a.min_days, group_by=("slug",) if a.pool else ("slug", "instrument"))
        text = edges_text(tests, summary, a.top)
        from script_openalgo import config
        out_dir = os.path.join(config.results_dir(), "journal")
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        with open(os.path.join(out_dir, f"edges_{stamp}.md"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
        json.dump({"summary": summary, "tests": tests}, open(os.path.join(out_dir, f"edges_{stamp}.json"), "w", encoding="utf-8"), indent=1)
        print(json.dumps({"summary": summary, "tests": tests[:a.top]}, indent=1) if a.json else text)
        if not a.json:
            print(f"\nSaved: results/journal/edges_{stamp}.md (+ .json with all {len(tests)} comparisons)")
    elif a.cmd == "export":
        names = feature_names()
        base = ["run_id", "slug", "instrument", "day", "trade_no", "side", "symbol", "qty", "entry_time", "exit_time", "entry_price",
                "exit_price", *OUTCOME, "dte", "nearest", "code_hash", "feature_version"]
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=base + [n for n in names if n not in base], extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"{len(rows)} trades x {len(base) + len(names)} columns -> {a.out}")
    con.close()


if __name__ == "__main__":
    main()
