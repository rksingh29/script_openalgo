# Lessons (append-only, dated)

## 2026-10-17
- `backtest.py` re-implemented a different strategy (MA crossover on the index) and so could not test `nifty_direction_ce_pe.py`. Always import and run the real `Strategy` class in the backtest.
- `backtest.py` had a real API key hardcoded. Keys come from the `OPENALGO_API_KEY` env var only. The exposed key must be rotated by the user.
- Historify (`source="db"`) was empty and its download needs a web login; `source="api"` works. Do not assume Historify has data.
- Expired option contracts cannot be fetched. Cache each day's data while the contracts are still listed.
- `history(end_date=...)` can return extra days; always filter by date.

## 2026-10-17

## 2026-10-17

## 2026-10-17 (BANKNIFTY)
- Hardcoded NIFTY assumptions (symbol, strike step 50, weekly expiry gap 8 days, manifest filename) broke for BANKNIFTY (step 100, monthly expiries). Keep all instrument facts in `backtest_core/instruments.py`.
- The strategy's rupee thresholds (profit 1000, max loss 2500) are NIFTY-sized; BANKNIFTY results with the same numbers are not comparable. Use per-instrument parameter sets.

## 2026-10-17 (vwap_reclaim_scalp.py)
- Bug found while building the backtest: `backtest_core/report.py::build_trades` assumed exactly one
  SELL per BUY per (cycle, symbol). `vwap_reclaim_scalp.py` scales out (partial exit at target1, then a
  separate final exit), so the old code silently dropped the second SELL's P&L (the symbol's open-leg
  queue was already empty, so the sell fell into `continue`). Fixed by FIFO-matching SELL *quantity*
  against open BUY *quantity* and aggregating multi-sell exits into one trade row. Verified: manually
  forced a partial-then-final exit and confirmed `build_trades` reproduces the exact combined P&L
  ((qty1*px1 + qty2*px2)/total_qty as exit price).
- `vwap_reclaim_scalp.py` calls `self.c.history(...)` (5m bars) and `self.c.quotes(...)["data"]["high"/
  "low"]` (session high/low so far) -- neither existed in `FakeClient`. Added both (see
  `knowledge/openalgo_api.md`). Spot-checked: entry fill (104.55) and a forced partial-exit fill (111.45)
  and final-exit fill (86.60) for `NIFTY19OCT2623100CE` on 2026-10-16 all matched the option's raw
  parquet close of the previous completed minute exactly; `quotes()["data"]["high"]` matched a manual
  `idx_df.loc[:cutoff]["high"].max()` recomputation.
- 2026-10-16 (NIFTY and BANKNIFTY) produced 0 trades for `vwap_reclaim_scalp.py`: confirmed by hand (not
  a bug) -- both indices broke above VWAP once early (~09:40) with no qualifying pretrend first, then
  trended up all day without ever closing back below VWAP again, so no dip-then-reclaim (or rally-then-
  rejection) pattern ever formed. A trend day with no pullback is an expected zero-signal day for this
  setup; re-test on more days before judging the strategy.

## 2026-10-17 (s_9_21_ema_crossover_scalp.py)
- No `FakeClient` changes were needed: every `self.c.<method>` the strategy calls (`quotes`,
  `expiry`, `optionsymbol`, `placeorder`, `orderstatus`, `analyzerstatus`, and `history` for both the
  5m signal timeframe and the 15m HTF filter) was already implemented by earlier strategies'
  backtests. `FakeClient.history()`'s interval parsing/resampling and no-lookahead cutoff worked
  unchanged for the extra `interval="15m"` call used by the HTF trend filter.
- 2026-10-16 (NIFTY) produced 0 trades for `s_9_21_ema_crossover_scalp.py` with default params.
  Verified by hand (not a bug): resampling the cached 1m NIFTY candles to 5m and recomputing EMA9/
  EMA21 in a throwaway script reproduced the strategy's own `self.bars`/`self.ema9`/`self.ema21`
  exactly. The day was a one-way grind up with EMA9 staying above EMA21 almost the whole session; the
  only true crossover was a bearish (PE) cross at the 14:15 candle, but its confirmation-candle gap
  (`gap_pct` ~= 0.0207% of price) was below `MIN_SEPARATION_PCT` (0.05%), so `confirm_separation()`
  correctly dropped it ("Separation not confirmed for PE at bar 61; setup dropped"). To positively
  confirm the entry/exit/fill plumbing (not just the signal math) with zero real trades to point at,
  temporarily overrode `MIN_SEPARATION_PCT=0.0`, `REQUIRE_HTF_TREND_FILTER=False`,
  `STOP_MAX_PCT=5.0` in a scratch run (not saved in the backtest file) to force that same PE setup
  through to a real trade: entry BUY `NIFTY19OCT2623100PE` @ 103.75 at 14:25:00 and exit SELL @ 87.50
  at 14:50:02 (EMA9 flattening exit) both matched the raw parquet's close of the previous completed
  1-minute bar (14:24:00 -> 103.75, 14:49:00 -> 87.50) exactly -- no lookahead, fills correct.
- `backtest_core/report.py::summarize()`'s `reversals` field (`max(0, buys_per_cycle - 1)`) is
  meaningless for strategies with no `.cycle` concept (like this one and `vwap_reclaim_scalp.py`):
  `FakeClient` tags every order `cycle=0` for them, so any 2nd/3rd/4th same-day trade (this strategy
  allows up to `MAX_TRADES_PER_DAY`) would show up as a "reversal" even though it is just a normal new
  setup, not a reversal-on-loss like `nifty_direction_ce_pe.py`. Read `reversals` as "0 means exactly
  one BUY order that day" for cycle-less strategies, not as an actual reversal count.

2026-10-20: Confirmation candle after a signal is not a free win. parabolic_sar_flip_scalp with CONFIRM_BARS=1/2 (enter only if the next closed 5m candle(s) stay on the flip side and close beyond the flip close) cut trades by 30-60 % but lowered P&L on NIFTY and BANKNIFTY (the edge is in the early entry) and helped only on the stocks, mostly by paying the estimated spread fewer times. The whipsaw that prompted it (NIFTY 2026-10-19, CE at the 09:35 flip, -3130) was really a wide initial SAR stop (~174 pts, 0.75 %) bleeding for two hours; filter or cap the initial stop distance before adding confirmation delays. Test such filters across all cached days, never on the one day that looked bad.

2026-10-21: Entry filters for option buying, tested on parabolic_sar_flip_scalp over 37 days / 4 instruments. A fixed VIX level (VIX > 14.7) mostly blocks whole high-VIX days and cost ~11k gross; a late-entry filter (|price - SAR| > k ATR) was inconsistent (k=3 halved BANKNIFTY, helped stocks; k>=4 rarely triggers). The only filter that kept gross while cutting trades was "skip when India VIX is in the top 10 % of its range so far today" (just after a volatility spike): gross unchanged, net +8.6k on the stocks (fewer spreads). Measure a filter on all days; a rule picked from one bad trade (VIX 14.7) usually describes that day only.

2026-10-21: A backtest file generated by an OLD installed package (deployment) still had `strat.UNDERLYING = ...` instead of `instruments.apply_strategy_overrides(...)`, so per-instrument settings (CONFIRM_BARS for BANKNIFTY, OFSS DAILY_MAX_LOSS) were silently ignored. When a strategy comes back from the deployment, check its backtest file uses apply_strategy_overrides (grep -L apply_strategy_overrides strategies/backtest_*.py).

2026-10-30: `all_in_one.py`'s `self.lot` is a side effect of `atm()` (`self.lot = int(r["lotsize"])`) and starts `None`. Adding `LIQUID_STRIKE` (buy the most-OI/volume nearby strike instead of blind ATM) introduced `entry_symbol()` -> `best_strike()`, a path that resolves a tradeable symbol straight from the option chain and never calls `atm()`. Whenever the very first entry of the day took that path (the common case once LIQUID_STRIKE is on and the chain has data), `self.lot` stayed `None` all day and `qty = self.lot * lots` crashed with `TypeError: unsupported operand type(s) for *: 'NoneType' and 'int'` on the NEXT entry. Found via `multiday --strategy all_in_one,always_in_trade`: 4 of 10 usable days errored identically in both files (always_in_trade is a derived copy, so it inherited the bug). Fix: `best_strike()` now also returns the chain row's own `lotsize` (present on both the real OpenAlgo `optionchain` response and FakeClient's), `entry_symbol()` sets `self.lot` from it, and `enter()` keeps a last-resort `self.atm()` call if `self.lot` is still `None` for any reason. Lesson: a new strike-resolution path must set every side effect the old path set, not just return a symbol - diff what the function used to mutate, not just what it used to return.

2026-11-02: Live money bug found in `cci_100_100_momentum_scalp.py` (deployed workspace): `self.processed_bars`, a plain integer cursor into `self.bars`, was used across polls of `process_new_bars()`, but `refresh_bars()` rebuilds `self.bars` wholesale every call by re-fetching the whole day and re-sorting by timestamp. A transient fetch hiccup ("history error: No data available for the specified period") that made one poll's `history()` return an incomplete/out-of-order set, followed by a later poll returning the full correct set, could insert "new" bars *before* the stale cursor position after the sort - desyncing the index cursor from real time. Observed live: 3 real BUY/SELL pairs fired within a 20-second window, citing exit reasons from candle times (12:00, 13:20, 13:40) hours earlier than the actual clock. The same pattern (`while self.processed_bars < len(self.bars): handle_new_bar(self.processed_bars); self.processed_bars += 1`) existed verbatim in 24 other `*_scalp.py` strategies in this project (essentially the whole suite, incl. several with a live position or an active `CONFIRM_BARS` confirmation step at the time) - all fixed the same way: replace the index cursor with `self.last_bar_t` (a bar *timestamp*), iterating `for i, b in enumerate(self.bars)` and skipping any `b["t"] <= self.last_bar_t`. A sibling bug existed in the `CONFIRM_BARS > 0` confirmation path of 5 of those files (`parabolic_sar_*` x4 live with `CONFIRM_BARS=2`, `adx_dmi_directional_crossover_scalp` dormant at 0): the pending-signal dict stored a raw bar index (`p["i"]`) across polls and compared it to the current call's index (`i - p["i"] < CONFIRM_BARS`) - same desync risk. Fixed by storing the pending bar's timestamp (`p["t"]`) instead of its index, and counting confirmation candles with an explicit `p["bars"] += 1` counter rather than an index difference. Lesson: any per-poll state that is meant to track "how far through the day have I gotten" must be anchored to the bar's own timestamp, never to its position in a list that gets rebuilt from scratch on every poll - this is a systemic pattern across this codebase's `refresh_bars()`/`process_new_bars()` convention, not a one-off, so a new strategy copied from an existing one inherits it silently.
