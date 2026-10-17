"""
Bollinger Band Bounce Scalp (ATM CE/PE option buying) for OpenAlgo
--------------------------------------------------------------------
Signal is computed on the underlying INDEX's own 5-minute candles
(20-period Bollinger Bands, 2 std dev, and a 7-period RSI); the trade
itself is always an ATM option BUY sized in lots (CE expresses the
long/bounce-off-lower-band bias, PE expresses the short/bounce-off-
upper-band bias -- this project always buys options, never shorts
them, per nifty_direction_ce_pe.py / vwap_reclaim_scalp.py /
s_9_21_ema_crossover_scalp.py convention). Stops and the moving
middle-band target are tracked in INDEX points so they match the
signal that generated them, then converted to a lot-sized option P&L
at exit.

Long (CE) setup, evaluated on every newly CLOSED 5m index candle:
  1. Trigger candle: close is below the lower Bollinger Band
     (basis - BB_STDDEV * stddev, both over BB_PERIOD closes) AND the
     RSI_PERIOD-period RSI is below RSI_OVERSOLD.
  2. Range filter (see "filters" below): the trigger is only taken if
     the market is not persistently "walking the band" (see
     Adaptations).
  3. Confirmation candle (the very next completed 5m candle): closes
     back inside the bands, i.e. above the lower band again.
  Entry: BUY ATM CE at (approximately) the confirmation candle's
  close -- in practice placed at the next poll's LTP, same
  no-lookahead pattern as the other bar-driven strategies here.
Short (PE) setup mirrors this at the upper band (close above the
upper band, RSI above RSI_OVERBOUGHT, range filter, then the next
candle closes back below the upper band) -- BUY ATM PE.

Stop (set once, at entry): beyond the low (long) / high (short) of
the confirmation candle itself, OR 1x ATR(ATR_PERIOD) beyond the
entry index price, whichever is TIGHTER (closer to entry) -- per the
spec's "whichever is tighter" rule. A small STOP_BUFFER_PCT buffer is
added beyond the chosen boundary, STOP_MAX_PCT caps the trade if even
the tighter stop is too wide (safety cap, not in the source spec).

Target: the 20-period middle Bollinger Band (the basis/SMA line).
Because the basis is a moving average, not a fixed price, the target
is re-evaluated on every new completed 5m candle (the most recently
computed basis value) and checked continuously against the index LTP
-- the position is closed as soon as the index price touches the
*current* basis line, never held for the opposite band (per spec).

Exit (checked continuously via LTP for the stop and the moving
target):
  - Target hit: index LTP reaches the current middle-band value.
  - Stop hit: index LTP breaches the fixed stop set at entry.
  - Square-off at SQUARE_OFF_TIME regardless of P&L.

Entries stop after NO_NEW_ENTRY_AFTER or once MAX_TRADES_PER_DAY /
DAILY_MAX_LOSS is reached (safety caps, same pattern as the other
strategies in this project; not part of the source rules). Only one
position open at a time.

Adaptations from the raw research spec (documented, not guesses):
  - Bollinger Band stddev uses the population definition (divide by
    N, not N-1) -- the standard definition used by most charting
    platforms; the source does not specify sample vs population.
  - RSI uses Wilder smoothing (the textbook/standard RSI definition,
    same as rsi_2_mean_reversion_scalp.py); the source does not
    specify an alternate smoothing method.
  - ATR uses Wilder smoothing with ATR_PERIOD=14 (the standard
    default) -- the spec says "1x ATR" but never states a period.
  - "Only take this setup in range-bound conditions ... avoid when
    price is walking the band" has no numeric definition in the
    source. Testable proxy: at the trigger candle, count how many of
    the last TREND_FILTER_LOOKBACK closes (including the trigger
    candle) were already beyond the same band side; if more than
    MAX_BAND_WALK_BARS, treat it as a trending "band walk" and skip
    the setup (an isolated poke-and-reclaim is allowed; persistent
    closes beyond the band are not).
  - STOP_BUFFER_PCT / STOP_MAX_PCT / MAX_TRADES_PER_DAY /
    DAILY_MAX_LOSS / DAILY_PROFIT_TARGET are safety caps, not sourced
    from the research material.

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
STRATEGY = "BollingerBandBounceScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"         # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"        # candles are built from the open
START_TIME = "09:15"           # no separate entry delay; BB/RSI/ATR warm-up naturally gates early entries
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

BB_PERIOD = 20                 # Bollinger Band basis (SMA) period
BB_STDDEV = 2                  # Bollinger Band width, in std deviations
RSI_PERIOD = 7
RSI_OVERSOLD = 35              # trigger threshold for the long (CE) setup
RSI_OVERBOUGHT = 65             # trigger threshold for the short (PE) setup

TREND_FILTER_LOOKBACK = 10      # bars (incl. trigger bar) examined for "band walk" (ADAPTATION)
MAX_BAND_WALK_BARS = 2          # allow this many recent closes beyond the band before calling it a trend

ATR_PERIOD = 14                 # ADAPTATION: spec says "1x ATR" but not a period; 14 is the standard default
ATR_MULT = 1.0                  # 1x ATR beyond entry, per spec
STOP_BUFFER_PCT = 0.05          # stop placed this % of index price beyond the chosen boundary
STOP_MAX_PCT = 0.6              # skip the trade if stop distance exceeds this % of index price

POLL_SEC = 20                   # main loop tick while idle/monitoring

# ---- daily safety caps (not in the source rules; same pattern as other strategies here) ----
MAX_TRADES_PER_DAY = 6
DAILY_MAX_LOSS = 3000            # NIFTY-sized; resize per instrument (see knowledge/lessons.md)
DAILY_PROFIT_TARGET = 0          # 0 = off
# ==========================================================


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def hhmm(s):
    h, m = map(int, s.split(":"))
    return h, m


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


def bollinger_series(closes, period, stddev_mult):
    """(basis, upper, lower) aligned 1:1 with closes; None until index (period-1).
    Population standard deviation (divide by N), the common BB convention."""
    n = len(closes)
    basis = [None] * n
    upper = [None] * n
    lower = [None] * n
    if n < period:
        return basis, upper, lower
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        std = var ** 0.5
        basis[i] = mean
        upper[i] = mean + stddev_mult * std
        lower[i] = mean - stddev_mult * std
    return basis, upper, lower


def atr_wilder_series(bars, period):
    """Wilder-smoothed ATR aligned 1:1 with bars; None until index == period."""
    n = len(bars)
    out = [None] * n
    if n < period + 1:
        return out
    trs = [None] * n
    for i in range(1, n):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs[i] = max(h - l, abs(h - pc), abs(l - pc))
    first = sum(trs[1:period + 1]) / period
    out[period] = first
    prev = first
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []            # completed 5m index candles: {t, o, h, l, c}
        self.basis = []
        self.upper = []
        self.lower = []
        self.rsi = []
        self.atr = []
        self.processed_bars = 0

        self.pending = None       # {"side": "CE"/"PE", "trigger_idx": i}
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
        self.basis, self.upper, self.lower = bollinger_series(closes, BB_PERIOD, BB_STDDEV)
        self.rsi = rsi_wilder_series(closes, RSI_PERIOD)
        self.atr = atr_wilder_series(bars, ATR_PERIOD)

    # ---------- setup detection ----------
    def band_walk_count(self, i, side):
        lo = max(0, i - TREND_FILTER_LOOKBACK + 1)
        count = 0
        for j in range(lo, i + 1):
            c = self.bars[j]["c"]
            if side == "CE" and self.lower[j] is not None and c < self.lower[j]:
                count += 1
            elif side == "PE" and self.upper[j] is not None and c > self.upper[j]:
                count += 1
        return count

    def detect_signal(self, i):
        """Returns 'CE' or 'PE' if bar i is a fresh, range-bound band-touch trigger, else None."""
        basis, upper, lower, rsi = self.basis[i], self.upper[i], self.lower[i], self.rsi[i]
        if None in (basis, upper, lower, rsi):
            return None
        bar = self.bars[i]
        if bar["c"] < lower and rsi < RSI_OVERSOLD:
            walk = self.band_walk_count(i, "CE")
            if walk > MAX_BAND_WALK_BARS:
                log(f"CE trigger at bar {i} skipped: band-walk count {walk} > {MAX_BAND_WALK_BARS} (trending)")
                return None
            return "CE"
        if bar["c"] > upper and rsi > RSI_OVERBOUGHT:
            walk = self.band_walk_count(i, "PE")
            if walk > MAX_BAND_WALK_BARS:
                log(f"PE trigger at bar {i} skipped: band-walk count {walk} > {MAX_BAND_WALK_BARS} (trending)")
                return None
            return "PE"
        return None

    def evaluate_pending(self, i):
        p = self.pending
        self.pending = None
        bar = self.bars[i]
        lower_i, upper_i = self.lower[i], self.upper[i]
        if p["side"] == "CE":
            confirmed = lower_i is not None and bar["c"] > lower_i
        else:
            confirmed = upper_i is not None and bar["c"] < upper_i
        if confirmed:
            log(f"Reclaim confirmed for {p['side']} at bar {i} ({bar['t']:%H:%M}); entering")
            self.attempt_entry(p["side"], i)
        else:
            log(f"Reclaim not confirmed for {p['side']} at bar {i}; setup dropped")

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
        if self.pending:
            self.evaluate_pending(i)
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
            self.pending = {"side": side, "trigger_idx": i}
            log(f"{side} band-touch trigger at bar {i} ({bar['t']:%H:%M}); close {bar['c']:.2f} "
                f"RSI {self.rsi[i]:.1f}; waiting for reclaim confirmation")

    def manage_on_bar_close(self, i):
        """Target is the moving middle band; just log it so it is visible each candle."""
        basis = self.basis[i]
        if basis is not None:
            log(f"Target (middle band) now {basis:.2f} | position {self.position['side']}")

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
    def attempt_entry(self, side, confirm_i):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return
        bar = self.bars[confirm_i]
        basis_val = self.basis[confirm_i]
        atr_val = self.atr[confirm_i]
        if basis_val is None:
            log("Entry aborted: no basis (middle band) value")
            return
        if side == "CE" and basis_val <= idx_price:
            log("Entry skipped: middle band is not above current price, no room to target")
            return
        if side == "PE" and basis_val >= idx_price:
            log("Entry skipped: middle band is not below current price, no room to target")
            return

        buffer_pts = (STOP_BUFFER_PCT / 100.0) * idx_price
        if side == "CE":
            candle_stop = bar["l"] - buffer_pts
            atr_stop = idx_price - ATR_MULT * atr_val if atr_val else None
            candidates = [candle_stop] + ([atr_stop] if atr_stop is not None else [])
            stop_price = max(candidates)          # tighter stop = closer to price (larger value)
        else:
            candle_stop = bar["h"] + buffer_pts
            atr_stop = idx_price + ATR_MULT * atr_val if atr_val else None
            candidates = [candle_stop] + ([atr_stop] if atr_stop is not None else [])
            stop_price = min(candidates)          # tighter stop = closer to price (smaller value)

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
                          f"{side} BB bounce reclaim | index {idx_price:.2f} stop {stop_price:.2f} "
                          f"target(basis) {basis_val:.2f}")
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
        """Stop and the moving middle-band target checked against live LTP (can happen intrabar)."""
        if not self.position:
            return
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        pos = self.position
        target = self.basis[-1] if self.basis else None
        if pos["side"] == "CE":
            if idx_ltp <= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} <= {pos['stop']:.2f})")
            elif target is not None and idx_ltp >= target:
                self.exit_position(f"middle band target hit (index {idx_ltp:.2f} >= {target:.2f})")
        else:
            if idx_ltp >= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} >= {pos['stop']:.2f})")
            elif target is not None and idx_ltp <= target:
                self.exit_position(f"middle band target hit (index {idx_ltp:.2f} <= {target:.2f})")

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
