# Task list (see plan.md for design)

Legend: [x] done, [ ] todo. Order is the build order.

## Phase 0: Discovery
- [x] Confirm OpenAlgo reachable on :5000 and API key works (broker: dhan)
- [x] Check Historify DB (`source="db"`): empty for NIFTY and options
- [x] Check `source="api"` for NIFTY index and option candles: works
- [x] Check Historify download endpoints: web-session only
- [x] Inspect the extra option bars after 15:30 (padding? zero volume?)
- [x] Test whether the broker serves candles for an already-expired weekly contract
- [x] Check the API rate limit for `history` (needed to throttle the downloader)
- [x] Install `openalgo`, `pandas` and a parquet library in the Python environment
- [x] Remove the hardcoded API key from `backtest.py` (now reads `OPENALGO_API_KEY` env var)
- [ ] You: rotate the exposed API key in OpenAlgo and set `OPENALGO_API_KEY` in your environment

## Phase 1: Skill skeleton
- [x] Create `.claude/skills/strategy_builder/SKILL.md` (workflow, strategy contract, naming rules)
- [x] Seed `knowledge/openalgo_api.md` with Phase 0 findings (endpoints, symbol/expiry formats, quirks)
- [x] Seed `knowledge/strategy_patterns.md`, `backtest_rules.md`, `lessons.md`, `strategy_registry.md`
- [x] Add the append-before-finishing rule for agents to the skill

## Phase 2: Data layer
- [x] `backtest_core/data_loader.py`: previous-trading-day resolver (weekends/holidays)
- [x] Fetch NIFTY 1m, compute day high/low, list candidate strikes (step 50, +/-2 buffer, CE and PE)
- [x] Fetch option series via `history(source="api")` with throttling and retry
- [x] Clean data: filter to the requested date, drop post-15:30 bars, epoch to IST
- [x] Cache to `data_cache/<symbol>_<date>.parquet`
- [x] Verify coverage and write `data_cache/manifest_<date>.json` with a `complete` flag
- [x] Run it for the previous trading day and check the manifest by hand
- [ ] Daily scheduled fetch (after market close) so each day is cached before its contracts expire

## Phase 3: Simulation core
- [x] `backtest_core/sim_clock.py`: `now()` returns datetime on the sim date, `sleep(n)` advances time
- [x] `backtest_core/fake_client.py`: `quotes` (last completed bar, no lookahead)
- [x] `fake_client`: `expiry` (valid expiry for the sim date)
- [x] `fake_client`: `optionsymbol` (ATM from NIFTY at sim time, step 50, lot size 65)
- [x] `fake_client`: `placeorder` and `orderstatus` (fill at current price; slippage and charges hooks set to 0)
- [x] `fake_client`: `analyzerstatus` stub
- [x] `backtest_core/report.py`: trades.csv and summary.json (P&L, win rate, drawdown, cycles, reversals, stop reasons)

## Phase 4: First backtest
- [x] `backtest_nifty_direction_ce_pe.py`: config (dates default to previous trading day), runs the real `Strategy`
- [x] Run for one day and hand-check a few trades against the raw candles
- [x] Confirm the strategy file needs no changes (clock/sleeper contract holds)

## Phase 5: Agents
- [x] `.claude/agents/data-downloader.md`
- [x] `.claude/agents/strategy-writer.md`
- [x] `.claude/agents/backtest-writer.md`
- [x] `.claude/agents/backtest-reviewer.md` (lookahead, contract violations, missing API methods, P&L sanity)
- [x] Define the agent JSON output: `{status: done|needs_input, questions, artifacts}`

## Phase 6: Orchestrator
- [x] `.claude/orchestrator.py`: state machine over the phases, state in `.claude/state/<strategy>.json`
- [x] Run agents through `claude -p`; surface `needs_input` questions to the user and re-invoke with the answers
- [x] Save useful answers to `knowledge/`
- [x] Gate: backtest does not start unless the manifest says `complete: true`
- [x] Review loop: fix until the reviewer passes, max 3 rounds, then ask the user

## Phase 7: Wrap-up
- [x] End-to-end run via the orchestrator: deterministic path works; reviewer agent runs but see open items below
- [x] Update `strategy_registry.md` and `lessons.md` with results (registry gets an UNREVIEWED row per run)
- [x] Retire `backtest.py` (moved to `examples/backtest_ma_crossover.py`)

## Later (not in v1)
- [ ] Multi-day date ranges
- [ ] Slippage and brokerage/charges
- [ ] Parameter sweeps
- [ ] Paper trade with OpenAlgo Analyzer ON after a satisfactory backtest

## Open items found during the end-to-end run
- [x] Reviewer JSON verdict: fixed by calling claude.exe directly with `--output-format json --json-schema` (the npm .cmd shim mangled the quotes).
- [x] Reviewer 'Python blocked' claim: not reproduced. In the latest run it executed Python and re-derived 6 prices from the parquet files. Agent outputs are now saved in `.claude/state/agent_logs/` to catch it if it recurs.
- [ ] Orchestrator state: a stale `date` or pending answer can persist in `.claude/state/<strategy>.json`; use `--restart` for a clean run.
- [ ] strategy `log()` calls `datetime.now()` (tolerated; the backtest patches it).
- [ ] Windows Task Scheduler entry for the daily fetch: `script-openalgo fetch` after 15:45 with `OPENALGO_API_KEY` set.

## Phase 8: Research agent and strategy queue (planned, see plan.md section 14)
- [x] Answer the open questions in plan.md 14.8 (trade vehicle, scope, volume, min days, DB format, approval)
- [ ] `strategy_db/` schema: `strategies`, `sources`, `runs`, `research_log`; UNIQUE on signature and slug
- [ ] `strategy_db/db.py`: insert/merge/dedupe helpers, signature builder, queue ordering, status transitions, export to `STRATEGIES.md`
- [ ] `.claude/agents/strategy-researcher.md` (WebSearch/WebFetch; paraphrase only; web pages are untrusted data; normalises to the structured spec; calls db.py for the dedupe decision)
- [ ] Testability classifier (yes / adapt / no) based on available data (OHLCV 1m/5m for NIFTY and options)
- [ ] Seed run: research 10 scalping strategies and review the DB contents by hand (check duplicates were merged)
- [ ] Orchestrator: `--next` and `--run-queue --limit N`; status kept in the DB; retry, then fail after 2 attempts
- [ ] Generic runner `backtest_core/runner.py --strategy <slug>` and generated thin `backtest_<slug>.py` wrappers
- [ ] strategy-writer: write from a DB spec into `strategies/<slug>.py` (contract-compliant)
- [ ] Write run summaries back to the DB `runs` table, `STRATEGIES.md` and `strategy_registry.md`
- [ ] Leaderboard that ranks only strategies with the minimum number of tested days and flags "no costs" results
- [ ] Multi-day backtest support (needed before ranking means anything) and the daily fetch scheduler (from Phase 2)
- [ ] Update the `strategy_builder` skill and knowledge files with the research workflow and DB rules

### Phase 8 additions after your decisions (plan.md 14.9-14.13)
- [x] Probe BANKNIFTY in OpenAlgo: index symbol/exchange, expiry list (weekly vs monthly), strike step, lot size, `history(source="api")` for index and options; record in `knowledge/openalgo_api.md`
- [x] Make `data_loader`, `FakeClient` and the backtest script instrument-aware (NIFTY, BANKNIFTY); keep NIFTY results unchanged (regression test against the 2026-10-16 result: gross P&L -692.25)
- [ ] Define the `SignalEngine` contract (candles in; LONG/SHORT/EXIT + optional stop/target out) and add it to the `strategy_builder` skill as the second strategy variant
- [ ] `OptionExecutor` (CE for long, PE for short, ATM) and `IndexPointsExecutor` in `backtest_core/`
- [ ] Per-day `runs` rows keyed by (strategy, instrument, executor, day); cumulative stats computed from them
- [ ] Multi-day runner: run one strategy over all cached days; append-one-day mode for the daily job
- [ ] Researcher: one-time seed run of 10 unique strategies, then at most 1 new unique strategy per daily run (none if nothing new); adapts to NIFTY/BANKNIFTY
- [ ] Orchestrator: unattended mode (`needs_input` -> status `needs_review`, move on to the next strategy)
- [ ] Daily job script `daily_job.py` (steps 1-6 in plan.md 14.12) and a Windows Task Scheduler entry for ~15:45 (needs `OPENALGO_API_KEY` and a logged-in `claude` CLI on that machine)
- [ ] Leaderboard (days_tested, avg daily P&L, win-day rate, worst day, drawdown, profit factor; "low sample" under 20 days; "no costs" label) exported into `STRATEGIES.md`
- [ ] Daily summary file (`strategy_db/daily/<date>.md`)
- [ ] BANKNIFTY needs its own parameter set (profit/loss thresholds, readings) for `nifty_direction_ce_pe.py`; today it reuses the NIFTY rupee thresholds
- [ ] `report.py` is still instrument-agnostic but results folders now carry the instrument; check the orchestrator's BANKNIFTY path (it currently runs NIFTY only)

## Phase 9: Web app (built, see plan.md section 15)
- [x] SQLite strategy DB with uniqueness rules, tombstones, queue, runs, jobs, settings (`strategy_db/db.py`)
- [x] Agent-facing CLI `strategy_db/cli.py` (list / add / merge / count-new)
- [x] `strategy-researcher` agent; real run added one unique strategy
- [x] Flask app and API (`webapp/app.py`), job runner with live status (`webapp/jobs.py`)
- [x] UI: Strategies, Results (by date / by strategy), Data, Jobs; workspaces; archive / delete / delete run / delete cached data
- [x] Auto-run queue toggle (one test at a time; failed after 2 attempts; needs-input parked)
- [ ] Confirm the test pipeline finishes for a researched strategy (VWAP Reclaim Scalp): strategy-writer -> backtest-writer -> reviewer (started from the UI; check the Jobs tab)
- [ ] Move generated strategy/backtest files into a `strategies/` folder instead of the project root
- [ ] Signal-engine + index-points executor (plan 14.10) so both test modes exist
- [ ] Charts (equity curve, daily P&L bars) in Results
- [ ] Answer flow: remember the instrument of the original job (currently NIFTY)
- [ ] Run the app as a background service / Task Scheduler entry; add the daily job (fetch, append day, research, test next)
- [ ] Seed run of 10 strategies (click "Research: seed 10"); review the DB contents by hand
- [x] User-provided strategies are added through the Claude chat; `script-openalgo db register <slug> "<Name>"` registers the file in the app (no UI button needed)
- [ ] Note: the backtest-writer agent edited shared `backtest_core/report.py` and `fake_client.py` while building the VWAP backtest; NIFTY (-692.25) and BANKNIFTY (-1330.50) regressions still pass. Add an automatic regression check before accepting agent edits to `backtest_core/`

## Phase 10: Strategy page, tags, copy and edit (built 2026-10-17, see user_guide.md section 11)
- [x] Tags column + market-view buttons (bullish, bearish, neutral, both-sides, hedge) + free tags; tag filter and search in the list
- [x] Researcher agent now outputs tags (market view + style)
- [x] Strategy page: rules editor, strategy code editor, backtest file editor, runs with old/current version marker
- [x] Code saves are syntax-checked, keep version history (`strategy_db/versions/`), restore supported
- [x] Copy a strategy under a new name (copies code files and re-points the backtest module); rename; stats ignore runs older than the last code edit
- [ ] Verify the page visually in the browser (only the API and JavaScript syntax were tested; no browser run by me)
- [x] Restart safety: jobs now run through `webapp/runner.py`, write to their own log file and leave an exit-code file; on startup the app re-attaches. Tested with a simulated crash (one job still running, one finished meanwhile). Logs are per workspace (`strategy_db/logs/<workspace>/`). Applies after you restart the app once with the new code
- [ ] "Regenerate code from the edited rules" button (re-run strategy-writer on a copy)
- [ ] Delete should offer to remove the strategy's code files and version history
- [ ] Do not run two copies of the app on the same port/DB (an old instance answered requests while I tested)
- [x] Resume after a killed process (reboot/crash): a job with no exit-code file becomes `interrupted` and is resumed automatically: test = orchestrator continues from its saved phase (max 3 resumes), research = only the strategies still missing, fetch = re-run (cached series skipped). Tested for fetch with a simulated power cut; the test-pipeline resume relies on the orchestrator's saved phase and was not tested by killing a real pipeline
- [x] All strategy and backtest files moved to `strategies/`; the orchestrator, web app, agents and skill create and look for them there. Backtest files find the project root by themselves (work from any folder). Slugs cannot shadow stdlib/project modules
- [x] Claude usage limit handling: a job that fails with the limit message gets status `limit` (not `failed`), the strategy returns to the queue without a failure count, auto-run pauses until the reset time, manual runs are refused until then, banner in the UI. Needs one app restart to take effect. 14 jobs and 6 strategies wrongly marked failed by the limit were repaired
- [ ] Add a cost guard: limit auto-run to N strategies per hour/day, because each strategy uses several agent runs
- [x] Hold and auto-resume on the Claude limit: strategy status `waiting`, resumes by itself after the reset (even with auto-run off) from the saved phase; research resumes with the missing count; Re-queue cancels a hold; max 6 holds. Tested with a simulated limit message on a scratch workspace; needs one app restart. Not tested with a real limit event

## Phase 11: speed and Claude cost control (built 2026-10-17)
- [x] Backtest file generated from a template (`backtest_core/scaffold.py`); agent only if FakeClient lacks a method the strategy calls or the backtest crashes
- [x] Deterministic review (`backtest_core/verify.py`): fills re-derived from candles, P&L reconciliation, session times, static contract checks; tested on all existing results and on tampered/bad inputs
- [x] AI reviewer is optional (`--llm-review`, UI checkbox); verdict labels: script-checked / reviewed (AI)
- [x] Claude usage tracking per call (tokens, cost, duration) in `claude_calls`; usage card in the Jobs tab and a header counter
- [x] Claude gate: rolling hourly/daily budget, minimum gap, optional cost cap; orchestrator and researcher both reserve calls; over budget -> job held (exit code 5, BUDGET_HOLD) and resumed automatically
- [x] Tested without spending Claude: full pipeline with --no-agents on a strategy lacking a backtest file; budget hold end to end (job `limit`, strategy `waiting`, resume time, held call not counted, manual run refused)
- [ ] Not tested: usage numbers from a real agent call (the CLI envelope has the fields; the first real run will fill the table); researcher run through the gate
- [ ] Per-day data file with pre-built 1/5/15-minute bars (backtests 75 s -> a few seconds; option 3 in the speed discussion)
- [ ] Use a cheaper/faster model for some agents (`--model`)

## Phase 12: CPR and PCR (built 2026-10-17)
- [x] Daily index bars fetched and cached (`data_cache/daily_<INSTRUMENT>.parquet`); manifest requires the previous day's bar
- [x] `strategy_lib/levels.py`: CPR, classic pivots, CPR width/relation, PCR from an option chain, combined `market_bias`; checked against hand-calculated values
- [x] Option-chain snapshots (`data_loader --chain`) and a simulated `optionchain()` (snapshot if saved, else partial chain from cached 1m bars with OI); simulated `history(interval="D")` returns completed days only
- [x] `backtest_core/bias_report.py`: with/against bias per trade
- [x] Optional `BIAS_FILTER` in `nifty_direction_ce_pe.py` (default off; regression unchanged: NIFTY -692.25, BANKNIFTY -1330.50)
- [x] Researcher/writer instructions and knowledge updated (CPR/PCR testable; `filter` tag)
- [ ] Schedule chain snapshots (09:20, 12:00, 15:20) in the daily job so real full-chain PCR builds up
- [ ] Decide whether the bias filter should also apply to REVERSAL legs in `nifty_direction_ce_pe.py` (today it does not; the 13:00 losing PE on 16 Oct was a reversal)
- [ ] Re-run `bias_report` as more days accumulate; judge the idea only on many days
- [ ] Ask the researcher for CPR / PCR strategies and `filter` entries (they are now testable)
- [ ] Verify cached-bar OI against a snapshot taken at the same timestamp (the 17 Oct live check was after the close, when OI had dropped about 20%)

## Phase 13: multi-day option backfill (feasibility confirmed 2026-10-17, NOT built)
- [ ] `backfill` command: fetch each needed option series ONCE for the contract's whole listed life, split into the per-day cache files, write per-day manifests with `expiry`, `days_to_expiry` and `nearest: true/false` (about 56 calls = 2 min for NIFTY 19OCT26, 16 days)
- [ ] Relax the loader's "expiry too far" error into a label (usable with a warning, opt-in)
- [ ] Multi-day backtest runner (`--from/--to`): one run per day, aggregated stats; web app Run with a date range
- [ ] Historical PCR: fetch OI for all strikes of the expiry (about 470 calls = 16 min once, then incremental) to compute full-chain PCR per bar
- [ ] Daily job: every day fetch the nearest contract's history so coverage with the TRUE nearest expiry is continuous from now on

## Phase 13 update: multi-day backfill and runner (BUILT 2026-10-17)
- [x] `backtest_core/backfill.py`: whole listed life of the nearest contract in one call per series; per-day files and manifests; core vs buffer strike completeness; tested on NIFTY 19OCT26 (16 days; 4 nearest-expiry, 12 far-expiry, 5 incomplete)
- [x] Manifests carry `days_to_expiry`, `nearest`, `warnings`; relaxed from an error to a label (multi-day runs skip far-expiry unless `--allow-far`)
- [x] `backtest_core/multiday.py`: per-day runs, per-day script check, aggregate statistics, JSON report; reproduces the single-day result exactly (16 Oct NIFTY -692.25)
- [x] Web app: Run range on the strategy page, Backfill button and expiry labels in the Data tab, far-expiry days kept out of the main stats, runs store code hash / dte / nearest
- [x] Loader reads the project `.env` itself when OPENALGO_API_KEY is not set
- [ ] BANKNIFTY backfill result review (29 days, monthly expiry: nearest from 30 Sep)
- [ ] Scheduled backfill/daily fetch so every new nearest contract is captured (see the retention discussion: keep expired-contract data, compact rather than delete)
- [ ] Per-day OI-based PCR history from the backfilled series; regime/direction gate experiment across all days (next phase)
- [ ] Robustness runs (entry delay, slippage, parameter variants) on top of the multi-day runner

## Phase 14: package, host option, backup (built 2026-10-17)
- [x] Package `script_openalgo` (src layout), central `config.py` (workspace folder, OpenAlgo host via --host / OPENALGO_HOST / toml / default, API key from env or .env)
- [x] CLI `script-openalgo`: init, doctor, fetch, backfill, multiday, verify, bias-report, scaffold, orchestrate, webapp, db, export, import (--host / --home work for every command and child process)
- [x] Agents, skill (+ cleaned knowledge seeds) and 12 example strategies ship inside the package; `init` copies them into the workspace
- [x] `build_all.py` around `uv build`: sync assets, secret scan, build tar.gz + wheel, inspect both, clean-venv install test (10 checks)
- [x] Full export / import (CLI and web Backup tab): SHA-256 manifest, verification before applying, safety backup, conflicts reported, no path escape; round trip 1764/1764 files and identical DB rows
- [x] Proven on a clean environment: installed wheel + new workspace + `--host` to the live OpenAlgo + fetch + backtest = -692.25 (same as before)
- [ ] Merge import for two different databases (today: replace + safety backup)
- [ ] Test on a second operating system / Python 3.11 and 3.12 (only 3.14 was tested; a 3.15 beta crashed pandas)
- [ ] Choose a licence for the package (none set)
- [ ] The Claude CLI is not part of the package; document/verify agent runs on the other machine

## Phase 15: stocks and stock options, first RELIANCE then OFSS (BUILT 2026-10-20, see plan.md section 16)
- [x] Probe OpenAlgo for RELIANCE and OFSS (expiries, lot size, strike step, candle counts, option bars); recorded in plan.md 16.3
- [x] Instrument table: add RELIANCE and OFSS with `underlying_exchange`, `min_underlying_bars` (360 for stocks), `kind`, monthly expiry; keep `index_symbol`/`index_exchange` aliases
- [x] Data loader: use per-instrument minimum bars and exchange (stock candles end at 15:14, so 360 bars, not 370)
- [x] Data loader/backfill: treat "symbol not found" strikes as `unlisted` (OFSS strikes are irregular: 50 near the money, 100 further out); manifest lists `unlisted` and `thin` strikes; per-instrument `min_option_coverage`
- [x] RELIANCE: backfill 30 days of the nearest monthly contract; review the manifest by hand
- [x] FakeClient: ATM = nearest cached strike (not `round(spot/step)*step`); chain from the manifest's strike list
- [x] Backtest runner sets `INDEX_EXCHANGE` as well as `UNDERLYING`; per-instrument overrides for `DAILY_MAX_LOSS` and point constants
- [x] Run one strategy (opening range breakout) on RELIANCE for one day; verify fills with `verify`
- [x] Regression: NIFTY -692.25 (2026-10-16) and BANKNIFTY -1330.50 unchanged
- [x] OFSS: add and backfill; check the thin/unlisted handling; treat all OFSS results as unreliable until a slippage model exists
- [x] Per-instrument slippage (points or % of premium) in the backtest; label stock results "no spread" until then
- [x] Run all strategies over the 30 days for both stocks; compare with NIFTY behaviour
- [x] Check the backfill window for corporate actions (splits/bonus)
- [x] Copy updated strategy/backtest files to the workspace `strategies/` folder (package assets only seed new workspaces)
- [ ] Later, if worthwhile: trade the stock itself (equity/futures executor and cost model)
- Notes from the build (2026-10-20):
  - RELIANCE backfill: 30 days (2026-09-04 .. 10-19), 26 complete, 13 nearest-expiry days (30 Sep .. 19 Oct); strikes every 10, 360 underlying bars, 375 per option.
  - OFSS backfill: 19 days (contract listed 22 Sep), 22 unlisted symbols recorded (10150, 10350, 10650 ...); 9 usable nearest-expiry days, the others incomplete because near-the-money strikes traded in 3-55 % of the minutes.
  - ORB on RELIANCE (13 days): gross +12450, net about +10753 after the 0.5 % spread estimate; all days script-checked; one trade hand-checked (ATM 1170 PE, fills = last completed bar close).
  - ORB on OFSS (9 days): gross -7755, net about -16794 after the 1.5 % spread estimate; ATM fallback checked on 10-13 (spot 10539 -> 10550 unlisted -> 10500 used).
  - No corporate action in the window (largest opening gap RELIANCE 0.9 %, OFSS 2.3 %).
  - Index regressions unchanged (NIFTY -692.25, BANKNIFTY -1330.50); index CSV/summary format unchanged.
  - Spread is an ESTIMATE (slippage_pct), booked in net_pnl only, so fill verification still works.
- [x] Delete for results (per run, per group, in the run window; with or without the result folder; kept folders are not re-imported) and for job logs (per job, "clear finished")
- [ ] Store net P&L in the runs table so the web app's stats can use it (today: gross; net is in each run's summary)
- [ ] Replace the spread estimate with measured bid/ask (snapshots during market hours)
  - All 13 strategies over both stocks: 286 day-runs, all script-checked, no errors. Best net: RELIANCE ORB +10753, nifty_direction_ce_pe +7187, MACD +3457 (13 days, LOW SAMPLE). OFSS: every strategy negative net after the 1.5 % spread estimate.
- [ ] vwap_reclaim_scalp, stochastic_oscillator_crossover_scalp, s_9_21_ema_crossover_scalp: 0 trades on every instrument incl. NIFTY/BANKNIFTY; check their entry conditions
- [x] Instruments tab: search OpenAlgo for underlyings with options, add with auto-derived settings (strike step, expiry cycle, bars per day, lot), edit coverage/spread/daily max loss, remove added ones; stored in `strategy_db/instruments.json` (2026-10-20). Tested end to end with HDFCBANK (add -> backfill 3 days -> ORB run, fills/P&L checked) and then removed again
- [x] Multi-day comparison table shows net (after the estimated spread) next to gross
- [ ] Measure each added stock's real spread (bid/ask snapshots) instead of the 1.0 % default
- [x] Single-day data gate: a thin/missing strike OUTSIDE the day's range (the +/- strike_buffer strikes) is a warning, not an issue, same rule as backfill (MCX 2026-10-16 was blocked by the deep-ITM 2950 CE at 68% coverage; 2026-10-20)
- [x] MCX added to the repo's instrument list (copied from the deployment)
- [x] Signals tab (2026-10-20): choose strategy, instrument, live or replay (date, from time, speed), Start/Stop; chart of the underlying with signal markers, signal table, strategy log, paper P&L; Act sends a live signal to OpenAlgo (Analyzer mode only unless allow_live_orders). Tested: replay ORB RELIANCE 2026-10-12 (same signals as the backtest), stop, live start (market holiday: no candles, ended at square-off), act refused for replay, session delete removes the events file
- [ ] Signals: test a live session during market hours, and Act in Analyzer mode (not done: 2026-10-20 had no market data)
- [ ] Signals: follow the real position after Act (today the page only shows paper signals)
- [x] Signals chart: always candles (it switched to a line above 200 bars); strategy values (EMA, VWAP, OR high/low, Camarilla levels, stop/target) as lines, others (RSI...) in a values panel; click to hide/show
- [ ] Signals: values a strategy only keeps in local variables (not on self) cannot be shown; such strategies would need to store them on self
- [x] Signals: per-session strategy parameters (NAME=value, validated against the strategy file; shown in the session list/header). Checked: parabolic_sar_flip_scalp NIFTY 2026-10-19 replay, default -3129.75 vs CONFIRM_BARS=2 +351 (same as the backtest comparison)
- [x] India VIX (INDIAVIX, NSE_INDEX) cached per day by ensure_data/backfill (context only); Signals chart lower VIX panel + VIX per signal (2026-10-20); 29 cached days back-filled with VIX
- [ ] Strategy filters to test across all days: late-entry (move already extended, e.g. > k x ATR since the swing), chop/sideways (ADX or choppiness), VIX trend at entry for option buyers, max initial stop distance
- [x] parabolic_sar_flip_scalp: VIX_NEAR_HIGH_PCT=10 is the default (user decision 2026-10-21); repo runs regenerated
- [ ] VIX filter robustness: backfill 60+ days, sweep 5-30 %, choose on older days and check on newer ones; consider a range that ignores the opening minutes
- [x] parabolic_sar_flip_scalp: CONFIRM_BARS=2 default as well (user decision 2026-10-21, against the 37-day comparison; revisit after more days)
- [x] BANKNIFTY: CONFIRM_BARS=0 for parabolic_sar_flip_scalp via instruments.py strategy_overrides (user decision 2026-10-21); fixed backtest_parabolic_sar_flip_scalp.py (old template line ignored instrument overrides)
- [x] Strategy options per run in the UI (Signals + strategy page Run/Run range): checkboxes for yes/no and '0 = off' settings, values editable, instrument defaults shown; runs with options labelled and excluded from stats (runs.params); Signals sessions deletable (one / all finished) (2026-10-21)
- [x] Signals: several strategies in one run (up to 8; per-strategy options; one chart with lettered/coloured markers; per-strategy summary and indicators picker; Stop all / Delete) (2026-10-21)
