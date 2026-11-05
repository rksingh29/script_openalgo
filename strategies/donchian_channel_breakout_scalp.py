"""
Donchian Channel Breakout Scalp (ATM CE/PE option buying) for OpenAlgo
-----------------------------------------------------------------------
Signal on the underlying INDEX's own 5-minute candles; the trade is always an ATM option BUY sized in lots (CE for an
upside breakout, PE for a downside breakout; this project buys options, it does not short them). Stops are tracked in
INDEX points, like the other breakout strategies here (bollinger_band_squeeze_breakout_scalp.py).

Donchian channel at a candle = the previous DONCHIAN_PERIOD (20) COMPLETED candles, the current candle excluded:
  upper = highest high, lower = lowest low, middle = (upper + lower) / 2.

Entry, evaluated on every newly CLOSED 5m candle (from START_TIME):
  - close > upper band -> LONG  (BUY ATM CE)
  - close < lower band -> SHORT (BUY ATM PE)
  A wick through the band with the close inside does not count. Optional filters (off by default, from the sources):
  ATR_BUFFER_MULT > 0 -> the close must clear the band by that x ATR(ATR_PERIOD); RSI_CONFIRM -> RSI(RSI_PERIOD) >
  RSI_LONG_MIN for longs / < RSI_SHORT_MAX for shorts.
Initial stop (index points, set at entry): the OPPOSITE band at entry (long: lower band, short: upper band). The trade
  is skipped when that stop is more than STOP_MAX_PCT (0.4) % of the index away.
Exit (trailing): a candle CLOSES back beyond the middle band (long: close < middle, short: close > middle); the stop is
  checked against the index LTP all the time; square-off at SQUARE_OFF_TIME (intraday only).
Entries stop after NO_NEW_ENTRY_AFTER, MAX_TRADES_PER_DAY or DAILY_MAX_LOSS (safety caps, not from the sources).

Adaptations from the research spec (strategy_db inbox 10_1, sources trendspider / litefinance / avatrade):
  - Warm-up: a 20-candle channel of TODAY's candles only exists from about 10:55. WARMUP_PREVIOUS_DAYS (on) fills the
    channel with the previous trading day's last candles (completed bars only, no lookahead), so signals can start at
    START_TIME 09:20 ("avoid the first 5 minutes after the open"). Off = wait for 20 candles of today.
  - REQUIRE_FRESH_CROSS (on): an entry needs the previous candle's close INSIDE the channel, so a trade stopped out
    while price is still beyond the band is not re-entered on the next candle without a new breakout.
  - "Optional first partial at the opposite band" is not implemented: for a breakout the opposite band is on the stop
    side, so the source sentence has no usable target meaning; the middle-band trail is the only exit (as the spec says).
  - STOP_MAX_PCT skips the trade (spec: "skip the trade if the band stop exceeds that cap") rather than tightening it.

Run first with OpenAlgo Analyzer (paper) mode ON.
"""

import os
import time
from datetime import datetime, timedelta
from openalgo import api

# ======================= PARAMETERS =======================
API_KEY = os.getenv("OPENALGO_API_KEY", "PASTE_YOUR_API_KEY_HERE")
HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
STRATEGY = "DonchianChannelBreakoutScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"

SESSION_START = "09:15"
START_TIME = "09:20"           # avoid the first 5 minutes after the open
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

DONCHIAN_PERIOD = 20           # channel = previous N completed candles (current one excluded)
WARMUP_PREVIOUS_DAYS = True    # fill the channel with the previous day's candles so signals start at START_TIME
REQUIRE_FRESH_CROSS = True     # the previous candle must have closed inside the channel
STOP_MAX_PCT = 0.4             # skip the trade if the opposite-band stop is further than this % of the index

ATR_BUFFER_MULT = 0.0          # breakout close must clear the band by this x ATR; 0 = off
ATR_PERIOD = 14
RSI_CONFIRM = False            # RSI filter: longs need RSI > RSI_LONG_MIN, shorts RSI < RSI_SHORT_MAX
RSI_PERIOD = 14
RSI_LONG_MIN = 60
RSI_SHORT_MAX = 40

POLL_SEC = 20

# ---- daily safety caps (not in the source rules; same pattern as the other strategies here) ----
MAX_TRADES_PER_DAY = 6
DAILY_MAX_LOSS = 3000          # NIFTY-sized: resize per instrument (options panel: Save for <instrument>)
# ==========================================================


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def hhmm(s):
    h, m = map(int, s.split(":"))
    return h, m


def donchian(bars, i, period):
    """(upper, middle, lower) from the `period` candles BEFORE index i, or None if there are not enough."""
    if i < period:
        return None
    w = bars[i - period:i]
    up, lo = max(b["h"] for b in w), min(b["l"] for b in w)
    return up, (up + lo) / 2, lo


def atr(bars, i, period):
    """Simple average true range over the `period` candles ending at i (None if too few)."""
    if i < period:
        return None
    trs = []
    for k in range(i - period + 1, i + 1):
        b, pc = bars[k], bars[k - 1]["c"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    return sum(trs) / period


def rsi(closes, period):
    """Wilder RSI of the last close (None if too few)."""
    if len(closes) <= period:
        return None
    gains = losses = 0.0
    for k in range(1, period + 1):
        d = closes[k] - closes[k - 1]
        gains, losses = gains + max(d, 0), losses + max(-d, 0)
    ag, al = gains / period, losses / period
    for k in range(period + 1, len(closes)):
        d = closes[k] - closes[k - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper
        self.warm = []               # previous day's completed candles (channel warm-up)
        self.bars = []               # warm-up + today's completed 5m index candles: {t, o, h, l, c}
        self.n_warm = 0
        self.processed = 0
        self.levels = {}             # latest channel {upper, middle, lower} (shown on the Signals chart)
        self.position = None
        self.option_sym = None
        self.lot = None
        self.trades_today = 0
        self.day_pnl = 0.0
        self._cap_logged = False

    # ---------- time ----------
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
        for _ in range(2):
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
        log(f"ATM {option_type} {self.option_sym} | lot {self.lot} | expiry {exp} | underlying_ltp {r.get('underlying_ltp')}")

    @staticmethod
    def _rows(df, keep):
        out = []
        for ts, row in df.iterrows():
            t = ts.to_pydatetime()
            if t.tzinfo is not None:
                t = t.replace(tzinfo=None)
            if keep(t):
                out.append({"t": t, "o": float(row["open"]), "h": float(row["high"]), "l": float(row["low"]), "c": float(row["close"])})
        out.sort(key=lambda b: b["t"])
        return out

    def load_warmup(self):
        """The previous trading day's last DONCHIAN_PERIOD candles (completed days only: no lookahead)."""
        today = self.now().date()
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=TIMEFRAME,
                                start_date=(today - timedelta(days=7)).isoformat(),
                                end_date=(today - timedelta(days=1)).isoformat(), source=HISTORY_SOURCE)
        except Exception as e:
            log(f"warm-up history failed ({e}); the channel starts with today's candles")
            return
        if not hasattr(df, "iterrows") or df.empty:
            log("no previous-day candles; the channel starts with today's candles")
            return
        rows = self._rows(df, lambda t: t.date() < today)
        self.warm = rows[-DONCHIAN_PERIOD:]
        if self.warm:
            log(f"channel warm-up: {len(self.warm)} candles of {self.warm[-1]['t']:%Y-%m-%d} "
                f"({self.warm[0]['t']:%H:%M}-{self.warm[-1]['t']:%H:%M})")

    def refresh_bars(self):
        today = self.now().strftime("%Y-%m-%d")
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=TIMEFRAME,
                                start_date=today, end_date=today, source=HISTORY_SOURCE)
        except Exception as e:
            log(f"history exception ({TIMEFRAME}): {e}")
            return
        if not hasattr(df, "iterrows") or df.empty:
            return
        now = self.now()
        rows = self._rows(df, lambda t: t.date() == now.date() and t + timedelta(minutes=BAR_MINUTES) <= now)
        if rows:
            self.n_warm = len(self.warm)
            self.bars = self.warm + rows

    # ---------- signals ----------
    def handle_bar(self, i):
        ch = donchian(self.bars, i, DONCHIAN_PERIOD)
        if ch is None:
            return
        up, mid, lo = ch
        bar = self.bars[i]
        self.levels = {"upper": round(up, 2), "middle": round(mid, 2), "lower": round(lo, 2)}
        if self.position:                              # trailing exit on the CLOSE beyond the middle band
            if self.position["side"] == "CE" and bar["c"] < mid:
                self.exit_position(f"close {bar['c']:.2f} below the middle band {mid:.2f} ({bar['t']:%H:%M})")
            elif self.position["side"] == "PE" and bar["c"] > mid:
                self.exit_position(f"close {bar['c']:.2f} above the middle band {mid:.2f} ({bar['t']:%H:%M})")
            return
        if not self.in_time_window() or i < self.n_warm:
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log("Daily trade/loss cap reached; no further entries today")
                self._cap_logged = True
            return
        buf = 0.0
        if ATR_BUFFER_MULT > 0:
            a = atr(self.bars, i, ATR_PERIOD)
            if a is None:
                return
            buf = ATR_BUFFER_MULT * a
        side = "CE" if bar["c"] > up + buf else "PE" if bar["c"] < lo - buf else None
        if not side:
            return
        if REQUIRE_FRESH_CROSS:
            prev = donchian(self.bars, i - 1, DONCHIAN_PERIOD)
            pc = self.bars[i - 1]["c"]
            if prev and (pc > prev[0] if side == "CE" else pc < prev[2]):
                log(f"{side} close beyond the band at {bar['t']:%H:%M} skipped: the previous candle was already outside (no fresh cross)")
                return
        if RSI_CONFIRM:
            r = rsi([b["c"] for b in self.bars[:i + 1]], RSI_PERIOD)
            if r is None or (side == "CE" and r <= RSI_LONG_MIN) or (side == "PE" and r >= RSI_SHORT_MAX):
                log(f"{side} breakout at {bar['t']:%H:%M} skipped: RSI {r if r is None else round(r, 1)}")
                return
        self.attempt_entry(side, bar, up, lo)

    def process_new_bars(self):
        if self.processed < self.n_warm:
            self.processed = self.n_warm               # warm-up candles only feed the channel
        while self.processed < len(self.bars):
            self.handle_bar(self.processed)
            self.processed += 1

    # ---------- orders ----------
    def fill_price(self, oid):
        try:
            self.sleep(1)
            r = self.c.orderstatus(order_id=oid, strategy=STRATEGY)
            return float(r.get("data", {}).get("average_price") or 0) or None
        except Exception:
            return None

    def order(self, action, symbol, qty, reason):
        r = self.c.placeorder(strategy=STRATEGY, symbol=symbol, action=action, exchange=OPT_EXCHANGE,
                              price_type="MARKET", product=PRODUCT, quantity=qty)
        ok = r.get("status") == "success"
        price = (self.fill_price(r.get("orderid")) if ok else None) or (self.ltp(symbol, OPT_EXCHANGE) if ok or action == "SELL" else None)
        if not ok:
            log(f"{action} FAILED {symbol}: {r}" + (" !!! CHECK POSITION MANUALLY IN OPENALGO !!!" if action == "SELL" else ""))
            if action == "BUY":
                return None
        log(f"{action} {symbol} qty {qty} @ {price} | order {r.get('orderid', r)} | {reason}")
        return price

    def attempt_entry(self, side, bar, up, lo):
        idx = self.ltp(UNDERLYING, INDEX_EXCHANGE) or bar["c"]
        stop = lo if side == "CE" else up
        dist_pct = abs(idx - stop) / idx * 100
        if dist_pct > STOP_MAX_PCT:
            log(f"{side} breakout at {bar['t']:%H:%M} skipped: band stop {stop:.2f} is {dist_pct:.2f}% away (cap {STOP_MAX_PCT}%)")
            return
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        px = self.order("BUY", self.option_sym, qty,
                        f"{side} Donchian breakout: close {bar['c']:.2f} {'>' if side == 'CE' else '<'} "
                        f"{'upper' if side == 'CE' else 'lower'} {up if side == 'CE' else lo:.2f} | stop {stop:.2f} ({dist_pct:.2f}%)")
        if px is None:
            return
        self.position = {"side": side, "symbol": self.option_sym, "qty": qty, "entry_premium": px, "index_entry": idx, "stop": stop}
        self.trades_today += 1
        log(f"ENTER {side} | index {idx:.2f} stop {stop:.2f} | trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

    def exit_position(self, reason):
        pos = self.position
        px = self.order("SELL", pos["symbol"], pos["qty"], reason)
        p = ((px or pos["entry_premium"]) - pos["entry_premium"]) * pos["qty"]
        self.day_pnl += p
        log(f"EXIT {pos['side']} {reason} | pnl {p:+.0f} | day P&L {self.day_pnl:+.0f}")
        self.position = None

    def check_stop(self):
        if not self.position:
            return
        idx = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx is None:
            return
        pos = self.position
        if (pos["side"] == "CE" and idx <= pos["stop"]) or (pos["side"] == "PE" and idx >= pos["stop"]):
            self.exit_position(f"stop hit (index {idx:.2f}, stop {pos['stop']:.2f})")

    # ---------- main loop ----------
    def run(self):
        log(f"Strategy {STRATEGY} starting on {UNDERLYING}")
        try:
            log(f"Analyzer (paper) mode: {self.c.analyzerstatus().get('data')}")
        except Exception:
            pass
        if WARMUP_PREVIOUS_DAYS:
            self.load_warmup()
        self.wait_until(self.at(SESSION_START))
        while True:
            if self.now() >= self.at(SQUARE_OFF_TIME):
                if self.position:
                    self.exit_position("square-off time")
                log("Square-off time reached; stopping for the day")
                break
            self.refresh_bars()
            self.process_new_bars()
            self.check_stop()
            self.sleep(POLL_SEC)
        log(f"Done for the day. Trades {self.trades_today} | day P&L {self.day_pnl:+.0f}")


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
