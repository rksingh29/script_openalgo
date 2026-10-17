"""
RSI(2) Mean Reversion Scalp (ATM CE/PE option buying) for OpenAlgo
--------------------------------------------------------------------
Source idea (Larry Connors / Connors RSI-2, paraphrased, see sources in the
spec): on daily bars, use a long-term SMA (200-day) as a trend filter and a
2-period RSI extreme as the pullback trigger, exiting on a close back above/
below a short SMA (5-day). This file adapts that to intraday 5-minute index
candles (NIFTY or BANKNIFTY), per the user's explicit spec:
  - trend filter: TREND_MA (default 50) period SMA on 5m closes, replacing
    the source's 200-day SMA.
  - exit filter: EXIT_MA (default 5) period SMA on 5m closes, replacing the
    source's 5-day SMA.
This rescaling is an adaptation, not the literal source timeframe (flagged
"testable": "adapt" in the spec).

Signal is computed on the underlying INDEX's own 5-minute candles; the
trade itself is always an ATM option BUY sized in lots (CE expresses a long/
pullback-in-uptrend bias, PE expresses a short/bounce-in-downtrend bias --
this project always buys options, never shorts them, per
nifty_direction_ce_pe.py / vwap_reclaim_scalp.py / s_9_21_ema_crossover_scalp.py
convention).

Long (CE) setup, evaluated on every newly CLOSED 5m index candle:
  1. Trend filter: candle's close is above TREND_MA-period SMA (uptrend).
  2. Trigger: 2-period RSI (Wilder-smoothed, the standard RSI definition --
     the source does not specify the smoothing method) is <= OVERSOLD
     (deeply oversold short-term dip within the uptrend; spec's usable
     range is 5-10, default 5).
  Entry: BUY ATM CE at (approximately) the signal candle's close -- in
  practice placed at the next poll's LTP, same no-lookahead pattern as the
  other bar-driven strategies in this project.
Short (PE) setup mirrors this below the trend MA (close < TREND_MA SMA,
2-period RSI >= OVERBOUGHT, default 95) -- BUY ATM PE.

Exit (checked continuously via LTP for the stop, and on every new 5m candle
close for the signal exit):
  - Signal exit (the spec's literal exit rule): on a completed 5m candle,
    exit the long once the candle's close is above the EXIT_MA-period SMA;
    exit the short once the candle's close is below the EXIT_MA-period SMA.
  - Hard stop (ADAPTATION -- the source defines no stop at all; the spec
    explicitly asks for "a hard stop below/above the recent swing low/high
    to control risk intraday"): at entry, the stop is set just beyond the
    lowest low (long) / highest high (short) of the last SWING_LOOKBACK
    completed 5m candles (including the signal candle), with a small buffer
    (STOP_BUFFER_PCT of index price). Skipped if that distance exceeds
    STOP_MAX_PCT of index price (safety cap, not in the source spec). The
    stop is fixed at entry (not trailed) since the source/spec describe no
    trailing rule.
  - Square-off at SQUARE_OFF_TIME regardless of P&L.

Entries stop after NO_NEW_ENTRY_AFTER or once MAX_TRADES_PER_DAY /
DAILY_MAX_LOSS is reached (safety caps, same pattern as the other
strategies in this project; not part of the source rules). Only one
position open at a time.

Adaptations from the raw research spec (documented, not guesses):
  - 200-day / 5-day SMAs -> 50-period / 5-period SMA on 5-minute intraday
    candles (explicit in the spec's "params" and "testable_reason").
  - RSI(2) uses Wilder smoothing (the textbook/standard RSI definition);
    the source material does not specify an alternate smoothing method.
  - Hard swing-low/high stop (SWING_LOOKBACK, STOP_BUFFER_PCT,
    STOP_MAX_PCT) is an explicit adaptation requested in the spec; no
    fixed price target is used (exit is signal-based, per spec).
  - MAX_TRADES_PER_DAY / DAILY_MAX_LOSS / DAILY_PROFIT_TARGET are safety
    caps, not sourced from the research material.

Run first with OpenAlgo Analyzer (paper) mode ON. This script places real
orders once Analyzer is switched off -- do that only after a satisfactory
backtest and with the user's explicit go-ahead.
"""

import os
import time
from datetime import datetime, timedelta
from openalgo import api

# ======================= PARAMETERS =======================
API_KEY = os.getenv("OPENALGO_API_KEY", "PASTE_YOUR_API_KEY_HERE")
HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
STRATEGY = "Rsi2MeanReversionScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"         # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"        # candles are built from the open
START_TIME = "09:15"           # no separate entry delay; TREND_MA warm-up naturally gates early entries
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

RSI_PERIOD = 2
OVERSOLD = 5                   # RSI(2) <= this on an uptrend candle -> long trigger (spec range 5-10)
OVERBOUGHT = 95                # RSI(2) >= this on a downtrend candle -> short trigger (spec range 90-95)
TREND_MA = 50                  # SMA period (5m bars) used as the long-term trend filter
EXIT_MA = 5                    # SMA period (5m bars) used as the signal exit

SWING_LOOKBACK = 5             # bars (incl. signal bar) used for the hard swing stop (ADAPTATION)
STOP_BUFFER_PCT = 0.05         # stop placed this % of index price beyond the swing low/high
STOP_MAX_PCT = 0.6             # skip the trade if stop distance exceeds this % of index price

POLL_SEC = 20                  # main loop tick while idle/monitoring

# ---- daily safety caps (not in the source rules; same pattern as other strategies here) ----
MAX_TRADES_PER_DAY = 6
DAILY_MAX_LOSS = 3000           # NIFTY-sized; resize per instrument (see knowledge/lessons.md)
DAILY_PROFIT_TARGET = 0         # 0 = off
# ==========================================================


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def hhmm(s):
    h, m = map(int, s.split(":"))
    return h, m


def sma_series(values, period):
    """SMA aligned 1:1 with values; None until index (period-1)."""
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    window_sum = sum(values[:period])
    out[period - 1] = window_sum / period
    for i in range(period, n):
        window_sum += values[i] - values[i - period]
        out[i] = window_sum / period
    return out


def rsi_wilder_series(closes, period):
    """Wilder-smoothed RSI aligned 1:1 with closes; None until index == period."""
    n = len(closes)
    out = [None] * n
    if n < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return out


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []            # completed 5m index candles: {t, o, h, l, c}
        self.rsi = []
        self.trend_sma = []
        self.exit_sma = []
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
            log(f"history exception ({TIMEFRAME}): {e}")
            return
        if not hasattr(df, "empty"):
            log(f"history error ({TIMEFRAME}): {df}")
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
        closes = [b["c"] for b in bars]
        self.rsi = rsi_wilder_series(closes, RSI_PERIOD)
        self.trend_sma = sma_series(closes, TREND_MA)
        self.exit_sma = sma_series(closes, EXIT_MA)

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
            bar = self.bars[i]
            log(f"{side} RSI(2) extreme at bar {i} ({bar['t']:%H:%M}); RSI {self.rsi[i]:.1f} "
                f"vs trend SMA({TREND_MA}) {self.trend_sma[i]:.2f}, close {bar['c']:.2f}")
            self.attempt_entry(side, i)

    def detect_signal(self, i):
        """Returns 'CE' or 'PE' if bar i is a fresh RSI(2) extreme within the trend, else None."""
        r, t_sma = self.rsi[i], self.trend_sma[i]
        if r is None or t_sma is None:
            return None
        bar = self.bars[i]
        if bar["c"] > t_sma and r <= OVERSOLD:
            return "CE"
        if bar["c"] < t_sma and r >= OVERBOUGHT:
            return "PE"
        return None

    def manage_on_bar_close(self, i):
        """Signal exit: close back above/below the short EXIT_MA SMA."""
        pos = self.position
        e_sma = self.exit_sma[i]
        if e_sma is None:
            return
        bar = self.bars[i]
        if pos["side"] == "CE" and bar["c"] > e_sma:
            self.exit_position(f"signal exit: close {bar['c']:.2f} above EXIT_MA({EXIT_MA}) {e_sma:.2f}")
        elif pos["side"] == "PE" and bar["c"] < e_sma:
            self.exit_position(f"signal exit: close {bar['c']:.2f} below EXIT_MA({EXIT_MA}) {e_sma:.2f}")

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
    def swing_stop(self, i, side):
        lo = max(0, i - SWING_LOOKBACK + 1)
        window = self.bars[lo:i + 1]
        if side == "CE":
            return min(b["l"] for b in window)
        return max(b["h"] for b in window)

    def attempt_entry(self, side, bar_i):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return
        buffer_pts = (STOP_BUFFER_PCT / 100.0) * idx_price
        swing = self.swing_stop(bar_i, side)
        stop_price = swing - buffer_pts if side == "CE" else swing + buffer_pts
        stop_distance = (idx_price - stop_price) if side == "CE" else (stop_price - idx_price)
        if stop_distance <= 0:
            log(f"Entry skipped: non-positive stop distance (idx {idx_price:.2f}, stop {stop_price:.2f})")
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
                          f"{side} RSI(2) mean reversion | index {idx_price:.2f} stop {stop_price:.2f}")
        if price is None:
            return
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price, "stop": stop_price,
        }
        self.trades_today += 1
        log(f"ENTER {side} | index {idx_price:.2f} stop {stop_price:.2f} ({stop_pct:.2f}%) | "
            f"trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

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
        """Hard stop checked against live LTP (can happen intrabar, unlike the bar-close signal exit)."""
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
