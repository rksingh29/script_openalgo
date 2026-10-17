"""
9/21 EMA Crossover Scalp (ATM CE/PE option buying) for OpenAlgo
-----------------------------------------------------------------
Signal is computed on the underlying INDEX's own 5-minute candles
(9-period and 21-period EMA of closes); the trade itself is an ATM
option BUY sized in lots (CE on a bullish cross, PE on a bearish
cross -- this project always buys options, never shorts them, per
nifty_direction_ce_pe.py / vwap_reclaim_scalp.py convention). Stops
and targets are tracked in INDEX points so they match the signal that
generated them, then converted to a lot-sized option P&L at exit.

Long (CE) setup, evaluated on every newly CLOSED 5m index candle:
  1. Crossover candle: EMA9 was <= EMA21 on the previous candle and
     EMA9 > EMA21 on this candle (bullish cross), with this candle's
     close above both EMAs.
  2. Confirmation candle (the next completed 5m candle): EMA9 is still
     above EMA21, the EMA9-EMA21 gap has grown versus the crossover
     candle (the "visibly separating, steep angle" requirement -- the
     concrete, testable form of that qualitative spec text), the gap
     is at least MIN_SEPARATION_PCT of price (filters out a cross that
     immediately goes flat/overlapping again), EMA9 itself is still
     rising, and price is still above both EMAs.
  2a. Optional higher-timeframe filter (REQUIRE_HTF_TREND_FILTER,
      default True per the spec's "performs best when a 15m trend
      filter agrees" guidance): the 15-minute EMA9/EMA21 relationship
      on the index must also be bullish (EMA9 > EMA21) at the last
      completed 15m candle.
  Entry: BUY ATM CE at the confirmation candle's close.
Short (PE) setup mirrors this below the EMAs (bearish cross, gap
widening down, EMA9 falling, 15m filter bearish) -- BUY ATM PE.

Stop: just beyond the LOW (long) / HIGH (short) of the crossover
candle itself (index points), with a small buffer
(STOP_BUFFER_PCT of index price). Skipped if that distance exceeds
STOP_MAX_PCT of the index price (safety cap, not in the source spec).

Target: fixed reward:risk of REWARD_RISK x the initial stop distance
from the entry index level (per spec "target": 2:1 R:R).

Exit (checked continuously via LTP, and on every new 5m candle close):
  - Target or stop hit (index LTP) -> exit.
  - EMA9 trailing stop: on every new completed 5m candle while in a
    trade, the stop is ratcheted toward price using the current EMA9
    value (never loosened) -- this is the literal "trail the stop
    using the 9 EMA" alternative from the spec.
  - EMA9 flattening/reversal invalidation: if a completed 5m candle
    closes back on the wrong side of EMA9 (below it for a long, above
    it for a short), exit immediately -- this is the concrete form of
    the spec's "exit when the 9 EMA begins flattening against the
    trade direction" alternative (the spec offers both as alternatives
    to target/stop; implementing the EMA9 trail also naturally
    produces a quick exit once EMA9 flattens, since price then crosses
    it almost immediately).
  - Square-off at SQUARE_OFF_TIME regardless of P&L.

Entries stop after NO_NEW_ENTRY_AFTER or once MAX_TRADES_PER_DAY /
DAILY_MAX_LOSS is reached (safety caps, same pattern as
nifty_direction_ce_pe.py / vwap_reclaim_scalp.py; not part of the
source rules).

Adaptations from the raw research spec (documented, not guesses):
  - "EMAs visibly separating (steep angle)" -> gap(EMA9, EMA21) at the
    confirmation candle must exceed the gap at the crossover candle
    AND exceed MIN_SEPARATION_PCT of price. A qualitative "steepness"
    cue has no single numeric definition in the sources, so a growing,
    non-trivial gap is used as the testable proxy.
  - "Trail the stop using the 9 EMA" and "exit when 9 EMA flattens"
    are offered as alternatives in the spec; both are implemented
    together (ratcheting EMA9 stop + wrong-side-of-EMA9 close exit)
    since they are complementary, not conflicting.
  - "Stop beyond the crossover candle, or beyond the most recent swing
    low/high" -> the crossover candle's own low/high is used (the
    simpler, deterministic choice of the two alternatives offered).
  - Higher-timeframe filter uses the same 9/21 EMA pair on the 15m
    index candles (the spec names "15-minute trend direction" without
    specifying how to measure it).
  - MAX_TRADES_PER_DAY / DAILY_MAX_LOSS / STOP_MAX_PCT are safety
    caps, not sourced from the research material.

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
STRATEGY = "Ema921CrossoverScalp"

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
START_TIME = "09:15"          # no separate entry delay; EMA21 warm-up naturally gates early entries
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

FAST_EMA = 9
SLOW_EMA = 21
MIN_SEPARATION_PCT = 0.05     # min EMA9-EMA21 gap, as % of price, at the confirmation candle

REQUIRE_HTF_TREND_FILTER = True   # 15m EMA9 vs EMA21 must agree with the 5m cross direction
HTF_FAST_EMA = 9
HTF_SLOW_EMA = 21

STOP_BUFFER_PCT = 0.05        # stop placed this % of index price beyond the crossover candle low/high
STOP_MAX_PCT = 0.6            # skip the trade if stop distance exceeds this % of index price
REWARD_RISK = 2.0             # target = this x the stop distance

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


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []           # completed 5m index candles: {t, o, h, l, c}
        self.ema9 = []
        self.ema21 = []
        self.processed_bars = 0

        self.htf_bars = []       # completed 15m index candles
        self.htf_ema9 = []
        self.htf_ema21 = []

        self.pending_cross = None   # {"side": "CE"/"PE", "idx": i, "low": .., "high": ..}
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
        self.ema9 = ema_series(closes, FAST_EMA)
        self.ema21 = ema_series(closes, SLOW_EMA)

    def refresh_htf_bars(self):
        bars = self._fetch_bars(HTF_TIMEFRAME, HTF_BAR_MINUTES)
        if not bars:
            return
        self.htf_bars = bars
        closes = [b["c"] for b in bars]
        self.htf_ema9 = ema_series(closes, HTF_FAST_EMA)
        self.htf_ema21 = ema_series(closes, HTF_SLOW_EMA)

    def htf_trend(self):
        """'up' / 'down' / None from the last completed 15m candle's EMA9 vs EMA21."""
        if not self.htf_ema9 or not self.htf_ema21:
            return None
        e9, e21 = self.htf_ema9[-1], self.htf_ema21[-1]
        if e9 is None or e21 is None:
            return None
        if e9 > e21:
            return "up"
        if e9 < e21:
            return "down"
        return None

    # ---------- setup detection ----------
    def detect_cross(self, i):
        """Returns 'CE' or 'PE' if bar i is a fresh EMA9/EMA21 crossover candle, else None."""
        if i < 1:
            return None
        e9, e21 = self.ema9, self.ema21
        if None in (e9[i - 1], e21[i - 1], e9[i], e21[i]):
            return None
        bar = self.bars[i]
        if e9[i - 1] <= e21[i - 1] and e9[i] > e21[i] and bar["c"] > e9[i] and bar["c"] > e21[i]:
            return "CE"
        if e9[i - 1] >= e21[i - 1] and e9[i] < e21[i] and bar["c"] < e9[i] and bar["c"] < e21[i]:
            return "PE"
        return None

    def confirm_separation(self, side, cross_i, confirm_i):
        """Checks the 'visibly separating / steep angle' requirement at the confirmation candle."""
        e9, e21 = self.ema9, self.ema21
        if None in (e9[confirm_i], e21[confirm_i], e9[cross_i], e21[cross_i]):
            return False
        gap_cross = e9[cross_i] - e21[cross_i]
        gap_confirm = e9[confirm_i] - e21[confirm_i]
        bar = self.bars[confirm_i]
        gap_pct = abs(gap_confirm) / bar["c"] * 100 if bar["c"] else 0
        rising_ema9 = e9[confirm_i] > e9[cross_i]
        if side == "CE":
            return (gap_confirm > gap_cross > 0 and gap_pct >= MIN_SEPARATION_PCT
                    and rising_ema9 and bar["c"] > e9[confirm_i] and bar["c"] > e21[confirm_i])
        else:
            return (gap_confirm < gap_cross < 0 and gap_pct >= MIN_SEPARATION_PCT
                    and not rising_ema9 and bar["c"] < e9[confirm_i] and bar["c"] < e21[confirm_i])

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
        if self.pending_cross:
            self.evaluate_pending(i)
            return
        if not self.in_time_window():
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log("Daily trade/loss cap reached; no further entries today")
                self._cap_logged = True
            return
        side = self.detect_cross(i)
        if side:
            bar = self.bars[i]
            self.pending_cross = {"side": side, "idx": i, "low": bar["l"], "high": bar["h"]}
            log(f"{side} EMA9/EMA21 cross at bar {i} ({bar['t']:%H:%M}); "
                f"waiting for separation confirmation")

    def evaluate_pending(self, i):
        p = self.pending_cross
        self.pending_cross = None
        if self.confirm_separation(p["side"], p["idx"], i):
            log(f"Separation confirmed for {p['side']} at bar {i}; entering")
            self.attempt_entry(p["side"], p["low"], p["high"])
        else:
            log(f"Separation not confirmed for {p['side']} at bar {i}; setup dropped")

    def manage_on_bar_close(self, i):
        """EMA9 trailing stop + flattening/wrong-side-of-EMA9 invalidation."""
        pos = self.position
        e9 = self.ema9[i]
        bar = self.bars[i]
        if e9 is None:
            return
        if pos["side"] == "CE":
            if bar["c"] < e9:
                self.exit_position(f"EMA9 flattening/reversal: candle closed below EMA9 "
                                    f"({bar['c']:.2f} < {e9:.2f})")
                return
            new_stop = e9 - (STOP_BUFFER_PCT / 100.0) * bar["c"]
            if new_stop > pos["stop"]:
                log(f"Trailing stop (EMA9) raised {pos['stop']:.2f} -> {new_stop:.2f}")
                pos["stop"] = new_stop
        else:
            if bar["c"] > e9:
                self.exit_position(f"EMA9 flattening/reversal: candle closed above EMA9 "
                                    f"({bar['c']:.2f} > {e9:.2f})")
                return
            new_stop = e9 + (STOP_BUFFER_PCT / 100.0) * bar["c"]
            if new_stop < pos["stop"]:
                log(f"Trailing stop (EMA9) lowered {pos['stop']:.2f} -> {new_stop:.2f}")
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
    def attempt_entry(self, side, cross_low, cross_high):
        if REQUIRE_HTF_TREND_FILTER:
            trend = self.htf_trend()
            wanted = "up" if side == "CE" else "down"
            if trend != wanted:
                log(f"Entry skipped: 15m trend filter is {trend!r}, need {wanted!r}")
                return
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
                          f"{side} 9/21 EMA cross | index {idx_price:.2f} stop {stop_price:.2f}")
        if price is None:
            return
        target = idx_price + REWARD_RISK * stop_distance if side == "CE" \
            else idx_price - REWARD_RISK * stop_distance
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price,
            "stop": stop_price, "target": target,
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
            if REQUIRE_HTF_TREND_FILTER:
                self.refresh_htf_bars()
            self.process_new_bars()
            self.manage_position_ltp()
            self.sleep(POLL_SEC)
        log(f"Done for the day. Trades {self.trades_today} | day P&L {self.day_pnl:+.0f}")


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
