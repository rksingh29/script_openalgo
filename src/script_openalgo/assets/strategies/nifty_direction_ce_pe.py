"""
Nifty ATM CE/PE direction strategy for OpenAlgo
------------------------------------------------
09:30  Start cycle 1. Each cycle: lock the ATM strike, record CE/PE premium and Nifty level.
Every 5 min take a reading. When the last 4 readings (base + 3) of one
option are strictly rising and the other's are not -> BUY that option.
If both rise, Nifty's direction decides. If neither, roll the window.

While in a trade (checked every minute):
  cycle P&L >= +PROFIT_TARGET              -> exit all, cycle ends; or, with RIDE_TREND,
      keep holding while every new completed 5-min Nifty candle makes a
      higher high (CE position) / lower low (PE position); exit on the first
      candle that does not, or if cycle P&L falls below the ride floor: the higher of RIDE_FLOOR_PCT % of the
      target (100 = never give back below the target) and the best P&L of the ride minus RIDE_TRAIL_PCT %
      (checked every RIDE_CHECK_SEC seconds while riding)
  cycle P&L <= -MAX_LOSS                   -> exit all, cycle ends
  cycle P&L <= -REVERSAL_LOSS              -> BUY REVERSAL_LOTS lots (default 2) of the
      opposite option, but only if that option's last 3 five-minute readings
      are rising (keeps the first leg open; at most MAX_REVERSALS times), then
      hold both legs until the cycle P&L reaches +PROFIT_TARGET (max loss and
      square-off still apply)
      REVERSAL_IN_RANGE = "exit": when the cycle first reaches -REVERSAL_LOSS and the market is range-bound
      (RANGE_DETECTOR), exit the cycle instead of reversing (chop makes both legs lose). The range check is
      logged at every such point ("RANGE CHECK ..."), also when the setting is off.
      REVERSAL_PENDING_MAX_MIN: if the opposite option still hasn't shown RISING_READINGS rises this many
      minutes after the cycle first reached -REVERSAL_LOSS, exit the cycle instead of waiting indefinitely
      for the confirmation (a stuck, unconfirmed leg otherwise rides all the way to MAX_LOSS or square-off);
      0 = off (old behaviour).
  SQUARE_OFF_TIME                          -> exit all, stop for the day

After a cycle closes, wait COOLDOWN_MIN, then start a new cycle with a
fresh ATM strike. Stops for the day at MAX_CYCLES, DAILY_MAX_LOSS,
DAILY_PROFIT_TARGET (if set) or NO_NEW_ENTRY_AFTER.

Run it first with OpenAlgo Analyzer (paper) mode ON.
"""

import os
import sys
import time
from datetime import datetime, timedelta
from openalgo import api

from script_openalgo.strategy_lib import levels  # noqa: E402
from script_openalgo.strategy_lib import regime  # noqa: E402

# ======================= PARAMETERS =======================
API_KEY = os.getenv("OPENALGO_API_KEY", "PASTE_YOUR_API_KEY_HERE")
HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
STRATEGY = "NiftyDirectionCEPE"

UNDERLYING = "NIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

START_TIME = "09:30"
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"

READING_INTERVAL_MIN = 5   # direction readings
READINGS_ON_CLOCK_GRID = False # readings at fixed times START_TIME + n x READING_INTERVAL_MIN (09:30, 09:35, ...), whenever
                               # the process started, so Signals, the trade manager and backtests read at the same moments
MONITOR_INTERVAL_SEC = 60  # P&L checks while in a trade
RISING_READINGS = 3        # x0<x1<x2<x3 -> 3 consecutive rises

PROFIT_TARGET = 1000
REVERSAL_LOSS = 1000
MAX_LOSS = 2500
MAX_REVERSALS = 1           # per cycle
RIDE_TREND = True           # at PROFIT_TARGET keep riding while 5-min Nifty candles make higher highs (CE) / lower lows (PE)
RIDE_CANDLE_MIN = 5         # candle size for the higher-high / lower-low check
RIDE_FLOOR_PCT = 100        # while riding, exit if cycle P&L drops below this % of PROFIT_TARGET (100 = keep at least the target)
RIDE_TRAIL_PCT = 0          # while riding, exit if cycle P&L falls this % below its best value so far; 0 = off (30 helped BANKNIFTY, hurt NIFTY)
RIDE_CHECK_SEC = 15         # P&L check interval while riding (fast expiry-day premiums move a lot in a minute)
REVERSAL_LOTS = 2           # lots bought on the reversal leg (opposite option); 1 = same size as the first leg
REVERSAL_IN_RANGE = "reverse"  # at -REVERSAL_LOSS in a detected range: "reverse" (as before) or "exit" the cycle
RANGE_DETECTOR = "box"      # "box" | "er" | "box_or_er" | "box_and_er" (see RANGE_BOX_ATR / RANGE_ER_BELOW)
RANGE_BOX_ATR = 1.5         # box: index high-low since the cycle's entry is below this x ATR(14) of 5-min candles
RANGE_ER_BELOW = 0.30       # er: efficiency ratio (10 x 5-min candles) below this = noisy, no direction
REVERSAL_PENDING_MAX_MIN = 0  # exit the cycle if the opposite option still hasn't shown RISING_READINGS rises
                               # this many minutes after first hitting -REVERSAL_LOSS; 0 = off, wait indefinitely

# ---- multiple cycles per day ----
MAX_CYCLES = 5              # max trade cycles in one day
DAILY_MAX_LOSS = 3000       # stop for the day when realised loss reaches this
DAILY_PROFIT_TARGET = 0     # stop for the day at this realised profit (0 = off)
COOLDOWN_MIN = 5            # wait after a cycle closes before next readings

# ---- optional direction filter (off by default = behaviour unchanged) ----
BIAS_FILTER = "off"         # "off" | "cpr" (price vs Central Pivot Range) | "cpr_pcr" (also Put-Call Ratio from the option chain)
PCR_WITHIN_POINTS = 250     # PCR uses strikes within +/- this many index points of ATM (a backtest only has the cached strikes)
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
        self.day_pnl = 0.0          # realised P&L across cycles
        self.cycle = 0
        self.reset_cycle()

    def reset_cycle(self):
        self.ce_sym = self.pe_sym = None
        self.lot = None
        self.readings = []          # list of dicts: t, nifty, ce, pe
        self.legs = []              # list of dicts: symbol, qty, price
        self.reversals = 0
        self.done = False
        self.pending_logged = False
        self.entry_t = None         # time of the cycle's first leg (range box starts here)
        self.range_checked = False  # the range check runs once per cycle, at the first -REVERSAL_LOSS
        self.reversal_pending_since = None  # time of the first -REVERSAL_LOSS breach this cycle (REVERSAL_PENDING_MAX_MIN)
        self.cpr = None             # previous-day CPR, loaded on first use (BIAS_FILTER)
        self.prev_cpr = None
        self.expiry_str = None
        self.riding = False         # RIDE_TREND: past the profit target, following the trend
        self.ride_peak = 0.0        # best cycle P&L while riding (RIDE_TRAIL_PCT)
        self.ride_side = None
        self.ride_seen = None       # start time of the last candle already checked

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
        r = self.c.expiry(symbol=UNDERLYING, exchange=OPT_EXCHANGE,
                          instrumenttype="options")
        if r.get("status") != "success" or not r.get("data"):
            raise RuntimeError(f"expiry lookup failed: {r}")
        # API returns e.g. "21-OCT-26"; optionsymbol wants "21OCT26"
        return r["data"][0].replace("-", "").upper()

    def setup_symbols(self):
        exp = self.nearest_expiry()
        self.expiry_str = exp       # used by the optional PCR filter
        info = {}
        for ot in ("CE", "PE"):
            r = self.c.optionsymbol(underlying=UNDERLYING, exchange=INDEX_EXCHANGE,
                                    expiry_date=exp, offset="ATM", option_type=ot)
            if r.get("status") != "success":
                raise RuntimeError(f"optionsymbol failed: {r}")
            info[ot] = r
        self.ce_sym, self.pe_sym = info["CE"]["symbol"], info["PE"]["symbol"]
        self.lot = int(info["CE"]["lotsize"])
        log(f"Expiry {exp} | ATM CE {self.ce_sym} | ATM PE {self.pe_sym} | "
            f"lot {self.lot} | Nifty {info['CE'].get('underlying_ltp')}")

    def take_reading(self):
        n = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        ce = self.ltp(self.ce_sym, OPT_EXCHANGE)
        pe = self.ltp(self.pe_sym, OPT_EXCHANGE)
        if None in (n, ce, pe):
            log("Reading skipped (data error)")
            return None
        rd = {"t": self.now(), "nifty": n, "ce": ce, "pe": pe}
        self.readings.append(rd)
        log(f"READING #{len(self.readings)-1} Nifty {n:.2f} | CE {ce:.2f} | PE {pe:.2f}")
        return rd

    @staticmethod
    def rising(values):
        return all(a < b for a, b in zip(values, values[1:]))

    # ---------- orders ----------
    def buy(self, symbol, reason, lots=None):
        qty = self.lot * (lots or LOTS)
        r = self.c.placeorder(strategy=STRATEGY, symbol=symbol, action="BUY",
                              exchange=OPT_EXCHANGE, price_type="MARKET",
                              product=PRODUCT, quantity=qty)
        if r.get("status") != "success":
            log(f"BUY FAILED {symbol}: {r}")
            return False
        oid = r.get("orderid")
        price = self.fill_price(oid) or self.ltp(symbol, OPT_EXCHANGE)
        nifty = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if not self.legs:
            self.entry_t = self.now()
        self.legs.append({"symbol": symbol, "qty": qty, "price": price})
        log(f"BUY {symbol} qty {qty} @ {price} | order {oid} | Nifty {nifty} | {reason}")
        return True

    def fill_price(self, oid):
        try:
            self.sleep(1)
            r = self.c.orderstatus(order_id=oid, strategy=STRATEGY)
            p = float(r.get("data", {}).get("average_price") or 0)
            return p or None
        except Exception:
            return None

    def exit_all(self, reason):
        realised = 0.0
        for leg in self.legs:
            r = self.c.placeorder(strategy=STRATEGY, symbol=leg["symbol"], action="SELL",
                                  exchange=OPT_EXCHANGE, price_type="MARKET",
                                  product=PRODUCT, quantity=leg["qty"])
            ok = r.get("status") == "success"
            px = (self.fill_price(r.get("orderid")) if ok else None) \
                or self.ltp(leg["symbol"], OPT_EXCHANGE) or leg["price"]
            p = (px - leg["price"]) * leg["qty"]
            realised += p
            log(f"SELL {leg['symbol']} qty {leg['qty']} @ {px} -> "
                f"{r.get('status')} {r.get('orderid', r)} | leg P&L {p:+.0f}")
            if not ok:
                log("!!! SELL FAILED - CHECK POSITION MANUALLY IN OPENALGO !!!")
        self.day_pnl += realised
        log(f"EXIT ALL (cycle {self.cycle}): {reason} | cycle P&L {realised:+.0f} "
            f"| day P&L {self.day_pnl:+.0f}")
        self.done = True

    def pnl(self):
        total = 0.0
        parts = []
        for leg in self.legs:
            ltp = self.ltp(leg["symbol"], OPT_EXCHANGE)
            if ltp is None:
                return None
            p = (ltp - leg["price"]) * leg["qty"]
            total += p
            parts.append(f"{leg['symbol']} {leg['price']:.2f}->{ltp:.2f} ({p:+.0f})")
        log("P&L " + " | ".join(parts) + f" | TOTAL {total:+.0f}")
        return total

    def next_grid(self, after, strictly=False):
        """The next reading time: START_TIME + n x READING_INTERVAL_MIN at or after `after` (strictly after when asked).
        READINGS_ON_CLOCK_GRID off: the old behaviour, counted from `after` itself."""
        base = self.at(START_TIME)
        if not READINGS_ON_CLOCK_GRID:
            return after.replace(second=0, microsecond=0) + (timedelta(minutes=READING_INTERVAL_MIN) if strictly else timedelta(0))
        if after < base or (after == base and not strictly):
            return base
        step = READING_INTERVAL_MIN * 60
        n = -(-int((after - base).total_seconds()) // step)          # ceil
        t = base + timedelta(seconds=n * step)
        return t + timedelta(seconds=step) if strictly and t <= after else t

    def sleep_until_or(self, seconds, next_reading):
        """Sleep `seconds`, but wake up for the next reading time."""
        self.sleep(max(1, min(seconds, (next_reading - self.now()).total_seconds() + 1)))

    # ---------- phases ----------
    def find_entry(self):
        """Returns 'CE', 'PE' or None (day over)."""
        next_t = self.next_grid(max(self.now().replace(microsecond=0), self.at(START_TIME)))
        while True:
            self.wait_until(next_t)
            if self.now() >= self.at(NO_NEW_ENTRY_AFTER):
                log("No entry signal before cut-off; no trade today")
                return None
            self.take_reading()
            next_t += timedelta(minutes=READING_INTERVAL_MIN)
            w = self.readings[-(RISING_READINGS + 1):]
            if len(w) < RISING_READINGS + 1:
                continue
            ce_up = self.rising([r["ce"] for r in w])
            pe_up = self.rising([r["pe"] for r in w])
            side = None
            if ce_up and not pe_up:
                side = "CE"
            elif pe_up and not ce_up:
                side = "PE"
            elif ce_up and pe_up:
                d = w[-1]["nifty"] - w[0]["nifty"]
                log(f"Both CE and PE rising; Nifty change {d:+.2f}")
                side = "CE" if d > 0 else ("PE" if d < 0 else None)
            if side and self.bias_allows(side, w[-1]["nifty"]):
                return side
            if side is None:
                log("No clear direction yet; rolling window")

    def bias_allows(self, side, nifty):
        """Optional CPR / PCR direction filter (BIAS_FILTER). A signal against a clear bias is skipped; 'neutral' allows both."""
        if BIAS_FILTER == "off":
            return True
        try:
            if self.cpr is None:                          # CPR from the previous COMPLETED daily bar, computed once per day
                today = self.now().date()
                d = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="D",
                                   start_date=(today - timedelta(days=15)).isoformat(), end_date=today.isoformat(), source="api")
                d = d[[ix.date() < today for ix in d.index]]
                self.cpr = levels.cpr_from_daily(d)
                self.prev_cpr = levels.cpr(*d.iloc[-2][["high", "low", "close"]]) if len(d) >= 2 else None
                log(f"CPR BC {self.cpr['bc']:.1f} pivot {self.cpr['pivot']:.1f} TC {self.cpr['tc']:.1f} width {self.cpr['width_pct']:.2f}%")
            pcr = None
            if BIAS_FILTER == "cpr_pcr":
                ch = self.c.optionchain(underlying=UNDERLYING, exchange=INDEX_EXCHANGE, expiry_date=self.expiry_str)
                pcr = levels.pcr_from_chain(ch, within_points=PCR_WITHIN_POINTS)
            b = levels.market_bias(nifty, self.cpr, pcr=pcr, prev_c=self.prev_cpr)
        except Exception as e:                            # a data problem must not stop trading: fall back to no filter
            log(f"bias filter unavailable ({e}); not filtering")
            return True
        ok = b["bias"] == "neutral" or (side == "CE" and b["bias"] == "bullish") or (side == "PE" and b["bias"] == "bearish")
        log(f"Bias {b['bias']} ({'; '.join(b['reasons'])}) -> {side} {'allowed' if ok else 'BLOCKED'}")
        return ok

    def candles(self):
        """Completed RIDE_CANDLE_MIN-minute Nifty candles of today (a candle still forming is dropped)."""
        today = self.now().date().isoformat()
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=f"{RIDE_CANDLE_MIN}m",
                                start_date=today, end_date=today, source="api")
        except Exception as e:
            log(f"candle fetch failed: {e}")
            return None
        if not hasattr(df, "index") or len(df) == 0:
            return None
        now = self.now()
        done = [ix for ix in df.index if ix.to_pydatetime().replace(tzinfo=None) + timedelta(minutes=RIDE_CANDLE_MIN) <= now]
        return df.loc[done]

    def range_check(self):
        """Is the market range-bound right now? Cycle box (index high-low since the entry vs ATR of 5-min candles, the
        previous days' candles included for the ATR warm-up) and efficiency ratio; completed candles only. Logged; a data
        problem counts as 'not range' (behaviour as before)."""
        import pandas as pd
        try:
            today = self.now().date()
            if getattr(self, "_warm5", None) is None or self._warm5[0] != today:
                w = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="5m",
                                   start_date=(today - timedelta(days=7)).isoformat(),
                                   end_date=(today - timedelta(days=1)).isoformat(), source="api")
                w = w[[ix.date() < today for ix in w.index]] if hasattr(w, "index") and len(w) else None
                self._warm5 = (today, w)
            c5 = self.candles()                              # today's completed 5-min candles
            parts = [x for x in (self._warm5[1], c5) if x is not None and len(x)]
            if not parts:
                raise ValueError("no 5-min candles")
            df = pd.concat(parts)[["open", "high", "low", "close"]]
            df.index = [ix.to_pydatetime().replace(tzinfo=None) for ix in df.index]
            atr = float(regime.atr(df, 14).iloc[-1])
            er = regime.efficiency_ratio(df["close"], 10).iloc[-1]
            er = None if pd.isna(er) else float(er)
            m1 = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="1m",
                                start_date=today.isoformat(), end_date=today.isoformat(), source="api")
            now = self.now()
            box = None
            if hasattr(m1, "iterrows") and len(m1):
                t = [ix.to_pydatetime().replace(tzinfo=None) for ix in m1.index]
                keep = [(tt >= self.entry_t.replace(second=0, microsecond=0) and tt + timedelta(minutes=1) <= now) for tt in t]
                seg = m1[keep]
                if len(seg):
                    box = float(seg["high"].max() - seg["low"].min())
        except Exception as e:
            log(f"RANGE CHECK unavailable ({e}); treated as not range-bound")
            return False
        box_atr = box / atr if box is not None and atr else None
        is_box = box_atr is not None and box_atr < RANGE_BOX_ATR
        is_er = er is not None and er < RANGE_ER_BELOW
        verdict = {"box": is_box, "er": is_er, "box_or_er": is_box or is_er, "box_and_er": is_box and is_er}.get(RANGE_DETECTOR, False)
        log(f"RANGE CHECK: box {box if box is None else round(box, 1)} pts = {box_atr if box_atr is None else round(box_atr, 2)} x ATR "
            f"{atr:.1f} (range < {RANGE_BOX_ATR}) | ER {er if er is None else round(er, 2)} (range < {RANGE_ER_BELOW}) "
            f"-> {RANGE_DETECTOR}: {'RANGE' if verdict else 'no range'}"
            + ("" if REVERSAL_IN_RANGE == "exit" else " (REVERSAL_IN_RANGE off: logged only)"))
        self.range_state = {"box_atr": None if box_atr is None else round(box_atr, 2), "er": None if er is None else round(er, 2),
                            "range": verdict}
        return verdict

    def ride(self, p):
        """Past the profit target: True = keep holding, False = exit (exit_all already called)."""
        if not self.riding:
            self.riding = True
            self.ride_peak = p
            self.ride_side = "CE" if self.legs[-1]["symbol"].endswith("CE") else "PE"
            c = self.candles()
            self.ride_seen = c.index[-1] if c is not None and len(c) else None
            log(f"profit target reached ({p:+.0f}): riding the {self.ride_side} trend while {RIDE_CANDLE_MIN}-min candles make "
                f"{'higher highs' if self.ride_side == 'CE' else 'lower lows'}")
            return True
        self.ride_peak = max(self.ride_peak, p)
        floor = PROFIT_TARGET * RIDE_FLOOR_PCT / 100
        trail = self.ride_peak * (1 - RIDE_TRAIL_PCT / 100) if RIDE_TRAIL_PCT else None
        if trail is not None and trail > floor:
            floor, why = trail, f"{RIDE_TRAIL_PCT}% below the ride's best {self.ride_peak:+.0f}"
        else:
            why = f"{RIDE_FLOOR_PCT}% of the target"
        if p < floor:
            self.exit_all(f"ride: profit fell below the floor ({p:+.0f} < {floor:.0f}, {why})")
            return False
        c = self.candles()
        if c is None or len(c) < 2 or (self.ride_seen is not None and c.index[-1] <= self.ride_seen):
            return True                                   # no new completed candle yet
        last, prev = c.iloc[-1], c.iloc[-2]
        self.ride_seen = c.index[-1]
        if self.ride_side == "CE":
            ok, what = last["high"] > prev["high"], f"high {last['high']:.2f} vs {prev['high']:.2f}"
        else:
            ok, what = last["low"] < prev["low"], f"low {last['low']:.2f} vs {prev['low']:.2f}"
        if ok:
            log(f"ride: {'higher high' if self.ride_side == 'CE' else 'lower low'} ({what}), holding at {p:+.0f}")
            return True
        self.exit_all(f"ride ended: no {'higher high' if self.ride_side == 'CE' else 'lower low'} ({what}) at {p:+.0f}")
        return False

    def manage(self):
        holding = {leg["symbol"] for leg in self.legs}
        next_reading = self.next_grid(self.now(), strictly=True) if READINGS_ON_CLOCK_GRID else             self.now() + timedelta(minutes=READING_INTERVAL_MIN)
        while not self.done:
            if self.now() >= self.at(SQUARE_OFF_TIME):
                self.exit_all("square-off time")
                break
            new_reading = False
            if self.now() >= next_reading:
                new_reading = self.take_reading() is not None
                next_reading += timedelta(minutes=READING_INTERVAL_MIN)
            p = self.pnl()
            if p is not None:
                if self.riding or p >= PROFIT_TARGET:
                    if not RIDE_TREND:
                        self.exit_all(f"profit target hit ({p:+.0f})")
                        break
                    if not self.ride(p):
                        break
                    self.sleep_until_or(RIDE_CHECK_SEC, next_reading) if READINGS_ON_CLOCK_GRID else self.sleep(RIDE_CHECK_SEC)
                    continue
                # never let one cycle breach the remaining daily loss budget
                cap = min(MAX_LOSS, DAILY_MAX_LOSS + self.day_pnl)
                if p <= -cap:
                    self.exit_all(f"max loss hit ({p:+.0f}, limit {cap:.0f})")
                    break
                if p <= -REVERSAL_LOSS and self.reversals < MAX_REVERSALS:
                    if self.reversal_pending_since is None:
                        self.reversal_pending_since = self.now()
                    elif REVERSAL_PENDING_MAX_MIN and self.now() - self.reversal_pending_since >= timedelta(minutes=REVERSAL_PENDING_MAX_MIN):
                        self.exit_all(f"reversal still pending after {REVERSAL_PENDING_MAX_MIN} min ({p:+.0f})")
                        break
                if p <= -REVERSAL_LOSS and self.reversals < MAX_REVERSALS and not self.range_checked:
                    self.range_checked = True
                    in_range = self.range_check()
                    if in_range and REVERSAL_IN_RANGE == "exit":
                        self.exit_all(f"range-bound at {p:+.0f}: exit instead of reversing ({RANGE_DETECTOR})")
                        break
                if (p <= -REVERSAL_LOSS and self.reversals < MAX_REVERSALS
                        and (new_reading or not self.pending_logged)):
                    self.pending_logged = True
                    opp = self.pe_sym if self.ce_sym in holding else self.ce_sym
                    key = "pe" if opp == self.pe_sym else "ce"
                    vals = [r[key] for r in self.readings[-RISING_READINGS:]]
                    if len(vals) == RISING_READINGS and self.rising(vals):
                        if self.buy(opp, f"reversal: loss {p:+.0f}, {key.upper()} rising {vals}, {REVERSAL_LOTS} lot(s)", REVERSAL_LOTS):
                            self.reversals += 1
                            holding.add(opp)
                    else:
                        log(f"Reversal pending: {key.upper()} not rising {vals}")
            self.sleep_until_or(MONITOR_INTERVAL_SEC, next_reading) if READINGS_ON_CLOCK_GRID else self.sleep(MONITOR_INTERVAL_SEC)

    def run(self):
        log(f"Strategy {STRATEGY} starting")
        try:
            a = self.c.analyzerstatus()
            log(f"Analyzer (paper) mode: {a.get('data', a)}")
        except Exception:
            pass
        self.wait_until(self.at(START_TIME))
        while True:
            stop = self.day_stop_reason()
            if stop:
                log(f"STOP FOR THE DAY: {stop}")
                break
            self.reset_cycle()
            self.cycle += 1
            log(f"===== CYCLE {self.cycle} START | day P&L {self.day_pnl:+.0f} =====")
            self.setup_symbols()            # fresh ATM strike each cycle
            side = self.find_entry()
            if side is None:
                break
            sym = self.ce_sym if side == "CE" else self.pe_sym
            if not self.buy(sym, f"{side} rising {RISING_READINGS} readings in a row"):
                log("Entry order failed; stopping for safety")
                break
            self.manage()
            log(f"===== CYCLE {self.cycle} END | reversals {self.reversals} =====")
            self.wait_until(self.now() + timedelta(minutes=COOLDOWN_MIN))
        log(f"Done for the day. Cycles {self.cycle} | day P&L {self.day_pnl:+.0f}")

    def day_stop_reason(self):
        if self.cycle >= MAX_CYCLES:
            return f"max cycles ({MAX_CYCLES}) done"
        if self.day_pnl <= -DAILY_MAX_LOSS:
            return f"daily max loss reached ({self.day_pnl:+.0f})"
        if DAILY_PROFIT_TARGET and self.day_pnl >= DAILY_PROFIT_TARGET:
            return f"daily profit target reached ({self.day_pnl:+.0f})"
        if self.now() >= self.at(NO_NEW_ENTRY_AFTER):
            return "past new-entry cut-off"
        return None


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
