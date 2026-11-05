"""
Manual2: single-leg ATM CE/PE direction trades with fixed profit booking (NIFTY) for OpenAlgo
---------------------------------------------------------------------------------------------
Based on nifty_direction_ce_pe.py (same entry rule), changed by the user on 2026-10-29:
  1. book the profit at every +PROFIT_TARGET, then check whether to continue the same side or change the side
  2. no multi-leg (no reversal leg, no riding past the target)
  3. exit a wrong entry at -MAX_LOSS
  4. no new trades between BLACKOUT_START and BLACKOUT_END (11:00-12:45)

Entry (as in nifty_direction_ce_pe.py): each cycle locks the ATM CE and PE and takes a reading every
READING_INTERVAL_MIN minutes. When the last RISING_READINGS + 1 readings of one option are strictly rising and the
other's are not -> BUY that option (LOTS lots). If both rise, the index move decides; if neither, roll the window.

In the trade (checked every MONITOR_INTERVAL_SEC):
  P&L >= +PROFIT_TARGET -> SELL (book). Then a fresh reading is taken and the last CONTINUE_READINGS + 1 readings
      decide at once: the same option still rising -> BUY the same side again (fresh ATM strike); the other option
      rising -> BUY the other side; neither -> a normal new cycle (cooldown, fresh readings).
  P&L <= -MAX_LOSS (capped by the day's remaining loss budget) -> SELL: wrong entry; cooldown, new cycle.
  SQUARE_OFF_TIME -> SELL, stop for the day.
  A trade open at BLACKOUT_START keeps its target and stop (BLACKOUT_EXIT = True exits it at BLACKOUT_START instead).

SR_EXIT (user, 2026-10-29: "exit nearby the support / resistance"): while in profit (SR_ONLY_IN_PROFIT) and the
index has moved the trade's way since the entry, exit when it comes within SR_EXIT_PTS of the next level in the
trade's direction: CE -> the nearest resistance above, PE -> the nearest support below. Levels: "oi" = the largest CE
OI strike above / PE OI strike below the index (chain refreshed every reading), "pivots" = previous day's pivot, CPR,
R1-R3, S1-S3, high and low, "both" = the nearer one. Default "pivots" (best in the 2026-10-29 test). A level exit counts like a stop exit (cooldown,
no continuation).

After the live run of 2026-10-29 (booked +1241 at 13:35, bought the same strike again 6 s later on a "rising" reading
taken 24 s after the previous one, then -1040; two entries right under a pivot sold within 25 s):
  - the continuation check takes a fresh reading only when the last one is older than FRESH_READING_MIN (2 min)
  - SR_MIN_ROOM: skip an entry (also a continuation) when the next level in the trade's direction is closer than this
  - AFTER_TARGET = "hold": at +PROFIT_TARGET with the direction holding, keep the trade (no second spread and charges)
    and lock a floor (LOCK_PCT 50: +1000 -> +500, +2000 -> +1500, ...); exit at the floor, when a new reading shows
    the other side rising, or as before. "rebook" = sell and buy again (the original rule).

After the live run of 2026-10-30 (NIFTY PE hit +1034 at 10:10, "hold" raised the floor to only +500 - a 50 % give-back
of the step reached - then the premium fell to +341 before the floor caught it; booked +247 instead of 1000+):
  - AFTER_TARGET back to "rebook" (default): the target always books the trade, no floor give-back
  - REBOOK_TARGET_PCT: a continuation re-entry right after a booked target (after_target) uses this % of
    PROFIT_TARGET as its own target instead of the full PROFIT_TARGET (the move may already be partly spent)
  - the re-entry direction check (continuation()) already requires the last reading to be older than
    FRESH_READING_MIN (2 min, already past the 1 min minimum needed) before it looks again, so a re-entry decided
    on a reading only a few seconds old (like 2026-10-29's 24 s one) still cannot happen

Readings during the blackout are not used: entry readings start again at BLACKOUT_END. Stops for the day at
MAX_TRADES, DAILY_MAX_LOSS, DAILY_PROFIT_TARGET (if set) or NO_NEW_ENTRY_AFTER.
PROFIT_TARGET / MAX_LOSS are NIFTY-sized rupees: resize per instrument (options panel: Save for <instrument>).

Run it first with OpenAlgo Analyzer (paper) mode ON.
"""

import os
import time
from datetime import datetime, timedelta
from openalgo import api

from script_openalgo.strategy_lib import levels  # noqa: E402

# ======================= PARAMETERS =======================
API_KEY = os.getenv("OPENALGO_API_KEY", "PASTE_YOUR_API_KEY_HERE")
HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
STRATEGY = "Manual2"

UNDERLYING = "NIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

START_TIME = "09:30"
NO_NEW_ENTRY_AFTER = "14:30"
SQUARE_OFF_TIME = "15:15"
BLACKOUT_START = "11:00"    # no new trades from here ... ("" = no blackout)
BLACKOUT_END = "12:45"      # ... until here
BLACKOUT_EXIT = False       # True = also exit an open trade at BLACKOUT_START

READING_INTERVAL_MIN = 5    # direction readings
READINGS_ON_CLOCK_GRID = False  # readings at START_TIME + n x READING_INTERVAL_MIN (09:30, 09:35, ...), whenever started
MONITOR_INTERVAL_SEC = 15   # P&L checks while in a trade
RISING_READINGS = 3         # entry: x0<x1<x2<x3 -> 3 consecutive rises
CONTINUE_READINGS = 2       # after booking: x0<x1<x2 of the latest readings decides same side / other side
FRESH_READING_MIN = 2       # the continuation check takes a new reading only if the last one is older than this

PROFIT_TARGET = 1000        # rupees: book the profit here
MAX_LOSS = 1000             # rupees: wrong entry, exit here

SR_EXIT = "pivots"          # exit near the next support / resistance in the trade's direction: "off" | "oi" (largest
                            # CE OI strike above / PE OI strike below the index) | "pivots" (previous day's pivot, CPR,
                            # R1-R3, S1-S3, high, low) | "both" (the nearer of the two)
SR_EXIT_PTS = 15            # exit when the index is within this many points of that level
SR_ONLY_IN_PROFIT = True    # only when the trade is in profit (a losing trade keeps its stop)
SR_OI_WITHIN = 300          # OI levels: strikes within this many points of the index

SR_MIN_ROOM = 30            # skip an entry when the next level in the trade's direction (SR_EXIT levels) is closer than
                            # this many points (no room to run); 0 = off

AFTER_TARGET = "rebook"     # at +PROFIT_TARGET: "rebook" = sell (book the full target), then after_target() checks
                            # continuation for a fresh entry; "hold" = keep the trade past the target instead of
                            # booking and lock a floor (raising the floor every PROFIT_TARGET) - gives back more of
                            # the peak than a clean "rebook" + continuation re-entry does
LOCK_PCT = 50               # hold only: floor = the profit step reached - (100 - LOCK_PCT) % of PROFIT_TARGET
                            # (+1000 -> floor +500, +2000 -> floor +1500)
REBOOK_TARGET_PCT = 60      # rebook: the profit target on a continuation re-entry (after_target), as a % of
                            # PROFIT_TARGET (smaller target since the move may already be partly spent)

MAX_TRADES = 8              # entries in one day (continuations count)
DAILY_MAX_LOSS = 3000       # stop for the day when realised loss reaches this
DAILY_PROFIT_TARGET = 0     # stop for the day at this realised profit (0 = off)
COOLDOWN_MIN = 5            # wait after a stop-loss / no-continuation exit before new readings
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
        self.day_pnl = 0.0          # realised P&L
        self.trades = 0
        self.cycle = 0
        self.last_side = None       # side of the last closed leg (continue / change side)
        self.pivots = None          # SR_EXIT pivots: previous day's levels, loaded once
        self.walls = {}             # SR_EXIT oi: {"support", "resistance"} from the latest chain
        self.levels = {}            # the level the open trade is heading for (shown on the Signals chart)
        self.reset_cycle()

    def reset_cycle(self):
        self.ce_sym = self.pe_sym = None
        self.lot = None
        self.readings = []          # list of dicts: t, nifty, ce, pe
        self.leg = None             # {symbol, side, qty, price}
        self.blackout_logged = False

    # ---------- time helpers ----------
    def at(self, s):
        h, m = hhmm(s)
        return self.now().replace(hour=h, minute=m, second=0, microsecond=0)

    def wait_until(self, target):
        while self.now() < target:
            self.sleep(min(30, max(1, (target - self.now()).total_seconds())))

    def in_blackout(self, t=None):
        if not BLACKOUT_START or not BLACKOUT_END:
            return False
        t = t or self.now()
        return self.at(BLACKOUT_START) <= t < self.at(BLACKOUT_END)

    def next_grid(self, after, strictly=False):
        """The next reading time: START_TIME + n x READING_INTERVAL_MIN at or after `after` (strictly after when asked).
        READINGS_ON_CLOCK_GRID off: counted from `after` itself."""
        base = self.at(START_TIME)
        if not READINGS_ON_CLOCK_GRID:
            return after.replace(second=0, microsecond=0) + (timedelta(minutes=READING_INTERVAL_MIN) if strictly else timedelta(0))
        if after < base or (after == base and not strictly):
            return base
        step = READING_INTERVAL_MIN * 60
        n = -(-int((after - base).total_seconds()) // step)          # ceil
        t = base + timedelta(seconds=n * step)
        return t + timedelta(seconds=step) if strictly and t <= after else t

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

    def setup_symbols(self):
        exp = self.nearest_expiry()
        info = {}
        for ot in ("CE", "PE"):
            r = self.c.optionsymbol(underlying=UNDERLYING, exchange=INDEX_EXCHANGE,
                                    expiry_date=exp, offset="ATM", option_type=ot)
            if r.get("status") != "success":
                raise RuntimeError(f"optionsymbol failed: {r}")
            info[ot] = r
        self.ce_sym, self.pe_sym = info["CE"]["symbol"], info["PE"]["symbol"]
        self.lot = int(info["CE"]["lotsize"])
        log(f"Expiry {exp} | ATM CE {self.ce_sym} | ATM PE {self.pe_sym} | lot {self.lot} | "
            f"{UNDERLYING} {info['CE'].get('underlying_ltp')}")

    def take_reading(self):
        n = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        ce = self.ltp(self.ce_sym, OPT_EXCHANGE)
        pe = self.ltp(self.pe_sym, OPT_EXCHANGE)
        if None in (n, ce, pe):
            log("Reading skipped (data error)")
            return None
        rd = {"t": self.now(), "nifty": n, "ce": ce, "pe": pe}
        self.readings.append(rd)
        log(f"READING #{len(self.readings) - 1} {UNDERLYING} {n:.2f} | CE {ce:.2f} | PE {pe:.2f}")
        return rd

    @staticmethod
    def rising(values):
        return all(a < b for a, b in zip(values, values[1:]))

    def direction(self, n_rises):
        """'CE', 'PE' or None from the last n_rises + 1 readings (the entry rule of nifty_direction_ce_pe.py)."""
        w = self.readings[-(n_rises + 1):]
        if len(w) < n_rises + 1:
            return None
        ce_up = self.rising([r["ce"] for r in w])
        pe_up = self.rising([r["pe"] for r in w])
        if ce_up and not pe_up:
            return "CE"
        if pe_up and not ce_up:
            return "PE"
        if ce_up and pe_up:
            d = w[-1]["nifty"] - w[0]["nifty"]
            log(f"Both CE and PE rising; {UNDERLYING} change {d:+.2f}")
            return "CE" if d > 0 else ("PE" if d < 0 else None)
        return None

    # ---------- orders ----------
    def fill_price(self, oid):
        try:
            self.sleep(1)
            r = self.c.orderstatus(order_id=oid, strategy=STRATEGY)
            p = float(r.get("data", {}).get("average_price") or 0)
            return p or None
        except Exception:
            return None

    def buy(self, side, reason, target):
        symbol = self.ce_sym if side == "CE" else self.pe_sym
        qty = self.lot * LOTS
        r = self.c.placeorder(strategy=STRATEGY, symbol=symbol, action="BUY", exchange=OPT_EXCHANGE,
                              price_type="MARKET", product=PRODUCT, quantity=qty)
        if r.get("status") != "success":
            log(f"BUY FAILED {symbol}: {r}")
            return False
        price = self.fill_price(r.get("orderid")) or self.ltp(symbol, OPT_EXCHANGE)
        self.leg = {"symbol": symbol, "side": side, "qty": qty, "price": price,
                    "index": self.ltp(UNDERLYING, INDEX_EXCHANGE) if SR_EXIT != "off" else None}
        self.trades += 1
        log(f"BUY {symbol} qty {qty} @ {price} | order {r.get('orderid')} | {reason} | "
            f"target +{target:.0f} stop -{MAX_LOSS} | trade {self.trades}/{MAX_TRADES}")
        return True

    def sell(self, reason):
        leg = self.leg
        r = self.c.placeorder(strategy=STRATEGY, symbol=leg["symbol"], action="SELL", exchange=OPT_EXCHANGE,
                              price_type="MARKET", product=PRODUCT, quantity=leg["qty"])
        ok = r.get("status") == "success"
        px = (self.fill_price(r.get("orderid")) if ok else None) or self.ltp(leg["symbol"], OPT_EXCHANGE) or leg["price"]
        p = (px - leg["price"]) * leg["qty"]
        self.day_pnl += p
        log(f"SELL {leg['symbol']} qty {leg['qty']} @ {px} -> {r.get('status')} {r.get('orderid', r)} | {reason} "
            f"| P&L {p:+.0f} | day P&L {self.day_pnl:+.0f}")
        if not ok:
            log("!!! SELL FAILED - CHECK POSITION MANUALLY IN OPENALGO !!!")
        self.last_side = leg["side"]
        self.leg = None

    def pnl(self):
        ltp = self.ltp(self.leg["symbol"], OPT_EXCHANGE)
        if ltp is None:
            return None
        p = (ltp - self.leg["price"]) * self.leg["qty"]
        log(f"P&L {self.leg['symbol']} {self.leg['price']:.2f}->{ltp:.2f} ({p:+.0f})")
        return p

    # ---------- support / resistance exit ----------
    def load_pivots(self):
        if self.pivots is not None:
            return
        self.pivots = []
        try:
            today = self.now().date()
            d = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="D",
                               start_date=(today - timedelta(days=15)).isoformat(), end_date=today.isoformat(), source="api")
            d = d[[ix.date() < today for ix in d.index]]
            c = levels.cpr_from_daily(d)
            if c:
                self.pivots = sorted({round(c[k], 2) for k in ("s3", "s2", "s1", "bc", "pivot", "tc", "r1", "r2", "r3",
                                                               "prev_high", "prev_low")})
                log("Pivot levels: " + ", ".join(f"{x:.0f}" for x in self.pivots))
        except Exception as e:
            log(f"pivot levels unavailable ({e})")

    def refresh_walls(self, idx):
        try:
            r = self.c.optionchain(underlying=UNDERLYING, exchange=INDEX_EXCHANGE, expiry_date=self.nearest_expiry(),
                                   strike_count=12)
        except Exception as e:
            log(f"optionchain exception: {e}")
            return
        if not r or r.get("status") != "success":
            return
        rows = [(float(x["strike"]), float((x.get("ce") or {}).get("oi") or 0), float((x.get("pe") or {}).get("oi") or 0))
                for x in r.get("chain", []) if abs(float(x["strike"]) - idx) <= SR_OI_WITHIN]
        above = [x for x in rows if x[0] > idx and x[1] > 0]
        below = [x for x in rows if x[0] < idx and x[2] > 0]
        self.walls = {"resistance": max(above, key=lambda x: x[1])[0] if above else None,
                      "support": max(below, key=lambda x: x[2])[0] if below else None}

    def sr_target(self, side, idx):
        """The level the trade is heading for: CE -> the next resistance above the index, PE -> the next support below."""
        cands = []
        if SR_EXIT in ("pivots", "both") and self.pivots:
            cands += [x for x in self.pivots if (x > idx if side == "CE" else x < idx)]
        if SR_EXIT in ("oi", "both"):
            w = self.walls.get("resistance" if side == "CE" else "support")
            if w is not None and (w > idx if side == "CE" else w < idx):
                cands.append(w)
        if not cands:
            return None
        return min(cands) if side == "CE" else max(cands)

    def room_ok(self, side, idx):
        """SR_MIN_ROOM: is there room to the next level in the trade's direction?"""
        if not SR_MIN_ROOM or SR_EXIT == "off":
            return True
        if SR_EXIT in ("pivots", "both"):
            self.load_pivots()
        if SR_EXIT in ("oi", "both"):
            self.refresh_walls(idx)
        lvl = self.sr_target(side, idx)
        if lvl is not None and abs(lvl - idx) < SR_MIN_ROOM:
            log(f"{side} signal skipped: {'resistance' if side == 'CE' else 'support'} {lvl:.0f} only "
                f"{abs(lvl - idx):.0f} pts away (room < {SR_MIN_ROOM})")
            return False
        return True

    def sr_exit_reason(self, p):
        """A reason to exit near support / resistance, or None."""
        leg = self.leg
        if SR_EXIT == "off" or (SR_ONLY_IN_PROFIT and p <= 0):
            return None
        idx = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx is None or leg.get("index") is None:
            return None
        moved = idx > leg["index"] if leg["side"] == "CE" else idx < leg["index"]
        lvl = self.sr_target(leg["side"], idx)
        self.levels = {"exit_level": lvl} if lvl is not None else {}
        if not moved or lvl is None or abs(lvl - idx) > SR_EXIT_PTS:
            return None
        return (f"{'resistance' if leg['side'] == 'CE' else 'support'} {lvl:.0f} near: index {idx:.2f} "
                f"({abs(lvl - idx):.0f} pts, {SR_EXIT}) at {p:+.0f}")

    # ---------- phases ----------
    def entry_block(self):
        """Why a new entry is not allowed now (None = allowed)."""
        if self.trades >= MAX_TRADES:
            return f"max trades ({MAX_TRADES}) done"
        if self.day_pnl <= -DAILY_MAX_LOSS:
            return f"daily max loss reached ({self.day_pnl:+.0f})"
        if DAILY_PROFIT_TARGET and self.day_pnl >= DAILY_PROFIT_TARGET:
            return f"daily profit target reached ({self.day_pnl:+.0f})"
        if self.now() >= self.at(NO_NEW_ENTRY_AFTER):
            return "past new-entry cut-off"
        return None

    def find_entry(self):
        """Readings until a direction: 'CE', 'PE' or None (day over)."""
        next_t = self.next_grid(max(self.now().replace(microsecond=0), self.at(START_TIME)))
        while True:
            self.wait_until(next_t)
            if self.entry_block():
                log(f"No entry: {self.entry_block()}")
                return None
            if self.in_blackout():
                if not self.blackout_logged:
                    log(f"No new trades {BLACKOUT_START}-{BLACKOUT_END}; readings start again at {BLACKOUT_END}")
                    self.blackout_logged = True
                self.readings = []                       # readings across the gap are stale
                self.wait_until(self.at(BLACKOUT_END))
                next_t = self.next_grid(self.now().replace(microsecond=0))
                continue
            self.take_reading()
            next_t += timedelta(minutes=READING_INTERVAL_MIN)
            side = self.direction(RISING_READINGS)
            if side and self.room_ok(side, self.readings[-1]["nifty"]):
                return side
            if len(self.readings) > RISING_READINGS:
                log("No clear direction yet; rolling window")

    def manage(self, target):
        """Hold the single leg until target / stop / square-off. Returns 'target', 'stop', 'blackout' or 'squareoff'."""
        next_reading = self.next_grid(self.now(), strictly=True)
        if SR_EXIT in ("pivots", "both"):
            self.load_pivots()
        if SR_EXIT in ("oi", "both") and self.leg.get("index"):
            self.refresh_walls(self.leg["index"])
        step, floor = target, None                        # AFTER_TARGET hold: next profit step, locked floor
        while True:
            if self.now() >= self.at(SQUARE_OFF_TIME):
                self.sell("square-off time")
                return "squareoff"
            if BLACKOUT_EXIT and self.in_blackout():
                self.sell(f"blackout {BLACKOUT_START}-{BLACKOUT_END}")
                return "blackout"
            if self.now() >= next_reading:
                rd = self.take_reading()                 # readings continue: used for the continue / switch decision
                next_reading += timedelta(minutes=READING_INTERVAL_MIN)
                if rd and SR_EXIT in ("oi", "both"):
                    self.refresh_walls(rd["nifty"])
                if rd and floor is not None:             # holding past the target: has the other side taken over?
                    d = self.direction(CONTINUE_READINGS)
                    if d and d != self.leg["side"]:
                        self.sell(f"holding at the locked floor {floor:+.0f}: direction changed to {d}")
                        return "target"
            p = self.pnl()
            if p is not None:
                if floor is not None and p <= floor:
                    self.sell(f"locked profit: {p:+.0f} <= floor {floor:+.0f}")
                    return "target"
                if p >= step:
                    side = self.leg["side"]
                    if AFTER_TARGET == "hold" and self.entry_block() is None and self.continuation(side) == side:
                        floor = step - target * (100 - LOCK_PCT) / 100
                        log(f"HOLD {side} at {p:+.0f}: direction still holds; floor raised to {floor:+.0f}")
                        step += target
                    else:
                        self.sell(f"profit booked ({p:+.0f} >= {step})")
                        return "target"
                why = self.sr_exit_reason(p)
                if why:
                    self.sell(why)
                    return "level"
                cap = min(MAX_LOSS, DAILY_MAX_LOSS + self.day_pnl)  # never breach the remaining daily loss budget
                if p <= -cap:
                    self.sell(f"wrong entry: loss {p:+.0f} (limit {cap:.0f})")
                    return "stop"
            self.sleep(max(1, min(MONITOR_INTERVAL_SEC, (next_reading - self.now()).total_seconds() + 1)))

    def continuation(self, held):
        """At a profit step: 'CE' / 'PE' rising over the last CONTINUE_READINGS + 1 readings, or None (logged). A fresh
        reading is taken only when the last one is older than FRESH_READING_MIN, so two readings seconds apart do not
        count as a rise."""
        if not self.readings or self.now() - self.readings[-1]["t"] >= timedelta(minutes=FRESH_READING_MIN):
            self.take_reading()
        side = self.direction(CONTINUE_READINGS)
        vals = {k: [round(r[k], 2) for r in self.readings[-(CONTINUE_READINGS + 1):]] for k in ("ce", "pe")}
        if side == held:
            log(f"CONTINUE {held}: still rising {vals[held.lower()]}")
        elif side:
            log(f"CHANGE SIDE {held} -> {side}: {side} rising {vals[side.lower()]}")
        else:
            log(f"No continuation: CE {vals['ce']} PE {vals['pe']}")
        return side

    def after_target(self, held):
        """Right after booking: continue the same side, change the side, or None (new cycle)."""
        if self.entry_block() or self.in_blackout():
            return None
        side = self.continuation(held)
        if side and not self.room_ok(side, self.readings[-1]["nifty"]):
            return None
        return side

    def run(self):
        log(f"Strategy {STRATEGY} starting on {UNDERLYING}")
        try:
            a = self.c.analyzerstatus()
            log(f"Analyzer (paper) mode: {a.get('data', a)}")
        except Exception:
            pass
        self.wait_until(self.at(START_TIME))
        carry = None                                     # side chosen right after a booked profit
        while True:
            stop = self.entry_block()
            if stop:
                log(f"STOP FOR THE DAY: {stop}")
                break
            self.reset_cycle()
            self.cycle += 1
            log(f"===== CYCLE {self.cycle} START | day P&L {self.day_pnl:+.0f} =====")
            self.setup_symbols()                         # fresh ATM strike each cycle
            if carry:
                side, why = carry, f"{carry} after the booked profit"
                target = PROFIT_TARGET * REBOOK_TARGET_PCT / 100
            else:
                side = self.find_entry()
                why = f"{side} rising {RISING_READINGS} readings in a row"
                target = PROFIT_TARGET
            carry = None
            if side is None:
                break
            if not self.buy(side, why, target):
                log("Entry order failed; stopping for safety")
                break
            outcome = self.manage(target)
            log(f"===== CYCLE {self.cycle} END ({outcome}) =====")
            if outcome == "squareoff":
                break
            if outcome == "target":
                carry = self.after_target(self.last_side)
                if carry:
                    continue
            self.wait_until(self.now() + timedelta(minutes=COOLDOWN_MIN))
        log(f"Done for the day. Trades {self.trades} | day P&L {self.day_pnl:+.0f}")


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
