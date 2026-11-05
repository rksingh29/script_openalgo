"""
Manual1: option SELLING at open-interest support / resistance (NIFTY) for OpenAlgo
-----------------------------------------------------------------------------------
Built from the user's own manual trades on 2026-10-28 (NIFTY, 03NOV26 weekly): PEs were SOLD near the day's low, which
held exactly on the biggest PE open interest (22500, low 22497.70 at 10:22); CEs were SOLD near the day's high, which
stopped under the biggest CE open interest (22800, high 22778.05 at 13:00); in the afternoon range between the two walls
both sides were short at once. Buy-backs came after the premium fell about 20 %.

Levels, refreshed on every newly CLOSED 5-minute index candle from the option chain (strikes within OI_WITHIN_POINTS):
  support    = the strike at or below the index with the largest PE open interest (PE "wall")
  resistance = the strike at or above the index with the largest CE open interest (CE "wall")

Entry, evaluated on every newly CLOSED 5m index candle between START_TIME and NO_NEW_ENTRY_AFTER:
  SELL PE (support holds): the candle's low came within ZONE_PTS of support, it closed above support - BREAK_PTS, and it
    is a bounce candle (close > open and close > the previous candle's close).
  SELL CE (resistance holds): mirror: high within ZONE_PTS of resistance, close below resistance + BREAK_PTS, and a
    rejection candle (close < open and close < the previous close).
  OI confirmation (OI_CONFIRM): at the wall strike the wall side's OI is >= OI_WALL_RATIO x the other side's OI, and it
    has not fallen more than OI_DROP_PCT % since the previous reading (writers are holding the level, not unwinding).
  Strike sold: SELL_STRIKE (OTM1 = one strike out of the money: PE below, CE above the ATM). The run's Strike option in
  the web app applies only when SELL_STRIKE = "ATM".
  At most one short per side; a PE short and a CE short can be open together (short strangle while the range holds).

Exit of a short (bought back with a MARKET BUY), checked every POLL_SEC on the option premium, and on 5m closes:
  - target: premium <= entry x (1 - TARGET_PCT %)
  - stop: premium >= entry x (1 + STOP_PCT %), or the leg's loss >= MAX_LOSS (rupees; 0 = off)
  - level break: a 5m close beyond the wall by BREAK_PTS (PE: close < support - BREAK_PTS; CE: close > resistance +
    BREAK_PTS), using the wall recorded at entry
  - square-off at SQUARE_OFF_TIME (intraday only; the user's carried NRML shorts are not modelled)
After an exit a side waits COOLDOWN_BARS candles. Entries stop after MAX_TRADES_PER_DAY or DAILY_MAX_LOSS.

Options from the user's trades on 26-28 Oct (Zerodha trade book):
  RESELL: after a TARGET cover the same side sells again on a pullback-and-bounce candle (PE: previous candle down, this
    one up and above the previous close; CE: mirror) while the entry wall holds and the index is within RESELL_MAX_DIST
    points of it, up to RESELL_MAX re-sells per side a day (no new zone test needed: on 28 Oct the PEs were sold again
    100-150 points above the 22500 support while it held).
  LOT_STEP: each consecutive target on a side adds LOT_STEP lots to that side's next sell (up to MAX_LOTS); a loss
    resets it (the user sized up while a level kept holding).
  HEDGE: "off" | "wing" (BUY the same type HEDGE_WING_STRIKES strikes further out of the money, same expiry: a credit
    spread, less margin) | "next_expiry" (BUY the same strike of the next expiry: a calendar, like the user's 23 Nov
    CEs / 3 Nov PEs). The hedge is bought before the short is sold and sold after the short is covered; if it cannot
    be bought the short is not sold. HEDGE_LOTS_PCT = hedge lots as % of the short's lots.

Timing, adding while right, and the loss side (user, 29 Oct: "enter with 1 lot then keep increasing if the direction
is true; be ready for a buy position if the trade goes wrong; MACD helps the timing; my losses are unlimited"):
  PYRAMID: start with LOTS, SELL one more lot each time the premium falls ADD_STEP_PCT below the last add while the 5m
    MACD histogram moves the short's way (PE: rising, CE: falling), up to MAX_LOTS. The fixed target is replaced by a
    trailing exit (premium TRAIL_PCT above its low since entry) and a MACD cross against the short; MAX_LOSS and
    STOP_PCT then cover the WHOLE position (average entry), so adding never raises the loss limit.
  FAST_BREAK_EXIT: cover as soon as the index LTP is BREAK_PTS beyond the wall (checked every POLL_SEC), not only on
    the 5m close; fast moves otherwise overshoot MAX_LOSS.
  MACD_CONFIRM: entries only with the MACD histogram moving the short's way (warm-up: previous day's candles).
  REVERSE_ON_BREAK: after a level-break cover, BUY the ATM option of the break's direction (REVERSE_TARGET /
    REVERSE_STOP rupees).
  The strategy never carries a position overnight and never averages a losing short.

Adaptations / caveats:
  - "Looking at the market range" is modelled as the OI walls + the bounce/rejection candle; the user's scaling in
    (adding as the premium rose) and the long-option scalps of 13:24-13:44 are not modelled.
  - Backtest: the chain comes from the cached strikes (the day's range +/- 2 strikes, partial chain), so a wall outside
    them cannot be seen; the live chain covers all strikes within OI_WITHIN_POINTS.
  - OPTION_SELLING = True: the trade manager (option buying only) refuses this strategy; the Signals page's paper P&L
    reads every SELL as an exit, so read this strategy's own log ("P&L ...") for shorts.
  - Short options need margin (a NIFTY lot about 1-1.5 lakh rupees naked) and the loss is not limited by the premium.

Run first with OpenAlgo Analyzer (paper) mode ON.
"""

import os
import time
from datetime import datetime, timedelta
from openalgo import api

# ======================= PARAMETERS =======================
API_KEY = os.getenv("OPENALGO_API_KEY", "PASTE_YOUR_API_KEY_HERE")
HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
STRATEGY = "Manual1"

UNDERLYING = "NIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
STRIKE_STEP = 50              # set from the instrument by the backtest / web app
LOTS = 1
PRODUCT = "MIS"
OPTION_SELLING = True         # opens SHORT option positions (the trade manager refuses such strategies)

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"

SESSION_START = "09:15"
START_TIME = "09:30"          # walls and the first candles settle during the first 15 minutes
NO_NEW_ENTRY_AFTER = "14:45"
SQUARE_OFF_TIME = "15:15"

OI_WITHIN_POINTS = 300        # walls are searched among strikes within this many points of the index
ZONE_PTS = 30                 # the candle must come within this many points of the wall
BREAK_PTS = 20                # a 5m close this far beyond the wall = level broken (no entry / exit)
OI_CONFIRM = True             # wall side OI >= OI_WALL_RATIO x the other side at that strike, and not unwinding
OI_WALL_RATIO = 1.2
OI_DROP_PCT = 5               # the wall's OI may not fall more than this % between two readings

SELL_STRIKE = "OTM1"          # ATM | OTMn | ITMn (OTM = PE below / CE above the index)
TARGET_PCT = 20               # buy back when the premium has fallen this %
STOP_PCT = 35                 # buy back when the premium has risen this %
MAX_LOSS = 2500               # rupees per short leg; 0 = off
COOLDOWN_BARS = 2             # candles a side waits after an exit

RESELL = True                 # sell the same side again after a target while its wall holds (see the docstring)
RESELL_MAX = 3                # re-sells per side a day
RESELL_MAX_DIST = 200         # the index must be within this many points of the wall for a re-sell
LOT_STEP = 0                  # lots added per consecutive target on a side; 0 = off
MAX_LOTS = 5                  # cap for LOT_STEP
HEDGE = "off"                 # "off" | "wing" | "next_expiry"
HEDGE_WING_STRIKES = 3        # wing: strikes further out of the money than the sold strike
HEDGE_LOTS_PCT = 100          # hedge lots as % of the short's lots (rounded, at least 1)

# ---- timing, adding while right, and the loss side (user, 2026-10-29) ----
MACD_CONFIRM = False          # SELL PE only while the 5m MACD histogram rises, SELL CE only while it falls
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
WARMUP_CANDLES = 60           # previous day's 5m candles for the MACD warm-up (completed days only)
PYRAMID = True                # start with LOTS, add a lot each time the premium falls ADD_STEP_PCT more (MACD agreeing)
ADD_STEP_PCT = 10
TRAIL_PCT = 15                # PYRAMID: exit all when the premium rises this % above its low since entry (replaces
                              # the fixed target); MAX_LOSS / STOP_PCT then apply to the whole position
FAST_BREAK_EXIT = True        # also cover when the index LTP (every POLL_SEC) is BREAK_PTS beyond the wall, without
                              # waiting for the 5m close (fast moves overshoot MAX_LOSS between candles)
REVERSE_ON_BREAK = False      # when a short is stopped by its level breaking, BUY the ATM option of the break's
REVERSE_LOTS = 1              # direction (support broken -> PE, resistance broken -> CE)
REVERSE_TARGET = 1500         # rupees
REVERSE_STOP = 1000           # rupees

POLL_SEC = 15

MAX_TRADES_PER_DAY = 6
DAILY_MAX_LOSS = 4000         # NIFTY-sized: resize per instrument (options panel: Save for <instrument>)
# ==========================================================


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def hhmm(s):
    h, m = map(int, s.split(":"))
    return h, m


def ema(values, n):
    out, e, k = [], None, 2 / (n + 1)
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def macd_hist(closes, fast, slow, signal):
    """MACD histogram per close (None until slow + signal closes are available)."""
    if not closes:
        return []
    line = [a - b for a, b in zip(ema(closes, fast), ema(closes, slow))]
    sig = ema(line, signal)
    return [None if i < slow + signal - 2 else line[i] - sig[i] for i in range(len(closes))]


def oi_walls(chain, spot, within):
    """(support, resistance, {strike: (ce_oi, pe_oi)}) from an optionchain() response: the strike at/below spot with the
    largest PE OI and the strike at/above spot with the largest CE OI, among strikes within `within` points of spot."""
    book = {}
    for row in chain.get("chain", []):
        k = float(row["strike"])
        if abs(k - spot) <= within:
            book[k] = (float((row.get("ce") or {}).get("oi") or 0), float((row.get("pe") or {}).get("oi") or 0))
    below = [k for k in book if k <= spot and book[k][1] > 0]
    above = [k for k in book if k >= spot and book[k][0] > 0]
    sup = max(below, key=lambda k: book[k][1]) if below else None
    res = max(above, key=lambda k: book[k][0]) if above else None
    return sup, res, book


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper
        self.bars = []               # today's completed 5m index candles: {t, o, h, l, c}
        self.warm = []               # previous day's last candles (MACD warm-up)
        self.hist = []               # MACD histogram aligned with self.bars (None while warming up)
        self.long = None             # REVERSE_ON_BREAK position {side, symbol, qty, entry}
        self.processed = 0
        self.levels = {}             # latest support / resistance / their OI (shown on the Signals chart)
        self.book = {}               # latest {strike: (ce_oi, pe_oi)}
        self.prev_book = {}
        self.shorts = {}             # side -> {symbol, qty, entry, wall, t}
        self.cool = {"PE": -1, "CE": -1}   # bar index until which a side waits
        self.last_cover = {}         # side -> {"won": bool, "wall": wall} of the side's last cover (re-sell)
        self.resells = {"PE": 0, "CE": 0}
        self.streak = {"PE": 0, "CE": 0}   # consecutive targets per side (LOT_STEP)
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

    def resolve(self, option_type, offset=SELL_STRIKE, expiry_index=0):
        r = self.c.expiry(symbol=UNDERLYING, exchange=OPT_EXCHANGE, instrumenttype="options")
        if r.get("status") != "success" or len(r.get("data") or []) <= expiry_index:
            raise RuntimeError(f"expiry lookup failed: {r}")
        exp = r["data"][expiry_index].replace("-", "").upper()
        r = self.c.optionsymbol(underlying=UNDERLYING, exchange=INDEX_EXCHANGE,
                                expiry_date=exp, offset=offset, option_type=option_type)
        if r.get("status") != "success":
            raise RuntimeError(f"optionsymbol failed: {r}")
        self.lot = int(r["lotsize"])
        return r["symbol"]

    @staticmethod
    def further_otm(offset, n):
        """The offset n strikes further out of the money than `offset` (ATM / OTMk / ITMk)."""
        o = offset.upper()
        k = 0 if o == "ATM" else int(o[3:]) * (1 if o.startswith("OTM") else -1)
        k += n
        return "ATM" if k == 0 else (f"OTM{k}" if k > 0 else f"ITM{-k}")

    def hedge_symbol(self, side):
        if HEDGE == "wing":
            return self.resolve(side, self.further_otm(SELL_STRIKE, HEDGE_WING_STRIKES))
        if HEDGE == "next_expiry":
            return self.resolve(side, SELL_STRIKE, expiry_index=1)
        return None

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
        rows = []
        for ts, row in df.iterrows():
            t = ts.to_pydatetime()
            if t.tzinfo is not None:
                t = t.replace(tzinfo=None)
            if t.date() == now.date() and t + timedelta(minutes=BAR_MINUTES) <= now:
                rows.append({"t": t, "o": float(row["open"]), "h": float(row["high"]), "l": float(row["low"]), "c": float(row["close"])})
        rows.sort(key=lambda b: b["t"])
        self.bars = rows
        h = macd_hist([b["c"] for b in self.warm + rows], MACD_FAST, MACD_SLOW, MACD_SIGNAL)
        self.hist = h[len(self.warm):]

    def load_warmup(self):
        """The previous trading day's last WARMUP_CANDLES 5m candles (completed days only: no lookahead)."""
        today = self.now().date()
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=TIMEFRAME,
                                start_date=(today - timedelta(days=7)).isoformat(),
                                end_date=(today - timedelta(days=1)).isoformat(), source=HISTORY_SOURCE)
        except Exception as e:
            log(f"warm-up history failed ({e}); MACD starts with today's candles")
            return
        if not hasattr(df, "iterrows") or df.empty:
            return
        rows = []
        for ts, row in df.iterrows():
            t = ts.to_pydatetime().replace(tzinfo=None)
            if t.date() < today:
                rows.append({"t": t, "o": float(row["open"]), "h": float(row["high"]), "l": float(row["low"]), "c": float(row["close"])})
        rows.sort(key=lambda b: b["t"])
        self.warm = rows[-WARMUP_CANDLES:]

    def macd_dir(self, i):
        """+1 histogram rising, -1 falling, 0 unknown, at today's candle i."""
        if i < 1 or i >= len(self.hist) or self.hist[i] is None or self.hist[i - 1] is None:
            return 0
        return 1 if self.hist[i] > self.hist[i - 1] else -1 if self.hist[i] < self.hist[i - 1] else 0

    def refresh_walls(self, spot):
        try:
            exp = self.nearest_expiry()
            r = self.c.optionchain(underlying=UNDERLYING, exchange=INDEX_EXCHANGE, expiry_date=exp,
                                   strike_count=int(OI_WITHIN_POINTS // STRIKE_STEP) + 1)
        except Exception as e:
            log(f"optionchain exception: {e}")
            return False
        if not r or r.get("status") != "success":
            log(f"optionchain failed: {r}")
            return False
        sup, res, book = oi_walls(r, spot, OI_WITHIN_POINTS)
        self.prev_book, self.book = self.book, book
        self.levels = {"support": sup, "resistance": res,
                       "support_pe_oi": book[sup][1] if sup else None, "resistance_ce_oi": book[res][0] if res else None}
        return True

    def oi_ok(self, side, wall):
        """OI confirmation at the wall strike (see OI_CONFIRM). Returns (ok, text)."""
        if not OI_CONFIRM:
            return True, "OI check off"
        ce, pe = self.book.get(wall, (0, 0))
        mine, other = (pe, ce) if side == "PE" else (ce, pe)
        before = self.prev_book.get(wall)
        prev = (before[1] if side == "PE" else before[0]) if before else None
        txt = f"{side} OI {mine / 1e5:.1f}L vs {'CE' if side == 'PE' else 'PE'} {other / 1e5:.1f}L"
        if prev:
            txt += f", {(mine / prev - 1) * 100:+.1f}% since the last reading"
        if mine < OI_WALL_RATIO * other:
            return False, txt + f" (needs {OI_WALL_RATIO}x)"
        if prev and mine < prev * (1 - OI_DROP_PCT / 100):
            return False, txt + f" (unwinding > {OI_DROP_PCT}%)"
        return True, txt

    # ---------- signals ----------
    def handle_bar(self, i):
        bar = self.bars[i]
        prev = self.bars[i - 1] if i else None
        if not self.refresh_walls(bar["c"]):
            return
        sup, res = self.levels["support"], self.levels["resistance"]
        md = self.macd_dir(i)
        for side, pos in list(self.shorts.items()):        # level break on the candle close
            if side == "PE" and bar["c"] < pos["wall"] - BREAK_PTS:
                if self.cover(side, f"support {pos['wall']:.0f} broken: 5m close {bar['c']:.2f} ({bar['t']:%H:%M})", i):
                    self.reverse("PE", bar)
            elif side == "CE" and bar["c"] > pos["wall"] + BREAK_PTS:
                if self.cover(side, f"resistance {pos['wall']:.0f} broken: 5m close {bar['c']:.2f} ({bar['t']:%H:%M})", i):
                    self.reverse("CE", bar)
            elif PYRAMID and i >= 1 and self.hist[i] is not None and self.hist[i - 1] is not None:
                h, hp = self.hist[i], self.hist[i - 1]               # MACD crossed against the short
                if (side == "PE" and hp >= 0 > h) or (side == "CE" and hp <= 0 < h):
                    self.cover(side, f"MACD crossed against the {side} short ({bar['t']:%H:%M})", i,
                               won=(pos["entry"] - (self.ltp(pos["symbol"], OPT_EXCHANGE) or pos["entry"])) > 0)
        if not prev or not self.in_time_window():
            return
        if MACD_CONFIRM and md == 0:
            return
        if self.trades_today >= MAX_TRADES_PER_DAY or self.day_pnl <= -DAILY_MAX_LOSS:
            if not self._cap_logged:
                log(f"Daily trade/loss cap reached (trades {self.trades_today}, day P&L {self.day_pnl:+.0f}); no further entries today")
                self._cap_logged = True
            return
        up = bar["c"] > bar["o"] and bar["c"] > prev["c"] and (not MACD_CONFIRM or md > 0)
        down = bar["c"] < bar["o"] and bar["c"] < prev["c"] and (not MACD_CONFIRM or md < 0)
        if "PE" not in self.shorts and i > self.cool["PE"] and up:
            if sup is not None and bar["l"] <= sup + ZONE_PTS and bar["c"] >= sup - BREAK_PTS:
                self.try_sell("PE", sup, bar, i)
            elif self.resell_ok("PE", bar, prev):
                self.try_sell("PE", self.last_cover["PE"]["wall"], bar, i, resell=True)
        if "CE" not in self.shorts and i > self.cool["CE"] and down:
            if res is not None and bar["h"] >= res - ZONE_PTS and bar["c"] <= res + BREAK_PTS:
                self.try_sell("CE", res, bar, i)
            elif self.resell_ok("CE", bar, prev):
                self.try_sell("CE", self.last_cover["CE"]["wall"], bar, i, resell=True)

    def resell_ok(self, side, bar, prev):
        """Re-sell after a target: the previous candle pulled back against the short, this one resumed, the entry wall
        still holds and the index is within RESELL_MAX_DIST of it."""
        lc = self.last_cover.get(side)
        if not RESELL or not lc or not lc["won"] or self.resells[side] >= RESELL_MAX:
            return False
        w = lc["wall"]
        if side == "PE":
            return prev["c"] < prev["o"] and bar["c"] >= w - BREAK_PTS and bar["c"] - w <= RESELL_MAX_DIST
        return prev["c"] > prev["o"] and bar["c"] <= w + BREAK_PTS and w - bar["c"] <= RESELL_MAX_DIST

    def process_new_bars(self):
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
        if r.get("status") != "success":
            log(f"{action} FAILED {symbol}: {r}" + (" !!! SHORT STILL OPEN: CHECK POSITION IN OPENALGO !!!" if action == "BUY" else ""))
            return None
        price = self.fill_price(r.get("orderid")) or self.ltp(symbol, OPT_EXCHANGE)
        log(f"{action} {symbol} qty {qty} @ {price} | order {r.get('orderid')} | {reason}")
        return price

    def try_sell(self, side, wall, bar, i, resell=False):
        ok, oi_txt = self.oi_ok(side, wall)
        what = "support" if side == "PE" else "resistance"
        if not ok:
            log(f"SELL {side} at {what} {wall:.0f} ({bar['t']:%H:%M}) skipped: {oi_txt}")
            return
        try:
            sym = self.resolve(side)
            hsym = self.hedge_symbol(side)
        except Exception as e:
            log(f"strike resolution failed ({side}{', hedge ' + HEDGE if HEDGE != 'off' else ''}): {e}")
            return
        lots = min(MAX_LOTS, LOTS + LOT_STEP * self.streak[side]) if LOT_STEP else LOTS
        qty = self.lot * lots
        why = (f"re-sell {self.resells[side] + 1}/{RESELL_MAX}: {what} {wall:.0f} still holds, 5m {bar['t']:%H:%M} close {bar['c']:.2f}"
               if resell else
               f"{what} {wall:.0f} holds: 5m {bar['t']:%H:%M} {'low' if side == 'PE' else 'high'} "
               f"{bar['l'] if side == 'PE' else bar['h']:.2f}, close {bar['c']:.2f}") + f" | {oi_txt} | {lots} lot(s)"
        hedge = None
        if hsym:                                           # hedge first: less margin for the short
            hqty = self.lot * max(1, round(lots * HEDGE_LOTS_PCT / 100))
            hpx = self.order("BUY", hsym, hqty, f"hedge ({HEDGE}) for the {side} short")
            if hpx is None:
                log(f"SELL {side} skipped: the hedge {hsym} could not be bought")
                return
            hedge = {"symbol": hsym, "qty": hqty, "entry": hpx}
        px = self.order("SELL", sym, qty, why)
        if px is None:
            if hedge:
                self.order("SELL", hedge["symbol"], hedge["qty"], "short not filled: hedge closed")
            return
        self.shorts[side] = {"symbol": sym, "qty": qty, "entry": px, "wall": wall, "t": bar["t"], "hedge": hedge,
                             "lots": lots, "low": px, "last_add": px}
        self.trades_today += 1
        if resell:
            self.resells[side] += 1
        log(f"SHORT {side} {sym} @ {px} | target {px * (1 - TARGET_PCT / 100):.2f} stop {px * (1 + STOP_PCT / 100):.2f} "
            f"| {what} {wall:.0f}" + (f" | hedge {hedge['symbol']} @ {hedge['entry']}" if hedge else "")
            + f" | trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

    def cover(self, side, reason, i=None, won=False):
        pos = self.shorts[side]
        px = self.order("BUY", pos["symbol"], pos["qty"], reason)
        if px is None:
            return False                                   # still short: retried on the next check
        p = (pos["entry"] - px) * pos["qty"]
        h = pos.get("hedge")
        if h:
            hpx = self.order("SELL", h["symbol"], h["qty"], f"hedge closed with the {side} short")
            if hpx is None:
                log(f"!!! HEDGE {h['symbol']} STILL OPEN: CHECK POSITION IN OPENALGO !!!")
            else:
                p += (hpx - h["entry"]) * h["qty"]
        self.day_pnl += p
        log(f"COVER {side} {reason} | pnl {p:+.0f}{' (with hedge)' if h else ''} | day P&L {self.day_pnl:+.0f}")
        del self.shorts[side]
        self.cool[side] = (i if i is not None else len(self.bars) - 1) + COOLDOWN_BARS
        self.last_cover[side] = {"won": won, "wall": pos["wall"]}
        self.streak[side] = self.streak[side] + 1 if won else 0
        return True

    def add_lot(self, side, px):
        """PYRAMID: sell one more lot of the open short (and of its hedge); the entry becomes the average."""
        pos = self.shorts[side]
        if pos.get("hedge"):
            h = pos["hedge"]
            hpx = self.order("BUY", h["symbol"], self.lot, f"hedge for the added {side} lot")
            if hpx is None:
                return
            h["entry"] = (h["entry"] * h["qty"] + hpx * self.lot) / (h["qty"] + self.lot)
            h["qty"] += self.lot
        fill = self.order("SELL", pos["symbol"], self.lot,
                          f"add lot {pos['lots'] + 1}: premium {pos['last_add']:.2f} -> {px:.2f} (-{ADD_STEP_PCT}%), MACD agrees")
        if fill is None:
            return
        pos["entry"] = (pos["entry"] * pos["qty"] + fill * self.lot) / (pos["qty"] + self.lot)
        pos["qty"] += self.lot
        pos["lots"] += 1
        pos["last_add"] = fill
        log(f"ADDED {side}: {pos['lots']} lots, average {pos['entry']:.2f}")

    def reverse(self, side, bar):
        """REVERSE_ON_BREAK: the short's level broke, so BUY the ATM option of the break's direction."""
        if not REVERSE_ON_BREAK or self.long or not self.in_time_window():
            return
        try:
            sym = self.resolve(side, "ATM")
        except Exception as e:
            log(f"reverse: strike resolution failed: {e}")
            return
        qty = self.lot * REVERSE_LOTS
        px = self.order("BUY", sym, qty, f"level broke at {bar['t']:%H:%M}: buy {side} in the direction of the break")
        if px is not None:
            self.long = {"side": side, "symbol": sym, "qty": qty, "entry": px}
            log(f"LONG {side} {sym} @ {px} | target +{REVERSE_TARGET} stop -{REVERSE_STOP}")

    def close_long(self, reason):
        lg = self.long
        px = self.order("SELL", lg["symbol"], lg["qty"], reason)
        if px is None:
            return False
        p = (px - lg["entry"]) * lg["qty"]
        self.day_pnl += p
        log(f"EXIT LONG {lg['side']} {reason} | pnl {p:+.0f} | day P&L {self.day_pnl:+.0f}")
        self.long = None
        return True

    def check_long(self):
        if not self.long:
            return
        px = self.ltp(self.long["symbol"], OPT_EXCHANGE)
        if px is None:
            return
        p = (px - self.long["entry"]) * self.long["qty"]
        if p >= REVERSE_TARGET:
            self.close_long(f"target {p:+.0f}")
        elif p <= -REVERSE_STOP:
            self.close_long(f"stop {p:+.0f}")

    def check_premiums(self):
        self.check_long()
        if FAST_BREAK_EXIT and self.shorts:
            idx = self.ltp(UNDERLYING, INDEX_EXCHANGE)
            for side, pos in list(self.shorts.items()):
                if idx is None:
                    break
                if (side == "PE" and idx < pos["wall"] - BREAK_PTS) or (side == "CE" and idx > pos["wall"] + BREAK_PTS):
                    what = "support" if side == "PE" else "resistance"
                    if self.cover(side, f"{what} {pos['wall']:.0f} broken: index {idx:.2f} (live)"):
                        self.reverse(side, {"t": self.now()})
        for side, pos in list(self.shorts.items()):
            px = self.ltp(pos["symbol"], OPT_EXCHANGE)
            if px is None:
                continue
            p = (pos["entry"] - px) * pos["qty"]
            if PYRAMID:
                pos["low"] = min(pos["low"], px)
                i = len(self.bars) - 1
                if px >= pos["low"] * (1 + TRAIL_PCT / 100) and pos["low"] < pos["entry"]:
                    self.cover(side, f"trail: premium {pos['low']:.2f} -> {px:.2f} (+{TRAIL_PCT}% off the low) | "
                                     f"{pos['lots']} lots avg {pos['entry']:.2f}", won=p > 0)
                elif px >= pos["entry"] * (1 + STOP_PCT / 100):
                    self.cover(side, f"stop: premium {pos['entry']:.2f} -> {px:.2f} (+{STOP_PCT}%)")
                elif MAX_LOSS and p <= -MAX_LOSS:
                    self.cover(side, f"max loss: {p:+.0f} <= -{MAX_LOSS}")
                elif (pos["lots"] < MAX_LOTS and px <= pos["last_add"] * (1 - ADD_STEP_PCT / 100)
                      and self.macd_dir(i) == (1 if side == "PE" else -1) and self.now() < self.at(NO_NEW_ENTRY_AFTER)):
                    self.add_lot(side, px)
                continue
            if px <= pos["entry"] * (1 - TARGET_PCT / 100):
                self.cover(side, f"target: premium {pos['entry']:.2f} -> {px:.2f} (-{TARGET_PCT}%)", won=True)
            elif px >= pos["entry"] * (1 + STOP_PCT / 100):
                self.cover(side, f"stop: premium {pos['entry']:.2f} -> {px:.2f} (+{STOP_PCT}%)")
            elif MAX_LOSS and p <= -MAX_LOSS:
                self.cover(side, f"max loss: {p:+.0f} <= -{MAX_LOSS}")

    # ---------- main loop ----------
    def run(self):
        log(f"Strategy {STRATEGY} starting on {UNDERLYING} (option SELLING at OI walls)")
        try:
            log(f"Analyzer (paper) mode: {self.c.analyzerstatus().get('data')}")
        except Exception:
            pass
        self.load_warmup()
        self.wait_until(self.at(SESSION_START))
        last_pnl_log = None
        while True:
            if self.now() >= self.at(SQUARE_OFF_TIME):
                for side in list(self.shorts):
                    self.cover(side, "square-off time")
                if self.long:
                    self.close_long("square-off time")
                if not self.shorts and not self.long:
                    log("Square-off time reached; stopping for the day")
                    break
                self.sleep(POLL_SEC)
                continue
            self.refresh_bars()
            self.process_new_bars()
            self.check_premiums()
            if self.shorts and (last_pnl_log is None or self.now() - last_pnl_log >= timedelta(minutes=1)):
                last_pnl_log = self.now()
                parts = []
                for side, pos in self.shorts.items():
                    px = self.ltp(pos["symbol"], OPT_EXCHANGE)
                    if px is not None:
                        parts.append(f"{pos['symbol']} {pos['entry']:.2f}->{px:.2f} ({(pos['entry'] - px) * pos['qty']:+.0f})")
                if parts:
                    log("P&L " + " | ".join(parts) + f" | day {self.day_pnl:+.0f}")
            self.sleep(POLL_SEC)
        log(f"Done for the day. Trades {self.trades_today} | day P&L {self.day_pnl:+.0f}")


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
