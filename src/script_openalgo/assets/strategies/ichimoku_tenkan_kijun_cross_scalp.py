"""
Ichimoku Tenkan/Kijun Cross Scalp (ATM CE/PE option buying) for OpenAlgo
------------------------------------------------------------------------
The signal is computed on the underlying INDEX's own 5-minute candles
using the Ichimoku Kinko Hyo lines; the trade itself is an ATM option
BUY sized in lots (CE on a bullish cross, PE on a bearish cross -- this
project always BUYS options, never shorts them, per the
nifty_direction_ce_pe.py / s_9_21_ema_crossover_scalp.py convention).
Stops/targets are tracked in INDEX points so they match the signal that
generated them, then converted to a lot-sized option P&L at exit.

Ichimoku components (classic 9 / 26 / 52, 26-bar displacement):
  - Tenkan-sen (Conversion) = (highest high + lowest low) / 2 over 9 bars
  - Kijun-sen  (Base)       = same over 26 bars
  - Senkou Span A (Leading A) = (Tenkan + Kijun) / 2, plotted 26 bars AHEAD
  - Senkou Span B (Leading B) = (HH + LL)/2 over 52 bars, plotted 26 ahead
  - The "Kumo" (cloud) at the current bar is Span A / Span B as computed
    26 bars ago (the forward displacement, read back to "now").
  - Chikou span (Lagging) = the current close read 26 bars back.

Long (CE) setup, on every newly CLOSED 5m index candle:
  1. Tenkan-sen crosses ABOVE the Kijun-sen (Tenkan was <= Kijun on the
     previous candle, > on this one).
  2. Price (this candle's close) is CLEANLY above the Kumo, i.e. above the
     cloud top by at least CLOUD_BUFFER_PCT of price (keeps us out of
     in-cloud chop).
  3. Optional (REQUIRE_CHIKOU_CONFIRM): the Chikou span is above price 26
     bars back (close[i] > close[i-26]).
  Entry: BUY ATM CE at this candle's close.
Short (PE) setup mirrors this below the cloud (Tenkan crosses below Kijun,
price cleanly below the cloud bottom, Chikou below price 26 bars back) ->
BUY ATM PE.

Stop (index points, STOP_REFERENCE):
  - "kijun" (default): just beyond the Kijun-sen (below it for a long,
    above it for a short) by STOP_BUFFER_PCT of price.
  - "kumo": just beyond the nearest Kumo edge (the cloud TOP for a long,
    the cloud BOTTOM for a short) by the same buffer.
  The trade is skipped if the stop distance exceeds STOP_MAX_PCT of the
  index price (safety cap, not in the source spec).
  TRAIL_STOP_KIJUN (default True): on every new completed candle the stop
  is ratcheted toward the Kijun-sen (never loosened) -- the "tight stop
  just beyond the Kijun" kept tight as the Kijun moves with the trend.

Profit lock (PROFIT_LOCK_TRIGGER_R, loss lock is the stop above): once the
index LTP has moved PROFIT_LOCK_TRIGGER_R x the initial stop distance in the
trade's favour, the stop is ratcheted (one-shot, never loosened) to lock in
at least PROFIT_LOCK_MIN_R x that distance of profit -- independent of (and
composing with) TRAIL_STOP_KIJUN, since the Kijun itself lags price and can
let a sharp move fully round-trip before its own trail catches up. 0 = off.

Target / exit (checked continuously via LTP and on every new 5m close):
  - OPPOSITE Tenkan/Kijun cross -> exit (the spec's trend-following
    target: ride until the lines cross back).
  - Stop hit (index LTP) -> exit.
  - Optional fixed target: if REWARD_RISK > 0, a target at REWARD_RISK x
    the initial stop distance is also armed (off by default; the spec's
    primary target is the opposite cross).
  - Square-off at SQUARE_OFF_TIME regardless of P&L.

Warm-up note (important): one 5m session is ~75 bars, but Senkou Span B
(52 bars) displaced 26 bars needs ~78 bars before the cloud exists. So
the strategy fetches several PRIOR trading days of 5m index candles once
(WARMUP_DAYS) and prepends them to today's candles, so the cloud and both
lines are ready from the open. Signals and trades only ever fire on
TODAY'S candles; the prior days are indicator warm-up only (no lookahead
-- all prior-day bars are completed, today's bars are only used once their
full 5m period has elapsed). In a backtest this needs the prior days to be
present in data_cache/; if they are missing the cloud simply warms up late
and fewer/no trades fire that day.

Adaptations from the raw research spec (documented, not guesses):
  - "take partial profits quickly at intraday support/resistance" is NOT
    implemented: the sources give no numeric definition of those S/R
    levels, so it is not deterministically testable. The position is held
    as a single lot block and exits on the opposite cross / stop /
    square-off. REWARD_RISK is a (default-off) fixed-target hook in its
    place.
  - "cleanly above/below the cloud" -> close beyond the cloud edge by
    CLOUD_BUFFER_PCT of price (a concrete, testable form of "cleanly").
  - MAX_TRADES_PER_DAY / DAILY_MAX_LOSS / STOP_MAX_PCT are safety caps,
    not sourced from the research material.

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
STRATEGY = "IchimokuTkCrossScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md
WARMUP_DAYS = 12              # calendar days of prior 5m candles fetched for Ichimoku warm-up (cloud needs ~78 bars)

SESSION_START = "09:15"       # candles are built from the open
START_TIME = "09:15"          # entries allowed from here (warm-up gates nothing if prior days are cached)
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

TENKAN = 9                    # Conversion Line period
KIJUN = 26                    # Base Line period
SENKOU_B = 52                 # Leading Span B period
DISPLACEMENT = 26             # forward displacement of the cloud (classic = Kijun period)

CLOUD_BUFFER_PCT = 0.0        # close must be beyond the cloud edge by this % of price ("cleanly" outside)
REQUIRE_CHIKOU_CONFIRM = False  # also require the Chikou span above/below price DISPLACEMENT bars back

STOP_REFERENCE = "kijun"      # "kijun" (just beyond the Base Line) or "kumo" (just beyond the nearest cloud edge)
STOP_BUFFER_PCT = 0.05        # stop placed this % of index price beyond the chosen reference
STOP_MAX_PCT = 0.6            # skip the trade if stop distance exceeds this % of index price
TRAIL_STOP_KIJUN = True       # ratchet the stop toward the Kijun-sen on each new candle (never loosen)
REWARD_RISK = 0.0             # optional fixed target = this x stop distance; 0 = off (opposite cross is the target)

PROFIT_LOCK_TRIGGER_R = 1.0   # once price has moved this many x the initial stop distance in favour, ratchet the
                               # stop to lock in profit (one-shot; composes with TRAIL_STOP_KIJUN -- whichever stop
                               # is more protective wins, neither ever loosens it); 0 = off (old behaviour)
PROFIT_LOCK_MIN_R = 0.3       # the locked stop guarantees at least this many x the initial stop distance of profit

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


def midpoint_series(highs, lows, period):
    """(highest high + lowest low) / 2 over a rolling `period`, aligned 1:1 with the bars; None until warm."""
    n = len(highs)
    out = [None] * n
    for i in range(n):
        if i < period - 1:
            continue
        hh = max(highs[i - period + 1:i + 1])
        ll = min(lows[i - period + 1:i + 1])
        out[i] = (hh + ll) / 2.0
    return out


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.warmup_bars = None      # prior trading days' 5m candles, fetched once
        self.bars = []               # warm-up + today's completed 5m index candles: {t, o, h, l, c}
        self.tenkan = []
        self.kijun = []
        self.span_a = []             # raw (un-displaced) Leading Span A
        self.span_b = []             # raw (un-displaced) Leading Span B
        self.closes = []
        self.last_bar_t = None   # timestamp cursor, not a list index: self.bars is rebuilt wholesale by refresh_bars()

        self.position = None         # see attempt_entry() for shape
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

    def _parse_bars(self, df, require_completed):
        """DataFrame -> list of completed bar dicts; `require_completed` drops a 5m bar still forming (today)."""
        if not hasattr(df, "empty") or df.empty:
            return []
        now = self.now()
        bars = []
        for ts, row in df.iterrows():
            t = ts.to_pydatetime()
            if t.tzinfo is not None:
                t = t.replace(tzinfo=None)   # SDK already converts intraday bars to IST wall time
            if require_completed and t + timedelta(minutes=BAR_MINUTES) > now:
                continue   # candle not fully completed yet; no lookahead
            bars.append({"t": t, "o": float(row["open"]), "h": float(row["high"]),
                         "l": float(row["low"]), "c": float(row["close"])})
        bars.sort(key=lambda b: b["t"])
        return bars

    def _fetch_warmup(self):
        """Prior trading days' 5m candles (fetched once). All such bars are completed, so no lookahead."""
        today = self.now().date()
        start = (today - timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
        end = (today - timedelta(days=1)).strftime("%Y-%m-%d")
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=TIMEFRAME,
                                 start_date=start, end_date=end, source=HISTORY_SOURCE)
        except Exception as e:
            log(f"warm-up history exception: {e}")
            return []
        bars = [b for b in self._parse_bars(df, require_completed=False) if b["t"].date() < today]
        log(f"Ichimoku warm-up: {len(bars)} prior 5m candles fetched "
            f"({'cloud will be ready from the open' if len(bars) >= SENKOU_B + DISPLACEMENT else 'short history -> cloud warms up late'})")
        return bars

    def _fetch_today(self):
        today = self.now().strftime("%Y-%m-%d")
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=TIMEFRAME,
                                 start_date=today, end_date=today, source=HISTORY_SOURCE)
        except Exception as e:
            log(f"history exception: {e}")
            return []
        d0 = self.now().date()
        return [b for b in self._parse_bars(df, require_completed=True) if b["t"].date() == d0]

    def refresh_bars(self):
        if self.warmup_bars is None:
            self.warmup_bars = self._fetch_warmup()
        today_bars = self._fetch_today()
        bars = self.warmup_bars + today_bars
        if not bars:
            return
        self.bars = bars
        highs = [b["h"] for b in bars]
        lows = [b["l"] for b in bars]
        self.closes = [b["c"] for b in bars]
        self.tenkan = midpoint_series(highs, lows, TENKAN)
        self.kijun = midpoint_series(highs, lows, KIJUN)
        self.span_b = midpoint_series(highs, lows, SENKOU_B)
        self.span_a = [None if (self.tenkan[i] is None or self.kijun[i] is None)
                       else (self.tenkan[i] + self.kijun[i]) / 2.0 for i in range(len(bars))]

    # ---------- Ichimoku helpers ----------
    def cloud(self, i):
        """(top, bottom) of the Kumo at bar i (Span A/B as computed DISPLACEMENT bars ago), or None."""
        j = i - DISPLACEMENT
        if j < 0:
            return None
        a, b = self.span_a[j], self.span_b[j]
        if a is None or b is None:
            return None
        return (max(a, b), min(a, b))

    def tk_cross(self, i):
        """'up' if Tenkan crossed above Kijun at bar i, 'down' if below, else None (raw, no cloud filter)."""
        if i < 1:
            return None
        tp, kp, tc, kc = self.tenkan[i - 1], self.kijun[i - 1], self.tenkan[i], self.kijun[i]
        if None in (tp, kp, tc, kc):
            return None
        if tp <= kp and tc > kc:
            return "up"
        if tp >= kp and tc < kc:
            return "down"
        return None

    def chikou_ok(self, side, i):
        j = i - DISPLACEMENT
        if j < 0:
            return False
        if side == "CE":
            return self.closes[i] > self.closes[j]
        return self.closes[i] < self.closes[j]

    def entry_signal(self, i):
        """'CE' / 'PE' if bar i is a TK cross with price cleanly outside the cloud (and Chikou if required)."""
        cross = self.tk_cross(i)
        if cross is None:
            return None
        cl = self.cloud(i)
        if cl is None:
            return None
        top, bottom = cl
        close = self.closes[i]
        buf = (CLOUD_BUFFER_PCT / 100.0) * close
        if cross == "up":
            if close <= top + buf:
                return None
            if REQUIRE_CHIKOU_CONFIRM and not self.chikou_ok("CE", i):
                return None
            return "CE"
        else:
            if close >= bottom - buf:
                return None
            if REQUIRE_CHIKOU_CONFIRM and not self.chikou_ok("PE", i):
                return None
            return "PE"

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        for i, b in enumerate(self.bars):
            if self.last_bar_t is not None and b["t"] <= self.last_bar_t:
                continue
            self.handle_new_bar(i)
            self.last_bar_t = b["t"]

    def in_time_window(self):
        return self.at(START_TIME) <= self.now() < self.at(NO_NEW_ENTRY_AFTER)

    def handle_new_bar(self, i):
        if self.bars[i]["t"].date() != self.now().date():
            return   # warm-up (prior-day) bar: indicator input only, never traded
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
        side = self.entry_signal(i)
        if side:
            self.attempt_entry(side, i)

    def manage_on_bar_close(self, i):
        """Opposite TK cross -> exit; otherwise ratchet the stop toward the Kijun-sen."""
        pos = self.position
        cross = self.tk_cross(i)
        if pos["side"] == "CE" and cross == "down":
            self.exit_position("opposite TK cross (Tenkan crossed below Kijun)")
            return
        if pos["side"] == "PE" and cross == "up":
            self.exit_position("opposite TK cross (Tenkan crossed above Kijun)")
            return
        if not TRAIL_STOP_KIJUN:
            return
        k = self.kijun[i]
        if k is None:
            return
        buf = (STOP_BUFFER_PCT / 100.0) * self.closes[i]
        if pos["side"] == "CE":
            new_stop = k - buf
            if new_stop > pos["stop"]:
                log(f"Trailing stop (Kijun) raised {pos['stop']:.2f} -> {new_stop:.2f}")
                pos["stop"] = new_stop
        else:
            new_stop = k + buf
            if new_stop < pos["stop"]:
                log(f"Trailing stop (Kijun) lowered {pos['stop']:.2f} -> {new_stop:.2f}")
                pos["stop"] = new_stop

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
    def attempt_entry(self, side, i):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return
        buf = (STOP_BUFFER_PCT / 100.0) * idx_price
        if STOP_REFERENCE == "kumo":
            cl = self.cloud(i)
            if cl is None:
                log("Entry aborted: no cloud for kumo stop")
                return
            top, bottom = cl
            stop_price = (top - buf) if side == "CE" else (bottom + buf)
        else:  # kijun
            k = self.kijun[i]
            if k is None:
                log("Entry aborted: no Kijun for stop")
                return
            stop_price = (k - buf) if side == "CE" else (k + buf)
        stop_distance = abs(idx_price - stop_price)
        if stop_distance <= 0 or (side == "CE" and stop_price >= idx_price) or (side == "PE" and stop_price <= idx_price):
            log(f"Entry aborted: stop {stop_price:.2f} not protective vs index {idx_price:.2f}")
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
                          f"{side} Ichimoku TK cross | index {idx_price:.2f} stop {stop_price:.2f} ({STOP_REFERENCE})")
        if price is None:
            return
        target = None
        if REWARD_RISK > 0:
            target = idx_price + REWARD_RISK * stop_distance if side == "CE" \
                else idx_price - REWARD_RISK * stop_distance
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price,
            "stop": stop_price, "target": target,
            "stop_distance": stop_distance, "lock_done": False,
        }
        self.trades_today += 1
        log(f"ENTER {side} | index {idx_price:.2f} stop {stop_price:.2f} ({stop_pct:.2f}%) "
            f"target {('%.2f' % target) if target else 'opposite cross'} | "
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

    def profit_lock(self, idx_ltp):
        """One-shot stop ratchet: once price has moved PROFIT_LOCK_TRIGGER_R x the initial stop distance in the
        trade's favour, move the stop to lock in at least PROFIT_LOCK_MIN_R x that distance of profit. Composes
        with TRAIL_STOP_KIJUN (both only ever move the stop in the protective direction). No-op once armed, or if
        PROFIT_LOCK_TRIGGER_R is off."""
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

    def manage_position_ltp(self):
        """Stop (and optional fixed target) checked against live index LTP (can happen intrabar)."""
        if not self.position:
            return
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        pos = self.position
        self.profit_lock(idx_ltp)
        stop_why = "profit-lock stop hit" if pos["lock_done"] else "stop hit"
        if pos["side"] == "CE":
            if idx_ltp <= pos["stop"]:
                self.exit_position(f"{stop_why} (index {idx_ltp:.2f} <= {pos['stop']:.2f})")
            elif pos["target"] and idx_ltp >= pos["target"]:
                self.exit_position(f"target hit (index {idx_ltp:.2f} >= {pos['target']:.2f})")
        else:
            if idx_ltp >= pos["stop"]:
                self.exit_position(f"{stop_why} (index {idx_ltp:.2f} >= {pos['stop']:.2f})")
            elif pos["target"] and idx_ltp <= pos["target"]:
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
