"""
Supertrend Flip Scalp (ATM CE/PE option buying) for OpenAlgo
---------------------------------------------------------------
Signal is a classic Supertrend (ATR period x multiplier) computed on the
underlying INDEX's own 5-minute candles. The trade itself is always an ATM
option BUY sized in lots (CE for a bullish flip, PE for a bearish flip --
this project always buys options, never shorts them, per
nifty_direction_ce_pe.py / vwap_reclaim_scalp.py / s_9_21_ema_crossover_scalp.py
convention; "CE long, PE short" in the spec means "buy CE for long bias, buy
PE for short bias").

Evaluated on every newly CLOSED 5m index candle:
  - Compute Supertrend (ATR_PERIOD, MULTIPLIER) on the index's OHLC.
  - A "flip" is the Supertrend trend direction changing between the
    previous closed candle and this one (red->green = bullish flip, buy
    CE; green->red = bearish flip, buy PE). The very first direction the
    indicator establishes after warm-up is NOT a flip (nothing to exit from
    yet) -- only genuine direction changes trigger trades.
  - On any flip: if a position is open, it is exited immediately (the flip
    invalidates the thesis that opened it -- "exit and reverse bias" in the
    spec). A new entry in the flip's new direction is then taken only if
    still inside the entry window, under the daily trade/loss caps, and (if
    REQUIRE_HTF_TREND_FILTER) the higher-timeframe Supertrend agrees.
  - Optional higher-timeframe filter (REQUIRE_HTF_TREND_FILTER, default True
    per the spec's "pair it with a momentum filter to reduce whipsaws"
    guidance): the same Supertrend indicator computed on 15-minute index
    candles must already be in the same direction as the 5m flip.

Stop / exit (per spec, "the line itself acts as the exit signal, no
separate target"):
  - The Supertrend line's value at entry is the initial stop; it is
    tracked/logged every bar as it trails along in the trade's favor
    (ratchets automatically -- that is how the Supertrend formula itself
    works while a trend persists).
  - The actual exit is executed ONLY when a candle closes on the opposite
    side of the line (the same flip event that would open the next trade).
    There is deliberately no separate intrabar LTP-based stop/target check:
    the spec explicitly says the line is the exit signal and there is no
    separate target, so adding an extra intrabar trigger would be inventing
    a mechanism not in the source text. The "stop"/"trail" language in the
    spec is realized as the same bar-close flip, with the line value logged
    each bar for visibility.

No fixed profit target; the position rides until the next flip or the
square-off time.

Adaptations from the raw research spec (documented, not guesses):
  - ATR period / multiplier are given as ranges (7-10 / 2-3) -> module
    constants default to the spec's own "params" (ATR_PERIOD=10,
    MULTIPLIER=3), tunable without code changes.
  - "Momentum / higher-timeframe filter" is named only qualitatively ->
    implemented as the same Supertrend indicator on 15-minute candles,
    gated by REQUIRE_HTF_TREND_FILTER so it can be A/B'd in backtests
    (same toggle pattern as s_9_21_ema_crossover_scalp.py's HTF filter).
  - "Reverse bias" on every flip is capped by the entry window and the
    daily trade/loss limits below, so a choppy day cannot force the
    strategy to keep re-entering through the daily loss cap.
  - MAX_TRADES_PER_DAY / DAILY_MAX_LOSS / NO_NEW_ENTRY_AFTER /
    SQUARE_OFF_TIME are safety caps, not part of the source rules (same
    convention as the other strategies in this project).

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
STRATEGY = "SupertrendFlipScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HTF_TIMEFRAME = "15m"
HTF_BAR_MINUTES = 15
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"       # candles are built from the open
START_TIME = "09:15"          # no separate entry delay; ATR warm-up naturally gates early entries
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

ATR_PERIOD = 10               # spec range 7-10
MULTIPLIER = 3                # spec range 2-3

REQUIRE_HTF_TREND_FILTER = True   # 15m Supertrend direction must agree with the 5m flip direction
HTF_ATR_PERIOD = 10
HTF_MULTIPLIER = 3

POLL_SEC = 20                 # main loop tick

# ---- daily safety caps (not in the source rules; same pattern as other strategies here) ----
MAX_TRADES_PER_DAY = 6        # flip-to-flip reversal system -> more entries than a one-shot directional strategy
DAILY_MAX_LOSS = 3000          # NIFTY-sized; resize per instrument (see knowledge/lessons.md)
DAILY_PROFIT_TARGET = 0        # 0 = off (spec defines no fixed target)
# ==========================================================


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def hhmm(s):
    h, m = map(int, s.split(":"))
    return h, m


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


def supertrend_series(bars, period, multiplier):
    """Classic Supertrend. Returns (trend, line), both aligned 1:1 with bars.
    trend[i] is "up" (line below price, green) or "down" (line above price,
    red), or None during ATR warm-up. line[i] is the Supertrend value."""
    n = len(bars)
    atr = atr_series(bars, period)
    final_upper = [None] * n
    final_lower = [None] * n
    line = [None] * n
    trend = [None] * n
    for i in range(n):
        if atr[i] is None:
            continue
        hl2 = (bars[i]["h"] + bars[i]["l"]) / 2.0
        basic_upper = hl2 + multiplier * atr[i]
        basic_lower = hl2 - multiplier * atr[i]
        prev_i = i - 1
        if prev_i < 0 or final_upper[prev_i] is None:
            # first valid bar: no prior final bands to carry; seed trend from the basic bands
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            close = bars[i]["c"]
            if close <= basic_upper:
                trend[i] = "down"
                line[i] = final_upper[i]
            else:
                trend[i] = "up"
                line[i] = final_lower[i]
            continue
        prev_close = bars[prev_i]["c"]
        final_upper[i] = (basic_upper if (basic_upper < final_upper[prev_i] or prev_close > final_upper[prev_i])
                          else final_upper[prev_i])
        final_lower[i] = (basic_lower if (basic_lower > final_lower[prev_i] or prev_close < final_lower[prev_i])
                          else final_lower[prev_i])
        close = bars[i]["c"]
        if trend[prev_i] == "down":
            if close <= final_upper[i]:
                trend[i] = "down"
                line[i] = final_upper[i]
            else:
                trend[i] = "up"
                line[i] = final_lower[i]
        else:  # prev trend "up"
            if close >= final_lower[i]:
                trend[i] = "up"
                line[i] = final_lower[i]
            else:
                trend[i] = "down"
                line[i] = final_upper[i]
    return trend, line


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []           # completed 5m index candles: {t, o, h, l, c}
        self.trend = []
        self.line = []
        self.processed_bars = 0

        self.htf_bars = []       # completed 15m index candles
        self.htf_trend = []
        self.htf_line = []

        self.position = None     # {"side", "symbol", "qty", "entry_premium", "entry_line", "stop"}
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
        self.trend, self.line = supertrend_series(bars, ATR_PERIOD, MULTIPLIER)

    def refresh_htf_bars(self):
        bars = self._fetch_bars(HTF_TIMEFRAME, HTF_BAR_MINUTES)
        if not bars:
            return
        self.htf_bars = bars
        self.htf_trend, self.htf_line = supertrend_series(bars, HTF_ATR_PERIOD, HTF_MULTIPLIER)

    def current_htf_trend(self):
        return self.htf_trend[-1] if self.htf_trend else None

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        while self.processed_bars < len(self.bars):
            self.handle_new_bar(self.processed_bars)
            self.processed_bars += 1

    def in_time_window(self):
        return self.at(START_TIME) <= self.now() < self.at(NO_NEW_ENTRY_AFTER)

    def handle_new_bar(self, i):
        if i == 0:
            return
        prev_t, cur_t = self.trend[i - 1], self.trend[i]
        if prev_t is None or cur_t is None:
            return
        if cur_t == prev_t:
            # no flip: just trail the stop display along the Supertrend line
            if self.position and self.line[i] is not None and self.line[i] != self.position["stop"]:
                log(f"Trailing stop (Supertrend line) {self.position['stop']:.2f} -> {self.line[i]:.2f}")
                self.position["stop"] = self.line[i]
            return

        # flip detected
        bar = self.bars[i]
        log(f"Supertrend flipped to {cur_t!r} at bar {i} ({bar['t']:%H:%M}), line {self.line[i]:.2f}")
        if self.position:
            self.exit_position(f"Supertrend flip to {cur_t} (reverse bias)")

        if not self.in_time_window():
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log("Daily trade/loss cap reached; no further entries today")
                self._cap_logged = True
            return

        new_dir = "CE" if cur_t == "up" else "PE"
        if REQUIRE_HTF_TREND_FILTER:
            htf = self.current_htf_trend()
            wanted = "up" if new_dir == "CE" else "down"
            if htf != wanted:
                log(f"Entry skipped: 15m Supertrend filter is {htf!r}, need {wanted!r}")
                return
        self.attempt_entry(new_dir, i)

    def attempt_entry(self, side, i):
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        price = self.buy(self.option_sym, qty, f"{side} Supertrend flip | line {self.line[i]:.2f}")
        if price is None:
            return
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "entry_line": self.line[i], "stop": self.line[i],
        }
        self.trades_today += 1
        log(f"ENTER {side} | Supertrend line {self.line[i]:.2f} | "
            f"trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

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
            if REQUIRE_HTF_TREND_FILTER:
                self.refresh_htf_bars()
            self.process_new_bars()
            self.sleep(POLL_SEC)
        log(f"Done for the day. Trades {self.trades_today} | day P&L {self.day_pnl:+.0f}")


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
