# CLAUDE.md: script_openalgo (Strategy Lab)

Research, backtest and manage OpenAlgo option strategies against an OpenAlgo server (default `http://127.0.0.1:5000`, broker dhan). Full design: [plan.md](plan.md). Full task history: [task.md](task.md). User manual: [user_guide.md](user_guide.md). This file is the short version of both, kept current.

## Layout
- `src/script_openalgo/` is the package (`script-openalgo` CLI, `uv build` via `build_all.py`).
  - `backtest_core/`: `instruments.py` (per-instrument table), `data_loader.py` (fetch + cache + manifests), `backfill.py` (whole listed life of a contract), `fake_client.py` (simulated OpenAlgo client), `sim_clock.py`, `report.py`, `verify.py` (deterministic fill/P&L check), `multiday.py`, `scaffold.py` + `templates/` (backtest file generator), `bias_report.py`.
  - `orchestrator.py`: test pipeline (phases strategy, data, backtest, review, learn; state in `.claude/state/<slug>.json`).
  - `strategy_db/`: SQLite DB (strategies, runs, jobs, settings, tombstones, ignored_results, claude_calls), CLI, Claude usage gate.
  - `webapp/`: Flask app "Strategy Lab" (`app.py`, `jobs.py`, `chart.py`, `static/index.html`), port 5055, localhost only.
  - `assets/`: agents, the `strategy_builder` skill + knowledge, 12 example strategies (copied into a workspace by `init` only when its `strategies/` is empty).
- `strategies/<slug>.py` and `strategies/backtest_<slug>.py`: strategies (15) and their backtests.
- **This repo is the working workspace**: run fetch, backfill, multiday and backtests with the repo as the working folder (`PYTHONPATH=src`).
- **Git tracks code + the strategy DB only** (decided 2026-10-20): code, `strategies/`, docs, `.claude/agents` and skill knowledge, `strategy_db/default.db` and `strategy_db/specs/`. NOT tracked (see `.gitignore`): `.env` (secret), `data_cache/`, `results/`, `backups/`, `dist/`, `strategy_db/logs|inbox`, `.claude/state/`. Back the workspace data up with `script-openalgo export` (Backup tab): `data_cache/` is irreplaceable because expired option contracts cannot be fetched again.
- **Deployment** (not the working copy): `G:\Claude\test_script\my_workspace` with venv `G:\Claude\test_script\venv` runs the web app from the installed wheel. Code changes reach it only after a rebuild + reinstall; strategy files must be copied into its `strategies/` (assets only seed an empty folder). Agents running in the deployed web app write new strategies and knowledge there: bring them back into the repo.

## Signals tab
`src/script_openalgo/signals.py` runs a strategy's real class as a job (kind `signal`): live (OpenAlgo SDK, real time) or replay (FakeClient on a cached day, full speed until `--from`, then `--speed`x). `snapshot()` records the numbers the strategy keeps on `self` (indicator lists -> latest value, dicts such as `levels`/`position` flattened) as `state` events for the chart's indicator lines and values panel. `SignalRecorder` turns every `placeorder()` into a signal event (paper fill at the current price) in `strategy_db/logs/<ws>/signals_<job>.jsonl`. Web app endpoints `/api/signals/*`; **Act** sends one live signal via `/api/v1/placeorder`, only in OpenAlgo Analyzer mode unless the setting `allow_live_orders` is on (typed LIVE confirmation).

## Strategy options per run
`GET /api/strategies/<id>/params` lists a strategy's simple UPPER_CASE constants (ast; bool -> checkbox, numbers whose comment says "0 = off" -> toggle + value), with the instrument's `strategy_overrides` value. The UI sends changed values as `params` ("NAME=value, ..."); Signals pass them as `--set`, backtests (Run / Run range) as the `STRATEGY_PARAMS` env var (JSON) which `instruments.apply_strategy_overrides` applies last. `report.write_results` stores them in summary.json, `runs.params` keeps them, and `strategy_stats` ignores runs with params.

## Rules that must not be broken
- A backtest **imports and replays the real strategy class** (`Strategy(client, clock, sleeper)`); never re-implement a strategy for a backtest. All tunables are module-level constants.
- No lookahead: FakeClient returns only completed bars; fills are the close of the last completed 1m bar. `verify.py` re-derives every fill from the candles.
- Regression after any change to `backtest_core/`: `nifty_direction_ce_pe` on 2026-10-16 must give NIFTY **-692.25** and BANKNIFTY **-1330.50** gross.
- Gross P&L has no slippage/charges. For stocks read `net_pnl` (estimated spread). Never rank strategies on a low sample (< 20 days).
- Strategies run with OpenAlgo Analyzer (paper) ON until the user explicitly approves live trading. The Signals page's Act button refuses real orders unless the user has turned on `allow_live_orders`.
- Knowledge files in the skill (`knowledge/*.md`) are append-only, dated.

## Instruments
| Instrument | Kind | Underlying | Strikes | Expiry | Lot | Notes |
|---|---|---|---|---|---|---|
| NIFTY | index | NSE_INDEX | 50 | weekly | from API | 375 bars/day |
| BANKNIFTY | index | NSE_INDEX | 100 | monthly | from API | |
| RELIANCE | stock | NSE | 10 | monthly | 500 | stock candles end 15:14 (360 bars); liquid options; spread estimate 0.5 % |
| OFSS | stock | NSE | 50/100 irregular | monthly | 100 | unlisted strikes recorded in manifests; thin options (coverage gate 0.60); spread estimate 1.5 %; DAILY_MAX_LOSS 6000 |

Backtest files call `instruments.apply_strategy_overrides(strat, instrument, PARAM_OVERRIDES)` (sets UNDERLYING, INDEX_EXCHANGE and the instrument's `strategy_overrides`; user overrides win).

**Adding instruments**: web app tab **Instruments** searches OpenAlgo (`/api/v1/search`; the stock list is OpenAlgo's master contract) and adds any underlying with options on NFO. `backtest_core/discovery.py` derives the settings from the listed contracts (exchange NSE/NSE_INDEX, strike step near the last close, weekly/monthly cycle, bars per session, lot); defaults for added stocks: coverage 0.80, spread 1.0 % (edit after the first backfill). Added instruments live in the workspace file `strategy_db/instruments.json` (git-tracked), merged over the built-in `BUILTIN` table by `instruments.reload()`; built-ins cannot be removed. OpenAlgo's search returns at most 500 rows, so search candidates are confirmed with an exact NFO query each.

## Plan summary (plan.md sections)
1-9. Strategy + backtest pair, replay design, data from OpenAlgo `history(source="api")` cached as parquet with per-day manifests, agents (strategy-writer, backtest-writer, backtest-reviewer, data-downloader), orchestrator with a data gate and review loop.
10-13. Decisions, Phase 0 findings (OpenAlgo API quirks), build status.
14. Research agent and strategy queue (researcher finds strategies on the web, dedupes them in the DB, queue is tested one at a time).
15. Web app "Strategy Lab" (strategies, results, data, jobs, backup tabs; workspaces; auto-run queue; Claude usage gate).
16. Stocks and stock options (RELIANCE, OFSS): instrument table, unlisted strikes, per-instrument bars/coverage/spread, FakeClient nearest-cached ATM.

## Status (2026-10-20)
Built: data layer, FakeClient, report/verify, orchestrator, agents, research DB and researcher, web app (strategy page, editing, versions, tags, copy), restart/resume safety, Claude limit hold, Claude usage gate, CPR/PCR helpers, multi-day backfill and runner, package + backup, trade chart with what-if analysis, queued manual runs, stop/reset and progress display, Camarilla ADX/bandwidth regime filter (off by default), cache-first previous-trading-day lookup, **stocks (RELIANCE, OFSS)**, **delete for results and job logs**.

## Open tasks
Stocks (Phase 15)
- [ ] Decide on more stocks after RELIANCE/OFSS; each needs its strike step, coverage and spread estimate checked first.
- [ ] Replace the estimated spread with a measured one (bid/ask snapshots during market hours).
- [ ] Trade the stock itself (equity/futures executor and cost model), only if the option results justify it.
- [ ] Store net P&L in the `runs` table so the web app's stats can use it (today the stats are gross; net is in each run's summary).

Data and scheduling
- [ ] Daily job (~15:45, Windows Task Scheduler): fetch the day, backfill the nearest contracts (so expired-contract data is captured), append to multi-day results, research, test next.
- [ ] Scheduled option-chain snapshots (09:20, 12:00, 15:20) for real full-chain PCR.
- [ ] Per-day OI-based PCR history from the backfilled series.
- [ ] Per-day pre-built 1/5/15-minute bar files to speed up backtests.

Strategies and research
- [ ] vwap_reclaim_scalp, stochastic_oscillator_crossover_scalp and s_9_21_ema_crossover_scalp made 0 trades on every instrument (NIFTY 4 days, BANKNIFTY 12, RELIANCE 13, OFSS 9): check their entry conditions (VWAP/stochastic log nothing at all).
- [ ] Camarilla regime filter: compare filter off / ADX 20 / 25 / 30 over 30-60 days; log ADX at entry for winners vs losers before deciding.
- [ ] Profit-protecting exits (trailing stop / exit after a fixed gain) for strategies that give back early gains (heikin_ashi on 2026-10-15).
- [ ] Robustness runs: entry delay, slippage, parameter variants on top of the multi-day runner.
- [ ] BANKNIFTY parameter set for `nifty_direction_ce_pe.py` (it reuses NIFTY rupee thresholds).
- [ ] Decide whether the bias filter applies to reversal legs in `nifty_direction_ce_pe.py`.
- [ ] Signal-engine contract + index-points executor (second test mode).
- [ ] Seed research run of 10 strategies; review the DB by hand.

Web app
- [ ] Verify the pages in a real browser (only API and JavaScript syntax were tested).
- [ ] Answer flow should remember the instrument of the original job (it uses NIFTY).
- [ ] When the previous-trading-day lookup fails, fall back to the latest cached day and say "check the broker login".
- [ ] Charts: equity curve and daily P&L bars in Results.
- [ ] "Regenerate code from edited rules"; strategy delete should offer to remove code files and versions.
- [ ] Cost guard for auto-run (N strategies per hour/day).
- [ ] Run the app as a background service.

Package and housekeeping
- [ ] You: rotate the exposed API key and keep it only in `.env` / `OPENALGO_API_KEY`.
- [ ] Merge import for two databases; test on Python 3.11/3.12 and another OS; choose a licence.
- [ ] Automatic regression check before accepting agent edits to `backtest_core/`.
- [ ] Orchestrator: a stale `date` or pending answer can persist in `.claude/state/<slug>.json` (use `--restart`).

## Common commands
```
script-openalgo doctor
script-openalgo backfill --instrument RELIANCE --days 30
script-openalgo multiday --strategy <slug> --instrument RELIANCE
script-openalgo webapp
python build_all.py            # build + check the wheel, then pip install --force-reinstall --no-deps dist\<wheel> into the venv
```
Regression (repo root): `PYTHONPATH=src python strategies/backtest_nifty_direction_ce_pe.py --date 2026-10-16 --instrument NIFTY --no-fetch`.
