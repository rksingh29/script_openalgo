"""
Backtest for supertrend_flip_scalp.py: replays the REAL Strategy class on cached
historical candles (data_cache/, filled by backtest_core.data_loader from OpenAlgo :5000).

    python strategies/backtest_supertrend_flip_scalp.py                  # previous trading day
    python strategies/backtest_supertrend_flip_scalp.py --date 2026-10-16
    python strategies/backtest_supertrend_flip_scalp.py --no-fetch       # never call the API; cache must be complete
"""
import argparse
import os
import sys
from datetime import date, datetime

import supertrend_flip_scalp as strat
from script_openalgo import config
from script_openalgo.backtest_core import data_loader as dl
from script_openalgo.backtest_core.fake_client import FakeClient
from script_openalgo.backtest_core.report import build_trades, print_summary, summarize, write_results
from script_openalgo.backtest_core.sim_clock import SimClock

# ======================= CONFIG =======================
START_DATE = None             # None = previous trading day
SLIPPAGE_PTS = 0.0            # v1: off
CHARGES_PER_ORDER = 0.0       # v1: off
PARAM_OVERRIDES = {}          # e.g. {"ATR_PERIOD": 7} overrides strategy module constants
RESULTS_DIR = config.results_dir()
# ======================================================


def run_day(day, quiet=False, instrument="NIFTY"):
    for k, v in PARAM_OVERRIDES.items():
        if not hasattr(strat, k):
            raise SystemExit(f"unknown strategy parameter {k}")
        setattr(strat, k, v)

    if hasattr(dl.instruments, "apply_strategy_overrides"):   # underlying, exchange, per-instrument sizing
        dl.instruments.apply_strategy_overrides(strat, instrument, PARAM_OVERRIDES)
    else:                                                     # older installed package
        strat.UNDERLYING = dl.instruments.get(instrument)["index_symbol"]
    clock = SimClock(day)
    client = FakeClient(day, clock, SLIPPAGE_PTS, CHARGES_PER_ORDER, instrument)
    log_lines = []

    def sim_log(msg):                      # strategy.log() stamps wall-clock time; stamp sim time instead
        line = f"{clock.now():%Y-%m-%d %H:%M:%S} | {msg}"
        log_lines.append(line)
        if not quiet:
            print(line)

    strat.log = sim_log
    s = strat.Strategy(client, clock=clock.now, sleeper=clock.sleep)
    client.strategy = s          # supertrend_flip_scalp has no `.cycle`; FakeClient tags every order cycle=0
    try:
        s.run()
    except RuntimeError as e:
        sim_log(f"!!! SIMULATION ABORTED: {e}")

    trades, unclosed = build_trades(client.orders, CHARGES_PER_ORDER)
    summary = summarize(trades, client.orders, log_lines, unclosed)
    out = os.path.join(RESULTS_DIR,
                        f"supertrend_flip_scalp_{instrument}_{day.isoformat()}_{datetime.now():%H%M%S}")
    write_results(out, day, trades, summary, log_lines)
    print_summary(day, trades, summary)
    print(f"\nResults saved in {out}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD")
    ap.add_argument("--instrument", default="NIFTY", help="NIFTY or BANKNIFTY")
    ap.add_argument("--no-fetch", action="store_true", help="do not call the API")
    a = ap.parse_args()

    if a.date or START_DATE:
        day = date.fromisoformat(a.date or START_DATE)
    elif a.no_fetch:
        sys.exit("--no-fetch needs --date")
    else:
        day = dl.previous_trading_day()

    m = dl.load_manifest(day, a.instrument)
    if (not m or not m.get("complete")) and not a.no_fetch:
        m = dl.ensure_data(day, a.instrument)
    if not m or not m.get("complete"):
        print(f"GATE: data for {day} is incomplete, not running the backtest.")
        for i in (m or {}).get("issues", ["no manifest"]):
            print("  -", i)
        sys.exit(2)
    run_day(day, instrument=a.instrument.upper())
