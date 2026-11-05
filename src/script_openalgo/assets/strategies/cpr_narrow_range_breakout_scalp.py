"""
CPR Narrow-Range Breakout Scalp (ATM CE/PE option buying) for OpenAlgo
----------------------------------------------------------------------
Central Pivot Range (CPR) trend-day breakout. The CPR (Pivot, TC, BC)
is derived once from the PREVIOUS day's OHLC (completed daily bar) via
`strategy_lib.levels`. When the CPR is NARROW (its width is a small % of
price) the day is read as a likely trend day, and a 5-minute close
beyond the CPR is traded as a breakout. The signal is computed on the
underlying INDEX's own 5-minute candles (CPR lines, 20-EMA, session
VWAP, volume); the trade itself is an ATM option BUY (CE for an upside
break, PE for a downside break -- this project always BUYS options,
never shorts them, per nifty_direction_ce_pe.py convention; the spec's
"CE long / PE short" means buy CE on a bullish break, buy PE on a
bearish one). Stops and targets are tracked in INDEX points so they
match the signal that generated them, then converted to a lot-sized
option P&L at exit.

Regime gate (checked once the CPR is known):
  - NARROW CPR (width_pct < CPR_NARROW_PCT, default 0.30 % of the
    pivot) => trend day expected => breakouts are taken.
  - A wider CPR is read as a range day and NO breakout is taken.

Evaluated on every newly CLOSED 5m index candle, only after the first
SKIP_OPEN_MINUTES of the session:

Long (CE):  a 5m candle CLOSES above the Top Central line (TC) with
            price/20-EMA/VWAP all pointing up (close above the 20-EMA
            and above VWAP, 20-EMA rising) and, if REQUIRE_VOLUME_SUPPORT
            is on, volume >= VOLUME_MULT x the recent average.
Short (PE): a 5m candle CLOSES below the Bottom Central line (BC) with
            price/20-EMA/VWAP all pointing down (close below the 20-EMA
            and below VWAP, 20-EMA falling) and the same volume support.

Stop (index points, STOP_MODE):
  - "candle" (default): the breakout candle's low (long) / high (short).
  - "cpr": just below TC (long) / just above BC (short).

Target (index points, TARGET_MODE):
  - "rr" (default): entry +/- REWARD_RISK x the initial stop distance
    (1:2 by default).
  - "pivot": the next classic pivot -- R1 for a long, S1 for a short
    (falls back to the R:R target if that pivot is not beyond entry).

Exit:
  - Target hit or stop hit (checked intrabar on the index LTP each poll).
  - Opposite CPR break (a candle closes back below BC while long / above
    TC while short) closes the trade, checked on bar close.
  - Square-off at SQUARE_OFF_TIME regardless of P&L; no new entries
    after NO_NEW_ENTRY_AFTER.

Daily safety caps (MAX_TRADES_PER_DAY / DAILY_MAX_LOSS /
DAILY_PROFIT_TARGET) follow the same pattern as the other strategies
here; they are not part of the source rules.

Profit lock (user, 2026-11-02, "S3" i.e. 12:00-13:00): while an open
position's clock is in PROFIT_LOCK_WINDOW_START-END, once its rupee P&L
first reaches PROFIT_LOCK_AMOUNT (300) it is locked there - the trade
exits if P&L later falls back to that floor, on top of the normal
stop/target/opposite-break exits above. A single fixed floor, not
stepped/raised further as profit grows past 300.

Adaptations from the raw research spec (documented, not guesses):
  - The 20-EMA is seeded from the first 5m bar (a running EMA) rather
    than from a 20-bar SMA, so the directional filter is available soon
    after the open instead of only ~11:00 (a true 20-period SMA seed on
    5m candles needs 20 bars, i.e. ~100 minutes). Documented here as the
    intended behaviour.
  - "Momentum/volume support" is implemented as: 20-EMA slope agreeing
    with the break AND (optionally) breakout-candle volume above a
    multiple of the recent average index volume. If index volume is
    unavailable (all zero) the volume check is skipped (logged).
  - "Just below TC / above BC" stops and the "breakout candle low/high"
    stop are offered as the STOP_MODE toggle; the candle extreme is the
    default (tighter, nearer price).

Run first with OpenAlgo Analyzer (paper) mode ON. This script places
real orders once Analyzer is switched off -- do that only after a
satisfactory backtest and with the user's explicit go-ahead.
"""

import os
import time
from datetime import datetime, timedelta
from openalgo import api

from script_openalgo.strategy_lib import levels  # noqa: E402

# ======================= PARAMETERS =======================
API_KEY = os.getenv("OPENALGO_API_KEY", "PASTE_YOUR_API_KEY_HERE")
HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
STRATEGY = "CprNarrowRangeBreakoutScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"       # candles/indicators are built from the open
SKIP_OPEN_MINUTES = 15        # no entries in the first N minutes of the session (spec filter)
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

# ---- CPR (from the previous day's OHLC) ----
CPR_NARROW_PCT = 0.30         # CPR width below this % of the pivot => narrow => trend day => trade breakouts
CPR_LOOKBACK_DAYS = 20        # calendar days of daily history to fetch for the previous completed bar

# ---- directional filter (computed on the 5m index candles) ----
EMA_PERIOD = 20
REQUIRE_VOLUME_SUPPORT = True # breakout candle volume must exceed VOLUME_MULT x the recent average
VOLUME_LOOKBACK = 20          # bars used for the average volume baseline
VOLUME_MULT = 1.2             # breakout-candle volume must be >= this x the baseline

# ---- stop / target ----
STOP_MODE = "candle"          # "candle" (breakout candle low/high) or "cpr" (just below TC / above BC)
TARGET_MODE = "rr"            # "rr" (REWARD_RISK x stop distance) or "pivot" (next classic pivot R1/S1)
REWARD_RISK = 2.0             # R:R target multiple (1:2 default)
STOP_MAX_PCT = 0.0            # skip the trade if the stop distance exceeds this % of index price; 0 = off

# ---- profit lock (user, 2026-11-02: "S3", i.e. 12:00-13:00) ----
PROFIT_LOCK_WINDOW_START = "12:00"   # while an open position's current time is in this window, once its rupee
PROFIT_LOCK_WINDOW_END = "13:00"     # P&L first reaches PROFIT_LOCK_AMOUNT it is locked in: exit if it falls back
PROFIT_LOCK_AMOUNT = 300             # to (not below) this. A single fixed floor, not stepped/raised further.

POLL_SEC = 20                 # main loop tick while idle/monitoring

# ---- daily safety caps (not in the source rules; same pattern as other strategies here) ----
MAX_TRADES_PER_DAY = 4
DAILY_MAX_LOSS = 3000         # NIFTY-sized; resize per instrument (see knowledge/lessons.md)
DAILY_PROFIT_TARGET = 0       # 0 = off
# ==========================================================


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def hhmm(s):
    h, m = map(int, s.split(":"))
    return h, m


def ema_running(values, period):
    """EMA aligned 1:1 with values, seeded from the FIRST value (available from bar 0).
    See the docstring adaptation note: a true 20-bar SMA seed would leave the filter blind
    for ~100 minutes, so the running EMA is used instead."""
    n = len(values)
    out = [None] * n
    if n == 0:
        return out
    k = 2.0 / (period + 1)
    out[0] = values[0]
    for i in range(1, n):
        out[i] = values[i] * k + out[i - 1] * (1 - k)
    return out


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []           # completed 5m index candles: {t, o, h, l, c, v}
        self.ema = []            # 20-EMA aligned with self.bars
        self.vwap = []           # session VWAP aligned with self.bars
        self.last_bar_t = None   # timestamp cursor, not a list index: self.bars is rebuilt wholesale by refresh_bars()

        self.cpr = None          # previous-day CPR dict (levels.cpr_from_daily)
        self.cpr_loaded = False  # True once we have tried to load it (success or permanent failure)
        self.narrow = False      # True if the CPR is narrow (trade regime)

        self.position = None     # see attempt_entry() for shape
        self.option_sym = None
        self.lot = None

        self.trades_today = 0
        self.day_pnl = 0.0
        self._cap_logged = False

    # ---------- time helpers ----------
    def at(self, s):
        h, m = hhmm(s)
        return self.now().replace(hour=h, minute=m, second=0, microsecond=0)

    def entries_start(self):
        return self.at(SESSION_START) + timedelta(minutes=SKIP_OPEN_MINUTES)

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

    def ensure_cpr(self):
        """Load the previous-day CPR once (from completed daily bars) and set the narrow-regime flag."""
        if self.cpr_loaded:
            return
        today = self.now().date()
        try:
            d = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="D",
                               start_date=(today - timedelta(days=CPR_LOOKBACK_DAYS)).isoformat(),
                               end_date=today.isoformat(), source=HISTORY_SOURCE)
        except Exception as e:
            log(f"CPR daily history exception: {e}; CPR unavailable, no trades today")
            self.cpr_loaded = True
            return
        if not hasattr(d, "index") or len(d) == 0:
            log(f"CPR daily history empty ({d}); CPR unavailable, no trades today")
            self.cpr_loaded = True
            return
        # drop today's (possibly forming) daily bar for live use; use the last COMPLETED day
        d = d[[ix.date() < today for ix in d.index]]
        if len(d) == 0:
            log("CPR: no completed daily bar before today; no trades today")
            self.cpr_loaded = True
            return
        self.cpr = levels.cpr_from_daily(d)
        self.narrow = self.cpr["width_pct"] < CPR_NARROW_PCT
        self.cpr_loaded = True
        log(f"CPR BC {self.cpr['bc']:.1f} pivot {self.cpr['pivot']:.1f} TC {self.cpr['tc']:.1f} | "
            f"width {self.cpr['width_pct']:.3f}% (narrow < {CPR_NARROW_PCT}%) -> "
            f"{'NARROW: trend day, breakouts ON' if self.narrow else 'not narrow: range day, no breakout'} | "
            f"R1 {self.cpr['r1']:.1f} S1 {self.cpr['s1']:.1f}")

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
                         "l": float(row["low"]), "c": float(row["close"]),
                         "v": float(row.get("volume", 0) or 0)})
        bars.sort(key=lambda b: b["t"])
        self.bars = bars
        self._recompute_indicators()

    def _recompute_indicators(self):
        closes = [b["c"] for b in self.bars]
        self.ema = ema_running(closes, EMA_PERIOD)
        cum_pv = cum_v = 0.0
        vwap = []
        for b in self.bars:
            tp = (b["h"] + b["l"] + b["c"]) / 3.0
            cum_pv += tp * b["v"]
            cum_v += b["v"]
            vwap.append(cum_pv / cum_v if cum_v > 0 else tp)
        self.vwap = vwap

    def volume_ok(self, i):
        """Breakout-candle volume vs the recent average. True if support is there OR volume data is absent."""
        if not REQUIRE_VOLUME_SUPPORT:
            return True
        lo = max(0, i - VOLUME_LOOKBACK)
        base = [self.bars[k]["v"] for k in range(lo, i)]
        avg = sum(base) / len(base) if base else 0.0
        if avg <= 0:
            return True   # index volume unavailable; do not block on it (logged by caller)
        return self.bars[i]["v"] >= VOLUME_MULT * avg

    # ---------- signal ----------
    def detect_breakout(self, i):
        """Returns ('CE', stop_price) / ('PE', stop_price) if bar i is a valid CPR breakout, else None."""
        bar = self.bars[i]
        ema_i = self.ema[i]
        vwap_i = self.vwap[i]
        if ema_i is None or vwap_i is None:
            return None
        ema_prev = self.ema[i - 1] if i > 0 else None
        # upside break of the Top Central line
        if bar["c"] > self.cpr["tc"]:
            up = bar["c"] > ema_i and bar["c"] > vwap_i and (ema_prev is None or ema_i >= ema_prev)
            if not up:
                return None
            if not self.volume_ok(i):
                return None
            stop = self.cpr["tc"] if STOP_MODE == "cpr" else bar["l"]
            return "CE", stop
        # downside break of the Bottom Central line
        if bar["c"] < self.cpr["bc"]:
            down = bar["c"] < ema_i and bar["c"] < vwap_i and (ema_prev is None or ema_i <= ema_prev)
            if not down:
                return None
            if not self.volume_ok(i):
                return None
            stop = self.cpr["bc"] if STOP_MODE == "cpr" else bar["h"]
            return "PE", stop
        return None

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        for i, b in enumerate(self.bars):
            if self.last_bar_t is not None and b["t"] <= self.last_bar_t:
                continue
            self.handle_new_bar(i)
            self.last_bar_t = b["t"]

    def entry_window_open(self):
        return self.entries_start() <= self.now() < self.at(NO_NEW_ENTRY_AFTER)

    def handle_new_bar(self, i):
        if self.position:
            self.manage_on_bar_close(i)
            return
        self.ensure_cpr()
        if self.cpr is None or not self.narrow:
            return
        if not self.entry_window_open():
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS or \
                (DAILY_PROFIT_TARGET and self.day_pnl >= DAILY_PROFIT_TARGET):
            if not self._cap_logged:
                log("Daily trade/loss/profit cap reached; no further entries today")
                self._cap_logged = True
            return
        sig = self.detect_breakout(i)
        if sig:
            side, stop = sig
            self.attempt_entry(side, stop, i)

    def manage_on_bar_close(self, i):
        """Opposite CPR break closes the trade (close back inside/through the other CPR line)."""
        pos = self.position
        bar = self.bars[i]
        if pos["side"] == "CE" and bar["c"] < self.cpr["bc"]:
            self.exit_position(f"opposite CPR break: close {bar['c']:.2f} below BC {self.cpr['bc']:.2f}")
        elif pos["side"] == "PE" and bar["c"] > self.cpr["tc"]:
            self.exit_position(f"opposite CPR break: close {bar['c']:.2f} above TC {self.cpr['tc']:.2f}")

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
    def attempt_entry(self, side, stop_price, i):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return
        stop_distance = abs(idx_price - stop_price)
        if stop_distance <= 0:
            log("Entry aborted: zero stop distance (price at the stop level)")
            return
        stop_pct = stop_distance / idx_price * 100
        if STOP_MAX_PCT and stop_pct > STOP_MAX_PCT:
            log(f"Entry skipped: stop distance {stop_pct:.2f}% of index > cap {STOP_MAX_PCT}%")
            return
        target = self._target(side, idx_price, stop_distance)
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        price = self.buy(self.option_sym, qty,
                         f"{side} CPR break | index {idx_price:.2f} stop {stop_price:.2f} target {target:.2f}")
        if price is None:
            return
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price,
            "stop": stop_price, "target": target, "lock_floor": None,
        }
        self.trades_today += 1
        log(f"ENTER {side} | index {idx_price:.2f} stop {stop_price:.2f} ({stop_pct:.2f}%) "
            f"target {target:.2f} ({TARGET_MODE}) | trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

    def _target(self, side, idx_price, stop_distance):
        rr_target = idx_price + REWARD_RISK * stop_distance if side == "CE" \
            else idx_price - REWARD_RISK * stop_distance
        if TARGET_MODE != "pivot":
            return rr_target
        pivot_t = self.cpr["r1"] if side == "CE" else self.cpr["s1"]
        beyond = pivot_t > idx_price if side == "CE" else pivot_t < idx_price
        if beyond:
            return pivot_t
        log(f"pivot target {pivot_t:.2f} not beyond entry {idx_price:.2f}; falling back to R:R target")
        return rr_target

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

    def profit_lock_check(self, pos):
        """PROFIT_LOCK_WINDOW_START-END: once this position's rupee P&L first reaches PROFIT_LOCK_AMOUNT while the
        clock is in that window, lock a floor there (user, 2026-11-02, "S3"/after 12-1) - exits if P&L falls back
        to the floor. Returns True if it exited the position."""
        if not (self.at(PROFIT_LOCK_WINDOW_START) <= self.now() < self.at(PROFIT_LOCK_WINDOW_END)):
            return False
        opt_ltp = self.ltp(pos["symbol"], OPT_EXCHANGE)
        if opt_ltp is None:
            return False
        p = (opt_ltp - pos["entry_premium"]) * pos["qty"]
        if pos["lock_floor"] is None:
            if p >= PROFIT_LOCK_AMOUNT:
                pos["lock_floor"] = PROFIT_LOCK_AMOUNT
                log(f"PROFIT LOCK armed: {pos['side']} P&L {p:+.0f} >= {PROFIT_LOCK_AMOUNT} "
                    f"({PROFIT_LOCK_WINDOW_START}-{PROFIT_LOCK_WINDOW_END}); floor set to {PROFIT_LOCK_AMOUNT}")
            return False
        if p <= pos["lock_floor"]:
            self.exit_position(f"profit lock: {p:+.0f} <= floor {pos['lock_floor']:+.0f} "
                               f"({PROFIT_LOCK_WINDOW_START}-{PROFIT_LOCK_WINDOW_END})")
            return True
        return False

    def manage_position_ltp(self):
        """Stop / target checked against the live index LTP each poll; the S3 profit lock (above) is checked first."""
        if not self.position:
            return
        pos = self.position
        if self.profit_lock_check(pos):
            return
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
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
