# User guide: Strategy Lab (step by step)

For: you, running the strategy research and backtest workflow on this Windows machine.
Project folder: `G:\Claude\pyscriptopenalgo`. Commands are for PowerShell, run from that folder.

What it does: finds scalping strategies on the web, keeps a de-duplicated list, backtests them on your OpenAlgo candle data (NIFTY and BANKNIFTY options), and shows status and results in a browser. **Backtests only. Nothing here places real orders.**

---

## 1. One-time setup

### 1.1 Requirements
| Need | Check |
|---|---|
| OpenAlgo running on port 5000 | open http://127.0.0.1:5000 in a browser |
| Python 3.10+ | `python --version` |
| Claude Code CLI, logged in | `claude --version`, then `claude` once to confirm you are signed in |

### 1.2 Install the Python packages
```powershell
pip install openalgo pandas pyarrow requests flask
```

### 1.3 Set your OpenAlgo API key (and rotate the old one)
The old key was visible in a file earlier, so **create a new key in OpenAlgo** (API Key page) and stop using the old one.

Put the new key in a file named `.env` in the project folder:
```
OPENALGO_API_KEY=your-new-key-here
```
Optional, only if OpenAlgo is not on the default address:
```
OPENALGO_HOST=http://127.0.0.1:5000
```
Never paste the key into scripts or share `.env`. The web app and the scripts read it themselves.

For scripts you run by hand in PowerShell (not through the web app), also set it in that window:
```powershell
$env:OPENALGO_API_KEY = "your-new-key-here"
```

### 1.4 Check the connection
```powershell
script-openalgo fetch --instrument NIFTY
```
Expected: it prints the previous trading day, the NIFTY range, the expiry, and `manifest: complete=True`. If it says the key is missing, fix section 1.3. If it cannot connect, start OpenAlgo.

---

## 2. Start the web app

```powershell
script-openalgo webapp
```
Open **http://127.0.0.1:5055**. Keep the PowerShell window open; closing it stops the app. (To use another port: `$env:STRATEGY_LAB_PORT = "5100"` before starting.)

A yellow banner at the top means the API key was not found (go back to 1.3).

The first start registers your own strategy, **Nifty Direction CE/PE**, with its existing results.

---

## 3. First-time workflow

### Step 1: Fetch data
Open the **Data** tab. Pick `ALL` (NIFTY and BANKNIFTY), leave the date empty (= previous trading day), click **Fetch data from OpenAlgo**. Watch it in the **Jobs** tab. When done, **Data** lists the cached day with `Complete`.

Why first: OpenAlgo cannot give you candles for option contracts that have expired. Fetch each day while its contracts are still listed (ideally every evening after 15:45).

### Step 2: Research strategies
Click **Research: seed 10** once. The researcher agent searches the web and adds up to 10 unique strategies. It takes several minutes and uses your Claude plan. Strategies appear in the **Strategies** tab as it works; the "Researcher activity" box shows each add or merge.

After the seed, use **Research: find 1 new** (about once a day). "Nothing new found" is a normal result.

### Step 3: Review what it found
In **Strategies**, click a name to see the rules, parameters, and source links. Check:
- `testable`: `yes` can be tested as is, `adapt` needs changes to fit the index, `untestable` needs data we do not have (tick or order book); those are stored but not queued.
- Sources: open them if you want to confirm the rules.

If a strategy looks wrong or useless, **Archive** it (hides it, reversible) or **Delete** it (type DELETE; the researcher will not add it again).

### Step 4: Test a strategy
Two ways:
- **Manually:** on a row choose the instrument (and optionally a date; empty = previous trading day) and click **Run**.
- **Automatically:** tick **Auto-run queue** in the header. The app tests the next queued strategy (pinned first, then by popularity), one at a time.

You can watch it: the row shows an **in progress** badge with the current phase:
`strategy` (agent writes the code) → `data` (checks the cache) → `backtest` → `review` (agent checks the result) → `learn`.
A new strategy usually takes 20 to 40 minutes for the first run because agents write and review the code. Re-runs of existing ones are faster.

### Step 5: Read the results
Open the **Results** tab. Switch between **By date** and **By strategy**. Click a run for the summary, each trade, and the log tail.

How to read them:
- P&L is **gross**: no slippage or brokerage yet.
- With only a few days of data, a ranking means almost nothing. Rows say **low sample** until a strategy has 20 days.
- A `reviewed` mark means the review agent re-checked prices against the candles; `unreviewed` means it did not.

---

## 4. When something needs your input, or fails

**Researched strategies can need an answer or fail too.** The researcher only stores the rules; a coding agent then writes the strategy and backtest from those paraphrased rules, and that step can hit an unclear rule or a bug. I expect it to be rarer than with unclear hand-written specs, but I have not yet seen a researched strategy finish the whole pipeline (the first one, VWAP Reclaim Scalp, was still in its backtest step when I last checked). Treat the first few as a test of the process.

What happens:
- **Needs input:** if an agent cannot continue without a decision, the strategy turns **needs review**. Open the **Jobs** tab, read the question, type your answer, click **Send**. Other strategies keep running meanwhile.
- **Failed:** a strategy that fails twice becomes **failed**. Open **Jobs**, click **Log** on its job to see why, then fix the cause and use **Re-queue** on the strategy row (or **Archive** it if it is not worth the effort).

Common causes:
| Cause | What to do |
|---|---|
| Rule is vague (for example "strong volume" with no number) | Answer with a concrete number or choice, or archive the strategy |
| The rule needs data we do not have | Archive it; it should have been `untestable` |
| Agent wrote code that crashes or the reviewer fails it 3 times | Read the log; re-queue to retry; if it fails again, archive it |
| Data missing for the date | Fetch data first (section 3, step 1), then re-queue |

**Your own hand-written strategies:** send the strategy to Claude in the chat (a description or the file). Claude writes `strategies\<name>.py` to follow the strategy contract, registers it with `script-openalgo db register <name> "<Display name>"`, and it then appears in the **Strategies** tab like the others. If the pipeline needs a decision, Claude asks you in the chat and enters your answer, or you can type it in the **Jobs** tab.

---

## 5. Daily routine (about 5 minutes)

1. After 15:45 (market closed): **Data** tab, fetch `ALL`.
2. Click **Research: find 1 new**.
3. Make sure **Auto-run queue** is on (or click **Run** on chosen strategies). To add the new day to a tested strategy, click **Run** on it with that day's date.
4. Next morning: look at **Results**, archive what is clearly not working.

---

## 6. Managing data and storage

| Task | How |
|---|---|
| See cached days | **Data** tab |
| Delete a cached day | **Data** tab, **Delete** (confirm). That day's candles are gone; expired contracts cannot be fetched again |
| Archive a strategy | **Strategies**, **Archive**; tick "archived" in the filter to see it, **Unarchive** to restore |
| Delete a strategy | **Strategies**, **Delete**, type DELETE. Optionally also delete its result folders. Strategy `.py` files are never deleted |
| Delete one run | open the strategy, click the × on the run |
| New database when data grows | header **+ workspace**: gives a new database file and a new candle folder. Switch with the **Workspace** dropdown. Each workspace has its own strategies, runs and data |

---

## 7. Running things from PowerShell (without the web app)

Fetch data for a day (or `ALL` instruments):
```powershell
script-openalgo fetch --instrument ALL --date 2026-10-16
```
Backtest your own strategy on a day (needs the data above):
```powershell
python strategies/backtest_nifty_direction_ce_pe.py --date 2026-10-16 --instrument NIFTY --no-fetch
python strategies/backtest_nifty_direction_ce_pe.py --date 2026-10-16 --instrument BANKNIFTY --no-fetch
```
Run the full pipeline for one strategy (data check, backtest, review) from the command line:
```powershell
script-openalgo orchestrate --strategy nifty_direction_ce_pe --date 2026-10-16 --instrument NIFTY --restart
```
If it prints `NEEDS_INPUT`, it needs an answer: run the same command with `--answer "your answer"` (without `--restart`). Add `--no-agents` to skip the Claude agents (faster, but the result is marked UNREVIEWED).

Results are written to `results\<strategy>_<instrument>_<date>_<time>\` (`trades.csv`, `summary.json`, `log.txt`).

---

## 8. Troubleshooting

| Problem | Likely cause and fix |
|---|---|
| Yellow banner: API key not found | Create `.env` as in 1.3, restart the app |
| "another test is already running" | One test at a time. Wait, or **Stop** it in the Jobs tab |
| Data job says "no data" / not a trading day | Weekend or holiday. Use a trading day |
| Data issue "nearest listed expiry … has probably expired" | The contracts for that day are no longer listed. That day cannot be backtested now; fetch daily from now on |
| Strategy stuck on **in progress** after you closed the app | Restart the app; it marks orphaned jobs as failed and re-queues the strategy |
| Port already in use | Set a different `STRATEGY_LAB_PORT` |
| Jobs show "held: Claude usage limit reached" | Your Claude usage limit is reached (agents run through your Claude account, the same pool as the chat). Nothing failed: the strategy shows status **waiting** with the time it resumes, and the app continues it **by itself after the reset**, even with Auto-run off, from the phase it had reached. A yellow banner shows the time. To cancel the hold, click **Re-queue** on the strategy. After 6 holds in a row the strategy is marked failed |
| Researcher or agents do nothing / fail at once | Run `claude` in a terminal to check you are logged in; read the job log (Jobs tab) |
| BANKNIFTY results look much worse than NIFTY | Expected for now: the profit/loss limits in your strategy are NIFTY-sized (₹1000 target, ₹2500 max loss). BANKNIFTY needs its own parameters |

---

## 9. Safety notes

- Everything here is a **backtest**. Do not trade a strategy live because of a backtest on one or a few days.
- Results exclude slippage and charges, which matter a lot for scalping.
- The researcher reads web pages; its stored rules are paraphrased, and it is told to treat pages as data only. Still, read the rules and sources of anything you plan to rely on.
- The web app only listens on your own computer (127.0.0.1) and has no login. Do not expose it to a network.
- Keep `.env` private and out of any shared folder or repository.

---

## 10. Where things are

| Path | What |
|---|---|
| `strategies\` | **All strategy files** (`<name>.py`) and their backtests (`backtest_<name>.py`). New strategies are created here |
| `webapp\` | The web app (`app.py`, `jobs.py`, `static\index.html`) |
| `strategy_db\` | Database code, workspaces list, databases (`*.db`), job logs, researcher inbox |
| `backtest_core\` | Data loader, simulated client and clock, report writer, instrument settings |
| `.claude\agents\` | The agents (researcher, strategy-writer, backtest-writer, backtest-reviewer, data-downloader) |
| `.claude\orchestrator.py` | The pipeline runner used by the web app |
| `.claude\skills\strategy_builder\` | The skill and its growing knowledge files |
| `data_cache\` | Cached candles for the default workspace |
| `results\` | Backtest results per run |
| `plan.md`, `task.md` | Design and task list (what is done and what is open) |

---

## 11. Strategy page: view, edit, tag, copy (new)

Click a strategy **name** in the Strategies tab to open its page (address `#/strategy/<id>`). Use **← All strategies** to go back.

**Top of the page**
- **Run** (instrument and optional date), **Copy…**, **Rename**, **Archive**, **Delete**.
- **Market view** buttons: `bullish`, `bearish`, `neutral`, `both-sides`, `hedge`. Click to switch on or off. Use `both-sides` for strategies that trade up and down moves, `hedge` for something that offsets another position.
- **Tags**: any tags you like (`scalp`, `favourite`, `high-risk`, …). Click a tag to remove it; type a new one and press Enter. Tags are lower-case, up to 20 per strategy.
- In the Strategies tab, tags show under each name; click one to filter, or use the **all tags** dropdown and the search box.
- The researcher now tags new strategies itself with a market-view tag plus style tags. Check them, because they come from its reading of the sources.

**Tabs**
| Tab | What you can do |
|---|---|
| Rules | Edit the stored rules (entry, exit, stop, target, filters, parameters, notes). This changes the description only; it does **not** change the code |
| Strategy code | Edit the Python file. Save is blocked on a syntax error; a missing `Strategy` class or missing `client, clock, sleeper` arguments gives a warning. Every save keeps the previous version (View / Restore below the editor) |
| Backtest file | Same editor for `strategies/backtest_<name>.py` |
| Runs | All runs; runs made before your last code edit are labelled **old version** |

**To change a strategy and test the change**
1. Open the strategy, click **Copy…** and give a new name (keeps the original untouched). The copy gets its own code files, marked *copied from …*.
2. In the copy, edit the **Strategy code** (and the Rules text so they match), click **Save**.
3. Click **Run**. Results appear under the copy.

If you edit a strategy in place instead of copying, its earlier runs stay visible but are marked **old version** and left out of the performance numbers until you run it again.

Notes
- You cannot edit the code while that strategy's test is running.
- Editing the rules of a researched strategy can make it identical to another one; the app then refuses the change and tells you which one.
- Delete never removes the `.py` files. Files of a deleted copy stay in the `strategies` folder; delete them by hand if you do not want them.
- **Restart the app after updating** (stop it with Ctrl+C in its window, start it again with `script-openalgo webapp`). From the first restart onward, running tests and research survive a restart: the new app picks them up again. The restart that installs this change is the last one that can lose a running test, so do it when no test is running.
- Make sure only **one** copy of the app is running (check for a second window); two copies on the same port confuse each other.

### Restarts and interruptions
- **Restarting the app** while a test or research job runs: the job keeps running, and the new app picks it up again.
- **Computer restarted or the job was killed:** on the next app start the job shows as **interrupted** and is resumed automatically (a test continues from the phase it had reached, research asks only for the strategies still missing, a data fetch skips what is already cached). A test that is interrupted more than 3 times is marked failed.
- Stopping a job with **Stop** is not an interruption: it stays stopped and the strategy goes back to the queue.

### Claude usage
Every agent step (researcher, strategy-writer, and only when needed backtest-writer and reviewer) runs the Claude Code CLI with **your Claude account**, so it uses your plan's usage limits, shared with your chat and VS Code sessions. One new strategy needs several agent runs, and the 10-strategy seed does many web searches. To avoid hitting the limit: keep **Auto-run queue** off unless you want a long unattended run, test a few strategies at a time, and use the Jobs tab to see what is running. When the limit is reached the app holds the work and resumes it by itself after the reset (section 8). A data fetch does not use Claude, so it is not blocked.

### Claude budget and the faster pipeline (new)
- **Fewer agent steps:** the backtest file is now generated from a template (no agent), and the review is done by a script that re-derives every fill price from the candles, checks the P&L adds up, session times, and that the strategy code cannot read data outside the simulated client. A normal run needs **one** agent step (strategy-writer) instead of three. Results get the label **script-checked**. An extra AI review is optional: Jobs tab, tick **also run AI review**.
- **Budget:** Jobs tab, **Claude usage and budget** card. Set max calls per hour, per day, a minimum gap between calls, and an optional cost cap. A researcher run counts as 3 calls, an agent step as 1. The header shows `Claude: used/limit per hour · per day`. When the budget is used up, jobs are **held** (strategy status **waiting**) and continue by themselves when it frees up, the same as for the real Claude limit.
- The card also lists the last calls with time, tokens and cost, as reported by the Claude CLI (the cost figure is a relative measure, not a bill).
- The budget is an **estimate**: Claude does not tell the app how much of your plan is left. Defaults are 12 calls/hour and 60/day; lower them if you still hit the real limit.

---

## 12. CPR and PCR (direction helpers)

**What they are**
- **CPR (Central Pivot Range):** three levels (BC, pivot, TC) computed from **yesterday's** high, low and close. Common reading: price above TC = bullish, below BC = bearish, inside = neutral; a narrow CPR often means a trending day, a wide one a range day. These are conventions, not guarantees.
- **PCR (Put-Call Ratio):** put open interest divided by call open interest, from the OpenAlgo option chain. Above about 1.2 is read as mildly bullish, below 0.8 as mildly bearish. The number depends on which strikes you include (on 17 Oct the full chain gave 1.33 but the +/-500-point version gave 0.89).

**What is in the system now**
- Daily candles are fetched with every day's data (so CPR can be computed for any date). `Data` for a day is incomplete if the previous day's bar is missing.
- `strategy_lib/levels.py` has the CPR/PCR functions and a combined `market_bias(...)`. New strategies written by the agents are told to use it.
- The simulated client supports `history(interval="D")` (completed days only) and `optionchain()`. In a backtest the chain is **partial** (only the cached strikes, from each bar's open interest) unless you saved real snapshots.
- **Your strategy** `nifty_direction_ce_pe.py` has an optional filter: set `BIAS_FILTER = "cpr"` or `"cpr_pcr"` at the top (default `"off"`, which changes nothing). A signal against a clear bias is skipped. It applies to the first entry of a cycle, **not** to the reversal leg.

**Commands**
```powershell
# see every backtested trade with the CPR/PCR bias at entry (with / against)
script-openalgo bias-report --date 2026-10-16 --instrument NIFTY

# save a REAL full option-chain snapshot now (slow, about a minute; do it during market hours, e.g. 09:20, 12:00, 15:20)
script-openalgo fetch --instrument ALL --chain
```
Snapshots are the only way to get real full-chain PCR in a later backtest, because OpenAlgo gives only live chain data.

**How to read the first result (16 Oct, NIFTY):** price opened above the CPR (bullish). Trades **against** that bias lost the most (ORB put -4,778, inside-bar put -1,930); trades with it also lost money overall. That is one day: it shows the idea is worth testing across many days, not that it works.

---

## 13. Many days at once: backfill and multi-day runs

**Why:** one day proves nothing. OpenAlgo returns every day since an option contract was listed in one call, so you can get many days now (NIFTY weekly: about 16 days, BANKNIFTY monthly: about 29) instead of waiting. Expired contracts disappear, so do this while the contract is listed.

**Step 1: backfill the data.** Data tab, pick the instrument (or ALL), click **Backfill all days of the nearest contract**. It takes a few minutes (60 to 100 API calls). Or from PowerShell: `script-openalgo backfill --instrument ALL`.
- The Data tab then shows each day with an **Expiry label**: *nearest* (the cached contract really was the nearest expiry that day) or *far expiry* (a farther contract: real data, but options decay and move differently, so treat results with care). Days that could not be completed (thin far-away strikes) show as failed and are skipped.

**Step 2: run a strategy on many days.** Open the strategy page, **Run on many days**: give a from/to range, or "last N days", and click **Run range**. No Claude is used. Far-expiry days are skipped unless you tick **include far-expiry days**. The strategy must already have code (run it once on a single day first). From PowerShell: `script-openalgo multiday --strategy nifty_direction_ce_pe --instrument NIFTY --days 10`. With **no arguments** it runs **every strategy on every usable day, for NIFTY and BANKNIFTY**, and ends with a comparison table (this is slow: slow strategies take about a minute per day). Useful options: `--strategy a,b` (a list), `--instrument NIFTY`, `--days 5`, `--allow-far`, `--verbose` (per-day table for every strategy).

**Reading the result:** the Strategies tab shows per instrument: days, total, average per day; far-expiry days are shown separately ("+N far-expiry days") and are not in the main numbers. The Runs tab lists each day with its expiry label, whether the code changed since, and the script check result. Everything is gross (no slippage or charges). Under 20 days is flagged *low sample*.

**Keep it fresh:** a new weekly contract becomes the nearest every week, so run the backfill (or the daily fetch) regularly; otherwise those days are gone for good.

---

## 14. Package, install on another machine, and back up everything (new)

**What changed for you:** the code is now a Python package called **script_openalgo** (folder `src/script_openalgo`). Your data stays in this folder, which is now called the **workspace**. Commands became `script-openalgo <command>`:

| Before | Now |
|---|---|
| `python -m webapp.app` | `script-openalgo webapp` |
| `python -m backtest_core.data_loader ...` | `script-openalgo fetch ...` |
| `python -m backtest_core.backfill ...` | `script-openalgo backfill ...` |
| `python -m backtest_core.multiday ...` | `script-openalgo multiday ...` |
| `python .claude\orchestrator.py ...` | `script-openalgo orchestrate ...` |
| `python -m strategy_db.cli ...` | `script-openalgo db ...` |

**One-time on this machine** (the old commands stop working until you do this): from this folder run `pip install -e .` (editable: edits to `src` apply at once), or install the built wheel. Then `script-openalgo doctor` checks everything.

**The OpenAlgo address is configurable.** Order of precedence: `--host URL` on the command, then the `OPENALGO_HOST` environment variable, then `host` in `script_openalgo.toml`, then `http://127.0.0.1:5000`. Example: `script-openalgo --host http://192.168.1.20:5000 fetch --instrument ALL`. The OpenAlgo server software is **not** part of the package; it can run on another computer.

**Build the package:** `python build_all.py` (uses `uv build`). It copies the agents, the skill and the example strategies into the package, scans for secrets, builds `dist/script_openalgo-0.1.1.tar.gz` and `.whl`, lists what is inside both, and installs the wheel in a clean environment to test it. **The Claude agents and the skill are inside both the tar.gz and the wheel**, and `init` copies them to the workspace's `.claude` folder, where the Claude CLI looks for them.

**On the other machine:**
```
pip install script_openalgo-0.1.1-py3-none-any.whl
script-openalgo init my_workspace
cd my_workspace
copy .env.example .env        (then put OPENALGO_API_KEY=... in it)
script-openalgo --host http://<openalgo-host>:5000 doctor
```
Python 3.11 or newer. Use a **stable** Python (a beta such as 3.15 crashed pandas). The AI steps also need the Claude Code CLI installed and logged in there; everything else works without it.

**Back up and move everything (Backup tab, or `script-openalgo export` / `import`):**
- **Export** makes one zip with every strategy and backtest file, all results, all candle data (including option open interest and chain snapshots), the databases (copied consistently while the app runs), saved code versions, specs, job logs, agents, skill, knowledge and orchestrator state. Each file has a SHA-256 checksum inside. **Not included:** `.env` (your API key), earlier backups.
- **Import** first checks every checksum and refuses a damaged, modified or unsafe zip before changing anything. It then makes an automatic **safety backup of the current state**, replaces the databases with the ones from the zip, and copies the other files. A file that already exists with different content is **kept** and listed as a conflict, unless you tick *overwrite*. Use **check only** first. Your local `script_openalgo.toml` (the host of that machine) is never overwritten.
- Tested: a full export of this workspace imported into an empty folder gave identical files (1,764 of 1,764 by checksum) and identical database rows; importing twice changes nothing; a tampered zip is refused.
- Not supported: merging two *different* databases. Import replaces the database; the safety backup keeps the old one.

## 15. Stocks and stock options (RELIANCE, OFSS)

The strategies can now run on stocks as well as NIFTY and BANKNIFTY. The signal comes from the stock's own 5-minute candles and the trade is an ATM option on that stock (CE for long, PE for short), exactly as for the indices.

**Getting data.** In the Data tab choose RELIANCE or OFSS and click "Backfill all days of the nearest contract". Stock options expire monthly, so a backfill covers several weeks. Only days on which the cached contract was the nearest expiry are used by the multi-day runner; the others are labelled "far expiry".

**Running.** Pick RELIANCE or OFSS in the instrument box of a strategy and click Run (one day) or Run range (many days). From PowerShell:

```
script-openalgo backfill --instrument RELIANCE --days 30
script-openalgo multiday --strategy opening_range_breakout_orb --instrument RELIANCE
```

**What is different for stocks**
- Stock candles stop at 15:14 (360 a day). That is normal; the strategies square off at 15:15.
- Some strikes do not exist (OFSS has 10200 and 10250 but not 10150). The Data tab shows them as "unlisted"; they are not missing data. The backtest buys the nearest strike that exists.
- OFSS options are thin: many minutes have no trade. Days where a near-the-money strike traded in less than 60 % of the minutes are marked incomplete and skipped.
- The backtest books an ESTIMATED spread cost per fill (RELIANCE 0.5 %, OFSS 1.5 % of the premium). The summary shows `net_pnl` and `spread_cost`; the multi-day report prints a NET line. Read the net number for stocks; it is still an estimate, not real quotes.
- Rupee limits are resized per stock where needed (OFSS daily loss cap 6000). The values are in `backtest_core/instruments.py` (`strategy_overrides`).
- For LIVE trading on a stock, set `UNDERLYING = "RELIANCE"` and `INDEX_EXCHANGE = "NSE"` in the strategy file. Live strike lookup only works while the market is open.

**Adding more stocks (Instruments tab)**
1. Open the **Instruments** tab, type part of a symbol or name (for example `HDFC`, `INFY`, `TATA`) and press Search. The list comes from OpenAlgo; only stocks and indices that have options on NFO are shown.
2. Click **Add**. The app reads the strike step, the expiry cycle (weekly or monthly), the number of candles per day and the lot size from OpenAlgo. This takes a few seconds.
3. Go to the **Data** tab, choose the new stock and click Backfill. After that it appears in every Run box.
4. Check the defaults with **Edit**: minimum option coverage (0.80) and estimated spread (1.0 %). Lower the coverage and raise the spread for stocks with thin options; set a daily max loss if the lot is large.
5. **Remove** takes a stock off the list; its downloaded data and results stay. NIFTY, BANKNIFTY, RELIANCE and OFSS are built in and cannot be removed (they can be edited).

Added stocks are saved in `strategy_db/instruments.json` of the workspace.

## 16. Deleting results and logs

- **Results tab**: each run has a × button, each date/strategy group has "Delete N run(s)", and the run window has "Delete run". You are asked whether to delete the result folder on disk as well. If you keep the files, the run is remembered as deleted and does not come back when the app syncs the results folder.
- **Jobs tab**: "Delete" on a finished job removes the job and its log file. "Clear finished jobs and logs" removes every finished job (ok, failed, cancelled, interrupted). Running jobs, jobs waiting for your answer and jobs held by the Claude limit are kept.
