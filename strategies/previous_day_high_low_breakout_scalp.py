"""
Previous Day High/Low Breakout Scalp -- ATM CE/PE option buying for OpenAlgo
---------------------------------------------------------------------------
Signal is computed on the underlying INDEX's own 5-minute candles against
the PREVIOUS trading day's high (PDH) and low (PDL); the trade itself is an
ATM option BUY (CE for a bullish break above PDH, PE for a bearish break
below PDL) sized in the option's lot. Stop/target are tracked in INDEX
points so they stay consistent with the signal that generated them
(converted to rupee P&L via the option fill price at entry, same pattern as
nifty_direction_ce_pe.py / opening_range_breakout_orb.py).

Previous-day levels come from COMPLETED daily bars (client.history
interval="D"); today's forming daily bar is filtered out, so there is no
lookahead. PDH/PDL and the inside-day filter are loaded once at
SESSION_START.

Rules (checked on every newly CLOSED BAR_MINUTES-minute index candle):
  1. Long (CE): the first candle whose CLOSE is above PDH + BUFFER_PTS
     triggers a long entry (close beyond the level, not just an intrabar
     wick), signalling bullish continuation.
  2. Short (PE): the first candle whose CLOSE is below PDL - BUFFER_PTS
     triggers a short entry, signalling bearish continuation.
  3. Only the first valid breakout per side is taken per day (one CE, one
     PE attempt max -> MAX_TRADES_PER_DAY = 2). A side's flag is set the
     moment its breakout candle closes, even if the order itself fails, so
     a failed/rejected order is not retried.
  4. At most one position is held at a time; a breakout on the opposite
     side while a position is open is ignored (a documented simplification
     to avoid hedged/overlapping legs, as in opening_range_breakout_orb.py).

Inside-day filter (SKIP_AFTER_INSIDE_DAY, default ON, per the spec): if the
PREVIOUS day was an "inside day" -- its high/low fully inside the day before
it (prev.high <= prevprev.high AND prev.low >= prevprev.low) -- skip all
breakout trades for the day (a prior-day range contained by the one before
it tends to break falsely). Needs >= 2 completed prior daily bars; if fewer
are available the filter is skipped and a warning is logged (never blocks
trading for lack of data).

Stop (STOP_MODE):
  "level" (default): long -> PDH - BUFFER_PTS, short -> PDL + BUFFER_PTS
      (price closing/moving back inside the broken level invalidates the
      break).
  "atr": long -> entry - ATR_STOP_MULT x ATR, short -> entry + ...; ATR is
      ATR(ATR_PERIOD) on completed DAILY bars (sizes the stop dynamically,
      per the spec's "ATR can size it").
The spec's alternative "beneath the most recent swing formed during the
pullback" stop is discretionary / not cleanly testable from candle data and
is intentionally NOT implemented; documented here as an omission.

Target (TARGET_MODE):
  "rr" (default): fixed reward:risk = REWARD_RISK x the initial stop
      distance from the entry index price (default 2:1, per spec params).
  "atr": entry +/- ATR_TARGET_MULT x ATR ("next ATR-based projection").

Failed-break exit (EXIT_ON_FAILED_BREAK, default ON, per the spec): while in
a position, if a completed BAR_MINUTES candle CLOSES back inside the broken
level (CE: close < PDH; PE: close > PDL) the break is treated as failed and
the position is exited at once, independent of stop/target.

Profit lock (PROFIT_LOCK_TRIGGER_R, loss lock is the stop above): once the
index LTP has moved PROFIT_LOCK_TRIGGER_R x the initial stop distance in the
trade's favour, the stop is ratcheted (one-shot, never loosened) to a level
that locks in at least PROFIT_LOCK_MIN_R x that same distance of profit --
so a round-trip back toward entry can no longer turn a working trade into a
loss. 0 = off (old behaviour: only the original stop/target apply). No
partial exit (unlike vwap_standard_deviation_band_reversion_scalp's 1SD
partial) -- this strategy's LOTS default is 1, and splitting a 1-lot
position would need LOTS >= 2 first; moving the stop alone works at any size.

Exit: stop or target, whichever is hit first (checked against the live index
LTP every poll, not only on candle close, since real stop/target fills
happen intrabar), the failed-break rule above (checked on candle close), or
a hard square-off at SQUARE_OFF_TIME regardless of P&L. NO_NEW_ENTRY_AFTER,
SQUARE_OFF_TIME and the DAILY_MAX_LOSS safety cap are not part of the source
rules -- they are the same defensive pattern used in the other strategies.

Run first with OpenAlgo Analyzer (paper) mode ON. This script places real
orders once Analyzer is switched off -- do that only after a satisfactory
backtest and with the user's explicit go-ahead.
"""

import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openalgo import api
from script_openalgo.strategy_lib import regime  # noqa: E402

# ======================= PARAMETERS =======================
API_KEY = os.getenv("OPENALGO_API_KEY", "PASTE_YOUR_API_KEY_HERE")
HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
STRATEGY = "PrevDayHighLowBreakoutScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1                      # lots per entry
PRODUCT = "MIS"

BAR_MINUTES = 5               # breakout candle timeframe
TIMEFRAME = f"{BAR_MINUTES}m"
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

BUFFER_PTS = 0.0              # extra index points beyond PDH/PDL to confirm a break (spec default 0)

STOP_MODE = "level"           # "level" (stop at the broken level) | "atr"
ATR_STOP_MULT = 1.0           # STOP_MODE == "atr": stop = entry -/+ this x daily ATR
TARGET_MODE = "rr"            # "rr" (reward:risk x stop distance) | "atr"
REWARD_RISK = 2               # TARGET_MODE == "rr": target = this x initial stop distance
ATR_TARGET_MULT = 1.0         # TARGET_MODE == "atr": target = entry +/- this x daily ATR
ATR_PERIOD = 14               # ATR period on completed daily bars
DAILY_LOOKBACK_DAYS = 45      # calendar days of daily history fetched (enough trading days for ATR + inside-day)

SKIP_AFTER_INSIDE_DAY = True  # skip the day if the previous day was an inside day (spec filter)
EXIT_ON_FAILED_BREAK = True   # exit if a candle closes back inside the broken level

PROFIT_LOCK_TRIGGER_R = 1.0   # once price has moved this many x the initial stop distance in favour, ratchet the
                               # stop to lock in profit (one-shot); 0 = off (stop/target only, old behaviour)
PROFIT_LOCK_MIN_R = 0.3       # the locked stop guarantees at least this many x the initial stop distance of profit

MAX_TRADES_PER_DAY = 2        # one breakout per side, per the source rules

POLL_SEC = 20                 # main loop tick while idle/monitoring

# ---- daily safety cap (not in the source rules; same pattern as the other strategies) ----
DAILY_MAX_LOSS = 3000         # NIFTY-sized; resize per instrument (see knowledge/lessons.md)
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

        self.bars = []           # completed BAR_MINUTES-minute index candles for today
        self.last_bar_t = None   # timestamp cursor, not a list index: self.bars is rebuilt wholesale by refresh_bars()

        self.pdh = self.pdl = None
        self.atr = None
        self.skip_today = False
        self.levels_loaded = False

        self.long_taken = False
        self.short_taken = False
        self.trades_today = 0

        self.position = None     # see attempt_entry() for shape
        self.option_sym = None
        self.lot = None

        self.day_pnl = 0.0
        self._cap_logged = False

    # ---------- time helpers ----------
    def at(self, s):
        h, m = hhmm(s)
        return self.now().replace(hour=h, minute=m, second=0, microsecond=0)

    def wait_until(self, target):
        while self.now() < target:
            self.sleep(min(30, max(1, (target - self.now()).total_seconds())))

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

    def load_daily_levels(self):
        """Load PDH/PDL, the inside-day filter and daily ATR from COMPLETED
        daily bars (today's forming bar is filtered out -> no lookahead).
        Sets self.skip_today if the previous day was an inside day."""
        import pandas as pd
        self.levels_loaded = True
        today = self.now().date()
        start = (today - timedelta(days=DAILY_LOOKBACK_DAYS)).isoformat()
        end = today.isoformat()
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="D",
                                 start_date=start, end_date=end, source=HISTORY_SOURCE)
        except Exception as e:
            log(f"daily history exception: {e}; skipping the day")
            self.skip_today = True
            return
        if not hasattr(df, "empty") or df.empty:
            log(f"no daily history ({df}); skipping the day")
            self.skip_today = True
            return
        rows = []
        for ts, row in df.iterrows():
            t = ts.to_pydatetime()
            d = t.date()
            if d >= today:            # drop today's (forming) daily bar and anything in the future
                continue
            rows.append((d, float(row["high"]), float(row["low"]), float(row["close"])))
        rows.sort(key=lambda r: r[0])
        if not rows:
            log("no completed prior daily bars; skipping the day")
            self.skip_today = True
            return
        prev = rows[-1]
        self.pdh, self.pdl = prev[1], prev[2]
        log(f"Previous day {prev[0]}: PDH {self.pdh:.2f} PDL {self.pdl:.2f}")

        # inside-day filter: previous day's range inside the day before it
        if SKIP_AFTER_INSIDE_DAY:
            if len(rows) >= 2:
                pp = rows[-2]
                if prev[1] <= pp[1] and prev[2] >= pp[2]:
                    self.skip_today = True
                    log(f"Previous day {prev[0]} was an inside day (inside {pp[0]}: "
                        f"{pp[2]:.2f}-{pp[1]:.2f}); skipping breakout trades today")
            else:
                log("Only 1 completed prior daily bar (< 2); inside-day filter skipped")

        # daily ATR (used only when STOP_MODE/TARGET_MODE == 'atr')
        if len(rows) >= ATR_PERIOD + 1:
            adf = pd.DataFrame([{"high": r[1], "low": r[2], "close": r[3]} for r in rows])
            try:
                self.atr = float(regime.atr(adf, ATR_PERIOD).iloc[-1])
                log(f"Daily ATR({ATR_PERIOD}) = {self.atr:.2f}")
            except Exception as e:
                log(f"ATR computation failed: {e}")
                self.atr = None
        else:
            log(f"Only {len(rows)} daily bars (< {ATR_PERIOD + 1}); ATR unavailable")

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
                         "l": float(row["low"]), "c": float(row["close"])})
        bars.sort(key=lambda b: b["t"])
        self.bars = bars

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        for i, b in enumerate(self.bars):
            if self.last_bar_t is not None and b["t"] <= self.last_bar_t:
                continue
            self.handle_new_bar(i)
            self.last_bar_t = b["t"]

    def handle_new_bar(self, i):
        bar = self.bars[i]
        # failed-break exit (checked on candle close) overrides entry logic
        if self.position and EXIT_ON_FAILED_BREAK:
            pos = self.position
            if pos["side"] == "CE" and bar["c"] < self.pdh:
                self.exit_position(f"failed break: 5m close {bar['c']:.2f} back below PDH {self.pdh:.2f}")
                return
            if pos["side"] == "PE" and bar["c"] > self.pdl:
                self.exit_position(f"failed break: 5m close {bar['c']:.2f} back above PDL {self.pdl:.2f}")
                return
        if self.skip_today or self.position:
            return
        if self.now() >= self.at(NO_NEW_ENTRY_AFTER):
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log("Daily trade/loss cap reached; no further entries today")
                self._cap_logged = True
            return
        if not self.long_taken and bar["c"] > self.pdh + BUFFER_PTS:
            self.long_taken = True
            self.attempt_entry("CE", bar)
        elif not self.short_taken and bar["c"] < self.pdl - BUFFER_PTS:
            self.short_taken = True
            self.attempt_entry("PE", bar)

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
    def attempt_entry(self, side, bar):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log(f"{side} entry aborted: no index LTP")
            return
        if STOP_MODE == "atr":
            if not self.atr:
                log(f"{side} entry aborted: STOP_MODE='atr' but ATR unavailable")
                return
            initial_stop = idx_price - ATR_STOP_MULT * self.atr if side == "CE" \
                else idx_price + ATR_STOP_MULT * self.atr
        else:  # "level"
            initial_stop = self.pdh - BUFFER_PTS if side == "CE" else self.pdl + BUFFER_PTS
        stop_distance = idx_price - initial_stop if side == "CE" else initial_stop - idx_price
        if stop_distance <= 0:
            log(f"{side} entry aborted: non-positive stop distance ({stop_distance:.2f})")
            return
        if TARGET_MODE == "atr":
            if not self.atr:
                log(f"{side} entry aborted: TARGET_MODE='atr' but ATR unavailable")
                return
            target = idx_price + ATR_TARGET_MULT * self.atr if side == "CE" \
                else idx_price - ATR_TARGET_MULT * self.atr
        else:  # "rr"
            target = idx_price + REWARD_RISK * stop_distance if side == "CE" \
                else idx_price - REWARD_RISK * stop_distance
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        level = self.pdh if side == "CE" else self.pdl
        price = self.buy(self.option_sym, qty,
                         f"{side} break of {'PDH' if side == 'CE' else 'PDL'} {level:.2f} "
                         f"(5m close {bar['c']:.2f}) | index {idx_price:.2f} stop {initial_stop:.2f} "
                         f"target {target:.2f}")
        if price is None:
            return
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price,
            "stop": initial_stop, "target": target,
            "stop_distance": stop_distance, "lock_done": False,
        }
        self.trades_today += 1
        log(f"ENTER {side} | index {idx_price:.2f} stop {initial_stop:.2f} "
            f"({stop_distance:.2f} pts) target {target:.2f} | trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

    def exit_position(self, reason):
        pos = self.position
        px = self.sell(pos["symbol"], pos["qty"], reason)
        if px is None:
            log("Exit failed; position may still be open - check manually")
            return
        p = (px - pos["entry_premium"]) * pos["qty"]
        self.day_pnl += p
        log(f"EXIT {pos['side']} {reason} | qty {pos['qty']} @ {px} | pnl {p:+.0f} | day P&L {self.day_pnl:+.0f}")
        self.position = None

    def profit_lock(self, idx_ltp):
        """One-shot stop ratchet: once price has moved PROFIT_LOCK_TRIGGER_R x the initial stop distance in the
        trade's favour, move the stop up (CE) / down (PE) to lock in at least PROFIT_LOCK_MIN_R x that distance of
        profit. Never loosens the stop. No-op once already armed, or if PROFIT_LOCK_TRIGGER_R is off."""
        pos = self.position
        if pos["lock_done"] or not PROFIT_LOCK_TRIGGER_R:
            return
        dist = pos["stop_distance"]
        moved = (idx_ltp - pos["index_entry"]) if pos["side"] == "CE" else (pos["index_entry"] - idx_ltp)
        if moved < PROFIT_LOCK_TRIGGER_R * dist:
            return
        lock_level = pos["index_entry"] + PROFIT_LOCK_MIN_R * dist if pos["side"] == "CE" \
            else pos["index_entry"] - PROFIT_LOCK_MIN_R * dist
        old_stop = pos["stop"]
        pos["stop"] = max(old_stop, lock_level) if pos["side"] == "CE" else min(old_stop, lock_level)
        pos["lock_done"] = True
        log(f"PROFIT LOCK armed: index {idx_ltp:.2f} moved {moved:.2f} pts (>= {PROFIT_LOCK_TRIGGER_R}R) -> "
            f"stop {old_stop:.2f} -> {pos['stop']:.2f} (locks >= {PROFIT_LOCK_MIN_R}R)")

    def manage_position(self):
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        pos = self.position
        self.profit_lock(idx_ltp)
        stop_why = "profit-lock stop hit" if pos["lock_done"] else "stop hit"
        if pos["side"] == "CE":
            if idx_ltp <= pos["stop"]:
                self.exit_position(f"{stop_why} (index {idx_ltp:.2f} <= {pos['stop']:.2f})")
            elif idx_ltp >= pos["target"]:
                self.exit_position(f"target hit (index {idx_ltp:.2f} >= {pos['target']:.2f})")
        else:
            if idx_ltp >= pos["stop"]:
                self.exit_position(f"{stop_why} (index {idx_ltp:.2f} >= {pos['stop']:.2f})")
            elif idx_ltp <= pos["target"]:
                self.exit_position(f"target hit (index {idx_ltp:.2f} <= {pos['target']:.2f})")

    # ---------- main loop ----------
    def run(self):
        log(f"Strategy {STRATEGY} starting on {UNDERLYING}")
        try:
            a = self.c.analyzerstatus()
            log(f"Analyzer (paper) mode: {a.get('data', a)}")
        except Exception:
            pass
        self.wait_until(self.at(SESSION_START))
        self.load_daily_levels()
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
