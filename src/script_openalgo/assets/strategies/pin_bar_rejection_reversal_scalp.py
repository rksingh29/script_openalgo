"""
Pin Bar Rejection Reversal Scalp -- ATM CE/PE option buying for OpenAlgo
-----------------------------------------------------------------------
A price-action reversal scalp. The signal is a single-candle "pin bar"
(hammer / shooting star: a long rejection wick with a small body at the far
end) on the underlying INDEX's own 5-minute candles, confirmed by the very
next candle. The trade itself is an ATM option BUY: a bullish pin (long
LOWER wick) -> CE long; a bearish pin (long UPPER wick) -> PE long.

Signal vs execution split (same pattern as previous_day_high_low_breakout_
scalp.py / nifty_direction_ce_pe.py): the pin geometry, confirmation, stop
and target are all measured in INDEX points on completed 5-minute candles;
the stop/target are then monitored against the live index LTP and converted
to rupee P&L through the option fill price at entry.

Pin bar geometry (bar b, measured on its OHLC; range = high - low):
    body        = |close - open|
    upper_wick  = high - max(open, close)
    lower_wick  = min(open, close) - low
  Bullish pin (CE candidate):
    lower_wick >= WICK_BODY_RATIO * body        (wick at least ~2x the body)
    lower_wick >= WICK_BODY_RATIO * upper_wick  (lower wick dominates -> body near the top)
    body <= BODY_MAX_FRAC * range               (small body)
  Bearish pin (PE candidate): the mirror image (long UPPER wick, body near
  the bottom).

Confirmation (spec: "wait for the next candle to close above/below the pin's
body"): the pin sets a PENDING signal; the IMMEDIATELY NEXT completed 5-min
candle must close above the pin body's top (bullish) / below the pin body's
bottom (bearish). Entry fires on that confirmation candle's close. If the
next candle does not confirm, the pending signal expires (CONFIRM_WINDOW_BARS
= 1). A new pin cannot be formed while a position is open or a signal is
pending (one trade at a time).

Entry: ENTRY_MODE = "confirm_close" (default) enters at market when the
confirmation candle closes. The spec's alternative "enter on a break of the
pin high/low" is a discretionary intrabar trigger and is NOT implemented;
documented here as an omission.

Stop (STOP_MODE):
  "wick" (default): just beyond the tip of the pin's wick -- long ->
      pin.low - STOP_BUFFER_PTS, short -> pin.high + STOP_BUFFER_PTS. "Never
      inside the wick." On a typical pin this wick tip sits roughly 1 ATR
      away from the entry, matching the spec's "about 1 ATR" description.
  "atr": long -> entry - STOP_ATR_MULT * ATR, short -> entry + ... ATR is
      ATR(ATR_PERIOD) on 5-minute candles (prior days fetched once to warm
      it up, since a single session has too few 5m bars for ATR(14) early
      in the day -- see knowledge/strategy_patterns.md 2026-10-20).

Target: fixed reward:risk = REWARD_RISK x the initial stop distance from the
entry index price (default 1:2, per the spec params). The spec's "or the
next intraday S/R level" is discretionary / not cleanly testable from OHLCV
and is NOT implemented (documented omission). The "prefer pins at tested
support/resistance" filter is likewise discretionary and omitted -- the pin
geometry + next-candle confirmation is the testable core.

Exit: stop or target, whichever the live index LTP hits first (checked every
poll, not only on candle close, since real stop/target fills happen
intrabar), or a hard square-off at SQUARE_OFF_TIME regardless of P&L.
NO_NEW_ENTRY_AFTER, SQUARE_OFF_TIME, MAX_TRADES_PER_DAY and the DAILY_MAX_LOSS
safety cap are not part of the source rules -- they are the same defensive
pattern used in the other strategies.

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
STRATEGY = "PinBarRejectionReversalScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1                      # lots per entry
PRODUCT = "MIS"

BAR_MINUTES = 5               # pin-bar timeframe
TIMEFRAME = f"{BAR_MINUTES}m"
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

# ---- pin-bar geometry ----
WICK_BODY_RATIO = 2.0         # rejection wick >= this x the body AND >= this x the opposite wick
BODY_MAX_FRAC = 0.35          # body must be <= this fraction of the candle's range (a "small" body)
MIN_RANGE_PTS = 0.0           # ignore pins whose high-low range is below this many index points (0 = off)

CONFIRM_WINDOW_BARS = 1       # confirmation must be the immediately next completed candle

ENTRY_MODE = "confirm_close"  # "confirm_close" only (the "break of pin high" variant is not implemented)

# ---- stop / target ----
STOP_MODE = "wick"            # "wick" (just beyond the pin's wick tip) | "atr"
STOP_BUFFER_PTS = 0.0         # extra index points beyond the wick tip (STOP_MODE == "wick")
STOP_ATR_MULT = 1.0           # STOP_MODE == "atr": stop = entry -/+ this x 5m ATR
REWARD_RISK = 2               # target = this x the initial stop distance (spec default 1:2)
ATR_PERIOD = 14               # ATR period on 5-minute candles
WARMUP_LOOKBACK_DAYS = 7      # calendar days of prior 5m candles fetched to warm up the ATR

MAX_TRADES_PER_DAY = 6        # scalp; defensive cap (not a source rule)
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

        self.pending = None      # {"side", "bar_index", "pin"} awaiting next-candle confirmation
        self.position = None     # see attempt_entry() for shape
        self.option_sym = None
        self.lot = None

        self.trades_today = 0
        self.day_pnl = 0.0
        self._cap_logged = False
        self._warm = None        # (date, [prior-day 5m rows]) ATR warm-up cache

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

    def current_atr(self):
        """ATR(ATR_PERIOD) on 5-minute candles, prior days prepended so it is warm
        even early in the session. Only completed candles are used (no lookahead)."""
        import pandas as pd
        today = self.now().date()
        if self._warm is None or self._warm[0] != today:
            rows = []
            try:
                w = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=TIMEFRAME,
                                    start_date=(today - timedelta(days=WARMUP_LOOKBACK_DAYS)).isoformat(),
                                    end_date=(today - timedelta(days=1)).isoformat(), source=HISTORY_SOURCE)
                if hasattr(w, "iterrows") and len(w):
                    for ts, row in w.iterrows():
                        if ts.to_pydatetime().date() < today:
                            rows.append({"high": float(row["high"]), "low": float(row["low"]),
                                         "close": float(row["close"])})
            except Exception as e:
                log(f"ATR warm-up fetch failed: {e}")
            self._warm = (today, rows)
        rows = list(self._warm[1])
        rows += [{"high": b["h"], "low": b["l"], "close": b["c"]} for b in self.bars]
        if len(rows) < ATR_PERIOD + 1:
            return None
        try:
            return float(regime.atr(pd.DataFrame(rows), ATR_PERIOD).iloc[-1])
        except Exception as e:
            log(f"ATR computation failed: {e}")
            return None

    # ---------- pin-bar detection ----------
    @staticmethod
    def classify_pin(b):
        """Return 'CE' for a bullish pin (long lower wick), 'PE' for a bearish pin
        (long upper wick), else None."""
        o, h, l, c = b["o"], b["h"], b["l"], b["c"]
        rng = h - l
        if rng <= 0 or rng < MIN_RANGE_PTS:
            return None
        body = abs(c - o)
        upper = h - max(o, c)
        lower = min(o, c) - l
        if body > BODY_MAX_FRAC * rng:
            return None
        if lower >= WICK_BODY_RATIO * body and lower >= WICK_BODY_RATIO * upper:
            return "CE"
        if upper >= WICK_BODY_RATIO * body and upper >= WICK_BODY_RATIO * lower:
            return "PE"
        return None

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        for i, b in enumerate(self.bars):
            if self.last_bar_t is not None and b["t"] <= self.last_bar_t:
                continue
            self.handle_new_bar(i)
            self.last_bar_t = b["t"]

    def handle_new_bar(self, i):
        bar = self.bars[i]
        # 1. resolve a pending pin against this (confirmation) candle first
        if self.pending is not None:
            self.evaluate_pending(i)
        # 2. one trade at a time: no new pin while in a position or already pending
        if self.position is not None or self.pending is not None:
            return
        side = self.classify_pin(bar)
        if side:
            self.pending = {"side": side, "bar_index": i, "pin": bar}
            tag = "bullish (hammer)" if side == "CE" else "bearish (shooting star)"
            log(f"PIN {tag} at {bar['t']:%H:%M} O{bar['o']:.1f} H{bar['h']:.1f} "
                f"L{bar['l']:.1f} C{bar['c']:.1f} -> awaiting confirmation")

    def evaluate_pending(self, i):
        p = self.pending
        pin = p["pin"]
        side = p["side"]
        conf = self.bars[i]
        top = max(pin["o"], pin["c"])
        bot = min(pin["o"], pin["c"])
        confirmed = (conf["c"] > top) if side == "CE" else (conf["c"] < bot)
        if confirmed:
            self.pending = None
            if self.entry_allowed(side):
                self.attempt_entry(side, pin, conf)
        elif i - p["bar_index"] >= CONFIRM_WINDOW_BARS:
            log(f"PIN {side} at {pin['t']:%H:%M} not confirmed by {conf['t']:%H:%M} "
                f"(close {conf['c']:.1f}); signal expired")
            self.pending = None

    def entry_allowed(self, side):
        if self.now() >= self.at(NO_NEW_ENTRY_AFTER):
            log(f"{side} confirmed but past NO_NEW_ENTRY_AFTER; skipped")
            return False
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log("Daily trade/loss cap reached; no further entries today")
                self._cap_logged = True
            return False
        return True

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
    def attempt_entry(self, side, pin, conf):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log(f"{side} entry aborted: no index LTP")
            return
        if STOP_MODE == "atr":
            atr = self.current_atr()
            if not atr:
                log(f"{side} entry aborted: STOP_MODE='atr' but ATR unavailable")
                return
            initial_stop = idx_price - STOP_ATR_MULT * atr if side == "CE" \
                else idx_price + STOP_ATR_MULT * atr
        else:  # "wick": just beyond the pin's wick tip
            initial_stop = pin["l"] - STOP_BUFFER_PTS if side == "CE" \
                else pin["h"] + STOP_BUFFER_PTS
        stop_distance = idx_price - initial_stop if side == "CE" else initial_stop - idx_price
        if stop_distance <= 0:
            log(f"{side} entry aborted: non-positive stop distance ({stop_distance:.2f})")
            return
        target = idx_price + REWARD_RISK * stop_distance if side == "CE" \
            else idx_price - REWARD_RISK * stop_distance
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        price = self.buy(self.option_sym, qty,
                         f"{side} pin confirmed (pin {pin['t']:%H:%M}, confirm {conf['t']:%H:%M} "
                         f"close {conf['c']:.1f}) | index {idx_price:.2f} stop {initial_stop:.2f} "
                         f"target {target:.2f}")
        if price is None:
            return
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price,
            "stop": initial_stop, "target": target,
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

    def manage_position(self):
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        pos = self.position
        if pos["side"] == "CE":
            if idx_ltp <= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} <= {pos['stop']:.2f})")
            elif idx_ltp >= pos["target"]:
                self.exit_position(f"target hit (index {idx_ltp:.2f} >= {pos['target']:.2f})")
        else:
            if idx_ltp >= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} >= {pos['stop']:.2f})")
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
