"""
VWAP Reclaim Scalp (ATM CE/PE option buying) for OpenAlgo
-----------------------------------------------------------
Signal is computed on the underlying INDEX's 5-minute candles (VWAP,
volume, RSI); the trade itself is an ATM option buy (CE for a bullish
reclaim, PE for a bearish rejection) sized in the option's lot. Stops,
targets and the hard VWAP-invalidation rule are all tracked in INDEX
points so they match the signal that generated them.

Long (CE) setup, checked on every newly CLOSED 5m candle:
  1. Price closed above VWAP for >= MIN_PRETREND_MINUTES immediately
     before the dip (a real prior uptrend vs VWAP, not noise).
  2. A shallow dip of 1..MAX_DIP_CANDLES candles closes below VWAP.
  3. A candle closes back above VWAP with its full body (open and
     close) above the VWAP line, on volume >= RECLAIM_VOLUME_MULTIPLE x
     the average volume of the dip candle(s) -> this is the reclaim
     candle.
  4. Optional (off by default, REQUIRE_RSI_CONFIRM): RSI(RSI_PERIOD)
     during the dip must have stayed inside RSI_DIP_ZONE.
  Entry: ENTRY_MODE="reclaim_close" buys on the reclaim candle itself;
  ENTRY_MODE="retest_bounce" (conservative variant) waits one more
  candle for a pullback to VWAP that holds (does not close back below
  it) and buys on that bounce instead, for a tighter stop.
Short (PE) setup mirrors this below VWAP (rally instead of dip,
rejection instead of reclaim).

Stop: the tighter of (a) the low/high of the deepest dip/rally candle
and (b) the low/high of the reclaim/rejection candle itself (index
points). Skipped entirely if that distance exceeds STOP_MAX_PCT of the
index price (sources cite ~0.2-0.5%).

Exit:
  - First target = REWARD_RISK_T1 x the initial stop distance. On hit,
    sell half the position, move the stop to breakeven on the rest and
    set a fixed runner target = the session high-so-far (long) / low-
    so-far (short) at that moment (adaptation of the US-style
    "premarket high/low" target, which does not exist for NSE index
    options -- see knowledge/strategy_patterns.md).
  - Hard invalidation (overrides everything): exit immediately if any
    later candle closes back on the wrong side of VWAP.
  - Square-off at SQUARE_OFF_TIME regardless of P&L.

Entries are only taken inside ENTRY_WINDOWS (the reliability windows
the source material cites) and stop altogether after NO_NEW_ENTRY_AFTER
or once MAX_TRADES_PER_DAY / DAILY_MAX_LOSS is reached (safety caps,
same pattern as nifty_direction_ce_pe.py; not part of the source rules).

Adaptations from the raw research spec (documented, not guesses):
  - "Premarket high/low" runner target -> session high/low-so-far via
    quotes() (index has no premarket session).
  - VWAP/volume use the index's own 5m OHLCV (incl. its `volume`
    field) rather than option volume, per the spec's own guidance that
    option volume is too noisy for this check.
  - RSI(9) dip-zone confirmation is implemented but OFF by default
    because the source calls it optional; flip REQUIRE_RSI_CONFIRM to
    use it.
  - Secondary "early afternoon" reliability window is set to 13:00-
    14:30; the source does not give exact times for it.

Run first with OpenAlgo Analyzer (paper) mode ON. This script places
real orders once Analyzer is switched off -- do that only after a
satisfactory backtest and with the user's explicit go-ahead.
"""

import os
import time
from datetime import datetime, timedelta
from openalgo import api

# ======================= PARAMETERS =======================
API_KEY = os.getenv("OPENALGO_API_KEY", "PASTE_YOUR_API_KEY_HERE")
HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
STRATEGY = "VwapReclaimScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 2                      # lots per entry; needs >=2 to scale out half at target1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"       # candles are built from the open even though entries start later
ENTRY_WINDOWS = [("09:50", "11:30"), ("13:00", "14:30")]   # primary + secondary reliability windows
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

MIN_PRETREND_MINUTES = 45
PRETREND_BARS = (MIN_PRETREND_MINUTES + BAR_MINUTES - 1) // BAR_MINUTES
MAX_DIP_CANDLES = 3
RECLAIM_VOLUME_MULTIPLE = 1.5

RSI_PERIOD = 9
RSI_DIP_ZONE = (40, 58)
REQUIRE_RSI_CONFIRM = False    # optional per source; set True to require it

ENTRY_MODE = "reclaim_close"   # "reclaim_close" or "retest_bounce" (conservative variant)

STOP_MAX_PCT = 0.5             # skip the trade if stop distance exceeds this % of index price
REWARD_RISK_T1 = 2.0           # first target = this x the stop distance

POLL_SEC = 20                  # main loop tick while idle/monitoring

# ---- daily safety caps (not in the source rules; same pattern as nifty_direction_ce_pe.py) ----
MAX_TRADES_PER_DAY = 4
DAILY_MAX_LOSS = 3000          # NIFTY-sized; resize per instrument (see knowledge/lessons.md)
DAILY_PROFIT_TARGET = 0        # 0 = off
# ==========================================================


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def hhmm(s):
    h, m = map(int, s.split(":"))
    return h, m


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []          # list of {t, o, h, l, c, v} completed 5m index candles
        self.vwap = []          # session VWAP aligned with self.bars
        self.rsi = []           # RSI aligned with self.bars
        self.processed_bars = 0

        self.pending_setup = None   # {"side": "CE"/"PE", "stop": price} awaiting retest bounce
        self.position = None        # see attempt_entry() for shape
        self.option_sym = None
        self.lot = None

        self.trades_today = 0
        self.day_pnl = 0.0
        self._cap_logged = False

    # ---------- time helpers ----------
    def at(self, s):
        h, m = hhmm(s)
        return self.now().replace(hour=h, minute=m, second=0, microsecond=0)

    def wait_until(self, target):
        while self.now() < target:
            self.sleep(min(30, max(1, (target - self.now()).total_seconds())))

    def in_entry_window(self):
        now = self.now()
        return any(self.at(s) <= now < self.at(e) for s, e in ENTRY_WINDOWS)

    # ---------- market data ----------
    def ltp(self, symbol, exchange):
        for attempt in range(2):
            try:
                r = self.c.quotes(symbol=symbol, exchange=exchange)
                if r.get("status") == "success":
                    p = float(r["data"].get("ltp") or 0)
                    if p > 0:
                        return p
                    log(f"zero/blank LTP for {symbol}, ignoring")
                else:
                    log(f"quotes error {symbol}: {r}")
            except Exception as e:
                log(f"quotes exception {symbol}: {e}")
            self.sleep(2)
        return None

    def session_extreme(self, field):
        try:
            r = self.c.quotes(symbol=UNDERLYING, exchange=INDEX_EXCHANGE)
            if r.get("status") == "success":
                v = float(r["data"].get(field) or 0)
                return v or None
        except Exception as e:
            log(f"quotes exception (session {field}): {e}")
        return None

    def session_high(self):
        return self.session_extreme("high")

    def session_low(self):
        return self.session_extreme("low")

    def nearest_expiry(self):
        r = self.c.expiry(symbol=UNDERLYING, exchange=OPT_EXCHANGE, instrumenttype="options")
        if r.get("status") != "success" or not r.get("data"):
            raise RuntimeError(f"expiry lookup failed: {r}")
        return r["data"][0].replace("-", "").upper()

    def resolve_atm(self, option_type):
        exp = self.nearest_expiry()
        r = self.c.optionsymbol(underlying=UNDERLYING, exchange=INDEX_EXCHANGE,
                                 expiry_date=exp, offset="ATM", option_type=option_type)
        if r.get("status") != "success":
            raise RuntimeError(f"optionsymbol failed: {r}")
        self.option_sym = r["symbol"]
        self.lot = int(r["lotsize"])
        log(f"ATM {option_type} {self.option_sym} | lot {self.lot} | expiry {exp} | "
            f"underlying_ltp {r.get('underlying_ltp')}")

    def refresh_bars(self):
        today = self.now().strftime("%Y-%m-%d")
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=TIMEFRAME,
                                 start_date=today, end_date=today, source=HISTORY_SOURCE)
        except Exception as e:
            log(f"history exception: {e}")
            return
        if not hasattr(df, "empty"):
            log(f"history error: {df}")
            return
        if df.empty:
            return
        now = self.now()
        bars = []
        for ts, row in df.iterrows():
            t = ts.to_pydatetime()
            if t.tzinfo is not None:
                t = t.replace(tzinfo=None)   # SDK already converts intraday bars to IST wall time
            if t.date() != now.date():
                continue
            if t + timedelta(minutes=BAR_MINUTES) > now:
                continue   # candle not fully completed yet; no lookahead
            bars.append({"t": t, "o": float(row["open"]), "h": float(row["high"]),
                         "l": float(row["low"]), "c": float(row["close"]),
                         "v": float(row.get("volume", 0) or 0)})
        bars.sort(key=lambda b: b["t"])
        self.bars = bars
        self._recompute_indicators()

    def _recompute_indicators(self):
        cum_pv = cum_v = 0.0
        vwap = []
        for b in self.bars:
            tp = (b["h"] + b["l"] + b["c"]) / 3.0
            cum_pv += tp * b["v"]
            cum_v += b["v"]
            vwap.append(cum_pv / cum_v if cum_v > 0 else tp)
        self.vwap = vwap
        self.rsi = self._rsi([b["c"] for b in self.bars], RSI_PERIOD)

    @staticmethod
    def _rsi(closes, period):
        n = len(closes)
        rsi = [None] * n
        if n <= period:
            return rsi
        gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
        losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        rsi[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            idx = i + 1
            rsi[idx] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
        return rsi

    # ---------- setup detection ----------
    def detect_long_setup(self, i):
        bars, vwap = self.bars, self.vwap
        bar_i = bars[i]
        vwap_i = vwap[i]
        if not (min(bar_i["o"], bar_i["c"]) > vwap_i):
            return None   # full body must close back above VWAP
        k, j = 0, i - 1
        while j >= 0 and bars[j]["c"] < vwap[j]:
            k += 1
            j -= 1
        if k == 0 or k > MAX_DIP_CANDLES:
            return None
        dip_bars = bars[i - k:i]
        pretrend_count, p = 0, j
        while p >= 0 and bars[p]["c"] > vwap[p]:
            pretrend_count += 1
            p -= 1
        if pretrend_count < PRETREND_BARS:
            return None
        baseline = dip_bars[-2:] if len(dip_bars) >= 2 else dip_bars
        baseline_vol = sum(b["v"] for b in baseline) / len(baseline)
        if baseline_vol <= 0 or bar_i["v"] < RECLAIM_VOLUME_MULTIPLE * baseline_vol:
            return None
        if REQUIRE_RSI_CONFIRM:
            zone_ok = any(self.rsi[idx] is not None and RSI_DIP_ZONE[0] <= self.rsi[idx] <= RSI_DIP_ZONE[1]
                          for idx in range(i - k, i))
            if not zone_ok:
                return None
        stop_price = max(min(b["l"] for b in dip_bars), bar_i["l"])
        return {"stop": stop_price}

    def detect_short_setup(self, i):
        bars, vwap = self.bars, self.vwap
        bar_i = bars[i]
        vwap_i = vwap[i]
        if not (max(bar_i["o"], bar_i["c"]) < vwap_i):
            return None
        k, j = 0, i - 1
        while j >= 0 and bars[j]["c"] > vwap[j]:
            k += 1
            j -= 1
        if k == 0 or k > MAX_DIP_CANDLES:
            return None
        rally_bars = bars[i - k:i]
        pretrend_count, p = 0, j
        while p >= 0 and bars[p]["c"] < vwap[p]:
            pretrend_count += 1
            p -= 1
        if pretrend_count < PRETREND_BARS:
            return None
        baseline = rally_bars[-2:] if len(rally_bars) >= 2 else rally_bars
        baseline_vol = sum(b["v"] for b in baseline) / len(baseline)
        if baseline_vol <= 0 or bar_i["v"] < RECLAIM_VOLUME_MULTIPLE * baseline_vol:
            return None
        if REQUIRE_RSI_CONFIRM:
            zone_ok = any(self.rsi[idx] is not None and RSI_DIP_ZONE[0] <= self.rsi[idx] <= RSI_DIP_ZONE[1]
                          for idx in range(i - k, i))
            if not zone_ok:
                return None
        stop_price = min(max(b["h"] for b in rally_bars), bar_i["h"])
        return {"stop": stop_price}

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        while self.processed_bars < len(self.bars):
            self.handle_new_bar(self.processed_bars)
            self.processed_bars += 1

    def handle_new_bar(self, i):
        if self.position:
            self.check_hard_invalidation(i)
            return
        if self.pending_setup:
            self.evaluate_pending(i)
            return
        if not self.in_entry_window() or self.now() >= self.at(NO_NEW_ENTRY_AFTER):
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log("Daily trade/loss cap reached; no further entries today")
                self._cap_logged = True
            return
        setup = self.detect_long_setup(i)
        if setup:
            self.handle_setup("CE", i, setup)
            return
        setup = self.detect_short_setup(i)
        if setup:
            self.handle_setup("PE", i, setup)

    def handle_setup(self, side, i, setup):
        if ENTRY_MODE == "retest_bounce":
            self.pending_setup = {"side": side, "stop": setup["stop"]}
            log(f"{side} reclaim/rejection candle confirmed at bar {i}; waiting for retest bounce")
        else:
            self.attempt_entry(side, setup["stop"])

    def evaluate_pending(self, i):
        p = self.pending_setup
        bar, vwap_i = self.bars[i], self.vwap[i]
        if p["side"] == "CE":
            touched, held = bar["l"] <= vwap_i, bar["c"] >= vwap_i
        else:
            touched, held = bar["h"] >= vwap_i, bar["c"] <= vwap_i
        self.pending_setup = None
        if touched and held:
            log(f"Retest held for {p['side']}; entering on bounce")
            self.attempt_entry(p["side"], p["stop"])
        else:
            log(f"Retest failed for {p['side']} (touched={touched}, held={held}); setup dropped")

    def check_hard_invalidation(self, i):
        pos = self.position
        bar, vwap_i = self.bars[i], self.vwap[i]
        if pos["side"] == "CE" and bar["c"] < vwap_i:
            self.exit_position(f"hard invalidation: candle closed below VWAP ({bar['c']:.2f} < {vwap_i:.2f})")
        elif pos["side"] == "PE" and bar["c"] > vwap_i:
            self.exit_position(f"hard invalidation: candle closed above VWAP ({bar['c']:.2f} > {vwap_i:.2f})")

    # ---------- orders ----------
    def buy(self, symbol, qty, reason):
        r = self.c.placeorder(strategy=STRATEGY, symbol=symbol, action="BUY", exchange=OPT_EXCHANGE,
                              price_type="MARKET", product=PRODUCT, quantity=qty)
        if r.get("status") != "success":
            log(f"BUY FAILED {symbol}: {r}")
            return None
        price = self.fill_price(r.get("orderid")) or self.ltp(symbol, OPT_EXCHANGE)
        log(f"BUY {symbol} qty {qty} @ {price} | order {r.get('orderid')} | {reason}")
        return price

    def sell(self, symbol, qty, reason):
        r = self.c.placeorder(strategy=STRATEGY, symbol=symbol, action="SELL", exchange=OPT_EXCHANGE,
                              price_type="MARKET", product=PRODUCT, quantity=qty)
        ok = r.get("status") == "success"
        price = (self.fill_price(r.get("orderid")) if ok else None) or self.ltp(symbol, OPT_EXCHANGE)
        if not ok:
            log(f"SELL FAILED {symbol}: {r} !!! CHECK POSITION MANUALLY IN OPENALGO !!!")
        log(f"SELL {symbol} qty {qty} @ {price} | order {r.get('orderid', r)} | {reason}")
        return price

    def fill_price(self, oid):
        try:
            self.sleep(1)
            r = self.c.orderstatus(order_id=oid, strategy=STRATEGY)
            p = float(r.get("data", {}).get("average_price") or 0)
            return p or None
        except Exception:
            return None

    # ---------- entry / exit ----------
    def attempt_entry(self, side, stop_price):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return
        stop_distance = abs(idx_price - stop_price)
        if stop_distance <= 0:
            log("Entry aborted: zero stop distance")
            return
        stop_pct = stop_distance / idx_price * 100
        if stop_pct > STOP_MAX_PCT:
            log(f"Entry skipped: stop distance {stop_pct:.2f}% of index > cap {STOP_MAX_PCT}%")
            return
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        price = self.buy(self.option_sym, qty, f"{side} VWAP setup | index {idx_price:.2f} stop {stop_price:.2f}")
        if price is None:
            return
        target1 = idx_price + REWARD_RISK_T1 * stop_distance if side == "CE" \
            else idx_price - REWARD_RISK_T1 * stop_distance
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty, "remaining_qty": qty,
            "entry_premium": price, "index_entry": idx_price, "stop": stop_price,
            "target1": target1, "partial_done": False, "runner_target": None,
        }
        self.trades_today += 1
        log(f"ENTER {side} | index {idx_price:.2f} stop {stop_price:.2f} ({stop_pct:.2f}%) "
            f"target1 {target1:.2f} | trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

    def partial_exit(self, reason):
        pos = self.position
        half = (pos["qty"] // 2 // self.lot) * self.lot
        if half <= 0:
            self.exit_position(reason)
            return
        px = self.sell(pos["symbol"], half, reason)
        if px is None:
            return
        p = (px - pos["entry_premium"]) * half
        self.day_pnl += p
        pos["remaining_qty"] -= half
        pos["partial_done"] = True
        pos["stop"] = pos["index_entry"]   # breakeven
        pos["runner_target"] = self.session_high() if pos["side"] == "CE" else self.session_low()
        log(f"PARTIAL EXIT {reason} | qty {half} @ {px} | pnl {p:+.0f} | "
            f"remaining {pos['remaining_qty']} | runner target {pos['runner_target']}")

    def exit_position(self, reason):
        pos = self.position
        qty = pos["remaining_qty"]
        px = self.sell(pos["symbol"], qty, reason)
        if px is None:
            log("Exit failed; position may still be open - check manually")
            return
        p = (px - pos["entry_premium"]) * qty
        self.day_pnl += p
        log(f"EXIT {pos['side']} {reason} | qty {qty} @ {px} | pnl {p:+.0f} | day P&L {self.day_pnl:+.0f}")
        self.position = None

    def manage_position(self):
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        pos = self.position
        if pos["side"] == "CE":
            if idx_ltp <= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} <= {pos['stop']:.2f})")
            elif not pos["partial_done"] and idx_ltp >= pos["target1"]:
                self.partial_exit(f"target1 hit (index {idx_ltp:.2f} >= {pos['target1']:.2f})")
            elif pos["partial_done"] and pos["runner_target"] and idx_ltp >= pos["runner_target"]:
                self.exit_position(f"runner target reached (index {idx_ltp:.2f} >= {pos['runner_target']:.2f})")
        else:
            if idx_ltp >= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} >= {pos['stop']:.2f})")
            elif not pos["partial_done"] and idx_ltp <= pos["target1"]:
                self.partial_exit(f"target1 hit (index {idx_ltp:.2f} <= {pos['target1']:.2f})")
            elif pos["partial_done"] and pos["runner_target"] and idx_ltp <= pos["runner_target"]:
                self.exit_position(f"runner target reached (index {idx_ltp:.2f} <= {pos['runner_target']:.2f})")

    # ---------- main loop ----------
    def run(self):
        log(f"Strategy {STRATEGY} starting on {UNDERLYING}")
        try:
            a = self.c.analyzerstatus()
            log(f"Analyzer (paper) mode: {a.get('data', a)}")
        except Exception:
            pass
        self.wait_until(self.at(SESSION_START))
        while True:
            if self.now() >= self.at(SQUARE_OFF_TIME):
                if self.position:
                    self.exit_position("square-off time")
                log("Square-off time reached; stopping for the day")
                break
            self.refresh_bars()
            self.process_new_bars()
            if self.position:
                self.manage_position()
            self.sleep(POLL_SEC)
        log(f"Done for the day. Trades {self.trades_today} | day P&L {self.day_pnl:+.0f}")


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
