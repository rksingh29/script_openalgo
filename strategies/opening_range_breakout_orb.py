"""
Opening Range Breakout (ORB) -- ATM CE/PE option buying for OpenAlgo
----------------------------------------------------------------------
Signal is computed on the underlying INDEX's own 5-minute candles; the
trade itself is an ATM option BUY (CE for a bullish breakout, PE for a
bearish breakdown) sized in the option's lot. Stop/target/trail are
tracked in INDEX points so they stay consistent with the signal that
generated them (converted to rupee P&L via the option fill price at
entry, same pattern as nifty_direction_ce_pe.py / vwap_reclaim_scalp.py).

Rules (checked on every newly CLOSED RANGE_MINUTES-minute candle):
  1. The first candle of the day (SESSION_START, default 09:15-09:20)
     is the "opening range": its high/low are ORH/ORL.
  2. Opening-range filter: if ORH-ORL > WIDE_RANGE_MULTIPLE x the
     average opening-range width of the last WIDE_RANGE_LOOKBACK_DAYS
     trading days, skip breakout trades for the day entirely (range
     too wide -> risk too large). If fewer than
     MIN_LOOKBACK_DAYS_FOR_FILTER prior days of data are available
     (e.g. a 1-day backtest cache), the filter is skipped and a
     warning is logged -- it never blocks trading when data is
     insufficient.
  3. Long (CE): the first subsequent candle whose CLOSE is above ORH
     triggers a long entry (not just an intrabar wick through it).
  4. Short (PE): the first subsequent candle whose CLOSE is below ORL
     triggers a short entry.
  5. Only the first valid breakout per side is taken per day (one CE
     attempt, one PE attempt max -> MAX_TRADES_PER_DAY = 2). A side's
     flag is set the moment its breakout candle closes, even if the
     order itself fails, so a failed/rejected order is not retried.
  6. At most one position is held at a time; a breakout on the
     opposite side while a position is open is ignored (not part of
     the source rules; a documented simplification to avoid hedged/
     overlapping legs).

Stop: long -> ORL - STOP_BUFFER_PTS; short -> ORH + STOP_BUFFER_PTS
(index points). STOP_BUFFER_PTS defaults to 0 (stop exactly at the
opening-range edge); the source material says "just below/above" the
level without giving a buffer size, so this is left as an explicit,
documented tunable rather than guessed.

Target: fixed reward:risk = REWARD_RISK x the initial stop distance
from the entry index price (default 2:1, per spec params).

Trailing (optional, ENABLE_TRAIL): the source says "trail stop by the
opening-range width once price has moved one range-width in favor".
This is implemented as a ratchet: every additional ORH-ORL width of
favorable move beyond entry moves the stop up (down for shorts) by
one more range-width (never loosened). This generalises the single
step the source describes into a continuous trail, which is the
standard reading of "trailing stop" and is toggleable via
ENABLE_TRAIL if a single-step trail (or none) is preferred.

Exit: stop or target, whichever is hit first (checked against the
live index LTP every poll, not only on candle close, since real
stop/target fills happen intrabar); hard square-off at SQUARE_OFF_TIME
regardless of P&L. NO_NEW_ENTRY_AFTER and SQUARE_OFF_TIME, and the
DAILY_MAX_LOSS safety cap, are not part of the source rules -- they
are the same defensive pattern used in nifty_direction_ce_pe.py and
vwap_reclaim_scalp.py.

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
STRATEGY = "OpeningRangeBreakoutORB"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1                      # lots per entry
PRODUCT = "MIS"

RANGE_MINUTES = 5             # opening-range candle size == breakout candle timeframe
BAR_MINUTES = RANGE_MINUTES
TIMEFRAME = f"{BAR_MINUTES}m"
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

REWARD_RISK = 2               # target = this x initial stop distance
STOP_BUFFER_PTS = 0.0         # extra index points beyond the OR edge for the stop
ENABLE_TRAIL = True           # ratchet stop by one OR-width per OR-width of favorable move

WIDE_RANGE_MULTIPLE = 1.5     # skip the day if ORH-ORL > this x the recent average
WIDE_RANGE_LOOKBACK_DAYS = 20
MIN_LOOKBACK_DAYS_FOR_FILTER = 5   # below this many prior days of data, skip the filter (don't block)

MAX_TRADES_PER_DAY = 2        # one breakout per side, per the source rules

POLL_SEC = 20                  # main loop tick while idle/monitoring

# ---- daily safety cap (not in the source rules; same pattern as the other strategies) ----
DAILY_MAX_LOSS = 3000          # NIFTY-sized; resize per instrument (see knowledge/lessons.md)
# ==========================================================


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def hhmm(s):
    h, m = map(int, s.split(":"))
    return h, m


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []           # completed RANGE_MINUTES-minute index candles for today
        self.processed_bars = 0

        self.orh = self.orl = self.or_width = None
        self.skip_today = False

        self.long_taken = False
        self.short_taken = False
        self.trades_today = 0

        self.position = None     # see attempt_entry() for shape
        self.option_sym = None
        self.lot = None

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
        self.bars = bars

    def compute_avg_opening_range(self):
        """Average width of the first RANGE_MINUTES candle over the last
        WIDE_RANGE_LOOKBACK_DAYS prior trading days. Returns None (and logs
        a warning) if too little history is available -- never blocks
        trading for lack of data."""
        end = (self.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        start = (self.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=TIMEFRAME,
                                 start_date=start, end_date=end, source=HISTORY_SOURCE)
        except Exception as e:
            log(f"history exception (avg opening range): {e}")
            return None
        if not hasattr(df, "empty") or df.empty:
            log("No prior-day history available; skipping wide-range filter")
            return None
        start_hm = hhmm(SESSION_START)
        by_day = {}
        for ts, row in df.iterrows():
            t = ts.to_pydatetime()
            if t.tzinfo is not None:
                t = t.replace(tzinfo=None)
            if t.date() >= self.now().date():
                continue   # never use today or the future (no lookahead)
            if (t.hour, t.minute) == start_hm:
                by_day[t.date()] = float(row["high"]) - float(row["low"])
        if len(by_day) < MIN_LOOKBACK_DAYS_FOR_FILTER:
            log(f"Only {len(by_day)} prior day(s) of opening-range data "
                f"(< {MIN_LOOKBACK_DAYS_FOR_FILTER}); skipping wide-range filter")
            return None
        widths = [by_day[d] for d in sorted(by_day)[-WIDE_RANGE_LOOKBACK_DAYS:]]
        return sum(widths) / len(widths)

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        while self.processed_bars < len(self.bars):
            self.handle_new_bar(self.processed_bars)
            self.processed_bars += 1

    def handle_new_bar(self, i):
        bar = self.bars[i]
        if i == 0:
            self.set_opening_range(bar)
            return
        if self.skip_today or self.position:
            return
        if self.now() >= self.at(NO_NEW_ENTRY_AFTER):
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log("Daily trade/loss cap reached; no further entries today")
                self._cap_logged = True
            return
        if bar["c"] > self.orh and not self.long_taken:
            self.long_taken = True
            self.attempt_entry("CE")
        elif bar["c"] < self.orl and not self.short_taken:
            self.short_taken = True
            self.attempt_entry("PE")

    def set_opening_range(self, bar):
        self.orh, self.orl = bar["h"], bar["l"]
        self.or_width = self.orh - self.orl
        avg = self.compute_avg_opening_range()
        if avg and self.or_width > WIDE_RANGE_MULTIPLE * avg:
            self.skip_today = True
            log(f"Opening range {self.orl:.2f}-{self.orh:.2f} (width {self.or_width:.2f}) > "
                f"{WIDE_RANGE_MULTIPLE}x the {WIDE_RANGE_LOOKBACK_DAYS}-day average "
                f"({avg:.2f}); skipping breakout trades today")
        else:
            log(f"Opening range set: high {self.orh:.2f} low {self.orl:.2f} "
                f"width {self.or_width:.2f}" + (f" (avg {avg:.2f})" if avg else " (avg unknown, filter skipped)"))

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
    def attempt_entry(self, side):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log(f"{side} entry aborted: no index LTP")
            return
        if side == "CE":
            initial_stop = self.orl - STOP_BUFFER_PTS
            stop_distance = idx_price - initial_stop
        else:
            initial_stop = self.orh + STOP_BUFFER_PTS
            stop_distance = initial_stop - idx_price
        if stop_distance <= 0:
            log(f"{side} entry aborted: non-positive stop distance ({stop_distance:.2f})")
            return
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        target = idx_price + REWARD_RISK * stop_distance if side == "CE" \
            else idx_price - REWARD_RISK * stop_distance
        price = self.buy(self.option_sym, qty,
                         f"{side} ORB breakout | index {idx_price:.2f} stop {initial_stop:.2f} "
                         f"target {target:.2f}")
        if price is None:
            return
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price,
            "initial_stop": initial_stop, "stop": initial_stop,
            "target": target, "trail_steps": 0,
        }
        self.trades_today += 1
        log(f"ENTER {side} | index {idx_price:.2f} stop {initial_stop:.2f} "
            f"({stop_distance:.2f} pts) target {target:.2f} | trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

    def exit_position(self, reason):
        pos = self.position
        px = self.sell(pos["symbol"], pos["qty"], reason)
        if px is None:
            log("Exit failed; position may still be open - check manually")
            return
        p = (px - pos["entry_premium"]) * pos["qty"]
        self.day_pnl += p
        log(f"EXIT {pos['side']} {reason} | qty {pos['qty']} @ {px} | pnl {p:+.0f} | day P&L {self.day_pnl:+.0f}")
        self.position = None

    def update_trail(self, idx_ltp):
        if not ENABLE_TRAIL or not self.or_width:
            return
        pos = self.position
        if pos["side"] == "CE":
            favorable = idx_ltp - pos["index_entry"]
            steps = int(favorable // self.or_width) if favorable > 0 else 0
            if steps > pos["trail_steps"]:
                new_stop = pos["initial_stop"] + steps * self.or_width
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop
                    pos["trail_steps"] = steps
                    log(f"TRAIL {pos['side']} stop -> {pos['stop']:.2f} ({steps} x OR width)")
        else:
            favorable = pos["index_entry"] - idx_ltp
            steps = int(favorable // self.or_width) if favorable > 0 else 0
            if steps > pos["trail_steps"]:
                new_stop = pos["initial_stop"] - steps * self.or_width
                if new_stop < pos["stop"]:
                    pos["stop"] = new_stop
                    pos["trail_steps"] = steps
                    log(f"TRAIL {pos['side']} stop -> {pos['stop']:.2f} ({steps} x OR width)")

    def manage_position(self):
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        self.update_trail(idx_ltp)
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
            if self.position:
                self.manage_position()
            self.sleep(POLL_SEC)
        log(f"Done for the day. Trades {self.trades_today} | day P&L {self.day_pnl:+.0f}")


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
