"""
Keltner Channel Breakout Scalp (ATM CE/PE option buying) for OpenAlgo
---------------------------------------------------------------------
Signal is computed on the underlying INDEX's own 1-minute candles (a Keltner
channel: middle = EMA(EMA_PERIOD) of close, upper/lower = middle +/- KC_MULT x
ATR(ATR_PERIOD), Wilder ATR). The trade itself is always an ATM option BUY
sized in lots (CE for an upside breakout, PE for a downside breakout -- this
project always buys options, never shorts them, same convention as
nifty_direction_ce_pe.py / bollinger_band_squeeze_breakout_scalp.py). Stops and
targets are tracked in INDEX points so they match the signal that generated
them, then converted to a lot-sized option P&L at exit.

Setup, evaluated on every newly CLOSED 1m index candle:
  1. Breakout trigger:
       - close > upper Keltner band  -> LONG  (BUY ATM CE)
       - close < lower Keltner band  -> SHORT (BUY ATM PE)
     A wick through the band with the close still inside does NOT count
     (close-based breakout only).
  2. Optional "strong close" filter (MIN_PENETRATION_ATR, default 0 = off):
     the close must clear the band by at least this many ATRs, to avoid
     marginal false breaks. See Adaptations.
  3. Optional volume confirmation (REQUIRE_VOLUME_CONFIRMATION, off by default
     -- see Adaptations): the breakout candle's volume must be >=
     VOLUME_CONFIRMATION_MULT x the average volume of the previous
     VOLUME_LOOKBACK_BARS candles.

Stop (set once, at entry): the MIDDLE band (EMA) value at the entry bar. Risk
in index points = |entry index price - middle band|.

Target: fixed reward-to-risk -> entry +/- RR x risk (RR default 2, i.e. 1:2).

In-trade management, re-checked on every newly CLOSED candle:
  - Trail (TRAIL_STOP_TO_MIDDLE, on by default): ratchet the stop toward the
    middle band (EMA) as it moves in the trade's favour -- never loosen it.
  - Invalidation (EXIT_ON_CLOSE): a candle that closes back on the wrong side
    of the middle band ("middle") or back inside the channel beyond the broken
    band ("band") exits the whole position. Default "middle".
  - Hard stop and fixed target are checked against live index LTP between bars
    (can fire intrabar).
  - Square-off at SQUARE_OFF_TIME regardless of P&L.

Entries stop after NO_NEW_ENTRY_AFTER or once MAX_TRADES_PER_DAY /
DAILY_MAX_LOSS is reached (safety caps, same pattern as the other strategies in
this project; not part of the source rules). Only one position open at a time.

Adaptations from the raw research spec (documented, not guesses):
  - EMA uses the SMA-seeded recursive EMA (ema_series); ATR is Wilder's ATR
    (atr_series), the same helpers as s_9_21_ema_crossover_scalp.py /
    supertrend_flip_scalp.py. The source does not pin a specific EMA/ATR
    formula.
  - "Place the stop at the middle band (EMA) at entry, roughly 1 ATR of risk":
    with KC_MULT 2.0 the breakout close sits ~2 ATR above the middle, so the
    actual EMA-to-entry distance is nearer 2 ATR than 1; the rule as written
    (stop AT the middle band) is implemented literally and the risk is measured
    from it, rather than forcing a 1-ATR stop that contradicts the stated
    level.
  - "Require a strong closing candle beyond the band to avoid false breaks" has
    no numeric definition; MIN_PENETRATION_ATR (close must clear the band by
    this many ATRs) is a testable proxy, default 0 (off) so the base
    close-beyond-band rule trades, with the knob available for A/B testing.
  - "Ideally with rising volume confirming the breakout" is implemented
    (REQUIRE_VOLUME_CONFIRMATION) but OFF by default: the NIFTY/BANKNIFTY spot
    index has no meaningful traded volume of its own (see
    knowledge/openalgo_api.md), so the index `volume` field is not a reliable
    confirmation. Flip it on only with a real volume proxy (e.g. futures).
  - "Trail or exit on a close back inside the channel / below the middle EMA"
    is split into a trailing stop toward the middle band AND a close-based
    invalidation (both implemented, each behind a constant), consistent with
    the EMA-as-trail + EMA-as-invalidation pattern in
    s_9_21_ema_crossover_scalp.py.
  - MAX_TRADES_PER_DAY / DAILY_MAX_LOSS / DAILY_PROFIT_TARGET / STOP_MAX_PCT are
    safety caps, not sourced from the research material.

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
STRATEGY = "KeltnerChannelBreakoutScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1                      # lots per entry
PRODUCT = "MIS"

TIMEFRAME = "1m"
BAR_MINUTES = 1
HISTORY_SOURCE = "api"         # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"        # candles are built from the open
START_TIME = "09:15"           # EMA/ATR warm-up naturally gates early entries
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

EMA_PERIOD = 20                # Keltner middle band (EMA of close)
ATR_PERIOD = 10                # Wilder ATR period
KC_MULT = 2.0                  # band distance = this x ATR
RR = 2.0                       # reward-to-risk target multiple (1:2; try 3 for 1:3)

MIN_PENETRATION_ATR = 0.0      # ADAPTATION: close must clear the band by this many ATRs (0 = off)

REQUIRE_VOLUME_CONFIRMATION = False   # off by default; see Adaptations (index volume is not meaningful)
VOLUME_CONFIRMATION_MULT = 1.5
VOLUME_LOOKBACK_BARS = 10

TRAIL_STOP_TO_MIDDLE = True    # ratchet the stop toward the middle band each completed candle
EXIT_ON_CLOSE = "middle"       # "middle" = exit on close back past the middle EMA; "band" = close back inside the channel; "off"

STOP_MAX_PCT = 0.6             # skip the trade if stop distance exceeds this % of index price (safety cap)

POLL_SEC = 15                  # main loop tick while idle/monitoring

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


def true_range(bars):
    tr = [None] * len(bars)
    for i, b in enumerate(bars):
        if i == 0:
            tr[i] = b["h"] - b["l"]
        else:
            pc = bars[i - 1]["c"]
            tr[i] = max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc))
    return tr


def atr_series(bars, period):
    """Wilder's ATR aligned 1:1 with bars; None until the SMA-seeded index (period-1)."""
    n = len(bars)
    atr = [None] * n
    if n < period:
        return atr
    tr = true_range(bars)
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []             # completed 1m index candles: {t, o, h, l, c, v}
        self.middle = []           # Keltner middle band (EMA), aligned with bars
        self.upper = []
        self.lower = []
        self.last_bar_t = None   # timestamp cursor, not a list index: self.bars is rebuilt wholesale by refresh_bars()

        self.position = None       # see attempt_entry() for shape
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

    def in_time_window(self):
        return self.at(START_TIME) <= self.now() < self.at(NO_NEW_ENTRY_AFTER)

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
                         "l": float(row["low"]), "c": float(row["close"]),
                         "v": float(row.get("volume", 0) or 0)})
        bars.sort(key=lambda b: b["t"])
        if not bars:
            return
        self.bars = bars
        closes = [b["c"] for b in bars]
        self.middle = ema_series(closes, EMA_PERIOD)
        atr = atr_series(bars, ATR_PERIOD)
        self.upper = [(m + KC_MULT * a) if (m is not None and a is not None) else None
                      for m, a in zip(self.middle, atr)]
        self.lower = [(m - KC_MULT * a) if (m is not None and a is not None) else None
                      for m, a in zip(self.middle, atr)]
        self._atr = atr

    # ---------- breakout detection ----------
    def detect_breakout(self, i):
        """Returns 'CE'/'PE' if bar i is a valid Keltner breakout close, else None."""
        upper_i, lower_i, atr_i = self.upper[i], self.lower[i], self._atr[i]
        if upper_i is None or lower_i is None or atr_i is None:
            return None
        bar = self.bars[i]
        pen = MIN_PENETRATION_ATR * atr_i
        if bar["c"] > upper_i + pen:
            side = "CE"
        elif bar["c"] < lower_i - pen:
            side = "PE"
        else:
            return None
        if REQUIRE_VOLUME_CONFIRMATION:
            lo = max(0, i - VOLUME_LOOKBACK_BARS)
            prev = self.bars[lo:i]
            avg_vol = (sum(b["v"] for b in prev) / len(prev)) if prev else 0
            if avg_vol <= 0 or bar["v"] < VOLUME_CONFIRMATION_MULT * avg_vol:
                log(f"{side} breakout at bar {i} skipped: volume {bar['v']:.0f} < "
                    f"{VOLUME_CONFIRMATION_MULT}x avg {avg_vol:.0f}")
                return None
        return side

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        for i, b in enumerate(self.bars):
            if self.last_bar_t is not None and b["t"] <= self.last_bar_t:
                continue
            self.handle_new_bar(i)
            self.last_bar_t = b["t"]

    def handle_new_bar(self, i):
        if self.position:
            self.manage_on_bar_close(i)
            return
        if not self.in_time_window():
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS \
                or (DAILY_PROFIT_TARGET and self.day_pnl >= DAILY_PROFIT_TARGET):
            if not self._cap_logged:
                log("Daily trade/loss/profit cap reached; no further entries today")
                self._cap_logged = True
            return
        side = self.detect_breakout(i)
        if side:
            self.attempt_entry(side, i)

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
        middle_i = self.middle[i]
        if middle_i is None:
            return
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return

        stop_price = middle_i
        if side == "CE":
            risk = idx_price - stop_price
        else:
            risk = stop_price - idx_price
        if risk <= 0:
            log(f"Entry skipped: non-positive risk (index {idx_price:.2f} vs middle {stop_price:.2f})")
            return

        stop_pct = risk / idx_price * 100
        if stop_pct > STOP_MAX_PCT:
            log(f"Entry skipped: stop distance {stop_pct:.2f}% of index > cap {STOP_MAX_PCT}%")
            return

        if side == "CE":
            target = idx_price + RR * risk
        else:
            target = idx_price - RR * risk

        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        price = self.buy(self.option_sym, qty,
                          f"{side} Keltner breakout (close {self.bars[i]['c']:.2f}) | "
                          f"index {idx_price:.2f} stop/middle {stop_price:.2f} risk {risk:.2f} "
                          f"target {target:.2f} (RR {RR})")
        if price is None:
            return
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price,
            "stop": stop_price, "target": target,
        }
        self.trades_today += 1
        log(f"ENTER {side} | index {idx_price:.2f} stop {stop_price:.2f} ({stop_pct:.2f}%) "
            f"target {target:.2f} | trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

    def exit_position(self, reason):
        pos = self.position
        qty = pos["qty"]
        px = self.sell(pos["symbol"], qty, reason)
        if px is None:
            log("Exit failed; position may still be open - check manually")
            return
        p = (px - pos["entry_premium"]) * qty
        self.day_pnl += p
        log(f"EXIT {pos['side']} {reason} | qty {qty} @ {px} | pnl {p:+.0f} | day P&L {self.day_pnl:+.0f}")
        self.position = None

    def manage_on_bar_close(self, i):
        """Trailing stop toward the middle band + close-based invalidation (completed candle)."""
        if not self.position:
            return
        pos = self.position
        middle_i = self.middle[i]
        upper_i, lower_i = self.upper[i], self.lower[i]
        bar = self.bars[i]

        # trail the stop toward the middle band (never loosen)
        if TRAIL_STOP_TO_MIDDLE and middle_i is not None:
            if pos["side"] == "CE":
                pos["stop"] = max(pos["stop"], middle_i)
            else:
                pos["stop"] = min(pos["stop"], middle_i)

        # close-based invalidation
        if EXIT_ON_CLOSE == "middle" and middle_i is not None:
            if pos["side"] == "CE" and bar["c"] < middle_i:
                self.exit_position(f"close {bar['c']:.2f} back below middle {middle_i:.2f}")
            elif pos["side"] == "PE" and bar["c"] > middle_i:
                self.exit_position(f"close {bar['c']:.2f} back above middle {middle_i:.2f}")
        elif EXIT_ON_CLOSE == "band" and upper_i is not None and lower_i is not None:
            if pos["side"] == "CE" and bar["c"] < upper_i:
                self.exit_position(f"close {bar['c']:.2f} back inside channel (< upper {upper_i:.2f})")
            elif pos["side"] == "PE" and bar["c"] > lower_i:
                self.exit_position(f"close {bar['c']:.2f} back inside channel (> lower {lower_i:.2f})")

    def manage_position_ltp(self):
        """Hard stop and fixed target checked against live index LTP (can fire intrabar)."""
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
