"""
Engulfing Candle Reversal Scalp -- ATM CE/PE option buying for OpenAlgo
----------------------------------------------------------------------
A price-action reversal scalp. The signal is a two-candle "engulfing"
pattern on the underlying INDEX's own 5-minute candles. The trade itself is
an ATM option BUY: a BULLISH engulfing -> CE long; a BEARISH engulfing ->
PE long (the spec's "PE short" is expressed as buying the PE, keeping this
a one-sided long-premium strategy -- same convention as the sibling scalps).

Signal vs execution split (same pattern as pin_bar_rejection_reversal_
scalp.py / previous_day_high_low_breakout_scalp.py / nifty_direction_ce_pe.py):
the engulfing geometry, stop and target are all measured in INDEX points on
completed 5-minute candles; the stop/target are then monitored against the
live index LTP and converted to rupee P&L through the option fill price at
entry.

Engulfing geometry (prior bar p, current bar b; bodies only):
    p_top = max(p.open, p.close) ; p_bot = min(p.open, p.close)
    b_top = max(b.open, b.close) ; b_bot = min(b.open, b.close)
  Bullish engulfing (CE candidate):
    prior candle is red  (p.close < p.open)
    current candle is green (b.close > b.open)
    current body fully engulfs the prior body: b_bot <= p_bot and b_top >= p_top
    current body >= ENGULF_BODY_RATIO x prior body ("large" engulfing candle)
  Bearish engulfing (PE candidate): the mirror image (prior green, current
  red, current body engulfs the prior body).

Entry: ENTRY_MODE = "engulf_close" (default) enters at market on the CLOSE
of the engulfing candle (the completed signal bar). The spec's alternative
"enter on a break above the engulfing candle's high / below its low" is a
discretionary intrabar trigger and is NOT implemented; documented here as an
omission. One trade at a time: no new engulfing pattern is acted on while a
position is open.

Stop: for longs, just below the LOW of the engulfing candle with a buffer of
STOP_ATR_BUFFER x ATR (spec: "about 0.5 ATR buffer"); for shorts, just above
its HIGH. ATR is ATR(ATR_PERIOD) on 5-minute candles (prior days fetched once
to warm it up, since a single session has too few 5m bars for ATR(14) early
in the day -- see knowledge/strategy_patterns.md 2026-10-20). If ATR is
unavailable the buffer falls back to STOP_BUFFER_PTS index points.

Target: fixed reward:risk = REWARD_RISK x the initial stop distance from the
entry index price (default 1:2, per the spec params). The spec's "or VWAP /
prior intraday swing" is a discretionary exit that is not cleanly testable
from OHLCV and is NOT implemented (documented omission). The "prefer setups
at prior support/resistance or after a pullback" filter is likewise
discretionary and omitted -- the engulfing geometry is the testable core.

Exit: stop or target, whichever the live index LTP hits first (checked every
poll, not only on candle close, since real stop/target fills happen
intrabar), or a hard square-off at SQUARE_OFF_TIME regardless of P&L.
NO_NEW_ENTRY_AFTER, SQUARE_OFF_TIME, MAX_TRADES_PER_DAY and the DAILY_MAX_LOSS
safety cap are not part of the source rules -- they are the same defensive
pattern used in the other strategies.

Added 2026-11-01 (user), all off by default: MAX_LOSS (rupee loss limit per trade on
the option premium), CONFIRM_CANDLES (N more completed candles must close beyond the
engulfing close the trade's way, else the signal is dropped; the stop/target still come
from the engulfing candle), REVERSE_ON_STOP (after a stop / max loss, buy REVERSE_LOTS of
the opposite option: stop = the first trade's entry level, target = REWARD_RISK x that
distance, REVERSE_MAX_LOSS; a reversal is never reversed again).

Run first with OpenAlgo Analyzer (paper) mode ON. This script places real
orders once Analyzer is switched off -- do that only after a satisfactory
backtest and with the user's explicit go-ahead.
"""

import os
import sys
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openalgo import api
from script_openalgo.strategy_lib import regime  # noqa: E402

# ======================= PARAMETERS =======================
API_KEY = os.getenv("OPENALGO_API_KEY", "PASTE_YOUR_API_KEY_HERE")
HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
STRATEGY = "EngulfingCandleReversalScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1                      # lots per entry
PRODUCT = "MIS"

BAR_MINUTES = 5               # engulfing timeframe
TIMEFRAME = f"{BAR_MINUTES}m"
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

# ---- engulfing geometry ----
ENGULF_BODY_RATIO = 1.0       # current body must be >= this x the prior body (1.0 = merely engulfs; raise for "large")
MIN_RANGE_PTS = 0.0           # ignore patterns whose engulfing candle range is below this many index points (0 = off)

ENTRY_MODE = "engulf_close"   # "engulf_close" only (the "break of high/low" variant is not implemented)

# ---- stop / target ----
STOP_ATR_BUFFER = 0.5         # stop sits this x 5m ATR beyond the engulfing candle's low (long) / high (short)
STOP_BUFFER_PTS = 0.0         # fallback buffer in index points when ATR is unavailable
REWARD_RISK = 2               # target = this x the initial stop distance (spec default 1:2)
ATR_PERIOD = 14               # ATR period on 5-minute candles
WARMUP_LOOKBACK_DAYS = 7      # calendar days of prior 5m candles fetched to warm up the ATR

MAX_TRADES_PER_DAY = 6        # scalp; defensive cap (not a source rule)
POLL_SEC = 20                 # main loop tick while idle/monitoring

# ---- daily safety cap (not in the source rules; same pattern as the other strategies) ----
DAILY_MAX_LOSS = 3000         # NIFTY-sized; resize per instrument (see knowledge/lessons.md)

# ---- added 2026-11-01 (user): limited loss, confirmation candle, reversal mode ----
MAX_LOSS = 0                  # rupees: close a trade when the option is down this much (the index stop can be far); 0 = off
CONFIRM_CANDLES = 0           # wait for N more completed candles before entering; 0 = off
CONFIRM_MODE = "every"        # "every": each of the N candles must close beyond the engulfing close the trade's way;
                              # "last": candles 1..N-1 are ignored, only candle N must close beyond it (user, 2026-11-01)
REVERSE_ON_STOP = True        # when a trade is stopped out, buy the opposite option (a failed engulfing often continues the other way)
REVERSE_LOTS = 2              # lots for the reversal trade
REVERSE_MAX_LOSS = 1500       # rupees: loss limit of the reversal trade (0 = only its index stop: the first trade's entry level)
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

        self.bars = []           # completed BAR_MINUTES-minute index candles for today
        self.last_bar_t = None   # timestamp cursor, not a list index: self.bars is rebuilt wholesale by refresh_bars()

        self.position = None     # see attempt_entry() for shape
        self.option_sym = None
        self.lot = None

        self.trades_today = 0
        self.day_pnl = 0.0
        self._cap_logged = False
        self._warm = None        # (date, [prior-day 5m rows]) ATR warm-up cache
        self.pending = None      # CONFIRM_CANDLES: {"side", "engulf", "n"} waiting for confirming candles

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

    def current_atr(self):
        """ATR(ATR_PERIOD) on 5-minute candles, prior days prepended so it is warm
        even early in the session. Only completed candles are used (no lookahead)."""
        import pandas as pd
        today = self.now().date()
        if self._warm is None or self._warm[0] != today:
            rows = []
            try:
                w = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=TIMEFRAME,
                                    start_date=(today - timedelta(days=WARMUP_LOOKBACK_DAYS)).isoformat(),
                                    end_date=(today - timedelta(days=1)).isoformat(), source=HISTORY_SOURCE)
                if hasattr(w, "iterrows") and len(w):
                    for ts, row in w.iterrows():
                        if ts.to_pydatetime().date() < today:
                            rows.append({"high": float(row["high"]), "low": float(row["low"]),
                                         "close": float(row["close"])})
            except Exception as e:
                log(f"ATR warm-up fetch failed: {e}")
            self._warm = (today, rows)
        rows = list(self._warm[1])
        rows += [{"high": b["h"], "low": b["l"], "close": b["c"]} for b in self.bars]
        if len(rows) < ATR_PERIOD + 1:
            return None
        try:
            return float(regime.atr(pd.DataFrame(rows), ATR_PERIOD).iloc[-1])
        except Exception as e:
            log(f"ATR computation failed: {e}")
            return None

    # ---------- engulfing detection ----------
    @staticmethod
    def classify_engulfing(prev, cur):
        """Return 'CE' for a bullish engulfing (prior red, current green body engulfs it),
        'PE' for a bearish engulfing (prior green, current red body engulfs it), else None."""
        rng = cur["h"] - cur["l"]
        if rng <= 0 or rng < MIN_RANGE_PTS:
            return None
        p_body = abs(prev["c"] - prev["o"])
        c_body = abs(cur["c"] - cur["o"])
        if c_body <= 0 or c_body < ENGULF_BODY_RATIO * p_body:
            return None
        p_top, p_bot = max(prev["o"], prev["c"]), min(prev["o"], prev["c"])
        c_top, c_bot = max(cur["o"], cur["c"]), min(cur["o"], cur["c"])
        if not (c_bot <= p_bot and c_top >= p_top):
            return None
        prev_red = prev["c"] < prev["o"]
        prev_green = prev["c"] > prev["o"]
        cur_green = cur["c"] > cur["o"]
        cur_red = cur["c"] < cur["o"]
        if prev_red and cur_green:
            return "CE"
        if prev_green and cur_red:
            return "PE"
        return None

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        for i, b in enumerate(self.bars):
            if self.last_bar_t is not None and b["t"] <= self.last_bar_t:
                continue
            self.handle_new_bar(i)
            self.last_bar_t = b["t"]

    def handle_new_bar(self, i):
        # one trade at a time; need a prior candle to form an engulfing pattern
        if self.position is not None or i < 1:
            return
        prev, cur = self.bars[i - 1], self.bars[i]
        side = self.classify_engulfing(prev, cur)
        if side:
            tag = "bullish" if side == "CE" else "bearish"
            log(f"ENGULFING {tag} at {cur['t']:%H:%M} O{cur['o']:.1f} H{cur['h']:.1f} "
                f"L{cur['l']:.1f} C{cur['c']:.1f} (prior {prev['t']:%H:%M} "
                f"O{prev['o']:.1f} C{prev['c']:.1f})")
            if CONFIRM_CANDLES:                        # a new pattern replaces a pending one
                self.pending = {"side": side, "engulf": cur, "n": 0}
                log(f"{side} engulfing at {cur['t']:%H:%M}: waiting for {CONFIRM_CANDLES} confirming candle(s)")
            elif self.entry_allowed(side):
                self.attempt_entry(side, cur)
            return
        p = self.pending
        if p:
            beyond = cur["c"] > p["engulf"]["c"] if p["side"] == "CE" else cur["c"] < p["engulf"]["c"]
            if CONFIRM_MODE == "last" and p["n"] + 1 < CONFIRM_CANDLES:
                p["n"] += 1                            # "last": the candles before the N-th are not checked
                return
            if not beyond:
                log(f"{p['side']} engulfing from {p['engulf']['t']:%H:%M} NOT confirmed at {cur['t']:%H:%M} "
                    f"(close {cur['c']:.1f} vs {p['engulf']['c']:.1f}); dropped")
                self.pending = None
                return
            p["n"] += 1
            if p["n"] >= CONFIRM_CANDLES:
                self.pending = None
                log(f"{p['side']} engulfing from {p['engulf']['t']:%H:%M} confirmed at {cur['t']:%H:%M}")
                if self.entry_allowed(p["side"]):
                    self.attempt_entry(p["side"], p["engulf"])

    def entry_allowed(self, side):
        if self.now() >= self.at(NO_NEW_ENTRY_AFTER):
            log(f"{side} engulfing but past NO_NEW_ENTRY_AFTER; skipped")
            return False
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log("Daily trade/loss cap reached; no further entries today")
                self._cap_logged = True
            return False
        return True

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
    def attempt_entry(self, side, engulf):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log(f"{side} entry aborted: no index LTP")
            return
        atr = self.current_atr()
        buffer_pts = STOP_ATR_BUFFER * atr if atr else STOP_BUFFER_PTS
        if not atr:
            log(f"ATR unavailable; using fallback stop buffer {STOP_BUFFER_PTS:.2f} pts")
        initial_stop = engulf["l"] - buffer_pts if side == "CE" else engulf["h"] + buffer_pts
        stop_distance = idx_price - initial_stop if side == "CE" else initial_stop - idx_price
        if stop_distance <= 0:
            log(f"{side} entry aborted: non-positive stop distance ({stop_distance:.2f})")
            return
        target = idx_price + REWARD_RISK * stop_distance if side == "CE" \
            else idx_price - REWARD_RISK * stop_distance
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        price = self.buy(self.option_sym, qty,
                         f"{side} engulfing (bar {engulf['t']:%H:%M}) | index {idx_price:.2f} "
                         f"stop {initial_stop:.2f} target {target:.2f}")
        if price is None:
            return
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty,
            "entry_premium": price, "index_entry": idx_price,
            "stop": initial_stop, "target": target, "reversal": False, "max_loss": MAX_LOSS,
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

    def manage_position(self):
        pos = self.position
        if pos.get("max_loss"):                        # MAX_LOSS / REVERSE_MAX_LOSS on the option premium
            opx = self.ltp(pos["symbol"], OPT_EXCHANGE)
            if opx is not None:
                p = (opx - pos["entry_premium"]) * pos["qty"]
                if p <= -pos["max_loss"]:
                    self.stopped(f"max loss {p:+.0f} <= -{pos['max_loss']}")
                    return
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        if pos["side"] == "CE":
            if idx_ltp <= pos["stop"]:
                self.stopped(f"stop hit (index {idx_ltp:.2f} <= {pos['stop']:.2f})")
            elif idx_ltp >= pos["target"]:
                self.exit_position(f"target hit (index {idx_ltp:.2f} >= {pos['target']:.2f})")
        else:
            if idx_ltp >= pos["stop"]:
                self.stopped(f"stop hit (index {idx_ltp:.2f} >= {pos['stop']:.2f})")
            elif idx_ltp <= pos["target"]:
                self.exit_position(f"target hit (index {idx_ltp:.2f} <= {pos['target']:.2f})")

    def stopped(self, reason):
        """Exit on a stop / max loss; with REVERSE_ON_STOP a first trade is followed by the opposite option."""
        pos = self.position
        self.exit_position(reason)
        if self.position is not None or not REVERSE_ON_STOP or pos["reversal"]:
            return
        side = "PE" if pos["side"] == "CE" else "CE"
        if not self.entry_allowed(side):
            return
        idx = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx is None:
            return
        stop = pos["index_entry"]                      # back at the first trade's entry = the reversal was wrong
        dist = (idx - stop) if side == "CE" else (stop - idx)   # the reversal needs the index past the first entry
        if dist <= 0:
            log(f"reversal skipped: index {idx:.2f} is not beyond the first entry {stop:.2f}")
            return
        target = idx + REWARD_RISK * dist if side == "CE" else idx - REWARD_RISK * dist
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"reversal: ATM resolution failed: {e}")
            return
        qty = self.lot * REVERSE_LOTS
        price = self.buy(self.option_sym, qty, f"REVERSAL {side} after the {pos['side']} stop | index {idx:.2f} "
                                                f"stop {stop:.2f} target {target:.2f} | {REVERSE_LOTS} lot(s)")
        if price is None:
            return
        self.position = {"side": side, "symbol": self.option_sym, "qty": qty, "entry_premium": price, "index_entry": idx,
                         "stop": stop, "target": target, "reversal": True, "max_loss": REVERSE_MAX_LOSS}
        self.trades_today += 1

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
