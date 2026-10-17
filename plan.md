# Plan: Strategy Build + Backtest Workflow (OpenAlgo @ :5000)

Status: Phase 0 (discovery) done; building not started. Task list: see `task.md`.

## 1. Goal

Given a strategy idea, produce two scripts that stay in sync:

| Script | Purpose |
|---|---|
| `<name>.py` | Live/paper strategy for OpenAlgo (e.g. `nifty_direction_ce_pe.py`) |
| `backtest_<name>.py` | Replays the **same strategy code** on historical data fetched from OpenAlgo at `http://127.0.0.1:5000` |

Naming rule: backtest file = `backtest_` + strategy file name.

## 2. Key design decision: replay, don't re-implement

The existing `backtest.py` re-implements a different strategy (MA crossover), so it can't validate the real one. The backtest must **import the real strategy class** and drive it with fakes, so the backtest and live logic can never drift apart.

`nifty_direction_ce_pe.py` already supports this via `Strategy(client, clock, sleeper)`.

Standard contract every strategy script must follow (enforced by the skill):
1. All logic in a `Strategy` class taking `client`, `clock`, `sleeper`.
2. No direct `datetime.now()` / `time.sleep()` / network calls outside those three.
3. Only OpenAlgo client methods from an allowed list (see 5.3) so the fake client can implement them.
4. `if __name__ == "__main__":` guard so it can be imported.
5. All tunables are module-level constants (so the backtest can override them for sweeps).

## 3. Components

```
g:\Claude\pyscriptopenalgo\
  plan.md
  nifty_direction_ce_pe.py             # strategy (agent A output)
  backtest_nifty_direction_ce_pe.py    # backtest (agent B output)
  backtest_core\                       # shared, reusable across strategies
    data_loader.py                     # fetch (source="api") + cache history from :5000
    fake_client.py                     # simulated OpenAlgo client
    sim_clock.py                       # simulated clock/sleeper
    report.py                          # trades CSV + summary stats
  data_cache\                          # parquet/csv of fetched candles
  results\                             # per-run trades + summary
  .claude\
    skills\strategy_builder\
      SKILL.md                         # workflow + rules
      knowledge\                       # grows over time (see 4); lives inside the skill folder
    agents\
      strategy-writer.md               # agent A
      backtest-writer.md               # agent B
      backtest-reviewer.md             # agent C (optional, verification)
    orchestrator.py                    # driver (see 6)
```

## 4. Skill: `strategy_builder`

Purpose: single source of truth for how strategies and backtests are built. Knowledge is **additive** so each session improves the next.

`SKILL.md` contains: the workflow (section 7), the strategy contract (section 2), naming rules, and pointers to `knowledge/`.

`knowledge/` files (append-only, dated entries):
- `openalgo_api.md`: verified API calls, response shapes, quirks (e.g. expiry format `21-OCT-26` vs `21OCT26`, `NSE_INDEX` vs `NSE`).
- `strategy_patterns.md`: reusable patterns (cycles, cooldown, reversal, loss caps).
- `backtest_rules.md`: fill model, slippage, charges, lot size, expiry-day handling, lookahead-bias rules.
- `lessons.md`: bugs found and fixes (e.g. "backtest.py reimplemented logic, don't").
- `strategy_registry.md`: one line per strategy: file, params, last backtest result.

Rule: any agent that learns something new must append to `knowledge/` before finishing.

## 5. Backtest design

### 5.1 Data (from OpenAlgo :5000)
- Source: `history(symbol, exchange, interval="1m", start_date, end_date, source="api")` (broker candles via API key). Phase 0 showed Historify (`source="db"`) is empty and its download endpoints need a web login, so Historify is **optional**, not the primary source.
- Needs per trading day: NIFTY index (`NSE_INDEX`) and the candidate ATM CE/PE option candles (`NFO`).
- The backtest reads only from our own cache `data_cache/` (parquet/csv); it never calls the broker. A dedicated **data-downloader agent** fills that cache (see 5.1a).
- Cleaning rules: filter rows to the requested date (end_date returns extra days); keep 09:15-15:29 bars and drop the extra post-15:30 option bars (after checking them); convert epoch seconds to IST.

### 5.1a Data-downloader agent
1. Resolve the target date: **previous trading day** by default (skips weekends/holidays; asks you if ambiguous).
2. Fetch NIFTY 1m (`NSE_INDEX`) first. Take the day high/low and list every strike (step 50) in that range plus a +/-2 strike buffer, CE and PE, for the expiry valid on that date. Build symbols like `NIFTY19OCT2623150CE`.
3. For each series, check the cache for full 09:15-15:29 coverage; build the missing list.
4. Fetch missing series via `history(source="api")`, clean them (5.1), save to `data_cache/<symbol>_<date>.parquet`. Throttle requests and retry on rate-limit errors.
5. Re-verify; write `data_cache/manifest_<date>.json` (symbol, rows, first/last timestamp, gaps, `complete`).
6. If something cannot be fetched (e.g. expired contract the broker no longer serves, or a gap), return `needs_input` listing exactly what is missing rather than letting the backtest run on partial data.

The backtest only starts when the manifest says `complete: true`.

### 5.2 Simulation engine
- `SimClock`: set to a historical timestamp; `sleep(n)` advances time by n seconds and nothing else. Replaces `datetime.now` / `time.sleep`.
- `FakeClient` implements what the strategy calls:
  - `quotes(symbol, exchange)`: returns candle close at sim-time (no future bars; use last completed bar to avoid lookahead).
  - `expiry(...)`: derived from known expiries for that date (not live).
  - `optionsymbol(...)`: ATM strike computed from NIFTY at sim-time, rounded to 50; builds the symbol string; returns `lotsize` for that date (lot size changed historically, so it must be date-aware).
  - `placeorder` / `orderstatus`: fill at current option price plus configurable slippage; record trade; return an order id.
  - `analyzerstatus`: stub.
- Run `Strategy(fake_client, clock=sim.now, sleeper=sim.sleep).run()` once per trading day, resetting state per day.

### 5.3 Allowed client surface
Maintained in `knowledge/openalgo_api.md`. Currently used by the strategy: `quotes`, `expiry`, `optionsymbol`, `placeorder`, `orderstatus`, `analyzerstatus`. If a strategy uses anything else, the backtest-writer must add it to `FakeClient` first.

### 5.4 Assumptions to make explicit (in config at top of backtest)
- **No slippage and no brokerage/charges for now** (fills at the sim-time quote price). Keep `SLIPPAGE_PTS = 0` and `CHARGES_PER_ORDER = 0` as config hooks so they can be turned on later.
- Date range: **previous trading day only** for now (config: `START_DATE`/`END_DATE`, both defaulting to it). Expiry rule: nearest weekly. Skip holidays/no-data days.
- Sim sleep granularity (the strategy sleeps 1s to 60s, so use 1-minute bar resolution, which matches the data).

### 5.5 Outputs
- `results/<strategy>_<runid>/trades.csv`: every entry/exit with reason.
- `summary.json` + printed table: total P&L, win rate, avg win/loss, max drawdown, per-day P&L, cycles per day, reversal count, stop-reason breakdown.
- Optional: parameter sweep (e.g. PROFIT_TARGET, REVERSAL_LOSS) by overriding module constants.

## 6. Agents and orchestrator

| Agent | Input | Output | Notes |
|---|---|---|---|
| **data-downloader** | Strategy file + target date(s) | Fetches every candle series the backtest needs through the API into `data_cache/`; verifies coverage; writes a data manifest | Runs **before** backtest-writer/run; hard gate: no data, no backtest |
| **strategy-writer** | Strategy spec (plain English) + skill knowledge | `<name>.py` following the contract | Also does a static check of the contract |
| **backtest-writer** | Strategy file + `backtest_core` + knowledge | `backtest_<name>.py`; extends `FakeClient` if needed | Must not copy strategy logic |
| **backtest-reviewer** | Both scripts + sample run | Pass/fail report: lookahead bias, contract violations, missing API methods, sanity of P&L | Gate before results are trusted |

### Orchestrator (`.claude/orchestrator.py`)
Runs the pipeline as a state machine and **asks for input through a Claude session** whenever it is blocked, instead of guessing.

How the Claude session is used:
- Each agent is run with the `claude` CLI in print mode (`claude -p`), with its agent prompt and the skill loaded.
- Agents return structured JSON: `{status: "done" | "needs_input", questions: [...], artifacts: [...]}`.
- On `needs_input` the orchestrator surfaces the questions to you in the session (terminal/VS Code), collects your answers, and re-invokes the agent with the answers appended. Answers worth keeping get appended to `knowledge/`.
- State is saved to `.claude/state/<strategy>.json` so a run can pause and resume.

Typical questions it will raise: data that cannot be fetched, ambiguous spec rules, ambiguous spec rules, which dates to test.

## 7. Workflow (end to end)

```
Phase 0  Discovery        DONE (section 11). Remaining: expired-contract test, post-15:30 bar check.
Phase 1  Spec             You describe the strategy (or point to an existing .py).
                          Orchestrator asks clarifying questions.
Phase 2  Strategy         strategy-writer -> <name>.py (or audit existing one vs contract).
Phase 3  Data download    data-downloader agent: compute required symbols for the previous trading
                          day, fetch gaps via the API into data_cache/, verify, write manifest. GATE.
Phase 4  Backtest build   backtest-writer -> backtest_<name>.py (+ backtest_core pieces).
Phase 5  Review           backtest-reviewer; fix loop until pass (max 3 rounds, then ask you).
Phase 6  Run              Execute over the date range; write results/.
Phase 7  Learn            Append findings to knowledge/ and strategy_registry.md.
Phase 8  Paper trade      Only after a satisfactory backtest: run live with Analyzer ON.
```

## 8. Applying it to `nifty_direction_ce_pe.py` (first target)

Already contract-compliant: `Strategy(client, clock, sleeper)`, import-safe `__main__` guard, constants at module level.

Things the backtest must handle specifically:
- `nearest_expiry()` takes `data[0]`; the fake must return the expiry valid on the sim date (and roll on expiry day).
- `optionsymbol(offset="ATM")` is recomputed each cycle, so the fake must compute ATM from NIFTY at sim-time.
- `manage()` loops with `sleep(60)` and checks `now() >= at(SQUARE_OFF_TIME)`; `at()` uses `self.now().replace(...)`, so SimClock must return real `datetime` objects on the sim date.
- `wait_until` and `START_TIME` mean each day's run starts at 09:30 sim-time.
- `LOTS` and lot size changes over history.
- Fill price: `fill_price()` reads `orderstatus().data.average_price`, so the fake must return it.

Minor items noticed in the strategy (not blockers; flag for review):
- `API_KEY` default is a placeholder, fine; keep keys in env vars only. (`backtest.py` has a real key hardcoded, rotate it and remove it.)
- Day P&L is realised-only and excludes charges; the backtest report will show both gross and net of charges.

## 9. Build order

1. Create `strategy_builder` skill skeleton + `knowledge/` seed files.
2. Phase 0 discovery: DONE; option history is available via `source="api"` (section 11).
2a. data-downloader agent + `data_loader.py`; run it for the previous trading day.
3. `backtest_core` (clock, fake client, loader, report).
4. `backtest_nifty_direction_ce_pe.py` for one day; compare against a manual walk-through of that day.
5. Agent definitions + orchestrator.
6. Multi-day run + reviewer pass.
7. Retire `backtest.py` (MA crossover) or move it to `examples/`.

## 10. Decisions and open questions

Decided:
- Date range: previous trading day only (short-term intraday focus).
- A data-downloader agent guarantees the required history is downloaded before any backtest.
- No slippage or brokerage in v1.

Still open:
1. Orchestrator form: Python driver calling `claude -p` (proposed, resumable), or a skill run inside this session?
2. Parameter sweeps: skip in v1 (proposed)?
3. (Resolved) Historify downloads are web-session only; we use `source="api"` instead.

## 11. Phase 0 findings (explored 2026-10-17 against OpenAlgo :5000, broker = dhan)

- Server reachable; `/api/v1/ping`, `expiry`, `optionsymbol`, `quotes`, `intervals` all work with the API key. Intervals offered: 1m, 5m, 15m, 1h, D.
- **Historify local DB (`source="db"`) is empty** for both NIFTY (`NSE_INDEX`) and the option `NIFTY19OCT2623150CE` (`NFO`). Error: "Download data first using Historify."
- **`source="api"` works** and returns real broker candles: NIFTY index 2026-10-16 gave 375 one-minute bars (09:15-15:29); the ATM-area options (23150 CE/PE, 23000 CE) each gave 385 bars (09:15-15:39). So the data-downloader agent can pull everything through the API-key route without Historify.
- **Historify download endpoints** (`/historify/api/...`) are web-session only (redirect to `/auth/login`), so an API key can't drive them. They would need a login session or the UI.
- Expiry list (as of 17 Oct) starts at `19-OCT-26`, and the format for `optionsymbol` is `19OCT26`. Option symbol format: `NIFTY19OCT2623150CE`; lot size currently 65.
- Quirks to handle: (1) a request for 10-15 returned 750 rows running into 10-16, so `end_date` appears to include extra days and results must be filtered by date; (2) option series have 10 extra bars after 15:30 (check whether they're padding/zero volume and drop them); (3) timestamps are epoch seconds (IST day starts at 03:45 UTC).
- Expired contracts: not tested yet. Whether the broker serves candles for an already-expired weekly contract decides if backtesting older days is possible.

**Decision for the plan:** the data-downloader agent fetches via `history(source="api")` and caches to `data_cache/` (parquet/csv) itself, and the backtest reads that cache. Historify is optional (only if you want the data stored in OpenAlgo too). Update sections 5.1/5.1a accordingly.

### Phase 0 results (completed 2026-10-17)
- Bars after 15:29 exist for options (15:30-15:39, with real volume/OI). Rule: drop them; the strategy squares off at 15:15 anyway.
- **Expired contracts are not available.** `NIFTY12OCT2623150CE` and `NIFTY05OCT2623000CE` return "Symbol not found ... master contracts" (400). Only currently listed contracts can be fetched. Consequence: a day can be backtested only while that day's option contracts are still listed. The previous-day default works except right after an expiry (e.g. the day after a Monday expiry: the expired weekly is gone). The downloader must detect this and report it. To backtest many days later, we must **fetch and cache each day's data daily** (a scheduled run) so history builds up.
- Rate limit: 30 consecutive `history` calls in 10.6s, all 200. Throttle ~0.4s between calls (a day needs roughly 25-30 series).
- `openalgo`, `pandas`, `pyarrow` installed.
- `backtest.py` no longer contains the API key.

## 12. Phase 1 done (2026-10-17)
Skill created at `.claude/skills/strategy_builder/` (`SKILL.md` + `knowledge/` with openalgo_api, backtest_rules, strategy_patterns, lessons, strategy_registry), seeded with Phase 0 findings. Added a task for a daily scheduled data fetch.

## 13. Build status (2026-10-17)
Built: `backtest_core/` (data_loader, sim_clock, fake_client, report), `backtest_nifty_direction_ce_pe.py`, four agents in `.claude/agents/`, `.claude/orchestrator.py`. First backtest of 2026-10-16: 3 trades, gross P&L -692.25 (cycle 1 +1856 profit target; cycle 2 -2548 max loss after a reversal). Entry/exit prices hand-checked against the cached candles. Not yet working end to end: the reviewer agent's JSON verdict (see open items at the bottom of task.md).

## 14. Research agent and strategy pipeline (PLAN, not built)

Goal: a **strategy-researcher** agent finds popular scalping strategies worldwide, keeps a de-duplicated database of them, hands them to the orchestrator one at a time in a fixed order, and records a result summary for each tested strategy.

### 14.1 Flow

```
researcher --> strategy DB (status: queued) --> orchestrator --next
      ^                                              |
      |                                  strategy-writer (spec -> <slug>.py)
      |                                  data gate -> backtest -> reviewer
      |                                              |
      +---------- summary + status written back <----+
```

### 14.2 Strategy database
SQLite `strategy_db/strategies.db` plus an auto-exported `strategy_db/STRATEGIES.md` for reading. One row per unique strategy:

| Field | Meaning |
|---|---|
| `id`, `slug` | stable id, filename-safe name (becomes `strategies/<slug>.py`) |
| `name`, `aliases` | main name and other names found for the same idea |
| `family` | momentum, mean-reversion, breakout, VWAP, opening-range, EMA/MA cross, RSI/stochastic, supertrend, price-action/candles, order-flow, pivot, volatility |
| `spec` | structured rules: entry long/short, exit, stop, target, timeframe, indicators + default params, time filters |
| `signature` | hash of the normalised rules (see 14.3) |
| `sources` | URLs, title, date accessed (links plus our own paraphrase of the rules, never copied text) |
| `popularity` | score 1-5 from how many independent sources mention it |
| `data_needs` | `ohlcv_1m` / `ohlcv_5m` / `tick` / `orderbook` / `volume_profile` ... |
| `testable` | yes / adapt / no, with reason (14.4) |
| `status` | `discovered` -> `queued` -> `in_progress` -> `tested` / `rejected` / `untestable` / `failed` |
| `queue_pos` | position in the test sequence (14.5) |
| `created`, `updated` | dates |

Table `runs`: strategy id, date(s), params, trades, gross P&L, win rate, max drawdown, result dir, reviewer verdict, notes. Table `sources` is separate so many sources can point to one strategy. Table `research_log` records what was searched, added and merged.

### 14.3 Uniqueness (the agent must guarantee it)
1. **Normalise** each candidate into the structured spec (indicators, thresholds, entry/exit conditions, timeframe) with parameters bucketed (e.g. "EMA 9/21" and "9 EMA crossing 21 EMA" collapse to one).
2. **Signature** = hash of (family, sorted indicator set, entry rule, exit rule type, timeframe class). Exact match = duplicate: add the source and alias to the existing row, do not create a new one.
3. **Near-duplicate check:** before inserting, the agent compares against all rows of the same family (name, aliases, rules) and decides same / variant / new. A *variant* (same idea, different parameters) is stored as a parameter set on the parent, not as a new strategy.
4. The DB enforces UNIQUE on `signature` and `slug`, so even a sloppy agent run cannot insert a duplicate.
5. Every run appends to `research_log`, so decisions are auditable.

### 14.4 Testability filter (important)
We can only backtest what our data supports: **1-minute and 5-minute OHLCV for the NIFTY index and its option premiums**, from OpenAlgo. So:
- `yes`: rules use OHLCV/indicators only (EMA, VWAP, RSI, supertrend, opening-range breakout, pivots, candle patterns).
- `adapt`: needs a change to fit (e.g. forex or stock scalps converted to NIFTY; rules that depend on volume the index lacks).
- `no`: needs tick data, order book, footprint/volume profile, news or latency edge. Stored as `untestable` with the reason, never silently dropped (so it is not researched again).

The trade vehicle must be fixed first (open question 1 in 14.8). Proposal: new strategies give long/short signals on the NIFTY index and trade by buying CE (long) or PE (short), as the existing strategy does.

### 14.5 Queue and sequence
- Order = `priority = popularity x testability weight` (yes = 1.0, adapt = 0.5), ties broken by oldest discovery. `queue_pos` is assigned once and kept stable; you can reorder or pin a strategy.
- New orchestrator modes: `python .claude/orchestrator.py --next` (take the next queued strategy) and `--run-queue --limit N`. It sets the strategy `in_progress`, then runs the existing phases.
- One strategy at a time. Status lives in the DB so runs are crash/pause-safe; a strategy stuck `in_progress` is retried, then marked `failed` after 2 attempts.

### 14.6 Per-strategy pipeline (reuses what exists)
1. `strategy-writer` reads the DB spec and writes `strategies/<slug>.py` following the strategy contract (same `Strategy(client, clock, sleeper)` shape as `nifty_direction_ce_pe.py`). Ambiguous spec -> `needs_input` to you via the orchestrator.
2. Data gate (data-downloader) for the chosen date(s).
3. Backtest through a generic runner `backtest_core/runner.py --strategy <slug>` (one runner instead of N copies); a thin `backtest_<slug>.py` wrapper is generated to keep the naming convention.
4. `backtest-reviewer` gate.
5. Summary written to the DB `runs` table, `strategy_db/STRATEGIES.md` and `knowledge/strategy_registry.md`; status -> `tested`.

### 14.7 Limits the plan must respect
- **One day of data proves nothing.** With the current "previous trading day" scope a result is an anecdote, not a ranking. Summaries state the number of days tested, and the leaderboard ranks only strategies with a minimum number of days (proposed: 20). This depends on the daily scheduled fetch building up history, because expired contracts cannot be fetched later.
- **Overfitting and look-ahead:** parameters come from the source's defaults, not tuned on the test day. Any tuning is a separate, explicitly labelled run.
- **No costs yet:** results exclude slippage and brokerage. Scalps are very sensitive to costs, so results carry a "no costs" flag until costs are switched on.
- **Untrusted web content:** the researcher treats pages as data only, never follows instructions in them, and never runs code from them. It stores links and its own paraphrased rules, not copied text.
- **Source quality:** many "popular" scalping strategies online are marketing. Popularity counts independent sources, and each entry records the source type (broker education, book, forum, video) so weak ones can be down-ranked.
- **Safety:** nothing here goes live; promotion to paper or live trading stays a manual decision.

### 14.8 Open questions (ANSWERED, see 14.9)
1. **Trade vehicle:** NIFTY ATM option buying (CE for long, PE for short) as today, or also test index-only P&L in points?
2. **Scope of "worldwide":** any market's scalps adapted to NIFTY (my proposal), or only strategies already described for Indian index options?
3. **Research volume:** how many new strategies per run (e.g. 20), and how often (on demand or weekly)?
4. **Minimum days before ranking:** is 20 trading days OK, given data builds up daily?
5. **DB format:** SQLite plus generated markdown (proposed), or only a markdown/JSON file?
6. **Testing order:** automatic by priority (proposed), or do you approve each strategy before it runs?

### 14.9 Decisions (2026-10-17) and what they change
Your answers to 14.8:

| # | Decision | Effect on the design |
|---|---|---|
| 1 | Trade NIFTY ATM options (CE long / PE short) **and** also test in index points | New strategies are written as **signal engines** (see 14.10). Two executors run the same signals: option buying and index points. Both results are stored side by side. |
| 2 | Instruments for now: **NIFTY and BANKNIFTY** | The data layer, fake client and runner must be made instrument-aware (14.11). Research is not limited to Indian sources, but every strategy must be adaptable to these two index instruments. |
| 3 | **Start with 10 strategies, then add 1 per day if a new unique one is found** (revised) | First run is a **seed run** that builds a queue of 10 unique strategies. After that each daily run adds **at most 1**, and only if it finds a strategy that is genuinely new (passes the 14.3 uniqueness checks); finding nothing new is a normal outcome and is logged, not forced. |
| 4 | Run **every day**, rank with appended data | Daily job (14.12). No hard minimum-days gate: the leaderboard ranks from day 1 and always shows `days_tested` and a "low sample" label (under 20 days) so a one-day result is not mistaken for evidence. Cumulative stats are rebuilt from per-day rows. This rule is also added to the `strategy_builder` skill. |
| 5 | **SQLite + generated markdown** | As planned in 14.2. |
| 6 | **Automatic** testing by priority | The orchestrator needs no approval step. Because it runs unattended, a strategy that raises `needs_input` is parked as `needs_review` and the queue moves on instead of blocking. Auto applies to backtests only; nothing is ever promoted to paper or live trading automatically. |

### 14.10 Strategy shape for researched strategies (signal engine + executors)
The existing `nifty_direction_ce_pe.py` places option orders itself. For researched strategies that would mean rewriting order logic N times and could not support the index-points test. So new strategies are split:
- `strategies/<slug>.py` has a `SignalEngine` class: it receives completed candles (1m/5m of the underlying) and returns `LONG`, `SHORT` or `EXIT`, plus stop and target in index points if the rules define them. It has no API calls, no clock and no orders (so it is trivially testable and cannot look ahead).
- Executors live in `backtest_core/`: **OptionExecutor** (LONG buys the ATM CE, SHORT buys the ATM PE, exit sells it; fills from cached option candles as today) and **IndexPointsExecutor** (P&L in index points from the underlying candles).
- For a live run later, a matching live executor would use the same signal engine; that is out of scope for now.
- `nifty_direction_ce_pe.py` stays as it is (it is the reference "option-native" strategy); the strategy contract in the skill gets a second variant for signal engines.

### 14.11 Making the data and simulation layers instrument-aware
BANKNIFTY differs from NIFTY, so nothing may stay hardcoded to NIFTY. Per-instrument config (to be verified by a Phase 0 style probe, not assumed): index symbol and exchange, option exchange, strike step, lot size (read from `optionsymbol`), the expiry list (BANKNIFTY may have only monthly expiries, which changes how the expiry valid on a date is chosen and how far back contracts stay listed), and the candidate-strike range logic. `data_loader`, `FakeClient`, `report` and the runner take an `instrument` parameter. The daily fetch covers both instruments.

### 14.12 Daily job (runs after market close, e.g. 15:45 via Windows Task Scheduler)
1. Fetch and cache today's candles for NIFTY and BANKNIFTY (underlying + candidate option strikes). This is also what keeps history growing while contracts are still listed.
2. **Append the new day** to every strategy with status `tested`: run it on today, store one `runs` row per (strategy, instrument, executor, day).
3. **Research for 1 new strategy** (14.3 uniqueness rules): add it to the queue only if it is new; otherwise log "nothing new today". (The 10-strategy seed run happens once, before the first daily job.)
4. **Test the next queued strategy** (priority order) on **all cached days**, both instruments and both executors, through the full pipeline (writer, backtest, reviewer).
5. Rebuild the leaderboard and `strategy_db/STRATEGIES.md`; append a dated line to `knowledge/strategy_registry.md`.
6. Write a short daily summary (what was added, what was tested, top 5, anything parked as `needs_review`).

Failure handling: any step failing is logged and does not stop the later steps; a strategy failing twice becomes `failed` with the reason. Nothing in this job places real orders.

### 14.13 Leaderboard rules
Per (strategy, instrument, executor): `days_tested`, total and average daily P&L, win-day rate, worst day, max drawdown, profit factor, trades per day. Ranked by average daily P&L with ties/low samples flagged. Rows are labelled "no costs" until slippage and charges are enabled. Because ranking happens on tiny samples early on, the label `low sample` stays until `days_tested` reaches 20; that label is informational, not a gate.

### 14.14 BANKNIFTY findings and refactor (2026-10-17, done)
Probe against OpenAlgo :5000 (details in `knowledge/openalgo_api.md`):
- Index `BANKNIFTY` on `NSE_INDEX`; 375 one-minute bars for 2026-10-16 via `source="api"`; Historify (`source="db"`) empty, same as NIFTY.
- **Monthly expiries only** as listed: 27-OCT-26, 23-NOV-26, 29-DEC-26, 30-MAR-27 ... So the nearest listed expiry can be up to ~5 weeks away; the "expired contract" test uses a 35-day threshold for BANKNIFTY (8 for NIFTY).
- **Strike step 100** near the money (ATM 55800 for spot 55844; ITM1 55700). **Lot size 30**, freeze qty 600.
- Option candles via API: 385 bars (09:15-15:39), same shape as NIFTY.
- Done: `backtest_core/instruments.py` (per-instrument config), `data_loader` / `FakeClient` / backtest script take `--instrument NIFTY|BANKNIFTY`; manifests are now `manifest_<INSTRUMENT>_<date>.json`. The NIFTY regression result is unchanged (gross P&L -692.25).
- First BANKNIFTY run of the existing strategy on 2026-10-16: 3 trades, gross P&L **-1330.50** (cycle 1 +1222.5 profit target; cycle 2 -2553 max loss after a reversal). Note the strategy's rupee thresholds (profit 1000, loss 2500) were set for NIFTY and are tighter in points on BANKNIFTY (lot 30, premiums ~450-700), so BANKNIFTY needs its own parameter set before its results are compared with NIFTY.

## 15. Web app "Strategy Lab" (built 2026-10-17)

Run: `script-openalgo webapp` then open http://127.0.0.1:5055 (port overridable with env `STRATEGY_LAB_PORT`; 8765 was already taken on this machine). Needs `pip install flask`. The OpenAlgo key is read from the environment or the project `.env` by the child processes only.

### 15.1 What it does
| Need | Where |
|---|---|
| Trigger the strategy-researcher | Header buttons: **Research: seed 10** (one-time) and **Research: find 1 new** (daily). One researcher job at a time. |
| See all strategies and their status | **Strategies** tab: queue position, status (queued / in progress / tested / failed / needs review / untestable / archived), testable flag, per-instrument performance (days, total, average, "low sample"). Click a name for rules, sources, runs. |
| Live status while a backtest runs | `in progress` badge with spinner plus the current phase (strategy, data, backtest, review, learn) and the agent working. Polls every 3 s. **Jobs** tab shows every job with its log, Stop button, and (if the pipeline needs input) the question with an answer box. |
| Results strategy-wise and date-wise | **Results** tab: toggle "By date" / "By strategy". Click a run for its summary, trades and log tail. |
| Multiple data sources / DBs | **Workspaces**: each workspace = its own SQLite file + its own candle-cache folder. Header dropdown switches; "+ workspace" creates one (e.g. when data grows). Registry in `strategy_db/workspaces.json`. |
| Delete or archive | Per strategy: **Archive / Unarchive** (hidden, reversible, still blocks re-adding), **Delete** (typed confirmation; removes the strategy and its runs, optional result folders; keeps a tombstone so the researcher never re-adds it; strategy `.py` files are never deleted). Per run: delete. **Data** tab: delete cached candles for an instrument+day (warns that expired contracts cannot be fetched again). |
| Auto testing | "Auto-run queue" checkbox: tests the next queued strategy (pinned first, then popularity x testability) whenever no test is running, one at a time. A strategy failing twice becomes `failed`; one needing input becomes `needs review` and the queue moves on. |

### 15.2 Files
- `strategy_db/db.py` (schema, uniqueness rules, queue, runs, stats, archive/delete, workspaces), `strategy_db/cli.py` (the only way the researcher writes to the DB)
- `webapp/app.py` (Flask API), `webapp/jobs.py` (child processes + live status), `webapp/static/index.html` (UI, no build step)
- `.claude/agents/strategy-researcher.md`

### 15.3 Safety decisions
- Binds to 127.0.0.1 only; rejects other Host headers; mutating requests need the `X-Requested-With: strategy-lab` header (blocks cross-site form posts).
- Everything from the web or the DB is shown via safe DOM text (no HTML injection from researched content). Source links are only clickable when they start with http(s).
- The researcher may only run `script-openalgo db ...` as a shell command, read files, search/fetch the web, and write its spec JSON (instructed to `strategy_db/inbox/`).
- Backtest result folders are only deleted when inside `results/`.

### 15.4 Verified on 2026-10-17
- Dedupe rules via the CLI: exact duplicate merged (sources/aliases added), different idea added, untestable stored but not queued, invalid spec rejected, deleted strategy not re-added (tombstone).
- API: guards (403 without header / bad host), workspaces (create, switch, isolation, name validation), data fetch job, results and run detail.
- **Real researcher run** (daily, N=1) added "VWAP Reclaim Scalp": family vwap, 5m, testable=adapt (RSI zone and premarket target need index substitutes), 3 independent sources (two Indian broker education pages and one blog), rules paraphrased.
- Test pipeline started from the UI on that strategy (strategy-writer first); live status visible in the UI.

### 15.5 Known limits / next
- The pipeline path used by the app for researched strategies is the existing one (full `Strategy` class with option buying through strategy-writer, backtest-writer, reviewer). The planned signal-engine + index-points executors (14.10) are not built, so the "index points" test is not available yet.
- The orchestrator creates `<slug>.py` and `backtest_<slug>.py` in the project root; this will get cluttered, so a `strategies/` folder is a follow-up.
- Answering a `needs input` question re-runs the pipeline for NIFTY only.
- No charts yet (tables only); no authentication beyond localhost binding.
- The app has to be started by you; it does not run as a service or on a schedule yet (daily job is still open).

### 15.6 Strategy page, tags, copy/edit (2026-10-17)
- `strategy_db/library.py`: tags, rules edit, rename, copy (copies `<slug>.py` and `backtest_<slug>.py`, re-pointing the backtest to the new module), code save with syntax check and version history in `strategy_db/versions/<slug>/`.
- API: `/api/tags`, `/api/strategies/<id>/tags|spec|rename|copy|code|code/version|code/restore`. Code files are only touched for slugs registered in the DB, only in the project root; version names are validated.
- Stats count only runs made after the strategy code was last edited (`code_edited_at`); older runs remain visible, labelled "old version".
- Tags: free text, normalised (lower-case, a-z 0-9 - _), max 20 per strategy; suggested market-view tags bullish / bearish / neutral / both-sides / hedge.
- Tested through the API (tags, rules, rename, copy incl. file copy, syntax-error block, versions, restore, warnings, delete) and the stats rule on a scratch DB. The browser page itself was only syntax-checked.
