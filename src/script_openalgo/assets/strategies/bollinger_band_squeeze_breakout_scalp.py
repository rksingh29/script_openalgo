"""
Bollinger Band Squeeze Breakout Scalp (ATM CE/PE option buying) for OpenAlgo
-----------------------------------------------------------------------------
Signal is computed on the underlying INDEX's own 5-minute candles (20-period
Bollinger Bands, 2 std dev, population stddev); the trade itself is always an
ATM option BUY sized in lots (CE for an upside breakout, PE for a downside
breakout -- this project always buys options, never shorts them, per
nifty_direction_ce_pe.py / vwap_reclaim_scalp.py / bollinger_band_bounce_scalp.py
convention). Stops and targets are tracked in INDEX points so they match the
signal that generated them, then converted to a lot-sized option P&L at exit.

Setup, evaluated on every newly CLOSED 5m index candle:
  1. Squeeze: this candle's Bollinger Band width (upper - lower, 20-period,
     2 std dev) is at (or ties) its lowest value of the last
     SQUEEZE_LOOKBACK_BARS candles -> the bands are pinched. The bar's width
     is recorded as the "squeeze width" and stays the active reference until
     either a breakout fires or it goes stale (see Adaptations).
  2. Breakout trigger (any candle within SQUEEZE_VALID_BARS of the squeeze
     bar): the candle CLOSES beyond a band --
       - close > upper band  -> LONG (BUY ATM CE)
       - close < lower band  -> SHORT (BUY ATM PE)
     A wick through the band with the close still inside does not count.
  3. Optional confirmation (REQUIRE_VOLUME_CONFIRMATION, off by default -- see
     Adaptations): the breakout candle's volume must be >=
     VOLUME_CONFIRMATION_MULT x the average volume of the candles between the
     squeeze bar and the breakout bar.
Filter: while still inside the squeeze (bands pinched, price inside the
bands), no trade is taken -- entries only fire on the breakout close itself.

Stop (set once, at entry): STOP_WIDTH_PCT (0.5) x the squeeze-time band width,
on the opposite side of the trade (e.g. long stop = entry - 0.5 x squeeze
width).

Targets (index points, both measured from the squeeze width recorded at the
breakout bar):
  - T1 = entry +/- T1_WIDTH_MULT (1.0) x squeeze width -> book ~50% of the
    position (sell the lower of half the lot count or all of it if halving
    would leave 0).
  - T2 = entry +/- T2_WIDTH_MULT (2.0) x squeeze width, OR the nearest
    higher-timeframe support/resistance level beyond entry in the trade's
    direction, whichever is CLOSER to entry -> exit the remainder. The HTF
    S/R levels used are the previous day's Central Pivot Range (BC, pivot,
    TC) via strategy_lib.levels (see Adaptations).
  - Square-off at SQUARE_OFF_TIME regardless of P&L.

Entries stop after NO_NEW_ENTRY_AFTER or once MAX_TRADES_PER_DAY /
DAILY_MAX_LOSS is reached (safety caps, same pattern as the other strategies
in this project; not part of the source rules). Only one position open at a
time; LOTS must be >= 2 to actually scale out half at T1 (odd/1-lot sizing
just exits everything at whichever target is hit first).

Adaptations from the raw research spec (documented, not guesses):
  - Bollinger Band stddev uses the population definition (divide by N, not
    N-1), the same convention as bollinger_band_bounce_scalp.py; the source
    does not specify sample vs population.
  - "Once squeezed ... enter when a candle closes beyond the band" does not
    say how long a squeeze stays valid before a later breakout no longer
    counts as squeeze-driven. SQUEEZE_VALID_BARS (10 bars = 50 min) is a
    testable proxy: a breakout must occur within that many candles of the
    squeeze bar, else the squeeze is treated as stale and a fresh 20-bar-low
    reading is required.
  - "Squeeze range ... measured at the moment of breakout" is read as the
    band width recorded AT THE SQUEEZE BAR (the tightest point found), not
    the breakout bar's own width (which is usually already expanding by the
    time the candle closes beyond the band) -- this is the width that stays
    constant for a given squeeze and is used for both the stop and both
    targets, consistent with "squeeze-time band width" in the stop rule.
  - The spec's optional volume confirmation ("breakout candle volume >= 1.5x
    the average volume of the bars during the squeeze") is implemented but
    OFF by default (REQUIRE_VOLUME_CONFIRMATION = False): the NIFTY/BANKNIFTY
    spot index has no meaningful traded volume of its own (see the spec's own
    testable_reason / knowledge/openalgo_api.md), so the index's `volume`
    field is not a reliable confirmation signal. Flip the flag on only if a
    better volume proxy (e.g. index futures volume) is wired in.
  - "Nearest higher-timeframe support/resistance" has no source-given
    definition; the previous day's Central Pivot Range (BC/pivot/TC, via
    strategy_lib.levels.cpr_from_daily) is used as the HTF S/R set, same
    source as the optional CPR bias filter in nifty_direction_ce_pe.py. If
    the daily history call fails, T2 silently falls back to the fixed
    squeeze-width target.
  - No stop-to-breakeven ratchet after the T1 partial exit: the source only
    specifies a single fixed stop (50% of the squeeze width) and does not
    mention moving it, so it stays at its original level for the runner.
  - STOP_MAX_PCT / MAX_TRADES_PER_DAY / DAILY_MAX_LOSS / DAILY_PROFIT_TARGET
    are safety caps, not sourced from the research material.

Run first with OpenAlgo Analyzer (paper) mode ON. This script places real
orders once Analyzer is switched off -- do that only after a satisfactory
backtest and with the user's explicit go-ahead.
"""

import os
import time
from datetime import datetime, timedelta
from openalgo import api

from script_openalgo.strategy_lib import levels

# ======================= PARAMETERS =======================
API_KEY = os.getenv("OPENALGO_API_KEY", "PASTE_YOUR_API_KEY_HERE")
HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
STRATEGY = "BollingerBandSqueezeBreakoutScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 2                      # lots per entry; needs >=2 to scale out half at target1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"         # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"        # candles are built from the open
START_TIME = "09:15"           # no separate entry delay; BB/squeeze warm-up naturally gates early entries
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

BB_PERIOD = 20                 # Bollinger Band basis (SMA) period
BB_STDDEV = 2                  # Bollinger Band width, in std deviations

SQUEEZE_LOOKBACK_BARS = 20     # BB width must be a new low over this many bars to flag a squeeze
SQUEEZE_VALID_BARS = 10        # ADAPTATION: how long (in 5m bars) a squeeze stays "active" for a breakout

REQUIRE_VOLUME_CONFIRMATION = False   # off by default; see Adaptations (index volume is not meaningful)
VOLUME_CONFIRMATION_MULT = 1.5

T1_WIDTH_MULT = 1.0            # first (partial) target = this x squeeze width
T2_WIDTH_MULT = 2.0            # second (final) target = this x squeeze width, or nearest HTF S/R if closer
STOP_WIDTH_PCT = 0.5           # stop = this x squeeze width, opposite side of the trade

STOP_MAX_PCT = 0.6             # skip the trade if stop distance exceeds this % of index price (safety cap)

POLL_SEC = 20                  # main loop tick while idle/monitoring

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


def bollinger_series(closes, period, stddev_mult):
    """(basis, upper, lower) aligned 1:1 with closes; None until index (period-1).
    Population standard deviation (divide by N), the common BB convention."""
    n = len(closes)
    basis = [None] * n
    upper = [None] * n
    lower = [None] * n
    if n < period:
        return basis, upper, lower
    for i in range(period - 1, n):
        window = closes[i - period + 1:i + 1]
        mean = sum(window) / period
        var = sum((x - mean) ** 2 for x in window) / period
        std = var ** 0.5
        basis[i] = mean
        upper[i] = mean + stddev_mult * std
        lower[i] = mean - stddev_mult * std
    return basis, upper, lower


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.bars = []             # completed 5m index candles: {t, o, h, l, c, v}
        self.basis = []
        self.upper = []
        self.lower = []
        self.bw = []                # band width (upper - lower), aligned with bars
        self.processed_bars = 0

        self.squeeze_idx = None     # index of the latest "20-bar-low BB width" bar
        self.squeeze_width = None   # band width recorded at that bar

        self.position = None        # see attempt_entry() for shape
        self.option_sym = None
        self.lot = None

        self.cpr = None             # previous-day CPR (lazy-loaded); {} means "tried, unavailable"

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

    def load_cpr(self):
        """Previous-day Central Pivot Range, used as the HTF S/R set for target2. Lazy, once per day."""
        if self.cpr is not None:
            return
        try:
            today = self.now().date()
            d = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="D",
                                start_date=(today - timedelta(days=15)).isoformat(),
                                end_date=today.isoformat(), source=HISTORY_SOURCE)
            d = d[[ix.date() < today for ix in d.index]]
            if len(d):
                self.cpr = levels.cpr_from_daily(d)
                log(f"CPR BC {self.cpr['bc']:.1f} pivot {self.cpr['pivot']:.1f} TC {self.cpr['tc']:.1f} "
                    f"(HTF S/R set for target2)")
            else:
                self.cpr = {}
        except Exception as e:
            log(f"CPR unavailable ({e}); target2 will use the fixed squeeze-width target only")
            self.cpr = {}

    def nearest_htf_target(self, side, entry, t2_price):
        """Returns (target_price, note). note is 'HTF S/R' when a CPR level is used instead of the fixed T2."""
        levels_list = [(self.cpr or {}).get(k) for k in ("bc", "pivot", "tc")]
        levels_list = [v for v in levels_list if v is not None]
        if not levels_list:
            return t2_price, None
        if side == "CE":
            candidates = [v for v in levels_list if v > entry]
            if not candidates:
                return t2_price, None
            nearest = min(candidates)
            return (nearest, "HTF S/R") if nearest < t2_price else (t2_price, None)
        else:
            candidates = [v for v in levels_list if v < entry]
            if not candidates:
                return t2_price, None
            nearest = max(candidates)
            return (nearest, "HTF S/R") if nearest > t2_price else (t2_price, None)

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
        self.basis, self.upper, self.lower = bollinger_series(closes, BB_PERIOD, BB_STDDEV)
        self.bw = [(u - l) if (u is not None and l is not None) else None
                   for u, l in zip(self.upper, self.lower)]

    # ---------- squeeze / breakout detection ----------
    def update_squeeze(self, i):
        bw_i = self.bw[i]
        if bw_i is None:
            return
        lo = i - SQUEEZE_LOOKBACK_BARS + 1
        if lo < 0:
            return
        window = self.bw[lo:i + 1]
        if None in window:
            return
        if bw_i <= min(window):
            self.squeeze_idx = i
            self.squeeze_width = bw_i
            log(f"Squeeze detected at bar {i} ({self.bars[i]['t']:%H:%M}); BB width {bw_i:.2f} "
                f"({SQUEEZE_LOOKBACK_BARS}-bar low) -> watching for a breakout close")

    def detect_breakout(self, i):
        """Returns 'CE'/'PE' if bar i is a valid, still-fresh squeeze breakout close, else None."""
        if self.squeeze_idx is None:
            return None
        if i - self.squeeze_idx > SQUEEZE_VALID_BARS:
            log(f"Squeeze from bar {self.squeeze_idx} is stale at bar {i}; waiting for a fresh squeeze")
            self.squeeze_idx = None
            self.squeeze_width = None
            return None
        bar = self.bars[i]
        upper_i, lower_i = self.upper[i], self.lower[i]
        if upper_i is None or lower_i is None:
            return None
        if bar["c"] > upper_i:
            side = "CE"
        elif bar["c"] < lower_i:
            side = "PE"
        else:
            return None
        if REQUIRE_VOLUME_CONFIRMATION:
            squeeze_bars = self.bars[self.squeeze_idx:i] or [self.bars[self.squeeze_idx]]
            avg_vol = sum(b["v"] for b in squeeze_bars) / len(squeeze_bars)
            if avg_vol <= 0 or bar["v"] < VOLUME_CONFIRMATION_MULT * avg_vol:
                log(f"{side} breakout at bar {i} skipped: volume {bar['v']:.0f} < "
                    f"{VOLUME_CONFIRMATION_MULT}x squeeze avg {avg_vol:.0f}")
                return None
        return side

    # ---------- bar-driven state machine ----------
    def process_new_bars(self):
        while self.processed_bars < len(self.bars):
            self.handle_new_bar(self.processed_bars)
            self.processed_bars += 1

    def handle_new_bar(self, i):
        self.update_squeeze(i)
        if self.position:
            return   # stop/targets are checked continuously against LTP (manage_position_ltp)
        if not self.in_time_window():
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log("Daily trade/loss cap reached; no further entries today")
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
        idx_price = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_price is None:
            log("Entry aborted: no index LTP")
            return
        squeeze_width = self.squeeze_width
        if not squeeze_width or squeeze_width <= 0:
            log("Entry aborted: no valid squeeze width recorded")
            return

        stop_distance = STOP_WIDTH_PCT * squeeze_width
        if stop_distance <= 0:
            log("Entry aborted: zero stop distance")
            return
        if side == "CE":
            stop_price = idx_price - stop_distance
            target1 = idx_price + T1_WIDTH_MULT * squeeze_width
            target2_raw = idx_price + T2_WIDTH_MULT * squeeze_width
        else:
            stop_price = idx_price + stop_distance
            target1 = idx_price - T1_WIDTH_MULT * squeeze_width
            target2_raw = idx_price - T2_WIDTH_MULT * squeeze_width

        stop_pct = stop_distance / idx_price * 100
        if stop_pct > STOP_MAX_PCT:
            log(f"Entry skipped: stop distance {stop_pct:.2f}% of index > cap {STOP_MAX_PCT}%")
            return

        self.load_cpr()
        target2, sr_note = self.nearest_htf_target(side, idx_price, target2_raw)

        try:
            self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        price = self.buy(self.option_sym, qty,
                          f"{side} squeeze breakout (squeeze width {squeeze_width:.2f}) | "
                          f"index {idx_price:.2f} stop {stop_price:.2f} target1 {target1:.2f} "
                          f"target2 {target2:.2f}" + (f" ({sr_note})" if sr_note else ""))
        if price is None:
            return
        self.position = {
            "side": side, "symbol": self.option_sym, "qty": qty, "remaining_qty": qty,
            "entry_premium": price, "index_entry": idx_price, "stop": stop_price,
            "target1": target1, "target2": target2, "partial_done": False,
        }
        self.trades_today += 1
        self.squeeze_idx = None     # consume the squeeze; next trade needs a fresh 20-bar-low reading
        self.squeeze_width = None
        log(f"ENTER {side} | index {idx_price:.2f} stop {stop_price:.2f} ({stop_pct:.2f}%) | "
            f"trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

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
        log(f"PARTIAL EXIT {reason} | qty {half} @ {px} | pnl {p:+.0f} | remaining {pos['remaining_qty']}")

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

    def manage_position_ltp(self):
        """Stop and both targets checked against live LTP (can happen intrabar)."""
        if not self.position:
            return
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
        pos = self.position
        if pos["side"] == "CE":
            if idx_ltp <= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} <= {pos['stop']:.2f})")
            elif not pos["partial_done"] and idx_ltp >= pos["target1"]:
                self.partial_exit(f"target1 hit (index {idx_ltp:.2f} >= {pos['target1']:.2f})")
            elif pos["partial_done"] and idx_ltp >= pos["target2"]:
                self.exit_position(f"target2 hit (index {idx_ltp:.2f} >= {pos['target2']:.2f})")
        else:
            if idx_ltp >= pos["stop"]:
                self.exit_position(f"stop hit (index {idx_ltp:.2f} >= {pos['stop']:.2f})")
            elif not pos["partial_done"] and idx_ltp <= pos["target1"]:
                self.partial_exit(f"target1 hit (index {idx_ltp:.2f} <= {pos['target1']:.2f})")
            elif pos["partial_done"] and idx_ltp <= pos["target2"]:
                self.exit_position(f"target2 hit (index {idx_ltp:.2f} <= {pos['target2']:.2f})")

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
