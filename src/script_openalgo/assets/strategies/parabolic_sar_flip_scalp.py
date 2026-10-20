"""
Parabolic SAR Flip Scalp (ATM CE/PE option buying) for OpenAlgo
----------------------------------------------------------------
Signal is computed on the underlying INDEX's own 5-minute candles
(Wilder Parabolic SAR, step 0.02 -> max 0.20); the trade itself is an
ATM option BUY sized in lots (CE on a bullish flip, PE on a bearish
flip -- this project always BUYS options, never shorts them, per
nifty_direction_ce_pe.py / s_9_21_ema_crossover_scalp.py convention).
Stops are tracked in INDEX points (the trailing SAR dot) so they match
the signal that generated them, then converted to a lot-sized option
P&L at exit.

Evaluated on every newly CLOSED 5m index candle:

Long (CE):  the Parabolic SAR flips from ABOVE price to BELOW price
            (the SAR trend turns up / the candle closes above the new
            SAR dot). Enter on the close of that confirming (flip)
            candle.
Short (PE): the Parabolic SAR flips from BELOW price to ABOVE price
            (trend turns down / candle closes below the new SAR dot).
            Enter on the close of that flip candle.

Optional confirmation (both default OFF, each an independent toggle;
see the adaptation note below on why):
  - USE_EMA_TREND_FILTER: price above the EMA_PERIOD EMA for a long,
    below it for a short (higher-timeframe-bias filter).
  - USE_MACD_FILTER: MACD histogram above zero for a long, below zero
    for a short.
  When both are ON, both must agree (AND).

Stop (index points): the current Parabolic SAR dot level at entry (it
sits just beyond the recent swing). The SAR dot trails each new bar and
becomes the moving stop -- ratcheted toward price every completed
candle, never loosened.

Exit:
  - Opposite SAR flip -> exit (and, since an opposite flip is itself an
    entry signal for the other side, stop-and-reverse into a new trade
    subject to the time window / daily caps). This is the primary exit;
    the SAR acts as a trailing stop that tightens each bar.
  - Trailing SAR stop hit (index LTP crosses the current SAR dot),
    checked intrabar on every poll.
  - Optional quick fixed target of ~1R (USE_FIXED_TARGET, default OFF
    per the spec's "no fixed target by default; let the trailing SAR
    exit"): target = entry index +/- FIXED_TARGET_R x the initial stop
    distance.
  - Square-off at SQUARE_OFF_TIME (EXIT_BEFORE_CLOSE_MIN minutes before
    the 15:30 close) regardless of P&L; no new entries after
    NO_NEW_ENTRY_AFTER.

Optional confirmation candle (CONFIRM_BARS, default 0 = off): on a flip the
signal is only noted; the entry waits for the next CONFIRM_BARS closed candles,
each of which must stay on the flip's side of the SAR and close beyond the flip
candle's close (+ CONFIRM_MIN_MOVE_PCT). Otherwise the signal is dropped. The
stop is the SAR at the confirming candle. Added 2026-10-20 after a whipsaw on
NIFTY 2026-10-19 (CE bought at the 09:35 flip, the market reversed at once).

Daily safety caps (MAX_TRADES_PER_DAY / DAILY_MAX_LOSS /
DAILY_PROFIT_TARGET) are the same pattern as the other strategies here;
they are not part of the source rules.

Adaptations from the raw research spec (documented, not guesses):
  - The 200-period EMA filter cannot warm up inside a single intraday
    session on 5m candles (a trading day has only ~75 five-minute bars),
    and the backtest/live history fetch only serves the current day, so
    USE_EMA_TREND_FILTER defaults OFF and is exposed as a toggle (the
    source calls the EMA an OPTIONAL confirmation). MACD (12/26/9) warms
    up ~mid-session, so USE_MACD_FILTER is likewise an optional toggle,
    default OFF, so the base SAR-flip signal can trade from the open.
  - "Enter on the close of the confirming candle (price closes beyond
    the new SAR dot)" is taken to mean the flip candle itself (the SAR
    algorithm only flips once price has closed on the new side), so
    entry fires on that bar's close.
  - "Quick scalp target ~= the initial risk (1R)" is implemented as the
    optional USE_FIXED_TARGET / FIXED_TARGET_R toggle.

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
STRATEGY = "ParabolicSarFlipScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"       # candles are built from the open
START_TIME = "09:15"          # SAR needs a couple of bars to establish a trend; early bars warm it up
NO_NEW_ENTRY_AFTER = "15:00"
EXIT_BEFORE_CLOSE_MIN = 15    # close any position this many minutes before the 15:30 session close
SQUARE_OFF_TIME = "15:15"     # = 15:30 close - EXIT_BEFORE_CLOSE_MIN

# ---- Parabolic SAR (Wilder) ----
SAR_STEP = 0.02               # acceleration factor step
SAR_MAX = 0.20                # acceleration factor cap
WARMUP_BARS = 2               # ignore flips before this many completed bars (seed artifacts)

# ---- optional confirmation filters (see docstring adaptation note) ----
USE_EMA_TREND_FILTER = False  # price vs EMA_PERIOD EMA; OFF: 200 EMA can't warm up intraday on 5m
EMA_PERIOD = 200
USE_MACD_FILTER = False       # MACD histogram sign must agree with the flip direction
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

# ---- optional quick 1R target (spec: no fixed target by default) ----
# ---- optional confirmation candle (default OFF = enter on the flip candle's close, as before) ----
CONFIRM_BARS = 0              # 0 = off; N = after a flip, wait N more closed 5m candles before entering
CONFIRM_MIN_MOVE_PCT = 0.0    # each confirming close must be beyond the flip candle's close by this % of price

USE_FIXED_TARGET = False
FIXED_TARGET_R = 1.0          # target = entry +/- this x the initial SAR stop distance

POLL_SEC = 20                 # main loop tick while idle/monitoring

# ---- daily safety caps (not in the source rules; same pattern as other strategies here) ----
MAX_TRADES_PER_DAY = 6
DAILY_MAX_LOSS = 3000          # NIFTY-sized; resize per instrument (see knowledge/lessons.md)
DAILY_PROFIT_TARGET = 0        # 0 = off
# ==========================================================


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def hhmm(s):
    h, m = map(int, s.split(":"))
    return h, m


def ema_series(values, period):
    """EMA aligned 1:1 with values; None until the SMA-seeded index (period-1)."""
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    for i in range(period, n):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def macd_hist_series(closes, fast, slow, signal):
    """MACD histogram (MACD line - signal line), aligned 1:1 with closes; None until warm."""
    n = len(closes)
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    macd = [None] * n
    for i in range(n):
        if ef[i] is not None and es[i] is not None:
            macd[i] = ef[i] - es[i]
    hist = [None] * n
    idxs = [i for i in range(n) if macd[i] is not None]
    if len(idxs) < signal:
        return hist
    sig = ema_series([macd[i] for i in idxs], signal)
    for j, i in enumerate(idxs):
        if sig[j] is not None:
            hist[i] = macd[i] - sig[j]
    return hist


def psar_series(highs, lows, closes, step, max_step):
    """Wilder Parabolic SAR aligned 1:1 with the bars.

    Returns (sar, trend) where trend[i] is +1 (up / SAR below price) or
    -1 (down / SAR above price). A flip is trend[i] != trend[i-1].
    """
    n = len(highs)
    sar = [None] * n
    trend = [None] * n
    if n == 0:
        return sar, trend
    # Seed the initial trend from the first move (resolves within a bar or two).
    up = True if n < 2 else (closes[1] >= closes[0])
    af = step
    ep = highs[0] if up else lows[0]
    sar_val = lows[0] if up else highs[0]
    sar[0] = sar_val
    trend[0] = 1 if up else -1
    for i in range(1, n):
        sar_val = sar_val + af * (ep - sar_val)
        if up:
            # SAR may not rise above the prior two lows
            sar_val = min(sar_val, lows[i - 1], lows[i - 2] if i >= 2 else lows[i - 1])
            if lows[i] < sar_val:            # price broke below SAR -> flip down
                up = False
                sar_val = ep                 # SAR jumps to the prior extreme point
                ep = lows[i]
                af = step
            elif highs[i] > ep:
                ep = highs[i]
                af = min(af + step, max_step)
        else:
            # SAR may not fall below the prior two highs
            sar_val = max(sar_val, highs[i - 1], highs[i - 2] if i >= 2 else highs[i - 1])
            if highs[i] > sar_val:           # price broke above SAR -> flip up
                up = True
                sar_val = ep
                ep = highs[i]
                af = step
            elif lows[i] < ep:
                ep = lows[i]
                af = min(af + step, max_step)
        sar[i] = sar_val
        trend[i] = 1 if up else -1
    return sar, trend


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []           # completed 5m index candles: {t, o, h, l, c}
        self.sar = []
        self.trend = []
        self.ema = []
        self.macd_hist = []
        self.processed_bars = 0

        self.pending = None      # flip waiting for confirmation: {side, i, close, sar}
        self.position = None     # see try_enter() for shape
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
        highs = [b["h"] for b in bars]
        lows = [b["l"] for b in bars]
        closes = [b["c"] for b in bars]
        self.sar, self.trend = psar_series(highs, lows, closes, SAR_STEP, SAR_MAX)
        self.ema = ema_series(closes, EMA_PERIOD) if USE_EMA_TREND_FILTER else []
        self.macd_hist = macd_hist_series(closes, MACD_FAST, MACD_SLOW, MACD_SIGNAL) \
            if USE_MACD_FILTER else []

    # ---------- signal ----------
    def detect_flip(self, i):
        """Returns 'CE' (flip up) / 'PE' (flip down) if bar i is a fresh SAR flip, else None."""
        if i < max(1, WARMUP_BARS):
            return None
        if self.trend[i] is None or self.trend[i - 1] is None:
            return None
        if self.trend[i] == self.trend[i - 1]:
            return None
        return "CE" if self.trend[i] == 1 else "PE"

    def filters_ok(self, side, i):
        bar = self.bars[i]
        if USE_EMA_TREND_FILTER:
            e = self.ema[i] if i < len(self.ema) else None
            if e is None:
                log(f"Entry skipped: EMA{EMA_PERIOD} not warm yet")
                return False
            if side == "CE" and not bar["c"] > e:
                log(f"Entry skipped: price {bar['c']:.2f} not above EMA{EMA_PERIOD} {e:.2f}")
                return False
            if side == "PE" and not bar["c"] < e:
                log(f"Entry skipped: price {bar['c']:.2f} not below EMA{EMA_PERIOD} {e:.2f}")
                return False
        if USE_MACD_FILTER:
            h = self.macd_hist[i] if i < len(self.macd_hist) else None
            if h is None:
                log("Entry skipped: MACD histogram not warm yet")
                return False
            if side == "CE" and not h > 0:
                log(f"Entry skipped: MACD histogram {h:.2f} not > 0")
                return False
            if side == "PE" and not h < 0:
                log(f"Entry skipped: MACD histogram {h:.2f} not < 0")
                return False
        return True

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        while self.processed_bars < len(self.bars):
            self.handle_new_bar(self.processed_bars)
            self.processed_bars += 1

    def in_time_window(self):
        return self.at(START_TIME) <= self.now() < self.at(NO_NEW_ENTRY_AFTER)

    def handle_new_bar(self, i):
        flip = self.detect_flip(i)
        if self.position:
            self.manage_on_bar_close(i, flip)
        if CONFIRM_BARS > 0:
            self.confirm_step(i, flip)
            return
        # An opposite flip exits above; the same flip is the entry signal for the new side
        # (stop-and-reverse), so re-evaluate entry now that the position may be flat.
        if not self.position and flip:
            self.try_enter(i, flip)

    def confirm_step(self, i, flip):
        """CONFIRM_BARS > 0: a flip is only noted; the entry needs the next CONFIRM_BARS closed candles to stay on the
        flip's side of the SAR and to close beyond the flip candle's close (+ CONFIRM_MIN_MOVE_PCT)."""
        bar = self.bars[i]
        if flip:                                       # a new flip (also an opposite one) replaces any pending signal
            if self.pending and self.pending["side"] != flip:
                log(f"Pending {self.pending['side']} flip from {self.bars[self.pending['i']]['t']:%H:%M} dropped: opposite flip")
            if not self.position:
                self.pending = {"side": flip, "i": i, "close": bar["c"], "sar": self.sar[i]}
                log(f"{flip} SAR flip at {bar['t']:%H:%M} close {bar['c']:.2f}: waiting {CONFIRM_BARS} candle(s) for confirmation")
            return
        p = self.pending
        if not p or self.position:
            return
        same_side = self.trend[i] == (1 if p["side"] == "CE" else -1)
        need = p["close"] * (1 + CONFIRM_MIN_MOVE_PCT / 100) if p["side"] == "CE" else p["close"] * (1 - CONFIRM_MIN_MOVE_PCT / 100)
        beyond = bar["c"] > need if p["side"] == "CE" else bar["c"] < need
        if not (same_side and beyond):
            why = "SAR side lost" if not same_side else f"close {bar['c']:.2f} not {'above' if p['side'] == 'CE' else 'below'} {need:.2f}"
            log(f"{p['side']} flip from {self.bars[p['i']]['t']:%H:%M} NOT confirmed at {bar['t']:%H:%M}: {why}; no trade")
            self.pending = None
            return
        if i - p["i"] < CONFIRM_BARS:
            return                                     # confirmed so far, more candles to wait for
        log(f"{p['side']} flip from {self.bars[p['i']]['t']:%H:%M} confirmed at {bar['t']:%H:%M} (close {bar['c']:.2f} vs {p['close']:.2f})")
        self.pending = None
        self.try_enter(i, p["side"])

    def manage_on_bar_close(self, i, flip):
        """Opposite-flip exit + ratchet the trailing SAR stop toward price."""
        pos = self.position
        wanted_flip = "PE" if pos["side"] == "CE" else "CE"
        if flip == wanted_flip:
            self.exit_position(f"opposite SAR flip at bar {i} ({self.bars[i]['t']:%H:%M})")
            return
        sar = self.sar[i]
        if sar is None:
            return
        if pos["side"] == "CE":
            if sar > pos["stop"]:
                log(f"Trailing SAR stop raised {pos['stop']:.2f} -> {sar:.2f}")
                pos["stop"] = sar
        else:
            if sar < pos["stop"]:
                log(f"Trailing SAR stop lowered {pos['stop']:.2f} -> {sar:.2f}")
                pos["stop"] = sar

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
    def try_enter(self, i, side):
        if not self.in_time_window():
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS or \
                (DAILY_PROFIT_TARGET and self.day_pnl >= DAILY_PROFIT_TARGET):
            if not self._cap_logged:
                log("Daily trade/loss/profit cap reached; no further entries today")
                self._cap_logged = True
            return
        sar = self.sar[i]
        if sar is None:
            return
        if not self.filters_ok(side, i):
            return
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return
        stop_price = sar
        stop_distance = abs(idx_price - stop_price)
        if stop_distance <= 0:
            log("Entry aborted: zero stop distance (price at SAR)")
            return
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        price = self.buy(self.option_sym, qty,
                          f"{side} SAR flip | index {idx_price:.2f} stop {stop_price:.2f}")
        if price is None:
            return
        target = None
        if USE_FIXED_TARGET:
            target = idx_price + FIXED_TARGET_R * stop_distance if side == "CE" \
                else idx_price - FIXED_TARGET_R * stop_distance
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price,
            "stop": stop_price, "target": target,
        }
        self.trades_today += 1
        tgt_txt = f"target {target:.2f} ({FIXED_TARGET_R}R)" if target is not None else "target none (trail SAR)"
        log(f"ENTER {side} | index {idx_price:.2f} stop {stop_price:.2f} {tgt_txt} | "
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
        """Trailing SAR stop (and optional fixed target) checked against live index LTP."""
        if not self.position:
            return
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        pos = self.position
        if pos["side"] == "CE":
            if idx_ltp <= pos["stop"]:
                self.exit_position(f"SAR stop hit (index {idx_ltp:.2f} <= {pos['stop']:.2f})")
            elif pos["target"] is not None and idx_ltp >= pos["target"]:
                self.exit_position(f"target hit (index {idx_ltp:.2f} >= {pos['target']:.2f})")
        else:
            if idx_ltp >= pos["stop"]:
                self.exit_position(f"SAR stop hit (index {idx_ltp:.2f} >= {pos['stop']:.2f})")
            elif pos["target"] is not None and idx_ltp <= pos["target"]:
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
            self.manage_position_ltp()
            self.sleep(POLL_SEC)
        log(f"Done for the day. Trades {self.trades_today} | day P&L {self.day_pnl:+.0f}")


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
