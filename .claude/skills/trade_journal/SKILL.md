---
name: trade_journal
description: Use the Strategy Lab trade journal (every backtested trade with its market context at entry) to judge a signal or a trading situation: how did similar past trades of this strategy do, under which VIX / trend / time-of-day conditions does a strategy win or lose. Use when the user asks whether to take a signal, which strategy suits today's market, or what conditions favour a strategy. Read-only analysis; never places orders.
---

# trade_journal

The journal is a table of every trade from the **current backtest run** of each strategy / instrument / day (the newest run with the strategy's default settings: the same runs the strategy stats use), with the market context **at the minute of entry** (no lookahead) and the outcome. It grows by itself after every test or multi-day run in the web app and on "Rescan folders"; `script-openalgo journal build` catches up from the command line.

## Which workspace
The journal lives in the workspace's strategy DB (`strategy_db/default.db`, tables `journal_trades`, `journal_days`). Daily runs made in the deployed web app are in the **deployment** workspace; work in the repo is in the repo. Point the commands at the one you want:
```
script-openalgo --home G:\Claude\test_script\my_workspace journal build      # deployment (the web app on 5055)
PYTHONPATH=src python -m script_openalgo.cli journal build                    # repo, run from the repo root
```
Always run `journal build` first: it only adds runs not yet journaled (seconds).

## Columns (journal_trades)
- Identity: `slug` (strategy), `instrument`, `day`, `entry_time`, `exit_time`, `side` (CE = bullish buy, PE = bearish buy), `symbol`, `qty`, `run_id`, `code_hash`.
- Outcome: `pnl` (gross), `net` (after the instrument's estimated spread; = gross for indices), `win` (net > 0), `hold_min`, `exit_reason`, `opt_mfe` / `opt_mae` (best / worst option P&L in rupees while held).
- Context at entry (data completed before that minute):
  - time: `minute_of_day` (09:15 = 555), `weekday` (0 = Monday), `dte` (days to expiry), `nearest` (0 = far-expiry day)
  - underlying: `u_price`, `u_from_open_pct`, `u_day_pos` (0 = at the day's low so far, 100 = at the high), `u_ret15_pct` / `u_ret30_pct` / `u_ret60_pct`, `atr5_pct` (ATR(14) of 5m candles, % of price), `gap_pct` (open vs previous close), `prev_range_pct`
  - India VIX: `vix`, `vix_pos` (position in its range so far today, 0-100), `vix_chg15` / `vix_chg30` (points), `vix_from_open`
- `journal_days`: per strategy / instrument / day: trades, pnl, net, `day_range_pct`, `day_change_pct`, `gap_pct`, `vix_open`, `vix_close`, `vix_range`.

## Commands
```
script-openalgo journal query --strategy parabolic_sar_flip_scalp --instrument BANKNIFTY --by vix_pos
script-openalgo journal query --strategy opening_range_breakout_orb --from 09:15 --to 10:30 --vix-pos-min 50 --by u_ret30_pct
script-openalgo journal similar --strategy parabolic_sar_flip_scalp --instrument NIFTY --side PE --time 12:05 --vix 16.85 --vix-pos 50 --u-day-pos 15 -k 15
script-openalgo journal export --out journal.csv
```
- `query` filters: `--strategy --instrument --side --from --to --include-far`, ranges `--<feature>-min / -max` for vix, vix-pos, vix-chg15, vix-chg30, u-day-pos, u-from-open-pct, u-ret30-pct, atr5-pct, dte, gap-pct. `--by FEATURE` buckets the result (vix_pos, vix, vix_chg15, vix_chg30, minute_of_day, u_day_pos, u_from_open_pct, u_ret30_pct, atr5_pct, dte, gap_pct, weekday, side, exit_reason, instrument, slug, day).
- `similar` returns the k past trades closest to a situation (features scaled by their spread; only the features you give are used).
- Add `--json` for machine-readable output. For anything else query SQLite directly (read-only): `SELECT ... FROM journal_trades`.

## How to answer "should I take this signal?" (or "which strategy for today?")
1. Get the situation: strategy, instrument, side, time; India VIX now and its range so far today; the underlying's move since the open and position in its day range. From a Signals session use the signal row and the chart's values; live, read them from OpenAlgo (`quotes`, `history` 1m for the underlying and `INDIAVIX` on `NSE_INDEX`).
2. `journal build`, then `journal similar` with those values, and `journal query --strategy ... --instrument ... --by <the feature that matters most>` (usually vix_pos, minute_of_day, u_day_pos).
3. Report: number of matching trades and days, win rate with its 95% interval, average and total net, profit factor, typical exit reasons and MFE (did winners give back?). Compare with the strategy's overall numbers: a bucket only matters if it differs from the overall result by more than its interval.
4. Say plainly when the evidence is weak: fewer than 30 trades or fewer than 15 days = LOW SAMPLE; an interval that spans 50% means "no edge shown". Days are not independent (one trend day can supply several trades).
5. Give a recommendation as information, not an order: "historically similar trades won 7 of 20 (35%, interval 18-57%), average -420: weak evidence against". Never place, modify or cancel orders from this skill; the user acts in the Signals tab (Act, Analyzer/paper mode by default).

## Rules
- Read-only: never write to the journal tables by hand; rebuild with `journal build [--rebuild]`.
- Backtest results are paper fills at 1-minute closes, gross for indices and with an ESTIMATED spread for stocks; they flatter thin options (OFSS).
- Do not tune a strategy from journal buckets without re-testing the change on all cached days (see the strategy_builder skill and its lessons: a rule picked from one bad day usually describes that day only).
- Far-expiry days (`nearest = 0`) are excluded unless `--include-far`; they behave differently.
- Before finishing, if the analysis found something durable (a condition that clearly helps or hurts a strategy over many days), append it dated to the strategy_builder skill's `knowledge/lessons.md`.
