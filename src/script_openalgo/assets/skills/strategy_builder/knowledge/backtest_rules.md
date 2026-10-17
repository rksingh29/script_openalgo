# Backtest rules (append-only, dated)

## 2026-10-17: initial rules
- Replay, don't re-implement: run the real `Strategy(fake_client, clock=sim.now, sleeper=sim.sleep)`.
- Data comes only from `data_cache/`. The backtest does not call the broker.
- Scope v1: previous trading day only. No slippage, no brokerage/charges (hooks `SLIPPAGE_PTS`, `CHARGES_PER_ORDER` = 0).
- Fill price: the option price at sim-time (last completed 1m bar close).
- No lookahead: a quote at sim-time T uses the last bar whose period has completed before T.
- Data cleaning: filter to the requested date; keep 09:15-15:29 only; epoch seconds -> IST.
- Candidate strikes: step 50 over the NIFTY day range +/- 2 strikes, CE and PE, for the expiry valid on that date.
- Manifest gate: backtest runs only when `data_cache/manifest_<date>.json` has `complete: true`.
- Sim clock: `now()` returns a real `datetime` on the sim date (strategy calls `.replace(hour=...)`); `sleep(n)` advances time; 1-minute bars match the data.
- Reports: gross P&L per trade/cycle/day, win rate, max drawdown, cycles, reversals, stop-reason breakdown.

## 2026-10-17: scale-out exits (vwap_reclaim_scalp.py)
- A strategy can buy once and sell the same symbol in two pieces (partial target exit, then a later
  final/stop/square-off exit). `backtest_core/report.py::build_trades` now matches SELLs to the oldest
  open BUY leg FIFO by *quantity*, not by order count: a single BUY can be closed by more than one SELL,
  and all of its fills are combined into one trade row (qty = original buy qty, exit_price = the
  quantity-weighted average of its sells, exit_time = the time of the sell that fully closes it). This
  is backward compatible: strategies with exactly one BUY and one SELL per (cycle, symbol) produce
  identical trades.csv rows as before.
- `history()` bars built from 5m (or any >1m) resampling of the 1m cache must drop a trailing bucket
  that isn't fully elapsed yet, not just bars after `now` -- otherwise a half-formed 5m candle would be
  handed to the strategy's own "candle closed" logic and could look spuriously closed. See
  `knowledge/openalgo_api.md` 2026-10-17 entry for the exact rule.

## 2026-10-17: verifying a zero-trade day (s_9_21_ema_crossover_scalp.py)
- A legitimate zero-trade backtest result is not itself a bug, but still needs two separate checks
  before accepting it: (1) re-derive the signal independently (e.g. resample the cached 1m candles to
  the strategy's timeframe and recompute its indicators in a throwaway script) to confirm the
  strategy's internal state (`bars`, `ema9`, `ema21`, ...) matches, explaining *why* no entry fired;
  (2) separately confirm the order/fill plumbing is correct by temporarily relaxing the strategy's
  module-level thresholds via `PARAM_OVERRIDES` (or a scratch script importing the backtest's
  `run_day`) to force at least one real trade through `FakeClient`, then spot-check that trade's
  entry/exit prices against the raw parquet (last completed 1-minute bar's close at the fill
  timestamp). Do this in a scratch invocation, not by changing the committed `PARAM_OVERRIDES` or the
  strategy file.

## 2026-10-17: daily bars, CPR and option chain in the backtest
- `data_cache/daily_<INSTRUMENT>.parquet` holds daily index bars (fetched by `ensure_data`, last 45 days, merged incrementally). A day's manifest is incomplete if there is no daily bar BEFORE it (CPR needs it).
- `FakeClient.history(interval="D")` returns only COMPLETED days (strictly before the simulated day): today's daily bar is never visible.
- `FakeClient.optionchain()` returns the latest saved snapshot (`data_cache/chain_<INSTR>_<date>_<HHMM>.json`) at or before the simulated time (`source: "snapshot"`); with no snapshot it builds a PARTIAL chain from the cached strikes using the last completed 1m bar's close/volume/oi (`source: "cached_bars", partial: true`, `strikes_covered`). Backtest PCR therefore covers only the cached strikes (about +/-250 points for NIFTY), so use `PCR_WITHIN_POINTS` consistently.
- `script-openalgo bias-report --date D` shows every backtested trade with the CPR/PCR bias at entry (with/against). It is post-hoc: removing trades would change later behaviour. Treat with many days only.

## 2026-10-17: multi-day backfill and multi-day runs
- `script-openalgo backfill --instrument NIFTY|BANKNIFTY|ALL [--expiry DDMMMYY] [--days N] [--force]` fetches the whole listed life of the nearest contract: the index 1m candles for all days in one call, then every option series ONCE (one call returns all days), splitting into the usual per-day cache files and manifests. First run: NIFTY 19OCT26 = 16 days, 56 series calls; BANKNIFTY 27OCT26 = 29 days, 90 calls. It must run while the contract is listed.
- Each manifest now has `days_to_expiry`, `nearest` (was this contract the nearest expiry that day; heuristic from the previous expiry: weekly = 7 days earlier, monthly = last same-weekday of the previous month, holiday shifts ignored), `previous_expiry`, `warnings`, `backfilled`. Days where `nearest` is false are real data but a farther contract (less gamma/theta): the multi-day runner skips them unless `--allow-far`, and statistics keep them separate ("+N far-expiry days").
- Completeness in a backfill: a strike inside the day's index range (the strikes ATM can reach) must have >= 90% of the bars, otherwise the day is incomplete; a thin strike only in the outer +/-2 buffer is a warning. Far-expiry early days of a 3-4 week old contract often fail this (far strikes barely trade): first backfill had 5 incomplete days out of 16.
- Lot size in a backfill is the current lot size (history of lot changes is not available).
- `script-openalgo multiday --strategy <slug> --instrument NIFTY [--from D --to D | --days N] [--allow-far] [--workers 2]` runs the strategy's own backtest script per day (separate processes, same result as a single-day run), verifies each day with `verify.py`, saves `results/multiday/*.json` and prints per-day and aggregate numbers (days, trades, days without a trade, P&L, win/loss days, worst day, max drawdown, profit factor; always gross, no costs; `LOW SAMPLE` under 20 days). It needs no Claude.
- Runs in the database store `code_hash` (md5 prefix of the strategy file), `dte` and `nearest`, so a result is tied to the code version and expiry label.
