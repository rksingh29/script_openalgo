"""
MACD Signal-Line Crossover Scalp (ATM CE/PE option buying) for OpenAlgo
------------------------------------------------------------------------
Signal is computed on the underlying INDEX's own 5-minute candles using a
fast MACD setting (8,17,9 by default); the trade itself is an ATM option
BUY sized in lots (CE on a bullish signal-line cross, PE on a bearish
cross -- this project always buys options, never shorts them, per
nifty_direction_ce_pe.py / s_9_21_ema_crossover_scalp.py convention). Stops
and targets are tracked in INDEX points so they match the signal that
generated them, then converted to a lot-sized option P&L at exit.

MACD = EMA(close, FAST) - EMA(close, SLOW); signal = EMA(MACD, SIGNAL);
histogram = MACD - signal.

Long (CE) setup, evaluated on every newly CLOSED 5m index candle:
  MACD line crosses above the signal line (MACD[i-1] <= signal[i-1] and
  MACD[i] > signal[i]). Zero-line trend filter (REQUIRE_ZERO_LINE_FILTER,
  default True per the spec's "filters" section): only taken if MACD is
  above zero at the crossover candle (confirms the bullish bias).
  Entry: BUY ATM CE at the crossover candle's close.
Short (PE) setup mirrors this: MACD crosses below the signal line, MACD
below zero at the crossover candle (if the filter is on) -- BUY ATM PE.

Stop: just beyond the LOW (long) / HIGH (short) of the crossover candle
itself (index points, "stop beyond the recent swing low/high at the
crossover candle" from the spec -- the crossover candle's own low/high is
used as the simpler, deterministic choice, same simplification as
s_9_21_ema_crossover_scalp.py), with a small buffer (STOP_BUFFER_PCT of
index price). Skipped if that distance exceeds STOP_MAX_PCT of the index
price (safety cap, not in the source spec).

Target: fixed reward:risk of REWARD_RISK x the initial stop distance from
the entry index level (per spec "~1.5:1 to 2:1 R:R"; default 1.5).

Exit (checked continuously via LTP, and on every new 5m candle close):
  - Target or stop hit (index LTP) -> exit.
  - Momentum-fading exit ("MACD and signal lines start converging again"):
    the histogram's absolute value is tracked for its post-entry peak; if
    it retraces CONVERGENCE_RETRACE_PCT% from that peak, momentum is
    judged to be fading and the trade exits. This is the concrete,
    testable form of the spec's qualitative "lines converging" cue.
  - Time-limit exit: if neither target/stop nor the convergence exit has
    fired within TIME_LIMIT_CANDLES completed 5m candles since entry
    (spec: "a short time limit, e.g. 2-5 candles"), exit regardless of
    P&L.
  - Square-off at SQUARE_OFF_TIME regardless of P&L.

Entries stop after NO_NEW_ENTRY_AFTER or once MAX_TRADES_PER_DAY /
DAILY_MAX_LOSS is reached (safety caps, same pattern as other strategies
in this project; not part of the source rules).

Adaptations from the raw research spec (documented, not guesses):
  - The spec does not say whether MACD is computed on the underlying or
    the option premium. Following this project's established convention
    (s_9_21_ema_crossover_scalp.py, vwap_reclaim_scalp.py): signal on the
    underlying index's 5m closes, trade = ATM option buy, stop/target
    tracked in index points.
  - "MACD above zero = only longs, below zero = only shorts" -> a toggle
    (REQUIRE_ZERO_LINE_FILTER, default True) rather than a hard-coded
    gate, so the backtest/reviewer can A/B it (same pattern used for the
    HTF trend filter in s_9_21_ema_crossover_scalp.py).
  - "Stop beyond the recent swing low/high at the crossover candle" -> the
    crossover candle's own low/high (simpler, deterministic choice).
  - "Exit when MACD and signal lines start converging again" -> histogram
    magnitude retracing CONVERGENCE_RETRACE_PCT% from its post-entry peak
    (a numeric proxy for a qualitative cue with no single definition in
    the sources).
  - "A short time limit (2-5 candles)" -> TIME_LIMIT_CANDLES = 4 (module
    constant, tunable).
  - MAX_TRADES_PER_DAY / DAILY_MAX_LOSS / STOP_MAX_PCT are safety caps,
    not sourced from the research material.

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
STRATEGY = "MacdSignalCrossoverScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"       # candles are built from the open
START_TIME = "09:15"          # no separate entry delay; EMA warm-up naturally gates early entries
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

FAST = 8
SLOW = 17
SIGNAL = 9

REQUIRE_ZERO_LINE_FILTER = True   # MACD above zero = only CE, below zero = only PE

STOP_BUFFER_PCT = 0.05        # stop placed this % of index price beyond the crossover candle low/high
STOP_MAX_PCT = 0.6            # skip the trade if stop distance exceeds this % of index price
REWARD_RISK = 1.5             # target = this x the stop distance

CONVERGENCE_RETRACE_PCT = 50.0   # exit when |histogram| retraces this % from its post-entry peak
TIME_LIMIT_CANDLES = 4           # exit after this many completed candles if nothing else fired

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


def ema_series(values, period):
    """EMA aligned 1:1 with values. `values` may have a leading run of
    None (e.g. the MACD line, which is undefined until both underlying
    EMAs warm up). Seeds with the SMA of the first `period` consecutive
    non-None values; None before that point."""
    n = len(values)
    out = [None] * n
    start = None
    for i in range(n):
        if values[i] is None:
            start = None
            continue
        if start is None:
            start = i
        if i - start + 1 == period:
            seed_start = start
            break
    else:
        return out
    seed = sum(values[seed_start:seed_start + period]) / period
    out[seed_start + period - 1] = seed
    k = 2.0 / (period + 1)
    for i in range(seed_start + period, n):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


def macd_series(closes, fast, slow, signal):
    """Returns (macd_line, signal_line, histogram) aligned 1:1 with closes."""
    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    macd_line = [None if (a is None or b is None) else a - b
                 for a, b in zip(ema_fast, ema_slow)]
    signal_line = ema_series(macd_line, signal)
    hist = [None if (m is None or s is None) else m - s
            for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []           # completed 5m index candles: {t, o, h, l, c}
        self.macd = []
        self.signal_line = []
        self.hist = []
        self.processed_bars = 0

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

    def _fetch_bars(self, interval, bar_minutes):
        today = self.now().strftime("%Y-%m-%d")
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=interval,
                                 start_date=today, end_date=today, source=HISTORY_SOURCE)
        except Exception as e:
            log(f"history exception ({interval}): {e}")
            return []
        if not hasattr(df, "empty"):
            log(f"history error ({interval}): {df}")
            return []
        if df.empty:
            return []
        now = self.now()
        bars = []
        for ts, row in df.iterrows():
            t = ts.to_pydatetime()
            if t.tzinfo is not None:
                t = t.replace(tzinfo=None)   # SDK already converts intraday bars to IST wall time
            if t.date() != now.date():
                continue
            if t + timedelta(minutes=bar_minutes) > now:
                continue   # candle not fully completed yet; no lookahead
            bars.append({"t": t, "o": float(row["open"]), "h": float(row["high"]),
                         "l": float(row["low"]), "c": float(row["close"])})
        bars.sort(key=lambda b: b["t"])
        return bars

    def refresh_bars(self):
        bars = self._fetch_bars(TIMEFRAME, BAR_MINUTES)
        if not bars:
            return
        self.bars = bars
        closes = [b["c"] for b in bars]
        self.macd, self.signal_line, self.hist = macd_series(closes, FAST, SLOW, SIGNAL)

    # ---------- setup detection ----------
    def detect_cross(self, i):
        """Returns 'CE' or 'PE' if bar i is a fresh MACD/signal-line crossover
        candle (zero-line filter applied if REQUIRE_ZERO_LINE_FILTER), else None."""
        if i < 1:
            return None
        m, s = self.macd, self.signal_line
        if None in (m[i - 1], s[i - 1], m[i], s[i]):
            return None
        bullish = m[i - 1] <= s[i - 1] and m[i] > s[i]
        bearish = m[i - 1] >= s[i - 1] and m[i] < s[i]
        if bullish:
            if REQUIRE_ZERO_LINE_FILTER and m[i] <= 0:
                return None
            return "CE"
        if bearish:
            if REQUIRE_ZERO_LINE_FILTER and m[i] >= 0:
                return None
            return "PE"
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
        if (self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS
                or (DAILY_PROFIT_TARGET and self.day_pnl >= DAILY_PROFIT_TARGET)):
            if not self._cap_logged:
                log("Daily trade/loss/profit cap reached; no further entries today")
                self._cap_logged = True
            return
        side = self.detect_cross(i)
        if side:
            bar = self.bars[i]
            log(f"{side} MACD/signal cross at bar {i} ({bar['t']:%H:%M}); entering")
            self.attempt_entry(side, i, bar["l"], bar["h"])

    def manage_on_bar_close(self, i):
        """Momentum-fading (histogram retrace) + candle time-limit exits."""
        pos = self.position
        h = self.hist[i] if i < len(self.hist) else None
        if h is not None:
            pos["peak_hist"] = max(pos["peak_hist"], abs(h))
            if pos["peak_hist"] > 0 and abs(h) <= pos["peak_hist"] * (1 - CONVERGENCE_RETRACE_PCT / 100.0):
                self.exit_position(
                    f"momentum fading: MACD/signal converging "
                    f"(|hist| {abs(h):.3f} retraced from peak {pos['peak_hist']:.3f})")
                return
        bars_since_entry = i - pos["entry_bar_index"]
        if bars_since_entry >= TIME_LIMIT_CANDLES:
            self.exit_position(
                f"time limit ({TIME_LIMIT_CANDLES} candles) reached with no target/stop/convergence hit")

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
    def attempt_entry(self, side, cross_bar_index, cross_low, cross_high):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return
        buffer_pts = (STOP_BUFFER_PCT / 100.0) * idx_price
        if side == "CE":
            stop_price = cross_low - buffer_pts
        else:
            stop_price = cross_high + buffer_pts
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
                          f"{side} MACD/signal cross | index {idx_price:.2f} stop {stop_price:.2f}")
        if price is None:
            return
        target = idx_price + REWARD_RISK * stop_distance if side == "CE" \
            else idx_price - REWARD_RISK * stop_distance
        start_hist = self.hist[cross_bar_index] if cross_bar_index < len(self.hist) else None
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price,
            "stop": stop_price, "target": target,
            "entry_bar_index": cross_bar_index,
            "peak_hist": abs(start_hist) if start_hist is not None else 0.0,
        }
        self.trades_today += 1
        log(f"ENTER {side} | index {idx_price:.2f} stop {stop_price:.2f} ({stop_pct:.2f}%) "
            f"target {target:.2f} (R:R {REWARD_RISK}) | trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

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
        """Target/stop hits checked against live LTP (can happen intrabar, unlike bar-close checks)."""
        if not self.position:
            return
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
            self.manage_position_ltp()
            self.sleep(POLL_SEC)
        log(f"Done for the day. Trades {self.trades_today} | day P&L {self.day_pnl:+.0f}")


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
