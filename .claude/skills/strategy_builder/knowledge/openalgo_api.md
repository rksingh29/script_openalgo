# OpenAlgo API knowledge (append-only, dated)

Server: `http://127.0.0.1:5000`, broker `dhan`. Auth: `apikey` in the JSON body, key from env `OPENALGO_API_KEY`. Python SDK: `from openalgo import api`.

## Allowed client surface (what strategies may call)
`quotes`, `expiry`, `optionsymbol`, `placeorder`, `orderstatus`, `analyzerstatus`, `history` (1m/5m/15m/1h and `D`), `optionchain`.
Anything else must first be added to the fake client in `backtest_core/fake_client.py`.

## Verified behaviour

### 2026-10-17: endpoints that work with the API key
- `POST /api/v1/ping` -> `{"data": {"broker": "dhan", "message": "pong"}}`
- `POST /api/v1/intervals` -> minutes `1m 5m 15m`, hours `1h`, days `D`
- `POST /api/v1/expiry` (`symbol=NIFTY, exchange=NFO, instrumenttype=options`) -> list like `["19-OCT-26", ...]`; `optionsymbol` wants `19OCT26` (strip dashes).
- `POST /api/v1/optionsymbol` (`underlying=NIFTY, exchange=NSE_INDEX, expiry_date=19OCT26, offset=ATM, option_type=CE`) -> `{symbol: "NIFTY19OCT2623150CE", exchange: "NFO", lotsize: 65, tick_size: 0.05, freeze_qty: 1800, underlying_ltp}`
- `POST /api/v1/quotes` -> `data` has `ltp, open, high, low, prev_close, volume, oi, bid, ask`
- Index exchange is `NSE_INDEX` (symbol `NIFTY`); options are `NFO`. Option symbol format: `NIFTY` + `DDMMMYY` + strike + `CE|PE`.

### 2026-10-17: history data
- `POST /api/v1/history` (`symbol, exchange, interval, start_date, end_date, source`) returns `data` = list of `{timestamp (epoch s), open, high, low, close, volume, oi}`.
- `source="db"` (Historify local DB) was **empty** for NIFTY and options. Error 404: "Download data first using Historify."
- `source="api"` (broker) **works**: NIFTY index 1m for 2026-10-16 = 375 bars (09:15-15:29). Options = 385 bars (09:15-15:39).
- Historify download endpoints (`/historify/api/...`) need a web login session (redirect to `/auth/login`); the API key cannot drive them. So fetch with `source="api"` and cache ourselves.
- `end_date` returns extra days: requesting start=10-15, end=10-15 gave 750 rows reaching 10-16. Always filter rows to the wanted date.
- Timestamps are epoch seconds; convert to IST (UTC+05:30). Trading day starts 03:45 UTC.
- Option bars exist after 15:29 (15:30-15:39, real volume/OI). Rule: drop bars after 15:29.

### 2026-10-17: availability and limits
- **Expired contracts are not available.** `NIFTY12OCT2623150CE` and `NIFTY05OCT2623000CE` -> 400 "Symbol not found ... master contracts". Only currently listed contracts can be fetched. A day is backtestable only while its contracts are still listed (problem right after an expiry). Fix: fetch and cache each day's data daily.
- Rate limit: 30 consecutive `history` calls in 10.6s, all 200. Throttle ~0.4s per call, retry on non-200.
- Observed expiries (as of 2026-10-17): 19-OCT-26, 27-OCT-26, 03-NOV-26, 09-NOV-26, 17-NOV-26, 23-NOV-26 then monthlies. Weekly expiry day varies; do not hardcode a weekday.
- Lot size 65 (as of 2026-10-17); it changes over time, so read it per date.

### 2026-10-17: BANKNIFTY (verified)
- Index `BANKNIFTY` / `NSE_INDEX`; options on `NFO`. `history(source="api")` 1m for 2026-10-16: index 375 bars (09:15-15:29), ATM option 385 bars (to 15:39). `source="db"` (Historify) empty.
- Expiries listed (as of 2026-10-17) are **monthly only**: 27-OCT-26, 23-NOV-26, 29-DEC-26, 30-MAR-27, 29-JUN-27, 28-SEP-27. Expiry argument format `27OCT26`.
- Strike step near the money is **100** (spot 55844 -> ATM `BANKNIFTY27OCT2655800CE`, ITM1 55700). Lot size **30**, freeze qty 600, tick 0.05. Do not assume the lot size is constant; read it per date.
- `search` (exchange NFO) lists contracts with fields `symbol, expiry, strike, instrumenttype, lotsize, freeze_qty`.
- Per-instrument config lives in `backtest_core/instruments.py` (strike step, buffer, max expiry gap days: NIFTY 8, BANKNIFTY 35). Manifests: `data_cache/manifest_<INSTRUMENT>_<date>.json`.

### 2026-10-17: FakeClient additions for vwap_reclaim_scalp.py
- `quotes()` now also returns `open`/`high`/`low` alongside `ltp`: the session-to-date open/high/low of
  `symbol` computed from 1m bars up to the last completed minute (same cutoff as `ltp`), so strategies
  that use `quotes(...)["data"]["high"/"low"]` as a "session high/low so far" (e.g. a runner-target that
  trails the day's high/low) work in the backtest with no lookahead.
- `history()` was missing from `FakeClient` entirely. Added: resamples the cached 1-minute candles to the
  requested `interval` (`"5m"`, `"15m"`, `"1h"`, `"D"`, ...), bucketed from the 09:15 session start
  (`label="left"`, bar indexed by its open time, matching the live SDK). No lookahead: 1m bars are first
  truncated to the last completed minute (`now - 1min`, same rule as `quotes`/fills), and a resampled
  bucket is only returned once all of its underlying 1m bars are available/completed. `start_date`/
  `end_date` are accepted but ignored (the fake client only ever holds one day's cache = `self.day`).

### 2026-10-17: option chain (PCR) and daily candles (CPR), verified
- `client.optionchain(underlying="NIFTY", exchange="NFO"|"NSE_INDEX", expiry_date="19OCT26", strike_count=None)`. The SDK argument is `expiry_date` (DDMMMYY), NOT `expiry`. Both exchange values worked. Returns `chain` = list of `{strike, ce, pe}`; each leg has `symbol, label (ATM/ITM1/OTM2..), ltp, bid, ask, open, high, low, volume, oi, lotsize, prev_close`; top level has `underlying_ltp, atm_strike, expiry_date, quotes_included, greeks_included`. All 235 strikes came back (17000-28700), so it is **slow** (about a minute): take snapshots in a background job, not inside a trading loop.
- It is **live data only** (no history). For backtests we either save snapshots (`script-openalgo fetch --instrument ALL --chain`, during market hours) or build a PARTIAL chain from the cached 1m option candles (their `oi` column is the open interest at each bar).
- PCR depends on which strikes are included: on 2026-10-17 the full-chain PCR(OI) was 1.328 but the +/-500-point PCR was 0.892. A strategy must say which one it uses (`strategy_lib.levels.pcr_from_chain(chain, within_points=...)`).
- Open interest changes after 15:30: ATM CE OI was 8.11M at the 15:29 bar and 6.53M at the 15:39 bar (post-close processing). The cache keeps bars to 15:29 only, so intraday OI is not polluted, but a chain fetched after the close will not match it.
- `client.history(symbol, exchange="NSE_INDEX", interval="D", start_date, end_date, source="api")` returns daily OHLC for past days (checked 13-16 Oct 2026). Daily bars are stamped at 00:00 UTC of the trading date (convert with the UTC date). `oi` is 0 for the index.

### 2026-10-17: what the /optionchain page is, and multi-day option history (read from G:\Claude\openalgo + verified)
- The `/optionchain` page (React app) polls the same `POST /api/v1/optionchain` about every 30 s and merges live WebSocket ticks; Greeks are computed in the browser. Nothing extra is available from it for backtests. API-key endpoints in OpenAlgo's `restx_api`: option_chain, history, multiquotes, option_greeks, multi_option_greeks, depth, synthetic_future, option_symbol, options_order... The OI Tracker, Max Pain, OI Profile, GEX, IV and straddle pages are session-login UI endpoints (`/oitracker/api/...`) built on `get_option_chain` and `get_history`; they are not reachable with the API key.
- **Multi-day option history works in ONE call**: `history(symbol=<option>, exchange="NFO", interval="1m", start_date=<early>, end_date=<today>, source="api")` returned every trading day since the contract was listed (NIFTY19OCT26: 16 days from 2026-09-24; NIFTY27OCT26 and BANKNIFTY27OCT26: 29 days from 2026-09-04), about 2 s per call, with `oi` on (nearly) every 1m bar. `interval="D"` also carries `oi` per day.
- Listing life: weekly NIFTY contracts list about 3 weeks before expiry (the 19OCT26 weekly from 24 Sep), BANKNIFTY monthlies about 6 weeks before. Only LISTED contracts can be fetched, so history of an expired contract is gone: fetch while it is listed.
- Caveat: on a day the fetched contract was NOT the nearest expiry, options behave differently (less gamma/theta) than what the strategy would have traded. Label such days (days to expiry) rather than mixing them silently.

### 2026-10-20: stocks and stock options (RELIANCE, OFSS), verified against OpenAlgo (broker dhan)
- Underlying: `exchange="NSE"`, symbol = the stock (`RELIANCE`, `OFSS`). Options on `NFO`, same symbol format as the index: `RELIANCE27OCT261200CE`. Expiries are monthly (`expiry` returned 27-OCT-26, 23-NOV-26, 29-DEC-26). Lot size from `optionsymbol`: RELIANCE 500, OFSS 100.
- **Stock 1m candles end at 15:14**: 360 bars (09:15-15:14), not 375. Option bars still run to 15:29 (+ padding). `instruments.py` has `min_underlying_bars` (355 for stocks).
- **Strikes can be irregular**: RELIANCE every 10; OFSS lists 10000, 10100, 10200, 10250, 10300, 10400 but NOT 10150 (50 apart near the money, 100 further out). An unknown strike returns HTTP 400 "Symbol ... not found for exchange 'NFO'". The loader records those as `unlisted` in the manifest (stocks only; for indices "not found" usually means an expired contract and stays an issue). FakeClient's ATM falls back to the nearest CACHED strike when the rounded strike is not listed.
- `optionsymbol` / `optionchain` compute ATM from `underlying_ltp`, which is 0 outside market hours (returned strike 620 for RELIANCE, 4000 for OFSS before the open). Live strike resolution only works while the market is open; backtests are unaffected.
- Liquidity: RELIANCE options trade millions per strike per day (full 375 bars). OFSS options are thin: near-the-money strikes can have 30-60 % of minutes traded; such days are marked incomplete (`min_option_coverage` 0.60 for OFSS). Use `slippage_pct` (instrument table, an ESTIMATE: RELIANCE 0.5 %, OFSS 1.5 % of premium per fill) and read the NET P&L for stocks.
- Backtest files call `instruments.apply_strategy_overrides(strat, instrument, PARAM_OVERRIDES)`: sets UNDERLYING, INDEX_EXCHANGE and the instrument's `strategy_overrides` (e.g. OFSS `DAILY_MAX_LOSS` 6000) and restores them when another instrument runs in the same process. User PARAM_OVERRIDES win.
- For a LIVE stock run set `UNDERLYING = "RELIANCE"` and `INDEX_EXCHANGE = "NSE"` in the strategy file (the backtest sets them itself).
