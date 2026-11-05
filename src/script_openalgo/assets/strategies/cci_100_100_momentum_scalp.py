"""
CCI +100/-100 Momentum Scalp (ATM CE/PE option buying) for OpenAlgo
-------------------------------------------------------------------
The signal is computed on the underlying INDEX's own 5-minute candles
(Commodity Channel Index, default period 20 on the typical price
TP = (high + low + close) / 3); the trade itself is an ATM option BUY
sized in lots (CE on a bullish breakout, PE on a bearish breakout --
this project always BUYS options, never shorts them, per
nifty_direction_ce_pe.py / parabolic_sar_flip_scalp.py convention).
Stops/targets are tracked in INDEX points so they match the signal that
generated them, then converted to a lot-sized option P&L at exit.

This is the MOMENTUM (breakout) variant, NOT the mean-reversion fade:
crossing the +/-100 line signals emerging strength/weakness that we ride.

Evaluated on every newly CLOSED 5m index candle:

Long (CE):  CCI crosses ABOVE the +UPPER line from below
            (cci[i-1] <= UPPER and cci[i] > UPPER). Enter on that
            candle's close.
Short (PE): CCI crosses BELOW the -LOWER line from above
            (cci[i-1] >= LOWER and cci[i] < LOWER). Enter on that
            candle's close.

Exit (opposite_signal / fade back through the line):
  - Long  exits when CCI falls back below +UPPER (which also covers an
    opposite PE breakout, since that needs CCI < LOWER < UPPER).
  - Short exits when CCI rises back above -LOWER.
  - An opposite breakout is both the exit of the current trade and the
    entry signal for the other side (stop-and-reverse on the same bar,
    subject to the time window / daily caps).
  - Fixed stop in index points (STOP_MODE): "swing" = recent swing
    low/high over SWING_LOOKBACK completed bars at entry; "points" = a
    flat STOP_POINTS distance. Checked intrabar on the index LTP.
  - Optional fixed reward:risk target (USE_FIXED_TARGET, default OFF per
    the spec's "optional fixed R:R"): entry +/- FIXED_TARGET_R x the
    initial stop distance.
  - Square-off at SQUARE_OFF_TIME regardless of P&L; no new entries
    after NO_NEW_ENTRY_AFTER.

Optional confirmation candle (CONFIRM_BARS, default 0 = off = enter on
the crossover candle's close): on a crossover the signal is only noted;
the entry waits for the next CONFIRM_BARS closed candles, each of which
must keep CCI beyond the line on the signal's side. Otherwise the signal
is dropped. A new opposite crossover replaces a pending one.

Daily safety caps (MAX_TRADES_PER_DAY / DAILY_MAX_LOSS /
DAILY_PROFIT_TARGET) are the same pattern as the other strategies here;
they are not part of the source rules.

Adaptations from the raw research spec (documented, not guesses):
  - CCI period 20 on 5m means CCI is not warm until ~20 bars into the
    session (~10:55). Use CCI_PERIOD = 14 for faster intraday signals
    (the spec lists this). Entries simply do not fire before CCI warms.
  - "Fixed stop OR recent swing low/high" -> STOP_MODE toggle; swing is
    the default (adapts to volatility). SWING_LOOKBACK is the number of
    completed bars used for the swing extreme.
  - "Target = opposite CCI extreme / zero-line fade; optional fixed R:R"
    -> the CCI fade-back exit above IS the primary target (ride momentum
    until CCI loses the +/-100 line); the fixed R:R is the optional
    USE_FIXED_TARGET toggle.

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
STRATEGY = "Cci100MomentumScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"       # candles are built from the open
START_TIME = "09:15"          # CCI needs CCI_PERIOD bars to warm up before any entry fires
NO_NEW_ENTRY_AFTER = "15:00"
EXIT_BEFORE_CLOSE_MIN = 15    # close any position this many minutes before the 15:30 session close
SQUARE_OFF_TIME = "15:15"     # = 15:30 close - EXIT_BEFORE_CLOSE_MIN

# ---- CCI ----
CCI_PERIOD = 20               # spec default 20; use 14 for faster intraday signals
UPPER = 100                   # bullish breakout line
LOWER = -100                  # bearish breakout line
WARMUP_BARS = 1               # need at least one prior CCI value to detect a cross

# ---- stop (index points) ----
STOP_MODE = "swing"           # "swing" (recent swing low/high over SWING_LOOKBACK bars) | "points" (flat STOP_POINTS)
SWING_LOOKBACK = 5            # completed bars used for the swing low (CE) / swing high (PE)
STOP_POINTS = 30              # used only when STOP_MODE == "points" (NIFTY-sized; resize per instrument)

# ---- optional fixed reward:risk target (spec: optional; primary exit is the CCI fade-back) ----
USE_FIXED_TARGET = False
FIXED_TARGET_R = 1.5          # target = entry +/- this x the initial stop distance

# ---- optional confirmation candle (default OFF = enter on the crossover candle's close) ----
CONFIRM_BARS = 0              # after a crossover, wait N more closed 5m candles that keep CCI beyond the line; 0 = off

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


def cci_series(highs, lows, closes, period):
    """Commodity Channel Index aligned 1:1 with the bars; None until warm (period-1).

    TP = (high + low + close) / 3; CCI = (TP - SMA(TP)) / (0.015 * mean deviation).
    """
    n = len(closes)
    out = [None] * n
    if n < period:
        return out
    tp = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(n)]
    for i in range(period - 1, n):
        window = tp[i - period + 1:i + 1]
        sma = sum(window) / period
        md = sum(abs(x - sma) for x in window) / period
        out[i] = 0.0 if md == 0 else (tp[i] - sma) / (0.015 * md)
    return out


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []           # completed 5m index candles: {t, o, h, l, c}
        self.cci = []
        self.last_bar_t = None   # timestamp of the last bar handled (NOT a list index: refresh_bars() rebuilds
                                  # self.bars wholesale and re-sorts it every call, so an index-based cursor can
                                  # desync and replay old bars if a transient fetch returns an out-of-order set)

        self.pending = None      # crossover waiting for confirmation: {side, t, cci, bars}
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
        self.cci = cci_series(highs, lows, closes, CCI_PERIOD)

    # ---------- signal ----------
    def detect_signal(self, i):
        """Returns 'CE' (CCI crosses above +UPPER from below) / 'PE' (crosses below -LOWER from above), else None."""
        if i < max(1, WARMUP_BARS):
            return None
        c, p = self.cci[i], self.cci[i - 1]
        if c is None or p is None:
            return None
        if p <= UPPER and c > UPPER:
            return "CE"
        if p >= LOWER and c < LOWER:
            return "PE"
        return None

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        """Handle every bar strictly newer than the last one handled, in order -- by timestamp, not list
        position, since self.bars is rebuilt wholesale (and re-sorted) on every refresh_bars() call."""
        for i, b in enumerate(self.bars):
            if self.last_bar_t is not None and b["t"] <= self.last_bar_t:
                continue
            self.handle_new_bar(i)
            self.last_bar_t = b["t"]

    def in_time_window(self):
        return self.at(START_TIME) <= self.now() < self.at(NO_NEW_ENTRY_AFTER)

    def handle_new_bar(self, i):
        signal = self.detect_signal(i)
        if self.position:
            self.manage_on_bar_close(i, signal)
        if CONFIRM_BARS > 0:
            self.confirm_step(i, signal)
            return
        # An opposite crossover exits above; the same crossover is the entry
        # signal for the new side (stop-and-reverse), so re-evaluate entry now.
        if not self.position and signal:
            self.try_enter(i, signal)

    def confirm_step(self, i, signal):
        """CONFIRM_BARS > 0: a crossover is only noted; entry needs the next CONFIRM_BARS closed candles to keep CCI
        beyond the line on the signal's side. A new opposite crossover replaces the pending one."""
        c = self.cci[i]
        if signal:                                     # a new crossover (also an opposite one) replaces any pending signal
            if self.pending and self.pending["side"] != signal:
                log(f"Pending {self.pending['side']} crossover from {self.pending['t']:%H:%M} dropped: opposite crossover")
            if not self.position:
                self.pending = {"side": signal, "t": self.bars[i]["t"], "cci": c, "bars": 0}
                log(f"{signal} CCI crossover at {self.bars[i]['t']:%H:%M} (CCI {c:.1f}): waiting {CONFIRM_BARS} candle(s)")
            return
        p = self.pending
        if not p or self.position or c is None:
            return
        still = c > UPPER if p["side"] == "CE" else c < LOWER
        if not still:
            log(f"{p['side']} crossover from {p['t']:%H:%M} NOT confirmed at {self.bars[i]['t']:%H:%M} (CCI {c:.1f}); no trade")
            self.pending = None
            return
        p["bars"] += 1                                 # one more genuinely-new closed bar has kept CCI beyond the line
        if p["bars"] < CONFIRM_BARS:
            return                                     # confirmed so far, more candles to wait for
        log(f"{p['side']} crossover from {p['t']:%H:%M} confirmed at {self.bars[i]['t']:%H:%M} (CCI {c:.1f})")
        self.pending = None
        self.try_enter(i, p["side"])

    def manage_on_bar_close(self, i, signal):
        """Primary exit: CCI fades back through the +/-100 line (also covers an opposite breakout)."""
        pos = self.position
        c = self.cci[i]
        if c is None:
            return
        if pos["side"] == "CE" and c < UPPER:
            self.exit_position(f"CCI fell back below +{UPPER} ({c:.1f}) at {self.bars[i]['t']:%H:%M}")
        elif pos["side"] == "PE" and c > LOWER:
            self.exit_position(f"CCI rose back above {LOWER} ({c:.1f}) at {self.bars[i]['t']:%H:%M}")

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
        """Recent swing low (CE) / swing high (PE) over the last SWING_LOOKBACK completed bars up to bar i."""
        lo = max(0, i - SWING_LOOKBACK + 1)
        seg = self.bars[lo:i + 1]
        if not seg:
            return None
        return min(b["l"] for b in seg) if side == "CE" else max(b["h"] for b in seg)

    def try_enter(self, i, side):
        if not self.in_time_window():
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS or \
                (DAILY_PROFIT_TARGET and self.day_pnl >= DAILY_PROFIT_TARGET):
            if not self._cap_logged:
                log("Daily trade/loss/profit cap reached; no further entries today")
                self._cap_logged = True
            return
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return
        if STOP_MODE == "swing":
            stop_price = self.swing_stop(i, side)
            if stop_price is None:
                log("Entry aborted: no swing stop available")
                return
            # a swing extreme on the wrong side of price (rare) is unusable; fall back to flat points
            if (side == "CE" and stop_price >= idx_price) or (side == "PE" and stop_price <= idx_price):
                stop_price = idx_price - STOP_POINTS if side == "CE" else idx_price + STOP_POINTS
        else:
            stop_price = idx_price - STOP_POINTS if side == "CE" else idx_price + STOP_POINTS
        stop_distance = abs(idx_price - stop_price)
        if stop_distance <= 0:
            log("Entry aborted: zero stop distance")
            return
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        price = self.buy(self.option_sym, qty,
                          f"{side} CCI breakout ({self.cci[i]:.1f}) | index {idx_price:.2f} stop {stop_price:.2f}")
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
        tgt_txt = f"target {target:.2f} ({FIXED_TARGET_R}R)" if target is not None else "target none (CCI fade-back exit)"
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
        """Fixed stop (and optional fixed target) checked against live index LTP."""
        if not self.position:
            return
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        pos = self.position
        if pos["side"] == "CE":
            if idx_ltp <= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} <= {pos['stop']:.2f})")
            elif pos["target"] is not None and idx_ltp >= pos["target"]:
                self.exit_position(f"target hit (index {idx_ltp:.2f} >= {pos['target']:.2f})")
        else:
            if idx_ltp >= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} >= {pos['stop']:.2f})")
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
