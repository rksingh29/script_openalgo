"""Run one strategy over many cached days and aggregate the results (no Claude involved).

    python -m script_openalgo.backtest_core.multiday                      # EVERY strategy on EVERY usable day, NIFTY and BANKNIFTY (slow)
    python -m script_openalgo.backtest_core.multiday --strategy <slug> --instrument NIFTY --from 2026-10-13 --to 2026-10-16
    python -m script_openalgo.backtest_core.multiday --strategy <slug> --instrument NIFTY --days 10 [--allow-far] [--workers 2]

Each day runs the strategy's own backtest script (strategies/backtest_<slug>.py) in a separate process, so results are
identical to a single-day run. A day is used only if its manifest is complete and the cached contract was the NEAREST expiry
that day, unless --allow-far (days on a farther expiry behave differently: less gamma and theta). Every day's result is also
checked by backtest_core/verify.py. The aggregate is saved to results/multiday/ and printed.
"""
import argparse
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from script_openalgo import config  # noqa: E402

ROOT = config.home()

from script_openalgo.backtest_core import data_loader as dl  # noqa: E402
from script_openalgo.backtest_core import instruments, scaffold, verify  # noqa: E402


def code_hash(slug):
    p = scaffold.strategy_path(slug)
    return hashlib.md5(open(p, "rb").read()).hexdigest()[:10] if os.path.exists(p) else None


def available_days(instrument, start=None, end=None, allow_far=False):
    """([usable dates], [(date, reason) skipped]) from the cached manifests."""
    usable, skipped = [], []
    for p in sorted(glob.glob(os.path.join(dl.CACHE_DIR, f"manifest_{instrument.upper()}_*.json"))):
        m = re.search(r"_(\d{4}-\d{2}-\d{2})\.json$", p)
        if not m:
            continue
        d = date.fromisoformat(m.group(1))
        if (start and d < start) or (end and d > end):
            continue
        mf = dl.load_manifest(d, instrument)
        if not mf or not mf.get("complete"):
            skipped.append((d, "incomplete data"))
        elif not mf.get("nearest", True) and not allow_far:
            skipped.append((d, f"cached contract was not the nearest expiry ({mf.get('days_to_expiry')} days to expiry); use --allow-far"))
        else:
            usable.append(d)
    return usable, skipped


def run_day(slug, instrument, day):
    bt = scaffold.backtest_path(slug)
    r = subprocess.run([sys.executable, bt, "--date", day.isoformat(), "--instrument", instrument, "--no-fetch"],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"Results saved in (.+)", r.stdout or "")
    if r.returncode != 0 or not m:
        return {"day": day.isoformat(), "error": ((r.stderr or r.stdout) or "no output")[-300:]}
    rdir = m.group(1).strip()
    summ = json.load(open(os.path.join(rdir, "summary.json"), encoding="utf-8"))
    try:
        v = verify.verify(slug, rdir, day, instrument)
        verdict = "script-checked" if v["status"] == "pass" else "verify-failed"
        findings = [f["issue"] for f in v["findings"] if f["severity"] in ("high", "medium")][:3]
    except Exception as e:                                       # a verifier problem must not hide the result
        verdict, findings = "unverified", [str(e)[:120]]
    return {"day": day.isoformat(), "trades": summ.get("trades"), "gross_pnl": summ.get("gross_pnl"), "net_pnl": summ.get("net_pnl"),
            "win_rate": summ.get("win_rate"), "max_drawdown": summ.get("max_drawdown"), "result_dir": rdir,
            "verdict": verdict, "findings": findings, "summary": summ}


def aggregate(rows):
    ok = [r for r in rows if "error" not in r]
    pnls = [r["gross_pnl"] or 0 for r in ok]
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    cum = peak = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"days": len(ok), "failed_days": len(rows) - len(ok), "total_pnl": round(sum(pnls), 1),
            "avg_per_day": round(sum(pnls) / len(pnls), 1) if pnls else None,
            "win_days": len(wins), "loss_days": len(losses), "flat_days": len(ok) - len(wins) - len(losses),
            "best_day": max(pnls) if pnls else None, "worst_day": min(pnls) if pnls else None,
            "max_drawdown": round(dd, 1), "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else None,
            "trades": sum(r["trades"] or 0 for r in ok), "zero_trade_days": sum(1 for r in ok if not r["trades"]),
            "total_net": round(sum((r.get("net_pnl") if r.get("net_pnl") is not None else r["gross_pnl"] or 0) for r in ok), 1)}


def run_range(slug, instrument="NIFTY", start=None, end=None, last_days=None, allow_far=False, workers=2):
    if not os.path.exists(scaffold.strategy_path(slug)):
        raise SystemExit(f"strategies/{slug}.py not found")
    if not os.path.exists(scaffold.backtest_path(slug)):
        missing, _ = scaffold.missing_client_methods(slug)
        if missing:
            raise SystemExit(f"FakeClient lacks {missing}; run the pipeline once so the backtest-writer extends it")
        scaffold.write_backtest(slug)
    days, skipped = available_days(instrument, start, end, allow_far)
    if last_days:
        days = days[-last_days:]
    if not days:
        raise SystemExit(f"no usable days for {instrument} (skipped: {skipped[:3]}). Run: script-openalgo backfill --instrument {instrument}")
    print(f"[{slug} {instrument}] {len(days)} day(s): {days[0]} .. {days[-1]}"
          + (f" | skipped {len(skipped)}: " + "; ".join(f"{d} ({r[:30]})" for d, r in skipped[:3]) if skipped else ""), flush=True)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        rows = list(ex.map(lambda d: run_day(slug, instrument, d), days))
    agg = aggregate(rows)
    out = {"strategy": slug, "instrument": instrument, "from": days[0].isoformat(), "to": days[-1].isoformat(),
           "allow_far": allow_far, "code_hash": code_hash(slug), "skipped": [(d.isoformat(), r) for d, r in skipped],
           "aggregate": agg, "days": rows, "created": datetime.now().isoformat(timespec="seconds")}
    os.makedirs(os.path.join(ROOT, "results", "multiday"), exist_ok=True)
    path = os.path.join(ROOT, "results", "multiday", f"{slug}_{instrument}_{days[0]}_{days[-1]}_{datetime.now():%H%M%S}.json")
    json.dump(out, open(path, "w", encoding="utf-8"), indent=1, default=str)
    return out, path


def print_report(out):
    print(f"\n===== {out['strategy']} on {out['instrument']}  {out['from']} .. {out['to']}  (code {out['code_hash']}) =====")
    print(f"{'day':12} {'trades':>6} {'P&L':>9} {'win%':>5} {'maxDD':>7}  check")
    for r in out["days"]:
        if "error" in r:
            print(f"{r['day']:12} ERROR {r['error'][:80]}")
            continue
        wr = "" if r["win_rate"] is None else f"{r['win_rate'] * 100:.0f}"
        print(f"{r['day']:12} {r['trades']:>6} {r['gross_pnl']:>9.0f} {wr:>5} {r['max_drawdown']:>7.0f}  {r['verdict']}" + (f" {r['findings']}" if r["findings"] else ""))
    a = out["aggregate"]
    print(f"TOTAL  days {a['days']} | trades {a['trades']} ({a['zero_trade_days']} days without a trade) | P&L {a['total_pnl']:+.0f} | avg/day {a['avg_per_day']} | "
          f"win/loss/flat days {a['win_days']}/{a['loss_days']}/{a['flat_days']} | worst day {a['worst_day']} | max drawdown {a['max_drawdown']} | profit factor {a['profit_factor']}")
    if a.get("total_net") is not None and abs(a["total_net"] - a["total_pnl"]) > 0.5:
        print(f"NET after the estimated spread (instrument slippage_pct): {a['total_net']:+.0f}  (gross {a['total_pnl']:+.0f})")
        print("Gross P&L above; net uses an ESTIMATED spread, not real quotes. " + ("LOW SAMPLE (< 20 days)." if a["days"] < 20 else ""))
    else:
        print("Gross P&L, no slippage or charges. " + ("LOW SAMPLE (< 20 days)." if a["days"] < 20 else ""))


def all_strategies():
    """Slugs of every strategy file in strategies/ (skips backtest_*, helper files and anything starting with _)."""
    out = []
    for p in sorted(glob.glob(os.path.join(scaffold.STRAT_DIR, "*.py"))):
        n = os.path.basename(p)[:-3]
        if not n.startswith(("backtest_", "_")) and n != "__init__":
            out.append(n)
    return out


def print_comparison(results):
    """One line per (strategy, instrument): days, trades, P&L ... sorted by average P&L per day."""
    print("\n" + "=" * 128)
    print(f"{'strategy':38} {'inst':9} {'days':>4} {'trades':>6} {'no-trade days':>13} {'P&L':>9} {'net':>9} {'avg/day':>8} {'win/loss days':>13} {'worst day':>9} {'PF':>5}")
    rows = sorted([r for r in results if "aggregate" in r], key=lambda r: -(r["aggregate"]["avg_per_day"] or 0))
    for r in rows:
        a = r["aggregate"]
        net = a.get("total_net", a["total_pnl"])
        print(f"{r['strategy'][:38]:38} {r['instrument']:9} {a['days']:>4} {a['trades']:>6} {a['zero_trade_days']:>13} {a['total_pnl']:>9.0f} {net:>9.0f} "
              f"{(a['avg_per_day'] or 0):>8.0f} {str(a['win_days']) + '/' + str(a['loss_days']):>13} {(a['worst_day'] or 0):>9.0f} {a['profit_factor'] if a['profit_factor'] is not None else '-':>5}")
    for r in results:
        if "error" in r:
            print(f"{r['strategy'][:38]:38} {r['instrument']:9} not run: {r['error'][:70]}")
    print("P&L = gross (fills at candle closes). net = after the instrument's ESTIMATED spread (slippage_pct; 0 for indices), no charges. "
          "Few days = LOW SAMPLE: do not rank strategies on this. Only nearest-expiry days unless --allow-far.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run strategies over many cached days. With no arguments: EVERY strategy on EVERY usable day, NIFTY and BANKNIFTY.")
    ap.add_argument("--strategy", default="all", help="a strategy slug, a comma-separated list, or 'all' (default)")
    ap.add_argument("--instrument", default="ALL", help="NIFTY, BANKNIFTY or ALL (default)")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--days", type=int, help="the last N usable days")
    ap.add_argument("--allow-far", action="store_true", help="also use days where the cached contract was not the nearest expiry")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--verbose", action="store_true", help="print the per-day table for every strategy (default only for a single strategy+instrument)")
    a = ap.parse_args()
    slugs = all_strategies() if a.strategy.lower() == "all" else [s.strip() for s in a.strategy.split(",") if s.strip()]
    insts = list(instruments.INSTRUMENTS) if a.instrument.upper() == "ALL" else [a.instrument.upper()]
    start = date.fromisoformat(a.start) if a.start else None
    end = date.fromisoformat(a.end) if a.end else None
    results, total = [], len(slugs) * len(insts)
    if total > 1:
        print(f"Running {len(slugs)} strategy(ies) x {len(insts)} instrument(s) = {total} runs. Each runs every usable day "
              f"(slow strategies take about a minute per day; this can take a while).", flush=True)
    n = 0
    for slug in slugs:
        for inst in insts:
            n += 1
            try:
                out, path = run_range(slug, inst, start, end, a.days, a.allow_far, a.workers)
                results.append(out)
                if total == 1 or a.verbose:
                    print_report(out)
                    print(f"MULTIDAY_RESULT {path}")
                else:
                    ag = out["aggregate"]
                    print(f"  [{n}/{total}] {slug} {inst}: {ag['days']} days, {ag['trades']} trades, P&L {ag['total_pnl']:+.0f}, avg/day {ag['avg_per_day']}", flush=True)
                    print(f"MULTIDAY_RESULT {path}")
            except SystemExit as e:
                results.append({"strategy": slug, "instrument": inst, "error": str(e)})
                print(f"  [{n}/{total}] {slug} {inst}: not run ({str(e)[:90]})", flush=True)
    if total > 1:
        print_comparison(results)
        os.makedirs(os.path.join(ROOT, "results", "multiday"), exist_ok=True)
        cp = os.path.join(ROOT, "results", "multiday", f"_comparison_{datetime.now():%Y%m%d_%H%M%S}.json")
        json.dump(results, open(cp, "w", encoding="utf-8"), indent=1, default=str)
        print(f"Saved: {cp}")
    try:                                                # make the command-line results visible in the web app
        from script_openalgo.strategy_db import db
        _c = db.connect()
        _n = db.sync_from_disk(_c)
        _c.close()
        print(f"Web app data updated: {_n['registered']} strategies registered, {_n['runs_imported']} results imported.")
    except Exception as e:                              # never let this hide the results printed above
        print(f"(could not update the web app database: {e})")
