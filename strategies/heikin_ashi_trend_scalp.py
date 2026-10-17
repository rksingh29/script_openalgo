"""
Heikin Ashi Trend Scalp (ATM CE/PE option buying) for OpenAlgo
-----------------------------------------------------------------
Heikin Ashi (HA) candles are built from the underlying INDEX's own
5-minute OHLC (history()); a 9/21 EMA pair is plotted on the index's
RAW (regular) candle close. The trade itself is an ATM option BUY
sized in lots (CE on a bullish signal, PE on a bearish signal -- this
project always buys options, never writes/shorts them, per
nifty_direction_ce_pe.py / vwap_reclaim_scalp.py / s_9_21_ema_crossover
_scalp.py convention; the spec's "PE short" is realised as "buy PE").

Long (CE) setup, evaluated on every newly CLOSED 5m index candle `i`:
  1. The last CONFIRM_CANDLES Heikin Ashi candles (bars i-CONFIRM_
     CANDLES+1 .. i) are all GREEN (HA close > HA open).
  2. Each of those candles has little/no LOWER wick: lower_wick /
     (high-low) <= MAX_WICK_RATIO (the side that would show
     hesitation against the up-move).
  3. Each of those candles has a real body: body / (high-low) >=
     MIN_BODY_RATIO (filters the "large wicks on both ends / doji"
     indecision case from the spec's "filters" field -- since
     body_ratio + lower_wick_ratio + upper_wick_ratio == 1, capping
     the body from below also caps both wicks together).
  4. The index's raw close at candle i is above EMA9 AND EMA21.
  Entry: BUY ATM CE at candle i's close.
Short (PE) setup mirrors this with RED candles, little/no UPPER wick,
and raw close below both EMAs -- BUY ATM PE.

Stop: beyond the low (long) / high (short) of the most recent Heikin
Ashi candle in the trend run, with a small buffer (STOP_BUFFER_PCT of
index price). While in a trade, every new candle that keeps the same
HA colour ratchets the stop to that candle's HA low/high (trailing
stop that only ever tightens, never loosens). Skipped at entry if the
initial stop distance exceeds STOP_MAX_PCT of the index price (safety
cap, not in the source spec).

Target: none -- per spec ("no fixed target; ride the trend"), the
only exits are the stop, the colour-flip signal and the square-off
time (plus the daily safety caps below).

Exit (checked continuously via LTP, and on every new 5m candle close):
  - Stop hit (index LTP vs the current trailing stop) -> exit.
  - Colour-flip signal: on every new completed 5m candle while in a
    trade, if the candle's HA colour is NOT the trend colour, bump an
    `opposite_streak` counter; once opposite_streak > EXIT_PAUSE_
    CANDLES, exit ("colour flip signals momentum loss"). A candle that
    matches the trend colour resets the streak to 0.
    EXIT_PAUSE_CANDLES = 0 -> exit on the FIRST flip candle (spec's
    primary rule). Set to 1 to tolerate one pause candle before
    exiting (spec's offered alternative).
  - Square-off at SQUARE_OFF_TIME regardless of P&L.

Entries stop after NO_NEW_ENTRY_AFTER or once MAX_TRADES_PER_DAY /
DAILY_MAX_LOSS is reached (safety caps, same pattern as the sibling
strategies in this project; not part of the source rules).

Adaptations from the raw research spec (documented, not guesses):
  - EMA9/EMA21 are computed on the RAW index close, not the HA close.
    The spec says "plot 9/21 EMAs" without saying on which series; the
    raw close is the more standard, less lagged choice, and keeps the
    EMA filter independent of the HA smoothing.
  - "Little/no wick" -> a wick/(high-low) ratio <= MAX_WICK_RATIO on
    the side that would contradict the trend (no single numeric
    threshold is given in the sources).
  - "Large wicks on both ends" (indecision filter) -> a minimum body-
    ratio requirement, see point 3 above.
  - "Stop beyond the low/high of the most recent HA candle in the
    run" -> trailing stop ratcheted to each new same-coloured
    candle's HA low/high; never loosened.
  - "Exit on the first flip candle, or the second to tolerate one
    pause candle" -> both offered in the spec as alternatives; kept as
    a single module constant (EXIT_PAUSE_CANDLES) so either behaviour
    can be selected/A-B'd without code changes.
  - MAX_TRADES_PER_DAY / DAILY_MAX_LOSS / STOP_MAX_PCT are safety
    caps, not sourced from the research material.

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
STRATEGY = "HeikinAshiTrendScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"       # candles are built from the open
START_TIME = "09:15"          # no separate entry delay; EMA21/HA warm-up naturally gates early entries
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

FAST_EMA = 9
SLOW_EMA = 21

CONFIRM_CANDLES = 2           # "two or three consecutive" HA candles; spec default
MAX_WICK_RATIO = 0.25         # trend-opposing wick / (high-low) must be <= this ("little/no wick")
MIN_BODY_RATIO = 0.5          # body / (high-low) must be >= this (indecision/doji filter)

STOP_BUFFER_PCT = 0.05        # stop placed this % of index price beyond the HA candle low/high
STOP_MAX_PCT = 0.6            # skip the trade if the initial stop distance exceeds this % of index price
EXIT_PAUSE_CANDLES = 0        # 0 = exit on first flip candle; 1 = tolerate one pause candle

POLL_SEC = 20                 # main loop tick while idle/monitoring

# ---- daily safety caps (not in the source rules; same pattern as other strategies here) ----
MAX_TRADES_PER_DAY = 4
DAILY_MAX_LOSS = 3000          # NIFTY-sized; resize per instrument (see knowledge/lessons.md)
DAILY_PROFIT_TARGET = 0        # 0 = off
# ==========================================================


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def hhmm(s):
    h, m = map(int, s.split(":"))
    return h, m


def ema_series(closes, period):
    """EMA aligned 1:1 with closes; None until the SMA-seeded index (period-1)."""
    n = len(closes)
    out = [None] * n
    if n < period:
        return out
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    for i in range(period, n):
        out[i] = closes[i] * k + out[i - 1] * (1 - k)
    return out


def heikin_ashi(bars):
    """Builds HA candles {o,h,l,c} 1:1 with raw bars {o,h,l,c}."""
    ha = []
    for i, b in enumerate(bars):
        o, h, l, c = b["o"], b["h"], b["l"], b["c"]
        ha_close = (o + h + l + c) / 4.0
        ha_open = (o + c) / 2.0 if i == 0 else (ha[i - 1]["o"] + ha[i - 1]["c"]) / 2.0
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        ha.append({"o": ha_open, "h": ha_high, "l": ha_low, "c": ha_close})
    return ha


def ha_stats(hb):
    """Returns (colour, body_ratio, lower_wick_ratio, upper_wick_ratio) for one HA candle."""
    rng = hb["h"] - hb["l"]
    body = abs(hb["c"] - hb["o"])
    if hb["c"] > hb["o"]:
        colour = "green"
        lower_wick, upper_wick = hb["o"] - hb["l"], hb["h"] - hb["c"]
    elif hb["c"] < hb["o"]:
        colour = "red"
        lower_wick, upper_wick = hb["c"] - hb["l"], hb["h"] - hb["o"]
    else:
        colour = "flat"
        lower_wick = upper_wick = rng / 2.0
    if rng <= 0:
        return colour, 1.0, 0.0, 0.0
    return colour, body / rng, lower_wick / rng, upper_wick / rng


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []            # completed 5m index candles: {t, o, h, l, c}
        self.ha_bars = []         # Heikin Ashi candles, 1:1 with self.bars
        self.ema9 = []
        self.ema21 = []
        self.processed_bars = 0

        self.position = None      # see attempt_entry() for shape
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
        if not bars:
            return
        self.bars = bars
        self.ha_bars = heikin_ashi(bars)
        closes = [b["c"] for b in bars]
        self.ema9 = ema_series(closes, FAST_EMA)
        self.ema21 = ema_series(closes, SLOW_EMA)

    # ---------- setup detection ----------
    def confirm_run(self, i, side):
        """True if bars i-CONFIRM_CANDLES+1 .. i are all a clean HA run for `side` ('CE'/'PE')."""
        if i - CONFIRM_CANDLES + 1 < 0:
            return False
        want_colour = "green" if side == "CE" else "red"
        for j in range(i - CONFIRM_CANDLES + 1, i + 1):
            colour, body_ratio, lower_wick_ratio, upper_wick_ratio = ha_stats(self.ha_bars[j])
            if colour != want_colour:
                return False
            if body_ratio < MIN_BODY_RATIO:
                return False
            opposing_wick_ratio = lower_wick_ratio if side == "CE" else upper_wick_ratio
            if opposing_wick_ratio > MAX_WICK_RATIO:
                return False
        return True

    def ema_filter(self, i, side):
        e9, e21 = self.ema9[i], self.ema21[i]
        if e9 is None or e21 is None:
            return False
        close = self.bars[i]["c"]
        return close > e9 and close > e21 if side == "CE" else close < e9 and close < e21

    def detect_signal(self, i):
        """Returns 'CE' or 'PE' if candle i completes a qualifying HA trend run, else None."""
        for side in ("CE", "PE"):
            if self.confirm_run(i, side) and self.ema_filter(i, side):
                return side
        return None

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        while self.processed_bars < len(self.bars):
            self.handle_new_bar(self.processed_bars)
            self.processed_bars += 1

    def in_time_window(self):
        return self.at(START_TIME) <= self.now() < self.at(NO_NEW_ENTRY_AFTER)

    def handle_new_bar(self, i):
        if self.position:
            self.manage_on_bar_close(i)
            return
        if not self.in_time_window():
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log("Daily trade/loss cap reached; no further entries today")
                self._cap_logged = True
            return
        side = self.detect_signal(i)
        if side:
            bar, hb = self.bars[i], self.ha_bars[i]
            log(f"{side} Heikin Ashi {CONFIRM_CANDLES}-candle run confirmed at bar {i} "
                f"({bar['t']:%H:%M}); entering")
            self.attempt_entry(side, hb)

    def manage_on_bar_close(self, i):
        """Trailing stop ratchet + colour-flip exit."""
        pos = self.position
        colour, _, _, _ = ha_stats(self.ha_bars[i])
        trend_colour = "green" if pos["side"] == "CE" else "red"
        bar, hb = self.bars[i], self.ha_bars[i]
        if colour == trend_colour:
            pos["opposite_streak"] = 0
            buffer_pts = (STOP_BUFFER_PCT / 100.0) * bar["c"]
            if pos["side"] == "CE":
                new_stop = hb["l"] - buffer_pts
                if new_stop > pos["stop"]:
                    log(f"Trailing stop (HA low) raised {pos['stop']:.2f} -> {new_stop:.2f}")
                    pos["stop"] = new_stop
            else:
                new_stop = hb["h"] + buffer_pts
                if new_stop < pos["stop"]:
                    log(f"Trailing stop (HA high) lowered {pos['stop']:.2f} -> {new_stop:.2f}")
                    pos["stop"] = new_stop
            return
        pos["opposite_streak"] += 1
        if pos["opposite_streak"] > EXIT_PAUSE_CANDLES:
            self.exit_position(f"HA colour flip against {pos['side']} "
                                f"(streak {pos['opposite_streak']})")
        else:
            log(f"Pause candle ({colour}) tolerated against {pos['side']} "
                f"(streak {pos['opposite_streak']}/{EXIT_PAUSE_CANDLES})")

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
    def attempt_entry(self, side, confirm_ha_bar):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return
        buffer_pts = (STOP_BUFFER_PCT / 100.0) * idx_price
        if side == "CE":
            stop_price = confirm_ha_bar["l"] - buffer_pts
        else:
            stop_price = confirm_ha_bar["h"] + buffer_pts
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
        price = self.buy(self.option_sym, qty,
                          f"{side} HA trend run | index {idx_price:.2f} stop {stop_price:.2f}")
        if price is None:
            return
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price,
            "stop": stop_price, "opposite_streak": 0,
        }
        self.trades_today += 1
        log(f"ENTER {side} | index {idx_price:.2f} stop {stop_price:.2f} ({stop_pct:.2f}%) "
            f"| trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

    def exit_position(self, reason):
        pos = self.position
        px = self.sell(pos["symbol"], pos["qty"], reason)
        if px is None:
            log("Exit failed; position may still be open - check manually")
            return
        p = (px - pos["entry_premium"]) * pos["qty"]
        self.day_pnl += p
        log(f"EXIT {pos['side']} {reason} | qty {pos['qty']} @ {px} | pnl {p:+.0f} | "
            f"day P&L {self.day_pnl:+.0f}")
        self.position = None

    def manage_position_ltp(self):
        """Stop hits checked against live LTP (can happen intrabar, unlike bar-close checks)."""
        if not self.position:
            return
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        pos = self.position
        if pos["side"] == "CE" and idx_ltp <= pos["stop"]:
            self.exit_position(f"stop hit (index {idx_ltp:.2f} <= {pos['stop']:.2f})")
        elif pos["side"] == "PE" and idx_ltp >= pos["stop"]:
            self.exit_position(f"stop hit (index {idx_ltp:.2f} >= {pos['stop']:.2f})")

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
            self.manage_position_ltp()
            self.sleep(POLL_SEC)
        log(f"Done for the day. Trades {self.trades_today} | day P&L {self.day_pnl:+.0f}")


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
