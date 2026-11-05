"""
VWAP Standard-Deviation Band Reversion Scalp (ATM CE/PE option buying) for OpenAlgo
-----------------------------------------------------------------------------------
A mean-reversion ("fade") scalp. The signal is computed on the underlying
INDEX's 5-minute candles: a session-anchored VWAP and its standard-deviation
bands (+/-1 SD and +/-2 SD). When price stretches to or beyond a 2nd-SD band
and then a candle REJECTS back (closes inside the band again), we fade the
move back toward VWAP. A down-stretch rejection -> BUY an ATM CE (expect a
bounce up); an up-stretch rejection -> BUY an ATM PE (expect a fade down).
The trade itself is an ATM option BUY sized in lots; stops, partial and final
targets are tracked in INDEX points so they match the signal that generated
them (converted to the option leg only at fill time).

Long setup (BUY CE), checked on every newly CLOSED 5m index candle:
  1. A candle's low stretches to or below the lower 2nd-SD band
     (low <= VWAP - SD_ENTRY * sigma).
  2. A rejection candle then closes back UP, above the lower 2nd-SD band
     (close > lower-2SD) and closes up vs its open (close >= open) -> enter
     on that rejection candle's close, targeting a move back toward VWAP.
Short setup (BUY PE) mirrors this at the upper band (high >= VWAP + 2SD,
rejection candle closes back below the upper-2SD band and closes down).

Exit (fixed R/R mean-reversion, tracked in index points):
  - PARTIAL at the 1st-SD band (SD_PARTIAL): sell half, move the stop to
    breakeven on the rest and let the remainder run to VWAP.
  - TARGET (final) = VWAP (the mean). Because VWAP drifts, it is re-read on
    every poll from the current band state.
  - STOP = just beyond the rejection extreme (the stretch low/high), padded
    by STOP_PAD_SD * sigma so a stop sits "just outside" the 2nd-SD band.
  - Square-off at SQUARE_OFF_TIME regardless of P&L.

Range/rotation-day filter (the setup's core precondition):
  Only trade when the day is range-bound, not trending and riding one band.
  Measured with the efficiency ratio (ER) of the last RANGE_ER_LOOKBACK closed
  5m closes: a value <= RANGE_ER_MAX means choppy/rotational (mean-reversion
  friendly). A high ER means a directional/trend day -> no fade entries.
  Toggle with USE_RANGE_FILTER.

Adaptations from the raw research spec (documented, not guesses):
  - The NIFTY/BANKNIFTY spot index has NO traded volume, so a true volume-
    weighted VWAP is impossible. VWAP here is a typical-price VWAP weighted by
    the index's own 5m `volume` field when present, and falls back to an
    unweighted typical-price mean (an anchored VWAP proxy) when volume is 0
    (spec `testable: adapt` / `testable_reason`). SD bands are the standard
    deviation of typical price around that VWAP.
  - "Let it run only if structure supports continuation" is not numerically
    defined in the sources; implemented as the simple, testable default of
    targeting VWAP for the remainder after the 1SD partial (no discretionary
    runner).
  - "Range-bound/rotation day" filter is the efficiency ratio (a standard,
    testable rotation measure); the sources describe it only qualitatively.
  - Daily safety caps (max trades, daily max loss / profit) are not in the
    source rules; same pattern as nifty_direction_ce_pe.py.

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
STRATEGY = "VwapSdBandReversionScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 2                      # lots per entry; needs >=2 to scale out half at the 1SD partial
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"        # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"       # VWAP/bands are anchored to the session open
WARMUP_BARS = 6               # need this many closed bars before VWAP/SD is meaningful (~30 min)
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

SD_ENTRY = 2.0                # stretch band that must be touched/exceeded (2nd SD)
SD_PARTIAL = 1.0              # band where half the position is taken off (1st SD)
STOP_PAD_SD = 0.25            # stop sits this many sigma beyond the stretch extreme ("just outside" 2SD)
REQUIRE_REJECTION_BODY = True # the rejection candle must also close in the reversion direction (close vs open)

STOP_MAX_PCT = 0.6            # skip the trade if the stop distance exceeds this % of the index price

USE_RANGE_FILTER = True       # only fade on range/rotation days (the setup's core precondition)
RANGE_ER_LOOKBACK = 10        # efficiency ratio lookback in closed 5m candles
RANGE_ER_MAX = 0.45           # ER at or below this = rotational/choppy -> fade allowed; above = trend day

POLL_SEC = 20                 # main loop tick while idle/monitoring

# ---- daily safety caps (not in the source rules; same pattern as nifty_direction_ce_pe.py) ----
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

        self.bars = []          # list of {t, o, h, l, c, v} completed 5m index candles
        self.vwap = []          # session VWAP aligned with self.bars
        self.sigma = []         # SD of typical price around VWAP, aligned with self.bars
        self.last_bar_t = None   # timestamp cursor, not a list index: self.bars is rebuilt wholesale by refresh_bars()

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
        # Session-anchored VWAP of typical price (volume-weighted when volume is
        # available; unweighted typical-price mean otherwise, since the spot index
        # has no traded volume -- see the module docstring) and the running SD of
        # typical price around that VWAP (the band width).
        vwap, sigma = [], []
        cum_w = cum_wp = cum_wp2 = 0.0
        for b in self.bars:
            tp = (b["h"] + b["l"] + b["c"]) / 3.0
            w = b["v"] if b["v"] > 0 else 1.0
            cum_w += w
            cum_wp += w * tp
            cum_wp2 += w * tp * tp
            mean = cum_wp / cum_w if cum_w > 0 else tp
            var = (cum_wp2 / cum_w - mean * mean) if cum_w > 0 else 0.0
            vwap.append(mean)
            sigma.append(var ** 0.5 if var > 0 else 0.0)
        self.vwap = vwap
        self.sigma = sigma

    def efficiency_ratio(self, i):
        """Kaufman efficiency ratio of the last RANGE_ER_LOOKBACK closed 5m closes
        ending at bar i. Near 0 = rotational/choppy, near 1 = clean trend. Returns
        None until there are enough bars."""
        n = RANGE_ER_LOOKBACK
        if i < n:
            return None
        closes = [b["c"] for b in self.bars[i - n:i + 1]]
        net = abs(closes[-1] - closes[0])
        noise = sum(abs(closes[k] - closes[k - 1]) for k in range(1, len(closes)))
        if noise <= 0:
            return None
        return net / noise

    def range_day_ok(self, i):
        if not USE_RANGE_FILTER:
            return True
        er = self.efficiency_ratio(i)
        if er is None:
            return False   # not enough data yet -> stay out
        ok = er <= RANGE_ER_MAX
        if not ok:
            log(f"Range filter: ER {er:.2f} > {RANGE_ER_MAX} (trend day) -> no fade at bar {i}")
        return ok

    # ---------- setup detection ----------
    def detect_long_setup(self, i):
        """Lower-band stretch then rejection back up -> fade long (CE)."""
        b, mid, sd = self.bars[i], self.vwap[i], self.sigma[i]
        if sd <= 0:
            return None
        lower2 = mid - SD_ENTRY * sd
        if b["l"] > lower2:
            return None                       # never stretched to the 2SD band
        if b["c"] <= lower2:
            return None                       # did not reject back inside the band
        if REQUIRE_REJECTION_BODY and b["c"] < b["o"]:
            return None                       # rejection candle must close up
        stop_price = b["l"] - STOP_PAD_SD * sd
        return {"stop": stop_price, "mid": mid, "sd": sd}

    def detect_short_setup(self, i):
        """Upper-band stretch then rejection back down -> fade short (PE)."""
        b, mid, sd = self.bars[i], self.vwap[i], self.sigma[i]
        if sd <= 0:
            return None
        upper2 = mid + SD_ENTRY * sd
        if b["h"] < upper2:
            return None
        if b["c"] >= upper2:
            return None
        if REQUIRE_REJECTION_BODY and b["c"] > b["o"]:
            return None
        stop_price = b["h"] + STOP_PAD_SD * sd
        return {"stop": stop_price, "mid": mid, "sd": sd}

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        for i, b in enumerate(self.bars):
            if self.last_bar_t is not None and b["t"] <= self.last_bar_t:
                continue
            self.handle_new_bar(i)
            self.last_bar_t = b["t"]

    def handle_new_bar(self, i):
        if self.position:
            return   # management is LTP-driven (manage_position); VWAP target re-read live
        if i < WARMUP_BARS:
            return
        now = self.now()
        if now < self.at(SESSION_START) or now >= self.at(NO_NEW_ENTRY_AFTER):
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log("Daily trade/loss cap reached; no further entries today")
                self._cap_logged = True
            return
        if DAILY_PROFIT_TARGET and self.day_pnl >= DAILY_PROFIT_TARGET:
            return
        if not self.range_day_ok(i):
            return
        setup = self.detect_long_setup(i)
        if setup:
            self.attempt_entry("CE", i, setup)
            return
        setup = self.detect_short_setup(i)
        if setup:
            self.attempt_entry("PE", i, setup)

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
    def attempt_entry(self, side, i, setup):
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return
        stop_price = setup["stop"]
        stop_distance = abs(idx_price - stop_price)
        if stop_distance <= 0:
            log("Entry aborted: zero stop distance")
            return
        stop_pct = stop_distance / idx_price * 100
        if stop_pct > STOP_MAX_PCT:
            log(f"Entry skipped: stop distance {stop_pct:.2f}% of index > cap {STOP_MAX_PCT}%")
            return
        mid, sd = setup["mid"], setup["sd"]
        # Partial at the 1SD band, final target at VWAP (the mean), in index points.
        if side == "CE":
            partial = mid - SD_PARTIAL * sd
        else:
            partial = mid + SD_PARTIAL * sd
        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        price = self.buy(self.option_sym, qty,
                         f"{side} VWAP {SD_ENTRY}SD-band fade | index {idx_price:.2f} "
                         f"VWAP {mid:.2f} SD {sd:.2f} stop {stop_price:.2f}")
        if price is None:
            return
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty, "remaining_qty": qty,
            "entry_premium": price, "index_entry": idx_price, "stop": stop_price,
            "partial_target": partial, "partial_done": False, "bar": i,
        }
        self.trades_today += 1
        log(f"ENTER {side} | index {idx_price:.2f} stop {stop_price:.2f} ({stop_pct:.2f}%) "
            f"partial@1SD {partial:.2f} target=VWAP | trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

    def current_vwap(self):
        return self.vwap[-1] if self.vwap else None

    def partial_exit(self, reason):
        pos = self.position
        half = (pos["qty"] // 2 // self.lot) * self.lot
        if half <= 0:
            self.exit_position(reason)
            return
        px = self.sell(pos["symbol"], half, reason)
        if px is None:
            return
        p = (px - pos["entry_premium"]) * half
        self.day_pnl += p
        pos["remaining_qty"] -= half
        pos["partial_done"] = True
        pos["stop"] = pos["index_entry"]   # breakeven on the remainder
        log(f"PARTIAL EXIT {reason} | qty {half} @ {px} | pnl {p:+.0f} | "
            f"remaining {pos['remaining_qty']} | stop -> breakeven, running to VWAP")

    def exit_position(self, reason):
        pos = self.position
        qty = pos["remaining_qty"]
        px = self.sell(pos["symbol"], qty, reason)
        if px is None:
            log("Exit failed; position may still be open - check manually")
            return
        p = (px - pos["entry_premium"]) * qty
        self.day_pnl += p
        log(f"EXIT {pos['side']} {reason} | qty {qty} @ {px} | pnl {p:+.0f} | day P&L {self.day_pnl:+.0f}")
        self.position = None

    def manage_position(self):
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        pos = self.position
        vwap = self.current_vwap()
        if pos["side"] == "CE":
            if idx_ltp <= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} <= {pos['stop']:.2f})")
            elif not pos["partial_done"] and idx_ltp >= pos["partial_target"]:
                self.partial_exit(f"1SD partial (index {idx_ltp:.2f} >= {pos['partial_target']:.2f})")
            elif vwap is not None and idx_ltp >= vwap:
                self.exit_position(f"VWAP target reached (index {idx_ltp:.2f} >= VWAP {vwap:.2f})")
        else:
            if idx_ltp >= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} >= {pos['stop']:.2f})")
            elif not pos["partial_done"] and idx_ltp <= pos["partial_target"]:
                self.partial_exit(f"1SD partial (index {idx_ltp:.2f} <= {pos['partial_target']:.2f})")
            elif vwap is not None and idx_ltp <= vwap:
                self.exit_position(f"VWAP target reached (index {idx_ltp:.2f} <= VWAP {vwap:.2f})")

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
