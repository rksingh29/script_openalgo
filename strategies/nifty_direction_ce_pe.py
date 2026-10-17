"""
Nifty ATM CE/PE direction strategy for OpenAlgo
------------------------------------------------
09:30  Start cycle 1. Each cycle: lock the ATM strike, record CE/PE premium and Nifty level.
Every 5 min take a reading. When the last 4 readings (base + 3) of one
option are strictly rising and the other's are not -> BUY that option.
If both rise, Nifty's direction decides. If neither, roll the window.

While in a trade (checked every minute):
  cycle P&L >= +PROFIT_TARGET              -> exit all, cycle ends
  cycle P&L <= -MAX_LOSS                   -> exit all, cycle ends
  cycle P&L <= -REVERSAL_LOSS              -> BUY the opposite option, but
      only if that option's last 3 five-minute readings are rising
      (keeps the first leg open; at most MAX_REVERSALS times)
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
MONITOR_INTERVAL_SEC = 60  # P&L checks while in a trade
RISING_READINGS = 3        # x0<x1<x2<x3 -> 3 consecutive rises

PROFIT_TARGET = 1000
REVERSAL_LOSS = 1000
MAX_LOSS = 2500
MAX_REVERSALS = 1           # per cycle

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
        self.cpr = None             # previous-day CPR, loaded on first use (BIAS_FILTER)
        self.prev_cpr = None
        self.expiry_str = None

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
    def buy(self, symbol, reason):
        qty = self.lot * LOTS
        r = self.c.placeorder(strategy=STRATEGY, symbol=symbol, action="BUY",
                              exchange=OPT_EXCHANGE, price_type="MARKET",
                              product=PRODUCT, quantity=qty)
        if r.get("status") != "success":
            log(f"BUY FAILED {symbol}: {r}")
            return False
        oid = r.get("orderid")
        price = self.fill_price(oid) or self.ltp(symbol, OPT_EXCHANGE)
        nifty = self.ltp(UNDERLYING, INDEX_EXCHANGE)
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

    # ---------- phases ----------
    def find_entry(self):
        """Returns 'CE', 'PE' or None (day over)."""
        next_t = max(self.now().replace(second=0, microsecond=0), self.at(START_TIME))
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

    def manage(self):
        holding = {leg["symbol"] for leg in self.legs}
        next_reading = self.now() + timedelta(minutes=READING_INTERVAL_MIN)
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
                if p >= PROFIT_TARGET:
                    self.exit_all(f"profit target hit ({p:+.0f})")
                    break
                # never let one cycle breach the remaining daily loss budget
                cap = min(MAX_LOSS, DAILY_MAX_LOSS + self.day_pnl)
                if p <= -cap:
                    self.exit_all(f"max loss hit ({p:+.0f}, limit {cap:.0f})")
                    break
                if (p <= -REVERSAL_LOSS and self.reversals < MAX_REVERSALS
                        and (new_reading or not self.pending_logged)):
                    self.pending_logged = True
                    opp = self.pe_sym if self.ce_sym in holding else self.ce_sym
                    key = "pe" if opp == self.pe_sym else "ce"
                    vals = [r[key] for r in self.readings[-RISING_READINGS:]]
                    if len(vals) == RISING_READINGS and self.rising(vals):
                        if self.buy(opp, f"reversal: loss {p:+.0f}, {key.upper()} rising {vals}"):
                            self.reversals += 1
                            holding.add(opp)
                    else:
                        log(f"Reversal pending: {key.upper()} not rising {vals}")
            self.sleep(MONITOR_INTERVAL_SEC)

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
