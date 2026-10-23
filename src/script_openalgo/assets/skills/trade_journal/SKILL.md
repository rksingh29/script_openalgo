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

## Columns
- Identity: `slug` (strategy), `instrument`, `day`, `entry_time`, `exit_time`, `side` (CE = bullish buy, PE = bearish buy), `symbol`, `qty`, `run_id`, `code_hash`.
- Outcome: `pnl` (gross), `net` (after brokerage + statutory charges from costs.py, and the estimated spread for stocks; journal version 2), `win` (net > 0), `hold_min`, `exit_reason`, `opt_mfe` / `opt_mae` (best / worst option P&L in rupees while held).
- Features (context at entry, data completed before that minute): stored as JSON in `features`, with `feature_version`. **List them with `script-openalgo journal features`** (they are pluggable and grow over time). Groups today: time (minute_of_day, weekday, dte), underlying (move from open, day position, 15/30/60-min returns, ATR5, gap, previous range), vix (level, position in day range, 15/30-min change, change from open), volume (underlying and option volume vs their own average so far, option volume), oi (PCR of open interest near ATM, ATM call/put OI change, traded option OI change), premium (option price % of spot, ATM straddle % of spot as an implied-volatility proxy, option 15-min change).
- `journal_days`: per strategy / instrument / day: trades, pnl, net, `day_range_pct`, `day_change_pct`, `gap_pct`, `vix_open`, `vix_close`, `vix_range`.

## Commands
```
script-openalgo journal features
script-openalgo journal query --strategy parabolic_sar_flip_scalp --instrument BANKNIFTY --by vix_pos
script-openalgo journal query --strategy opening_range_breakout_orb --filter "minute_of_day<630" --filter "vix_pos>=50" --by u_ret30_pct
script-openalgo journal similar --strategy parabolic_sar_flip_scalp --instrument NIFTY --side PE --time 12:05 --feature vix=16.85 --feature vix_pos=50 --feature u_day_pos=15 -k 15
script-openalgo journal edges [--strategy S] [--pool] [--min-trades 8 --min-days 5]
script-openalgo journal export --out journal.csv          # all features as columns: model training data
```
- Filters: `--strategy --instrument --side --include-far` and any number of `--filter "<feature><op><value>"` (ops < <= > >= = !=). `--by FEATURE` buckets numeric features into quartiles (categorical ones such as side, exit_reason, instrument, slug by value).
- `similar` returns the k past trades closest to a situation (features scaled by their spread; only the features you give are used).
- `edges`: every strategy x instrument x feature, split in thirds, each third against the rest (shuffle test on average net; win rate shown). It prints how many tests were made, how many would look significant by chance, and Benjamini-Hochberg q-values. **Only q < 0.10 rows are candidates, and a candidate is a hypothesis**, never a rule: confirm it as an optional strategy filter on days it was not found on. The report is saved under `results/journal/`.
- Add `--json` for machine-readable output. For anything else read SQLite directly (read-only).

## How to answer "should I take this signal?" (or "which strategy for today?")
1. Get the situation: strategy, instrument, side, time; India VIX now and its range so far today; the underlying's move since the open and position in its day range. From a Signals session use the signal row and the chart's values; live, read them from OpenAlgo (`quotes`, `history` 1m for the underlying and `INDIAVIX` on `NSE_INDEX`).
2. `journal build`, then `journal similar` with those values (`--feature NAME=value`), and `journal query --strategy ... --instrument ... --by <the feature that matters most>`. Check `journal edges` for that strategy: if no condition reaches q < 0.10, say that the journal shows no reliable condition yet.
3. Report: number of matching trades and days, win rate with its 95% interval, average and total net, profit factor, typical exit reasons and MFE (did winners give back?). Compare with the strategy's overall numbers: a bucket only matters if it differs from the overall result by more than its interval.
4. Say plainly when the evidence is weak: fewer than 30 trades or fewer than 15 days = LOW SAMPLE; an interval that spans 50% means "no edge shown". Days are not independent (one trend day can supply several trades).
5. Give a recommendation as information, not an order: "historically similar trades won 7 of 20 (35%, interval 18-57%), average -420: weak evidence against". Never place, modify or cancel orders from this skill; the user acts in the Signals tab (Act, Analyzer/paper mode by default).

## Rules
- Read-only: never write to the journal tables by hand; rebuild with `journal build [--rebuild]`.
- Backtest results are paper fills at 1-minute closes, net of Dhan brokerage + charges, with an ESTIMATED spread for stocks; they flatter thin options (OFSS).
- Do not tune a strategy from journal buckets without re-testing the change on all cached days (see the strategy_builder skill and its lessons: a rule picked from one bad day usually describes that day only).
- Far-expiry days (`nearest = 0`) are excluded unless `--include-far`; they behave differently.
- Before finishing, if the analysis found something durable (a condition that clearly helps or hurts a strategy over many days), append it dated to the strategy_builder skill's `knowledge/lessons.md`.
