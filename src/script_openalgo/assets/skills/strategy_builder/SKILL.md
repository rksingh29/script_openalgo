---
name: strategy_builder
description: Build and backtest OpenAlgo trading strategies in this project. Use when creating or changing a strategy script (e.g. nifty_direction_ce_pe.py), its backtest (backtest_<name>.py), or fetching historical data for a backtest from OpenAlgo on port 5000. Holds the strategy contract, workflow and a growing knowledge base.
---

# strategy_builder

Produces two scripts that stay in sync:

| Script | Purpose |
|---|---|
| `strategies/<name>.py` | Live/paper strategy for OpenAlgo |
| `strategies/backtest_<name>.py` | Replays the **same `Strategy` class** on cached historical data |

**All strategy and backtest files live in `strategies/`** (never the project root). Backtest files start with a small bootstrap that finds the project root, so they run from anywhere. Slugs must be valid Python module names and must not shadow a standard-library module.

Design and task list: `plan.md`, `task.md` in the project root. Read `plan.md` before big changes.

## Before you start
Read the files in `knowledge/` (next to this file). They hold verified API behaviour and lessons. **Do not guess API behaviour that is documented there.**

## Strategy contract (every strategy script must follow)
1. All logic lives in a `Strategy` class taking `client`, `clock=datetime.now`, `sleeper=time.sleep`.
2. No `datetime.now()`, `time.sleep()` or network calls outside those three.
3. Only OpenAlgo client methods listed in `knowledge/openalgo_api.md` ("Allowed client surface", which now includes `history` incl. `interval="D"` and `optionchain`), so the fake client can implement them. CPR/PCR helpers: `strategy_lib/levels.py`.
4. `if __name__ == "__main__":` guard, so the file is importable.
5. Tunables are module-level constants (the backtest can override them).
6. API key comes from the `OPENALGO_API_KEY` env var. Never hardcode keys.

## Backtest rules
- Import and run the real `Strategy`. Never re-implement strategy logic in the backtest (that is why `backtest.py` was retired).
- The backtest reads only from `data_cache/`, never from the broker.
- No lookahead: a quote at sim-time T is the last **completed** bar.
- v1: previous trading day only; no slippage or charges (keep `SLIPPAGE_PTS = 0`, `CHARGES_PER_ORDER = 0` as hooks).
- Full rules: `knowledge/backtest_rules.md`.

## Workflow
0. Discovery (done once, see `knowledge/openalgo_api.md`)
1. Spec: clarify the strategy with the user
2. Strategy: write or audit `strategies/<name>.py` against the contract
3. Data: data-downloader agent fills `data_cache/` and writes `manifest_<date>.json`. **Gate: continue only if `complete: true`.**
4. Backtest: `strategies/backtest_<name>.py` is generated from a template (`script-openalgo scaffold <name>`), no agent; an agent is only called if the strategy needs client methods FakeClient lacks or the backtest crashes
5. Review: deterministic `backtest_core/verify.py` first (free: prices re-derived from candles, P&L reconciliation, static contract checks); the AI backtest-reviewer is optional (`--llm-review`); fix loop, max 3 rounds, then ask the user
6. Run: results in `results/<strategy>_<runid>/`
7. Learn: append to `knowledge/`
8. Paper trade (Analyzer ON) only after a satisfactory backtest

## Agent protocol
Agents end with JSON: `{"status": "done" | "needs_input", "questions": [...], "artifacts": [...]}`.
If blocked or the request is ambiguous, return `needs_input` with specific questions. Do not guess.

## Knowledge rule (important)
Before finishing, append anything new you learned to the matching file in `knowledge/`: new API behaviour, a bug and its fix, a user decision, a result. Entries are dated `YYYY-MM-DD`, short, and appended (never silently delete old entries; mark them superseded instead).

| File | Holds |
|---|---|
| `knowledge/openalgo_api.md` | Verified endpoints, response shapes, symbol/expiry formats, data availability, quirks |
| `knowledge/strategy_patterns.md` | Reusable strategy patterns |
| `knowledge/backtest_rules.md` | Fill model, lookahead rules, data cleaning, assumptions |
| `knowledge/lessons.md` | Bugs found and fixes |
| `knowledge/strategy_registry.md` | One line per strategy: file, params, last backtest result |
| `knowledge/research_rules.md` | Researcher agent, strategy DB, daily job, ranking and unattended-run rules |

## Research and ranking (planned; rules in `knowledge/research_rules.md`)
- A `strategy-researcher` agent builds a seed queue of **10 unique strategies** once, then adds **at most 1 new unique strategy per daily run** (none if nothing new is found) to `strategy_db/strategies.db` (SQLite; UNIQUE signature and slug) and exports `STRATEGIES.md`. Instruments: NIFTY and BANKNIFTY.
- Researched strategies are **SignalEngines** (candles in; LONG/SHORT/EXIT out) tested through two executors: option buying (CE long / PE short, ATM) and index points.
- A daily job after market close fetches data, **appends the new day** to every tested strategy, looks for 1 new unique strategy, tests the next queued one on all cached days, and rebuilds the leaderboard. Testing is automatic; nothing is promoted to paper/live automatically.
- Ranking uses all days available; always show `days_tested`, label `low sample` under 20 days and `no costs` while slippage/charges are off.
- Update `knowledge/research_rules.md` whenever these rules change.

## Web app (Strategy Lab)
`script-openalgo webapp` -> http://127.0.0.1:5055. Triggers the researcher, lists strategies with live status, shows results by strategy/date, manages workspaces (separate DB + candle cache), archive/delete. Strategy DB code: `strategy_db/`; jobs: `webapp/jobs.py`. Details: plan.md section 15. The researcher writes to the DB only through `script-openalgo db`.

## Safety
- Strategies place real orders when run live. Default to OpenAlgo Analyzer (paper) mode; never switch it off without the user's say-so.
- Never print or commit API keys.

## Claude usage (cost control)
- Agent steps are ~95% of a pipeline's time and all of its Claude usage; the backtest itself takes seconds to ~1 minute. Prefer deterministic code over agents wherever the job is mechanical (templates, verification).
- Every Claude call goes through `strategy_db/gate.py`: a shared budget (calls per hour/day, minimum gap, optional cost cap) and usage tracking (tokens, cost, duration from the CLI's JSON). A researcher run weighs 3, an agent step 1. When the budget is used up the orchestrator exits with code 5 (`BUDGET_HOLD until=...`) and the web app holds the job and resumes it when the budget frees up. The budget is an estimate; the real limit message is handled the same way.
