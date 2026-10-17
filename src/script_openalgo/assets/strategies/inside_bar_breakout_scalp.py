"""
Inside Bar Breakout Scalp (ATM CE/PE option buying) for OpenAlgo
------------------------------------------------------------------
Signal is computed on the underlying INDEX's own 5-minute candles;
the trade itself is an ATM option BUY sized in lots (CE on a bullish
breakout, PE on a bearish breakout -- this project always buys
options, never shorts the index, per nifty_direction_ce_pe.py /
vwap_reclaim_scalp.py / s_9_21_ema_crossover_scalp.py convention).
Stops and targets are tracked in INDEX points so they match the
signal that generated them, then converted to a lot-sized option
P&L at exit.

Pattern, evaluated on every newly CLOSED 5m index candle:
  1. Mother bar (M) + inside bar (I): I's high < M's high and I's low
     > M's low (I is fully contained inside M's range).
  2. Expansion filter (REQUIRE_EXPANSION_FILTER, default True, per the
     spec's "prefer inside bars that form after a clear prior move"
     guidance): M's own range must be at least MIN_EXPANSION_RATIO x
     the average range of the EXPANSION_LOOKBACK bars immediately
     before M (range expansion -> contraction, not a mother bar sitting
     in the middle of tight chop).
  3. While waiting for the breakout, any further bar that stays fully
     inside M's range (high <= M.high, low >= M.low) is treated as
     another, tighter inside bar -- the pattern persists and its
     "conviction" count goes up (spec: "multiple consecutive inside
     bars tightening the range increase conviction"); the breakout
     levels stay anchored to the ORIGINAL mother bar M throughout.
  4. Breakout: the first subsequent candle that CLOSES above M's high
     -> long (BUY ATM CE); that CLOSES below M's low -> short (BUY ATM
     PE). The pattern expires (dropped, no trade) if no breakout
     candle appears within MAX_BARS_TO_BREAKOUT completed candles.

Stop: just beyond the mother bar's low (long) / high (short), with a
small buffer (STOP_BUFFER_PCT of index price). Skipped if that
distance exceeds STOP_MAX_PCT of the index price (safety cap, not in
the source spec).

Target: the CLOSER of (a) fixed REWARD_RISK x the initial stop
distance from the entry index level (spec: "minimum reward:risk of
1:2"), and (b) the session's high-so-far (long) / low-so-far (short)
as the testable proxy for "the next intraday support/resistance level"
-- the level is only used if it actually sits beyond the entry price
in the trade's direction, otherwise the R:R target is used alone.

Exit (checked continuously via LTP):
  - Target or stop hit (index LTP) -> exit.
  - Square-off at SQUARE_OFF_TIME regardless of P&L.

Entries stop after NO_NEW_ENTRY_AFTER or once MAX_TRADES_PER_DAY /
DAILY_MAX_LOSS is reached (safety caps, same pattern as the other
strategies in this project; not part of the source rules).

Adaptations from the raw research spec (documented, not guesses):
  - "Candle close above mother-bar high, or stop order placed just
    above it" -> close-based confirmation is implemented (consistent
    with this project's bar-close-driven strategies); the stop-order
    (intrabar wick) variant is not implemented.
  - "Range contraction following expansion" -> mother bar's range
    must exceed MIN_EXPANSION_RATIO x the average range of the
    preceding EXPANSION_LOOKBACK bars (a concrete, testable proxy for
    the qualitative "clear prior move" cue).
  - "Multiple consecutive inside bars... increase conviction" -> not
    a hard entry gate (the spec only says it increases conviction, not
    that it is required); tracked and logged as `inside_bar_count` on
    the pending setup. MIN_INSIDE_BAR_COUNT lets a reviewer turn this
    into a hard gate later without further code changes.
  - "Next intraday support/resistance level" has no single definition
    in the sources; the session high-so-far / low-so-far (via
    `quotes()["data"]["high"/"low"]`, the same no-lookahead feature
    added for vwap_reclaim_scalp.py) is used as the testable proxy,
    only when it sits beyond entry in the trade direction.
  - MAX_BARS_TO_BREAKOUT, STOP_BUFFER_PCT, STOP_MAX_PCT,
    MAX_TRADES_PER_DAY, DAILY_MAX_LOSS are safety caps, not sourced
    from the research material.

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
STRATEGY = "InsideBarBreakoutScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"       # candles are built from the open
START_TIME = "09:15"          # no separate entry delay
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

# ---- pattern filters ----
REQUIRE_EXPANSION_FILTER = True   # mother bar must follow a clear prior move
EXPANSION_LOOKBACK = 5            # bars before the mother bar used for the average range
MIN_EXPANSION_RATIO = 1.2         # mother bar range >= this x the preceding average range
MIN_INSIDE_BAR_COUNT = 1          # hard gate on consecutive inside bars before a breakout is taken (1 = off)
MAX_BARS_TO_BREAKOUT = 12         # pattern expires if no breakout within this many completed candles

# ---- stop / target ----
STOP_BUFFER_PCT = 0.05        # stop placed this % of index price beyond the mother bar low/high
STOP_MAX_PCT = 0.6            # skip the trade if stop distance exceeds this % of index price
REWARD_RISK = 2.0             # target = this x the stop distance (spec: params.reward_risk)

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


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []           # completed 5m index candles: {t, o, h, l, c}
        self.processed_bars = 0

        self.pending = None      # see detect_mother_inside() / handle_new_bar() for shape
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

    def session_level(self, symbol, exchange, which):
        """which='high' or 'low': session high/low-so-far via quotes(); None on failure."""
        try:
            r = self.c.quotes(symbol=symbol, exchange=exchange)
            if r.get("status") == "success":
                v = r["data"].get(which)
                if v is not None:
                    return float(v)
        except Exception as e:
            log(f"quotes({which}) exception {symbol}: {e}")
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
                         "l": float(row["low"]), "c": float(row["close"])})
        bars.sort(key=lambda b: b["t"])
        if bars:
            self.bars = bars

    # ---------- pattern detection ----------
    def expansion_ok(self, mother_i):
        if not REQUIRE_EXPANSION_FILTER:
            return True
        lo = max(0, mother_i - EXPANSION_LOOKBACK)
        window = self.bars[lo:mother_i]
        if len(window) < EXPANSION_LOOKBACK:
            return False   # not enough history yet; be conservative
        avg_range = sum(b["h"] - b["l"] for b in window) / len(window)
        if avg_range <= 0:
            return False
        mother_range = self.bars[mother_i]["h"] - self.bars[mother_i]["l"]
        return mother_range >= MIN_EXPANSION_RATIO * avg_range

    def detect_mother_inside(self, i):
        """Bar i-1 = candidate mother bar, bar i = candidate inside bar."""
        if i < 1:
            return None
        mother, inside = self.bars[i - 1], self.bars[i]
        if not (inside["h"] < mother["h"] and inside["l"] > mother["l"]):
            return None
        if not self.expansion_ok(i - 1):
            return None
        return {"mother_idx": i - 1, "mother_high": mother["h"], "mother_low": mother["l"],
                "formed_idx": i, "inside_bar_count": 1, "bars_waited": 0}

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        while self.processed_bars < len(self.bars):
            self.handle_new_bar(self.processed_bars)
            self.processed_bars += 1

    def in_time_window(self):
        return self.at(START_TIME) <= self.now() < self.at(NO_NEW_ENTRY_AFTER)

    def entries_capped(self):
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log("Daily trade/loss cap reached; no further entries today")
                self._cap_logged = True
            return True
        return False

    def handle_new_bar(self, i):
        if self.position:
            return   # managed via manage_position_ltp()
        if self.pending:
            self.evaluate_pending(i)
            return
        if not self.in_time_window() or self.entries_capped():
            return
        p = self.detect_mother_inside(i)
        if p:
            self.pending = p
            log(f"Inside bar at bar {i} ({self.bars[i]['t']:%H:%M}) inside mother bar "
                f"{p['mother_idx']} (high {p['mother_high']:.2f} / low {p['mother_low']:.2f}); "
                f"watching for breakout")

    def evaluate_pending(self, i):
        p = self.pending
        bar = self.bars[i]
        p["bars_waited"] += 1
        if bar["c"] > p["mother_high"]:
            self.pending = None
            if p["inside_bar_count"] < MIN_INSIDE_BAR_COUNT:
                log(f"Breakout above {p['mother_high']:.2f} at bar {i} but inside-bar count "
                    f"{p['inside_bar_count']} < MIN_INSIDE_BAR_COUNT {MIN_INSIDE_BAR_COUNT}; skipped")
                return
            log(f"Bullish breakout: bar {i} closed {bar['c']:.2f} > mother high "
                f"{p['mother_high']:.2f} (inside bars seen: {p['inside_bar_count']})")
            self.attempt_entry("CE", p)
            return
        if bar["c"] < p["mother_low"]:
            self.pending = None
            if p["inside_bar_count"] < MIN_INSIDE_BAR_COUNT:
                log(f"Breakdown below {p['mother_low']:.2f} at bar {i} but inside-bar count "
                    f"{p['inside_bar_count']} < MIN_INSIDE_BAR_COUNT {MIN_INSIDE_BAR_COUNT}; skipped")
                return
            log(f"Bearish breakdown: bar {i} closed {bar['c']:.2f} < mother low "
                f"{p['mother_low']:.2f} (inside bars seen: {p['inside_bar_count']})")
            self.attempt_entry("PE", p)
            return
        if bar["h"] <= p["mother_high"] and bar["l"] >= p["mother_low"]:
            p["inside_bar_count"] += 1
            log(f"Bar {i} is another inside bar ({p['inside_bar_count']} so far); "
                f"range tightening, still watching mother bar {p['mother_idx']}")
        if p["bars_waited"] >= MAX_BARS_TO_BREAKOUT:
            log(f"Pattern from mother bar {p['mother_idx']} expired after "
                f"{p['bars_waited']} bars with no breakout; dropped")
            self.pending = None

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
    def attempt_entry(self, side, pattern):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return
        buffer_pts = (STOP_BUFFER_PCT / 100.0) * idx_price
        if side == "CE":
            stop_price = pattern["mother_low"] - buffer_pts
        else:
            stop_price = pattern["mother_high"] + buffer_pts
        stop_distance = abs(idx_price - stop_price)
        if stop_distance <= 0:
            log("Entry aborted: zero/negative stop distance")
            return
        stop_pct = stop_distance / idx_price * 100
        if stop_pct > STOP_MAX_PCT:
            log(f"Entry skipped: stop distance {stop_pct:.2f}% of index > cap {STOP_MAX_PCT}%")
            return
        rr_target = idx_price + REWARD_RISK * stop_distance if side == "CE" \
            else idx_price - REWARD_RISK * stop_distance
        target = rr_target
        level = self.session_level(UNDERLYING, INDEX_EXCHANGE, "high" if side == "CE" else "low")
        if level is not None:
            if side == "CE" and level > idx_price:
                target = min(rr_target, level)
            elif side == "PE" and level < idx_price:
                target = max(rr_target, level)
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        price = self.buy(self.option_sym, qty,
                          f"{side} inside-bar breakout | index {idx_price:.2f} stop {stop_price:.2f} "
                          f"target {target:.2f}")
        if price is None:
            return
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
        """Target/stop hits checked against live LTP."""
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
