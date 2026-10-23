"""Strategy selector: which strategies to run today on the selected instruments, from how they did on days like today.

    script-openalgo selector days [--rebuild]                       # day labels (market_days) for every cached day
    script-openalgo selector pick --instruments NIFTY,BANKNIFTY [--day 2026-10-16] [--stage 1|2] [--max 8]
    script-openalgo selector backtest --instruments NIFTY,BANKNIFTY [--max 8]    # walk-forward: does following the picks pay?

Day labels (table market_days, one row per instrument and day), each known at a fixed time (no lookahead):
  at the open (09:16, stage 1): gap_pct, gap_class (gap-up / gap-down / flat, relative to the instrument's own 20-day
    average daily range), prev_trend, prev_range_class (wide / normal / narrow), prev_close_pos, vix_open, vix_regime
    (high / normal / low vs its 20-day average close), vix_overnight, dte, expiry (expiry / pre-expiry / normal), weekday,
    event tags from strategy_db/events.csv
  at 09:45 (stage 2): open_type (trend-up / trend-down / range over the first 30 minutes), gap_filled (by 09:45)
Success = a strategy-DAY with net > 0 (journal_days); trades of the same day are not independent.
Selection per instrument and strategy uses the most specific condition with enough history (MIN_DAYS days with trades):
  stage 2: gap_class + vix_regime + open_type -> gap_class + open_type -> ...; stage 1: gap_class + vix_regime -> gap_class
  -> vix_regime -> all days. Ranked by the lower end of the Wilson 95% range of the day win rate, then the average daily
  net shrunk toward the strategy's overall average. Up to --max picks in total across the selected instruments.
"""
import argparse
import csv
import json
import math
import os
from datetime import date, datetime, timedelta

import pandas as pd

from script_openalgo.strategy_db import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_days(
  instrument TEXT, day TEXT, data TEXT, built TEXT, PRIMARY KEY(instrument, day));
"""
GAP_K = 0.4            # |gap| >= GAP_K x 20-day average daily range (%) -> gap-up / gap-down
RANGE_WIDE, RANGE_NARROW = 1.3, 0.7
VIX_HIGH, VIX_LOW = 1.10, 0.90   # VIX at the open vs its 20-day average close
TREND_SHARE = 0.6      # first-30-min move >= 60 % of its range -> trend
MIN_DAYS = 8           # days with trades needed before a condition is used
SHRINK = 5             # days of the strategy's overall average mixed into a condition's average
PICK_MIN_LB = 0.35     # a pick needs the Wilson lower bound of its day win rate above this, PICK_MIN_DAYS days ...
PICK_MIN_DAYS = 5
MAX_PICKS = 8          # ... and a positive shrunk average daily net


def ensure(con):
    con.executescript(SCHEMA)


# ---------------- data ----------------
def _cache():
    return db.ws_paths()[1]


def _load(cache, name, day=None):
    p = os.path.join(cache, f"{name}.parquet" if day is None else f"{name}_{day}.parquet")
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)
    if "dt" in df.columns:
        df = df.set_index("dt")
    df.index = pd.to_datetime(df.index)
    return df.sort_index()


def fetch_daily_vix(cache, days=400):
    """Daily India VIX history -> data_cache/daily_INDIAVIX.parquet (merged with what is there)."""
    from script_openalgo.backtest_core import data_loader as dl
    end = date.today()
    code, b = dl._post("/api/v1/history", symbol="INDIAVIX", exchange="NSE_INDEX", interval="D",
                       start_date=(end - timedelta(days=days)).isoformat(), end_date=end.isoformat(), source="api")
    rows = b.get("data") or [] if code == 200 else []
    if not rows:
        return _load(cache, "daily_INDIAVIX")
    df = pd.DataFrame(rows)
    df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True).dt.tz_localize(None).dt.normalize()
    df = df.set_index("dt").sort_index()[["open", "high", "low", "close"]]
    old = _load(cache, "daily_INDIAVIX")
    if old is not None and len(old):
        df = pd.concat([old[~old.index.isin(df.index)], df]).sort_index()
    df.to_parquet(os.path.join(cache, "daily_INDIAVIX.parquet"))
    return df


def load_events(path=None):
    """strategy_db/events.csv: date,instrument,event[,source]  (instrument * = all; events such as results, rbi_policy)."""
    path = path or os.path.join(os.path.dirname(db.ws_paths()[0]), "events.csv")
    out = []
    if os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8")):
            if r.get("date") and r.get("event"):
                out.append({"date": r["date"].strip(), "instrument": (r.get("instrument") or "*").strip().upper(),
                            "event": r["event"].strip().lower(), "source": (r.get("source") or "").strip()})
    return out


def event_tags(events, instrument, day):
    d = date.fromisoformat(day)
    tags = set()
    for e in events:
        if e["instrument"] not in ("*", instrument.upper()):
            continue
        try:
            ed = date.fromisoformat(e["date"])
        except ValueError:
            continue
        delta = (ed - d).days
        if delta == 0:
            tags.add(f"{e['event']}_today")
        elif delta == 1 or (delta in (2, 3) and d.weekday() == 4):     # next trading day (Friday -> Monday)
            tags.add(f"{e['event']}_next")
        elif delta == -1 or (delta in (-2, -3) and d.weekday() == 0):
            tags.add(f"{e['event']}_prev")
    return sorted(tags)


# ---------------- day labels ----------------
def day_labels(cache, instrument, day, intraday=None, vix_intraday=None, daily=None, vix_daily=None, dte=None, events=None, upto=None):
    """Labels of `day` for `instrument`. `intraday` / `vix_intraday` (1m frames) default to the cache; `upto` (HH:MM) limits
    what is visible (live use at 09:16 / 09:46). Fields not yet known are left out."""
    from script_openalgo.backtest_core import instruments
    cfg = instruments.get(instrument)
    u = intraday if intraday is not None else _load(cache, cfg["index_symbol"], day)
    v = vix_intraday if vix_intraday is not None else _load(cache, "INDIAVIX", day)
    daily = daily if daily is not None else _load(cache, "daily_" + instrument.upper())
    vix_daily = vix_daily if vix_daily is not None else _load(cache, "daily_INDIAVIX")
    d = pd.Timestamp(date.fromisoformat(day))
    if upto:
        cut = pd.Timestamp(f"{day} {upto}") - pd.Timedelta(minutes=1)
        u = u.loc[:cut] if u is not None else None
        v = v.loc[:cut] if v is not None else None
    out = {"weekday": d.weekday()}
    if dte is not None:
        out["dte"] = dte
        out["expiry"] = "expiry" if dte == 0 else "pre-expiry" if dte == 1 else "normal"
    prev = daily[daily.index < d] if daily is not None else None
    if prev is not None and len(prev) >= 2:
        p = prev.iloc[-1]
        rng = float(p["high"] - p["low"])
        hist = prev.iloc[-20:]
        avg_rng_pct = float(((hist["high"] - hist["low"]) / hist["close"]).mean() * 100)
        out.update({"prev_trend": round(float(p["close"] - p["open"]) / rng, 3) if rng else 0.0,
                    "prev_close_pos": round(float(p["close"] - p["low"]) / rng * 100, 1) if rng else 50.0,
                    "avg_range20_pct": round(avg_rng_pct, 3)})
        rel = (rng / float(p["close"]) * 100) / avg_rng_pct if avg_rng_pct else 1
        out["prev_range_rel"] = round(rel, 3)
        out["prev_range_class"] = "wide" if rel >= RANGE_WIDE else "narrow" if rel <= RANGE_NARROW else "normal"
        if u is not None and len(u):
            opn = float(u["open"].iloc[0])
            gap = (opn - float(p["close"])) / float(p["close"]) * 100
            out["gap_pct"] = round(gap, 3)
            out["gap_class"] = "gap-up" if gap >= GAP_K * avg_rng_pct else "gap-down" if gap <= -GAP_K * avg_rng_pct else "flat"
            first = u.loc[: pd.Timestamp(f"{day} 09:44")]
            if len(first) >= 29:                                      # first 30 minutes complete -> stage 2 labels
                hi, lo = float(first["high"].max()), float(first["low"].min())
                mv = float(first["close"].iloc[-1]) - opn
                share = abs(mv) / (hi - lo) if hi > lo else 0
                out["open_type"] = ("trend-up" if mv > 0 else "trend-down") if share >= TREND_SHARE else "range"
                out["open30_range_pct"] = round((hi - lo) / opn * 100, 3)
                pc = float(p["close"])
                if out["gap_class"] == "gap-up":
                    out["gap_filled"] = lo <= pc
                elif out["gap_class"] == "gap-down":
                    out["gap_filled"] = hi >= pc
    if v is not None and len(v):
        vo = float(v["open"].iloc[0])
        out["vix_open"] = vo
        vprev = vix_daily[vix_daily.index < d] if vix_daily is not None else None
        if vprev is not None and len(vprev) >= 5:
            avg = float(vprev["close"].iloc[-20:].mean())
            out["vix_vs_avg20"] = round(vo / avg, 3)
            out["vix_regime"] = "high" if vo / avg >= VIX_HIGH else "low" if vo / avg <= VIX_LOW else "normal"
            out["vix_overnight"] = round(vo - float(vprev["close"].iloc[-1]), 3)
    tags = event_tags(events or [], instrument, day)
    out["events"] = ",".join(tags) if tags else ""
    return out


def build_days(con=None, rebuild=False, quiet=False):
    """market_days for every (instrument, day) that has a manifest in the cache."""
    import glob
    import re
    own = con is None
    con = con or db.connect()
    ensure(con)
    cache = _cache()
    fetch_daily_vix(cache)
    events = load_events()
    have = set() if rebuild else {(r[0], r[1]) for r in con.execute("SELECT instrument, day FROM market_days")}
    n = 0
    for p in sorted(glob.glob(os.path.join(cache, "manifest_*.json"))):
        m = re.search(r"manifest_([A-Z0-9&-]+)_(\d{4}-\d{2}-\d{2})\.json$", os.path.basename(p))
        if not m or (m.group(1), m.group(2)) in have:
            continue
        inst, day = m.groups()
        from script_openalgo.backtest_core import instruments
        if inst not in instruments.INSTRUMENTS:
            continue
        mf = json.load(open(p))
        if not mf.get("complete"):
            continue
        lab = day_labels(cache, inst, day, dte=mf.get("days_to_expiry"), events=events)
        lab["nearest"] = mf.get("nearest", True)
        con.execute("INSERT OR REPLACE INTO market_days VALUES(?,?,?,?)", (inst, day, json.dumps(lab), datetime.now().isoformat(timespec="seconds")))
        n += 1
    con.commit()
    total = con.execute("SELECT COUNT(*) FROM market_days").fetchone()[0]
    if own:
        con.close()
    if not quiet:
        print(f"market_days: {n} days labelled, {total} in total")
    return {"labelled": n, "total": total}


def market_days(con, instrument=None):
    ensure(con)
    q, a = "SELECT instrument, day, data FROM market_days", []
    if instrument:
        q += " WHERE instrument=?"
        a.append(instrument.upper())
    return {(r[0], r[1]): json.loads(r[2]) for r in con.execute(q, a)}


# ---------------- selection ----------------
def _wilson_lb(w, n, z=1.96):
    if not n:
        return 0.0
    p = w / n
    d = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / d


LEVELS = {1: [("gap_class", "vix_regime"), ("gap_class",), ("vix_regime",), ()],
          2: [("gap_class", "vix_regime", "open_type"), ("gap_class", "open_type"), ("open_type",), ("gap_class",), ()]}


def strategy_days(con, instrument, before=None):
    """[(slug, day, trades, net)] of current runs (journal_days) on nearest-expiry days, optionally only before `before`."""
    from script_openalgo import journal
    journal.ensure(con)
    q = "SELECT slug, day, trades, net FROM journal_days WHERE instrument=? AND COALESCE(nearest, 1) = 1"
    a = [instrument.upper()]
    if before:
        q += " AND day < ?"
        a.append(before)
    return [tuple(r) for r in con.execute(q, a)]


def evaluate(con, instrument, today_labels, stage=1, before=None, mdays=None):
    """Per strategy: the evidence under today's condition (most specific level with MIN_DAYS trade days)."""
    mdays = mdays if mdays is not None else market_days(con, instrument)
    rows = strategy_days(con, instrument, before)
    by = {}
    for slug, day, trades, net in rows:
        by.setdefault(slug, []).append((day, trades or 0, net or 0.0, mdays.get((instrument.upper(), day), {})))
    out = []
    for slug, items in by.items():
        traded = [x for x in items if x[1] > 0]
        if not traded:
            continue
        overall_avg = sum(x[2] for x in traded) / len(traded)
        chosen = None
        for keys in LEVELS[stage]:
            if any(k not in today_labels for k in keys):
                continue
            match = [x for x in traded if all(x[3].get(k) == today_labels[k] for k in keys)]
            if len(match) >= MIN_DAYS or not keys:
                chosen = (keys, match)
                break
        keys, match = chosen
        n = len(match)
        w = sum(1 for x in match if x[2] > 0)
        gp, gl = sum(x[2] for x in match if x[2] > 0), -sum(x[2] for x in match if x[2] < 0)
        shrunk = (sum(x[2] for x in match) + SHRINK * overall_avg) / (n + SHRINK) if n + SHRINK else 0
        lb = _wilson_lb(w, n)
        cond = ", ".join(f"{k} {today_labels[k]}" for k in keys) or "all days (no matching condition with enough history)"
        out.append({"slug": slug, "instrument": instrument.upper(), "condition": cond, "level": len(keys), "days": n,
                    "wins": w, "win_rate": round(w / n * 100, 1) if n else None, "win_lb": round(lb * 100, 1),
                    "avg_net": round(sum(x[2] for x in match) / n, 1) if n else 0, "shrunk_avg": round(shrunk, 1),
                    "profit_factor": round(gp / gl, 2) if gl else None, "all_days": len(traded),
                    "pick": n >= PICK_MIN_DAYS and lb >= PICK_MIN_LB and shrunk > 0, "avoid": n >= 5 and shrunk < 0 and (gl and gp / gl < 0.8)})
    return sorted(out, key=lambda e: (-e["win_lb"], -e["shrunk_avg"]))


def pick(con, instruments_, day=None, stage=1, max_picks=MAX_PICKS, labels_by=None, before=None):
    """Picks across the selected instruments (best evidence first, at least the best one per instrument when it qualifies)."""
    day = day or date.today().isoformat()
    per, labels = {}, {}
    for inst in instruments_:
        lab = (labels_by or {}).get(inst.upper())
        if lab is None:
            lab = market_days(con, inst).get((inst.upper(), day))
        labels[inst.upper()] = lab or {}
        per[inst.upper()] = evaluate(con, inst, lab or {}, stage, before or day)
    cands = [e for v in per.values() for e in v if e["pick"]]
    chosen, seen = [], set()
    for inst, v in per.items():                                       # the best qualifying strategy of each instrument first
        best = next((e for e in v if e["pick"]), None)
        if best and len(chosen) < max_picks:
            chosen.append(best)
            seen.add((best["slug"], inst))
    for e in sorted(cands, key=lambda e: (-e["win_lb"], -e["shrunk_avg"])):
        if len(chosen) >= max_picks:
            break
        if (e["slug"], e["instrument"]) not in seen:
            chosen.append(e)
            seen.add((e["slug"], e["instrument"]))
    for e in chosen:
        e["reason"] = (f"{e['condition']}: won {e['wins']} of {e['days']} such days (lower bound {e['win_lb']}%), "
                       f"average {e['avg_net']:+.0f}/day, PF {e['profit_factor'] if e['profit_factor'] is not None else '-'}")
    avoid = [dict(e, reason=f"{e['condition']}: average {e['avg_net']:+.0f}/day over {e['days']} days, PF {e['profit_factor']}")
             for v in per.values() for e in v if e["avoid"]]
    return {"day": day, "stage": stage, "labels": labels, "picks": chosen, "avoid": avoid, "evaluated": per,
            "note": "Success = strategy-days with net > 0 (backtests, paper fills; net after brokerage + charges). Thin evidence: a condition is "
                    f"used only with {MIN_DAYS}+ days, otherwise wider conditions; ranks use the cautious lower end of the win rate."}


def backtest(con, instruments_, max_picks=MAX_PICKS, stage=1):
    """Walk-forward: on each past day follow the picks made from EARLIER days only; compare with running every strategy and
    with the best strategy so far (by overall shrunk average) on each instrument."""
    out = {"days": 0, "selector": 0.0, "all": 0.0, "best_so_far": 0.0, "per_day": []}
    mdays = {inst.upper(): market_days(con, inst) for inst in instruments_}
    days = sorted({d for inst in instruments_ for (i, d), lab in mdays[inst.upper()].items() if lab.get("nearest", True)})
    for d in days:
        labels_by = {inst.upper(): mdays[inst.upper()].get((inst.upper(), d)) for inst in instruments_}
        res = pick(con, instruments_, d, stage, max_picks, labels_by, before=d)
        today = {(s, inst): net for inst in instruments_ for s, day, tr, net in strategy_days(con, inst) if day == d}
        if not today:
            continue
        sel = sum(today.get((p["slug"], p["instrument"]), 0) for p in res["picks"])
        alls = sum(today.values())
        best = 0.0
        for inst in instruments_:
            ev = evaluate(con, inst, {}, 1, d, mdays[inst.upper()])
            if ev:
                top = max(ev, key=lambda e: e["shrunk_avg"])
                best += today.get((top["slug"], inst.upper()), 0)
        out["days"] += 1
        out["selector"] += sel
        out["all"] += alls
        out["best_so_far"] += best
        out["per_day"].append({"day": d, "picks": [f"{p['slug']}@{p['instrument']}" for p in res["picks"]], "selector": round(sel, 1),
                               "all": round(alls, 1), "best_so_far": round(best, 1)})
    for k in ("selector", "all", "best_so_far"):
        out[k] = round(out[k], 1)
    return out


# ---------------- live day labels (today, from OpenAlgo) ----------------
def live_labels(instrument, upto=None):
    """Today's labels from OpenAlgo candles so far (for the selector at 09:16 / 09:46 in the web app)."""
    from script_openalgo.backtest_core import data_loader as dl, instruments
    cfg = instruments.get(instrument)
    today = date.today()
    cache = _cache()
    u = dl.fetch_candles(cfg["index_symbol"], cfg["index_exchange"], today)
    v = dl.fetch_candles("INDIAVIX", "NSE_INDEX", today)
    daily = dl.fetch_daily(instrument, today - timedelta(days=1)) if not os.path.exists(os.path.join(cache, f"daily_{instrument.upper()}.parquet")) \
        else _load(cache, "daily_" + instrument.upper())
    vix_daily = _load(cache, "daily_INDIAVIX")
    if vix_daily is None or vix_daily.index.max() < pd.Timestamp(today - timedelta(days=5)):
        vix_daily = fetch_daily_vix(cache)
    dte = None
    try:
        exps = [e for e in dl.listed_expiries(instrument) if e >= today]
        dte = (exps[0] - today).days if exps else None
    except Exception:
        pass
    return day_labels(cache, instrument, today.isoformat(), intraday=u if len(u) else None, vix_intraday=v if len(v) else None,
                      daily=daily, vix_daily=vix_daily, dte=dte, events=load_events(), upto=upto)


# ---------------- CLI ----------------
def main(argv=None):
    ap = argparse.ArgumentParser(prog="script-openalgo selector", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("days"); d.add_argument("--rebuild", action="store_true")
    for name in ("pick", "backtest"):
        p = sub.add_parser(name)
        p.add_argument("--instruments", required=True, help="comma-separated, e.g. NIFTY,BANKNIFTY,RELIANCE")
        p.add_argument("--max", type=int, default=MAX_PICKS)
        p.add_argument("--stage", type=int, choices=[1, 2], default=1)
        p.add_argument("--json", action="store_true")
        if name == "pick":
            p.add_argument("--day", help="YYYY-MM-DD of a cached day (default: today, live from OpenAlgo)")
    a = ap.parse_args(argv)
    con = db.connect()
    ensure(con)
    if a.cmd == "days":
        build_days(con, a.rebuild)
    else:
        insts = [x.strip().upper() for x in a.instruments.split(",") if x.strip()]
        if a.cmd == "pick":
            labels_by = None
            if not a.day:
                labels_by = {i: live_labels(i, None) for i in insts}
            res = pick(con, insts, a.day, a.stage, a.max, labels_by)
            if a.json:
                print(json.dumps(res, indent=1, default=str))
            else:
                for i, lab in res["labels"].items():
                    print(f"{i}: " + ", ".join(f"{k}={lab[k]}" for k in ("gap_class", "gap_pct", "vix_regime", "vix_open", "open_type", "prev_range_class", "expiry", "events") if lab.get(k) not in (None, "")))
                print(f"\nPICKS (stage {a.stage}, max {a.max}):")
                for p in res["picks"]:
                    print(f"  {p['instrument']:9} {p['slug']:38} {p['reason']}")
                if not res["picks"]:
                    print("  none qualifies (sit out, or run with care)")
                if res["avoid"]:
                    print("AVOID:")
                    for p in res["avoid"]:
                        print(f"  {p['instrument']:9} {p['slug']:38} {p['reason']}")
                print("\n" + res["note"])
        else:
            res = backtest(con, insts, a.max, a.stage)
            if a.json:
                print(json.dumps(res, indent=1, default=str))
            else:
                print(f"walk-forward over {res['days']} days: selector {res['selector']:+.0f} | all strategies {res['all']:+.0f} | best so far {res['best_so_far']:+.0f}")
                for r in res["per_day"]:
                    print(f"  {r['day']} selector {r['selector']:>8.0f}  all {r['all']:>8.0f}  best {r['best_so_far']:>8.0f}  picks {', '.join(r['picks']) or '-'}")
    con.close()


if __name__ == "__main__":
    main()
