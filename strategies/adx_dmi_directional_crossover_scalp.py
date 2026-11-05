"""
ADX/DMI Directional Crossover Scalp (ATM CE/PE option buying) for OpenAlgo
--------------------------------------------------------------------------
The signal is computed on the underlying INDEX's own 5-minute candles
(Wilder's Directional Movement system: +DI, -DI and ADX, default period
14); the trade itself is an ATM option BUY sized in lots (CE on a bullish
+DI/-DI cross, PE on a bearish cross -- this project always BUYS options,
never shorts them, per nifty_direction_ce_pe.py / cci_100_100_momentum_scalp.py
convention). Stops are tracked in INDEX points so they match the signal
that generated them, then converted to a lot-sized option P&L at exit.

Wilder DMI/ADX:
  +DM = up-move  (high-prevHigh) when it exceeds the down-move and > 0, else 0
  -DM = down-move(prevLow-low)  when it exceeds the up-move   and > 0, else 0
  +DI = 100 * Wilder-smoothed(+DM) / Wilder-smoothed(TR)
  -DI = 100 * Wilder-smoothed(-DM) / Wilder-smoothed(TR)
  DX  = 100 * |+DI - -DI| / (+DI + -DI);  ADX = Wilder-smoothed(DX)

Evaluated on every newly CLOSED 5m index candle:

Long (CE):  +DI crosses ABOVE -DI
            (plusDI[i-1] <= minusDI[i-1] and plusDI[i] > minusDI[i]),
            with ADX > ADX_THRESHOLD and (REQUIRE_ADX_RISING) ADX rising
            vs the prior bar -- confirms a real trend, not flat chop.
            Enter on that candle's close.
Short (PE): -DI crosses ABOVE +DI with the same ADX filter. Enter on the
            candle's close.

Exit (opposite_signal; also stop-and-reverse):
  - The OPPOSITE raw DI crossover (independent of the ADX filter) is both
    the exit of the current trade AND the entry signal for the other side
    (if that cross also passes the ADX filter it re-enters, otherwise the
    trade just closes).
  - ADX fading: exit when ADX drops below ADX_EXIT_THRESHOLD (trend gone).
  - Optional EXIT_ON_ADX_FALLING: exit the first bar ADX turns down vs the
    prior bar (off by default -- a single down-tick is noisy; the
    below-threshold exit is the robust form of "ADX turns down").
  - Stop in index points (STOP_MODE): "swing" = most recent swing
    low (CE) / swing high (PE) over SWING_LOOKBACK completed bars at the
    crossover ("just below the swing low formed before the crossover");
    "points" = a flat STOP_POINTS distance. Checked intrabar on the index LTP.
  - Optional trailing stop (USE_TRAILING_STOP): on each new bar ratchet the
    stop toward the recent TRAIL_LOOKBACK-bar swing low/high, tightening only.
  - No fixed profit target: ride the trend to the opposite crossover / ADX
    fade (per spec "no fixed target").
  - Square-off at SQUARE_OFF_TIME regardless of P&L; no new entries after
    NO_NEW_ENTRY_AFTER.

Optional confirmation candle (CONFIRM_BARS, default 0 = off = enter on the
crossover candle's close): on a qualifying cross the signal is only noted;
the entry waits for the next CONFIRM_BARS closed candles, each of which
must keep +DI/-DI on the signal's side with ADX still above threshold.
Otherwise the signal is dropped. A new opposite cross replaces a pending one.

Daily safety caps (MAX_TRADES_PER_DAY / DAILY_MAX_LOSS / DAILY_PROFIT_TARGET)
are the same pattern as the other strategies here; they are not part of the
source rules.

Adaptations from the raw research spec (documented, not guesses):
  - DMI_PERIOD 14 on 5m means ADX is not warm until ~2*period bars into the
    session (~11:35); entries simply do not fire before then. The spec lists
    a shorter intraday period (7-9) as an option -- set DMI_PERIOD = 7 for
    earlier, noisier signals.
  - "ADX above ~20-25 and rising" -> ADX_THRESHOLD (default 20) plus a
    REQUIRE_ADX_RISING toggle (default True), so the backtest/reviewer can
    A/B the rising filter.
  - "Exit when ADX turns down / drops below ~20" -> ADX_EXIT_THRESHOLD
    (below-threshold exit, robust) plus an optional EXIT_ON_ADX_FALLING
    (first down-tick, noisy, default off).
  - "Stop below the most recent swing low before the crossover" -> swing
    low/high over SWING_LOOKBACK completed bars (STOP_MODE "swing").
  - "Optional trailing stop" -> USE_TRAILING_STOP (default off) ratcheting
    toward the recent swing.

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
STRATEGY = "AdxDmiCrossoverScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"       # candles are built from the open
START_TIME = "09:15"          # ADX needs ~2*DMI_PERIOD bars to warm up before any entry fires
NO_NEW_ENTRY_AFTER = "15:00"
EXIT_BEFORE_CLOSE_MIN = 15    # close any position this many minutes before the 15:30 session close
SQUARE_OFF_TIME = "15:15"     # = 15:30 close - EXIT_BEFORE_CLOSE_MIN

# ---- DMI / ADX ----
DMI_PERIOD = 14               # spec default 14; use 7-9 for faster (noisier) intraday signals
ADX_THRESHOLD = 20            # only trade crossovers while ADX is above this (trend, not chop)
ADX_EXIT_THRESHOLD = 20       # exit a trade when ADX drops below this (trend fading)
REQUIRE_ADX_RISING = True     # also require ADX > prior bar's ADX at the crossover
EXIT_ON_ADX_FALLING = False   # exit the first bar ADX turns down vs the prior bar (noisy; off by default)

# ---- stop (index points) ----
STOP_MODE = "swing"           # "swing" (recent swing low/high over SWING_LOOKBACK bars) | "points" (flat STOP_POINTS)
SWING_LOOKBACK = 5            # completed bars used for the swing low (CE) / swing high (PE) before the crossover
STOP_POINTS = 30              # used only when STOP_MODE == "points" (NIFTY-sized; resize per instrument)

# ---- optional trailing stop (spec: optional; primary exit is the opposite crossover / ADX fade) ----
USE_TRAILING_STOP = False
TRAIL_LOOKBACK = 3            # on each new bar ratchet the stop toward this swing low/high (tighten only)

# ---- optional confirmation candle (default OFF = enter on the crossover candle's close) ----
CONFIRM_BARS = 0              # after a cross, wait N more closed candles that keep the DI order + ADX>threshold; 0 = off

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


def dmi_series(highs, lows, closes, period):
    """Wilder +DI, -DI and ADX aligned 1:1 with the bars; None until warm.

    +DI/-DI warm at index == period; ADX at index == 2*period - 1.
    """
    n = len(closes)
    plus_di = [None] * n
    minus_di = [None] * n
    adx = [None] * n
    if n < period + 1:
        return plus_di, minus_di, adx

    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))

    atr_s = [None] * n
    pdm_s = [None] * n
    mdm_s = [None] * n
    first = period                       # first Wilder-smoothed value (sum of i=1..period)
    atr_s[first] = sum(tr[1:period + 1])
    pdm_s[first] = sum(plus_dm[1:period + 1])
    mdm_s[first] = sum(minus_dm[1:period + 1])
    for i in range(first + 1, n):
        atr_s[i] = atr_s[i - 1] - atr_s[i - 1] / period + tr[i]
        pdm_s[i] = pdm_s[i - 1] - pdm_s[i - 1] / period + plus_dm[i]
        mdm_s[i] = mdm_s[i - 1] - mdm_s[i - 1] / period + minus_dm[i]

    dx = [None] * n
    for i in range(first, n):
        if atr_s[i]:
            pd_ = 100.0 * pdm_s[i] / atr_s[i]
            md_ = 100.0 * mdm_s[i] / atr_s[i]
        else:
            pd_ = md_ = 0.0
        plus_di[i] = pd_
        minus_di[i] = md_
        s = pd_ + md_
        dx[i] = 100.0 * abs(pd_ - md_) / s if s else 0.0

    adx_first = first + period - 1       # = 2*period - 1
    if adx_first < n:
        adx[adx_first] = sum(dx[first:first + period]) / period
        for i in range(adx_first + 1, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return plus_di, minus_di, adx


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []           # completed 5m index candles: {t, o, h, l, c}
        self.plus_di = []
        self.minus_di = []
        self.adx = []
        self.last_bar_t = None   # timestamp cursor, not a list index: self.bars is rebuilt wholesale by refresh_bars()

        self.pending = None      # cross waiting for confirmation: {side, i}
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
        self.plus_di, self.minus_di, self.adx = dmi_series(highs, lows, closes, DMI_PERIOD)

    # ---------- signal ----------
    def di_cross(self, i):
        """Raw +DI/-DI crossover (independent of the ADX filter): 'bull' (+DI over -DI), 'bear' (-DI over +DI), else None."""
        if i < 1:
            return None
        p0, m0 = self.plus_di[i], self.minus_di[i]
        p1, m1 = self.plus_di[i - 1], self.minus_di[i - 1]
        if None in (p0, m0, p1, m1):
            return None
        if p1 <= m1 and p0 > m0:
            return "bull"
        if m1 <= p1 and m0 > p0:
            return "bear"
        return None

    def adx_filter_ok(self, i):
        """ADX above threshold and (optionally) rising at bar i."""
        a = self.adx[i] if i < len(self.adx) else None
        if a is None or a <= ADX_THRESHOLD:
            return False
        if REQUIRE_ADX_RISING:
            ap = self.adx[i - 1] if i >= 1 else None
            if ap is None or a <= ap:
                return False
        return True

    def entry_signal(self, i, cross):
        """Map a raw cross to 'CE'/'PE' only if the ADX filter passes, else None."""
        if not cross or not self.adx_filter_ok(i):
            return None
        return "CE" if cross == "bull" else "PE"

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
        cross = self.di_cross(i)
        if self.position:
            self.manage_on_bar_close(i, cross)
        signal = self.entry_signal(i, cross)
        if CONFIRM_BARS > 0:
            self.confirm_step(i, cross, signal)
            return
        # An opposite crossover exits above; the same crossover (if it passes
        # the ADX filter) is the entry signal for the new side (stop-and-reverse).
        if not self.position and signal:
            self.try_enter(i, signal)

    def confirm_step(self, i, cross, signal):
        """CONFIRM_BARS > 0: a qualifying cross is noted; entry needs the next CONFIRM_BARS closed candles to keep the
        DI order on the signal's side with ADX still above threshold. A new opposite cross replaces the pending one."""
        if signal:                                     # a new qualifying cross (also opposite) replaces any pending one
            if self.pending and self.pending["side"] != signal:
                log(f"Pending {self.pending['side']} cross from {self.pending['t']:%H:%M} dropped: opposite cross")
            if not self.position:
                self.pending = {"side": signal, "t": self.bars[i]["t"], "bars": 0}
                log(f"{signal} +DI/-DI cross at {self.bars[i]['t']:%H:%M} "
                    f"(ADX {self.adx[i]:.1f}): waiting {CONFIRM_BARS} candle(s)")
            return
        p = self.pending
        if not p or self.position:
            return
        pd_, md_, a = self.plus_di[i], self.minus_di[i], self.adx[i]
        if None in (pd_, md_, a):
            return
        still = (pd_ > md_ if p["side"] == "CE" else md_ > pd_) and a > ADX_THRESHOLD
        if not still:
            log(f"{p['side']} cross from {p['t']:%H:%M} NOT confirmed at {self.bars[i]['t']:%H:%M}; no trade")
            self.pending = None
            return
        p["bars"] += 1
        if p["bars"] < CONFIRM_BARS:
            return                                     # confirmed so far, more candles to wait for
        log(f"{p['side']} cross from {p['t']:%H:%M} confirmed at {self.bars[i]['t']:%H:%M}")
        self.pending = None
        self.try_enter(i, p["side"])

    def manage_on_bar_close(self, i, cross):
        """Exit: opposite DI crossover / ADX fading. Optional trailing-stop ratchet."""
        pos = self.position
        opp = "bear" if pos["side"] == "CE" else "bull"
        if cross == opp:
            self.exit_position(f"opposite DI crossover at {self.bars[i]['t']:%H:%M}")
            return
        a = self.adx[i] if i < len(self.adx) else None
        ap = self.adx[i - 1] if i >= 1 else None
        if a is not None and a < ADX_EXIT_THRESHOLD:
            self.exit_position(f"ADX fell below {ADX_EXIT_THRESHOLD} ({a:.1f}) at {self.bars[i]['t']:%H:%M}")
            return
        if EXIT_ON_ADX_FALLING and a is not None and ap is not None and a < ap:
            self.exit_position(f"ADX turning down ({ap:.1f} -> {a:.1f}) at {self.bars[i]['t']:%H:%M}")
            return
        if USE_TRAILING_STOP:
            self.trail_stop(i)

    def trail_stop(self, i):
        new = self.swing_stop(i, self.position["side"], TRAIL_LOOKBACK)
        if new is None:
            return
        pos = self.position
        if pos["side"] == "CE":
            if new > pos["stop"]:
                pos["stop"] = new
                log(f"trail: stop raised to {new:.2f}")
        else:
            if new < pos["stop"]:
                pos["stop"] = new
                log(f"trail: stop lowered to {new:.2f}")

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
    def swing_stop(self, i, side, lookback):
        """Recent swing low (CE) / swing high (PE) over the last `lookback` completed bars up to bar i."""
        lo = max(0, i - lookback + 1)
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
            stop_price = self.swing_stop(i, side, SWING_LOOKBACK)
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
                          f"{side} +DI/-DI cross (ADX {self.adx[i]:.1f}) | index {idx_price:.2f} stop {stop_price:.2f}")
        if price is None:
            return
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price,
            "stop": stop_price,
        }
        self.trades_today += 1
        log(f"ENTER {side} | index {idx_price:.2f} stop {stop_price:.2f} "
            f"(+DI {self.plus_di[i]:.1f} / -DI {self.minus_di[i]:.1f} / ADX {self.adx[i]:.1f}) | "
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
        """Stop checked against live index LTP (can fire intrabar, before the next candle closes)."""
        if not self.position:
            return
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        pos = self.position
        if pos["side"] == "CE":
            if idx_ltp <= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} <= {pos['stop']:.2f})")
        else:
            if idx_ltp >= pos["stop"]:
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
