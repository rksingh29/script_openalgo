"""Compare backtested trades with the CPR / PCR market bias at the moment of entry.

For every trade in the latest result of every strategy on a day it computes the bias exactly as a strategy could have known it
at that time (CPR from the previous completed daily bar; PCR from the option chain as of entry, via FakeClient), then reports
whether the trade was WITH the bias (CE when bullish, PE when bearish, anything when neutral) or AGAINST it.

    python -m script_openalgo.backtest_core.bias_report --date 2026-10-16 [--instrument NIFTY]

Caveat: this is a post-hoc look at trades that were taken. Removing the "against" trades would also change what the strategy does
later in the day (it might re-enter), so the numbers show the idea's potential, not a guaranteed improvement. With few days of data
they prove nothing; run it again as days accumulate.
"""
import argparse
import csv
import glob
import json
import os
import sys
from datetime import date, datetime

from script_openalgo import config  # noqa: E402

ROOT = config.home()

from script_openalgo.backtest_core.fake_client import FakeClient  # noqa: E402
from script_openalgo.backtest_core.sim_clock import SimClock  # noqa: E402
from script_openalgo.strategy_lib import levels as L  # noqa: E402


def latest_results(day, instrument):
    """{strategy_slug: newest result folder} for one day and instrument."""
    best = {}
    for d in glob.glob(os.path.join(ROOT, "results", f"*_{instrument}_{day.isoformat()}_*")):
        slug = os.path.basename(d).split(f"_{instrument}_")[0]
        if slug not in best or os.path.getmtime(d) > os.path.getmtime(best[slug]):
            best[slug] = d
    return best


def side_of(symbol):
    return "CE" if symbol.endswith("CE") else "PE"


def aligned(side, bias):
    return bias == "neutral" or (side == "CE" and bias == "bullish") or (side == "PE" and bias == "bearish")


def analyse(day, instrument="NIFTY"):
    clock = SimClock(day)
    fc = FakeClient(day, clock, instrument=instrument)
    daily = fc.history(symbol=fc.cfg["index_symbol"], exchange=fc.cfg["index_exchange"], interval="D")
    c = L.cpr_from_daily(daily)
    prev_c = L.cpr(*daily.iloc[-2][["high", "low", "close"]]) if len(daily) >= 2 else None
    if c is None:
        raise SystemExit("no previous daily bar cached: run backtest_core.data_loader first")
    rows = []
    for slug, d in sorted(latest_results(day, instrument).items()):
        tp = os.path.join(d, "trades.csv")
        if not os.path.exists(tp):
            continue
        for t in csv.DictReader(open(tp, encoding="utf-8")):
            clock._t = datetime.combine(day, datetime.strptime(t["entry_time"], "%H:%M:%S").time())
            spot = fc._price(fc.cfg["index_symbol"])
            chain = fc.optionchain(underlying=fc.cfg["index_symbol"], exchange=fc.cfg["index_exchange"])
            pcr = L.pcr_from_chain(chain)
            full = L.market_bias(spot, c, pcr=pcr, prev_c=prev_c)
            simple = L.cpr_bias(spot, c)
            side = side_of(t["symbol"])
            rows.append({"strategy": slug, "time": t["entry_time"], "side": side, "pnl": float(t["gross_pnl"]), "spot": spot,
                         "cpr_bias": simple, "pcr": pcr, "full_bias": full["bias"], "score": full["score"],
                         "with_cpr": aligned(side, simple), "with_full": aligned(side, full["bias"])})
    return c, prev_c, rows


def summarize(rows, key):
    w = [r["pnl"] for r in rows if r[key]]
    a = [r["pnl"] for r in rows if not r[key]]
    return {"with": {"trades": len(w), "pnl": round(sum(w), 1)}, "against": {"trades": len(a), "pnl": round(sum(a), 1)}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--instrument", default="NIFTY")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    day = date.fromisoformat(a.date)
    c, prev_c, rows = analyse(day, a.instrument.upper())
    out = {"cpr": {k: round(c[k], 2) for k in ("pivot", "bc", "tc", "width_pct", "r1", "s1")}, "cpr_vs_yesterday": L.cpr_relation(c, prev_c),
           "cpr_only": summarize(rows, "with_cpr"), "cpr_plus_pcr": summarize(rows, "with_full"), "trades": rows}
    if a.json:
        print(json.dumps(out, indent=1, default=str))
        sys.exit(0)
    print(f"CPR {a.date}: BC {c['bc']:.1f}  pivot {c['pivot']:.1f}  TC {c['tc']:.1f}  width {c['width_pct']:.2f}% ({L.cpr_width_class(c['width_pct'])}); "
          f"vs yesterday's CPR: {L.cpr_relation(c, prev_c)}")
    print(f"{'strategy':36} {'time':8} {'side':4} {'spot':>8} {'CPR bias':>9} {'PCR':>5} {'CPR+PCR':>9} {'P&L':>9}  with CPR / with CPR+PCR")
    for r in rows:
        print(f"{r['strategy'][:36]:36} {r['time']:8} {r['side']:4} {r['spot']:8.1f} {r['cpr_bias']:>9} {r['pcr'] if r['pcr'] is None else round(r['pcr'], 2):>5} {r['full_bias']:>9} {r['pnl']:9.0f}  "
              f"{'WITH' if r['with_cpr'] else 'AGAINST':8} {'WITH' if r['with_full'] else 'AGAINST'}")
    for label, k in (("CPR only", "cpr_only"), ("CPR + PCR", "cpr_plus_pcr")):
        s = out[k]
        print(f"{label:10}: with bias {s['with']['trades']} trades, P&L {s['with']['pnl']:+.0f} | against bias {s['against']['trades']} trades, P&L {s['against']['pnl']:+.0f}")
