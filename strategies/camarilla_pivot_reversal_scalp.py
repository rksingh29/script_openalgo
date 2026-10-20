"""
Camarilla Pivot Reversal Scalp (ATM CE/PE option buying) for OpenAlgo
-----------------------------------------------------------------------
Once per day (at session start) compute Camarilla levels from the PREVIOUS
trading day's index daily high (H), low (L) and close (C):

    R3 = C + (H-L)*CAMARILLA_MULTIPLIER/4
    R4 = C + (H-L)*CAMARILLA_MULTIPLIER/2
    S3 = C - (H-L)*CAMARILLA_MULTIPLIER/4
    S4 = C - (H-L)*CAMARILLA_MULTIPLIER/2

These levels are fixed for the whole session (fetched once via a single
`history(interval="D")` call for the prior day -- no lookahead, the prior
day is always fully completed before today's session).

Signal (evaluated on every newly CLOSED 5-minute index candle -- spec's
"S3-reversal variant", chosen because the research spec names it as the
most commonly cited scalping rule; the alternative S4-breakdown-continuation
variant mentioned in the same source is NOT implemented, per the spec's own
disambiguation):

  LONG (buy ATM CE):  the candle's low touches/pierces S3
                       (low <= S3) AND the candle closes back above S3
                       (close > S3), AND it is a "clear rejection candle"
                       -- a lower wick that is at least MIN_WICK_RATIO of
                       the candle's high-low range (the spec only asks
                       qualitatively for "a clear rejection candle (long
                       wick)"; this ratio is the testable proxy for that,
                       documented as an adaptation, not a guess at the
                       spec's numeric intent).
  SHORT (buy ATM PE): the mirror image at R3 (high >= R3, close < R3,
                       upper wick >= MIN_WICK_RATIO of the range).

Only one signal/position is actioned at a time; while a position is open,
new bar signals are logged but not acted on.

Stop / target (per spec):
  - Stop is placed just beyond S4 (long) or R4 (short), i.e.
    stop = S4 - STOP_BUFFER_PCT% of price (long) / R4 + STOP_BUFFER_PCT%
    (short). If that stop distance is non-positive or larger than
    STOP_MAX_PCT% of price (a sanity filter, not in the source spec), the
    signal is skipped -- this guards against a day where S3/S4 (or R3/R4)
    are degenerate (e.g. H==L on the prior day).
  - Target: the spec names two alternative targets -- "the next inner
    level (R1/S1 or the central pivot)" OR "fixed reward:risk ~1:1 to
    2:1". Since the spec's own `params` give a single concrete number
    (`reward_risk: 1.5`), this strategy uses the FIXED REWARD:RISK target
    (entry +/- REWARD_RISK * risk distance) as the testable rule, and
    does not also compute the inner-level target -- documented adaptation,
    not a guess.
  - Both stop and target are tracked in INDEX POINTS (computed from the
    index candle that triggered the signal) and checked every poll against
    the index LTP, independent of the option premium -- same "signal vs
    execution split" pattern as vwap_reclaim_scalp.py, so the stop/target
    stay consistent with the signal that generated them.

Exit: stop hit, target hit, or SQUARE_OFF_TIME (forced exit regardless of
P&L). Daily caps: MAX_TRADES_PER_DAY, DAILY_MAX_LOSS, NO_NEW_ENTRY_AFTER
(not in the source spec; same safety-cap convention as the other
strategies in this project).

Adaptations from the raw research spec (documented, not guesses):
  - S3-reversal variant only (spec explicitly says sources differ and
    picks this one as the most common).
  - Rejection-candle wick ratio (MIN_WICK_RATIO) is a numeric proxy for the
    spec's qualitative "clear rejection candle (long wick)" cue.
  - Fixed reward:risk target (not the alternative inner-level target),
    using the spec's own `params.reward_risk` default.
  - STOP_BUFFER_PCT / STOP_MAX_PCT / MAX_TRADES_PER_DAY / DAILY_MAX_LOSS /
    NO_NEW_ENTRY_AFTER / SQUARE_OFF_TIME are safety caps, not source rules.

Optional regime filter (USE_REGIME_FILTER, off by default = original behaviour). Camarilla reversals are meant for
ranging markets; on a trending day S3/R3 are speed bumps. When on, a signal is taken only if the 5m ADX(ADX_LENGTH,
Wilder, as TradingView's ta.dmi) of the signal candle is below ADX_THRESHOLD. ADX lags, so USE_BW_FILTER can be added:
the signal is blocked when Bollinger bandwidth, (upper-lower)/middle, is more than BW_EXPANSION times its average
over the previous BW_LOOKBACK candles (volatility expanding). ADX/bands are computed on closed candles only and are
warmed up with the last WARMUP_BARS 5m candles of earlier days, so they are valid from the first candle of the day.
BW_EXPANSION is a judgement value, not from the source.

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
STRATEGY = "CamarillaPivotReversalScalp"

UNDERLYING = "NIFTY"          # or "BANKNIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
LOTS = 1
PRODUCT = "MIS"

TIMEFRAME = "5m"
BAR_MINUTES = 5
HISTORY_SOURCE = "api"         # source="db" (Historify) is empty, see knowledge/openalgo_api.md

SESSION_START = "09:15"        # candles are built from the open
START_TIME = "09:20"           # need at least one closed 5m candle before a signal can fire
NO_NEW_ENTRY_AFTER = "14:45"
SQUARE_OFF_TIME = "15:15"

CAMARILLA_MULTIPLIER = 1.1     # spec params.camarilla_multiplier
REWARD_RISK = 1.5              # spec params.reward_risk (fixed R:R target)

MIN_WICK_RATIO = 0.4           # rejection-candle proxy for "clear rejection candle (long wick)"
STOP_BUFFER_PCT = 0.05         # stop placed this % beyond S4/R4
STOP_MAX_PCT = 0.6             # sanity cap: skip signal if risk distance > this % of price

# ---- optional regime filter (see docstring) ----
USE_REGIME_FILTER = False      # False = original, unfiltered behaviour
ADX_LENGTH = 14
ADX_THRESHOLD = 20             # trade only while ADX < this
USE_BW_FILTER = False          # extra guard against the ADX lag; only used when USE_REGIME_FILTER is on
BB_LENGTH = 20
BB_MULT = 2.0
BW_LOOKBACK = 5
BW_EXPANSION = 1.25            # block if bandwidth > this x the average of the previous BW_LOOKBACK candles
WARMUP_BARS = 100              # earlier-day 5m candles used to warm the indicators up

POLL_SEC = 20                  # main loop tick / stop-target check interval

# ---- daily safety caps (not in the source spec; same pattern as other strategies here) ----
MAX_TRADES_PER_DAY = 4
DAILY_MAX_LOSS = 3000           # NIFTY-sized; resize per instrument (see knowledge/lessons.md)
DAILY_PROFIT_TARGET = 0         # 0 = off (spec defines no fixed daily target)
# ==========================================================


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def hhmm(s):
    h, m = map(int, s.split(":"))
    return h, m


def compute_camarilla_levels(high, low, close, multiplier=CAMARILLA_MULTIPLIER):
    rng = high - low
    return {
        "r3": close + rng * multiplier / 4,
        "r4": close + rng * multiplier / 2,
        "s3": close - rng * multiplier / 4,
        "s4": close - rng * multiplier / 2,
        "pivot": (high + low + close) / 3,  # logged for reference only; not used by the entry/target logic
    }


def adx_value(bars, n=ADX_LENGTH):
    """ADX of the last bar (Wilder smoothing, seeded with a simple average like TradingView's ta.dmi). None if there
    are fewer than 2*n bars."""
    if len(bars) < 2 * n + 1:
        return None
    tr, pdm, mdm = [], [], []
    for i in range(1, len(bars)):
        b, p = bars[i], bars[i - 1]
        up, dn = b["h"] - p["h"], p["l"] - b["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])))

    def rma(xs):
        out = [sum(xs[:n]) / n]
        for x in xs[n:]:
            out.append((out[-1] * (n - 1) + x) / n)
        return out
    atr, ps, ms = rma(tr), rma(pdm), rma(mdm)
    dx = []
    for a, p, m in zip(atr, ps, ms):
        pi, mi = (100 * p / a, 100 * m / a) if a else (0.0, 0.0)
        dx.append(100 * abs(pi - mi) / (pi + mi) if pi + mi else 0.0)
    return rma(dx)[-1]


def bandwidth(bars, n=BB_LENGTH, mult=BB_MULT):
    """(upper - lower) / middle of the Bollinger Bands on the last n closes; None if too few bars."""
    if len(bars) < n:
        return None
    cs = [b["c"] for b in bars[-n:]]
    mid = sum(cs) / n
    sd = (sum((c - mid) ** 2 for c in cs) / n) ** 0.5
    return 2 * mult * sd / mid if mid else None


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper

        self.levels = None        # {"r3","r4","s3","s4","pivot"} for today, set once at session start

        self.bars = []             # completed 5m index candles: {t, o, h, l, c}
        self.warm = []             # last WARMUP_BARS candles of earlier days (indicator warm-up only)
        self.processed_bars = 0

        self.position = None      # {"side","symbol","qty","entry_premium","index_entry","stop","target"}
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
        self.lot = int(r["lotsize"])
        log(f"ATM {option_type} {r['symbol']} | lot {self.lot} | expiry {exp} | "
            f"underlying_ltp {r.get('underlying_ltp')}")
        return r["symbol"]

    def previous_day_ohlc(self):
        """High/low/close of the most recent fully-completed trading day before today.
        Single history(interval="D") call; no lookahead (always a prior, closed day)."""
        end = self.now().date() - timedelta(days=1)
        start = end - timedelta(days=10)
        df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="D",
                             start_date=start.isoformat(), end_date=end.isoformat(),
                             source=HISTORY_SOURCE)
        if not hasattr(df, "empty") or df.empty:
            raise RuntimeError(f"no daily history returned for {UNDERLYING}: {df}")
        row = df.iloc[-1]
        return float(row["high"]), float(row["low"]), float(row["close"])

    def setup_levels(self):
        h, l, c = self.previous_day_ohlc()
        self.levels = compute_camarilla_levels(h, l, c)
        log(f"Prior day H {h:.2f} L {l:.2f} C {c:.2f} -> "
            f"R4 {self.levels['r4']:.2f} R3 {self.levels['r3']:.2f} "
            f"Pivot {self.levels['pivot']:.2f} "
            f"S3 {self.levels['s3']:.2f} S4 {self.levels['s4']:.2f}")

    def setup_warmup(self):
        if not USE_REGIME_FILTER:
            return
        today = self.now().date()
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=TIMEFRAME,
                                 start_date=(today - timedelta(days=7)).isoformat(),
                                 end_date=(today - timedelta(days=1)).isoformat(), source=HISTORY_SOURCE)
        except Exception as e:
            log(f"warm-up history exception: {e}")
            return
        if not hasattr(df, "empty") or df.empty:
            log("warm-up history empty: ADX will only be valid after about 30 candles today")
            return
        bars = []
        for ts, row in df.iterrows():
            t = ts.to_pydatetime()
            if t.tzinfo is not None:
                t = t.replace(tzinfo=None)
            if t.date() < today:
                bars.append({"t": t, "o": float(row["open"]), "h": float(row["high"]),
                             "l": float(row["low"]), "c": float(row["close"])})
        bars.sort(key=lambda b: b["t"])
        self.warm = bars[-WARMUP_BARS:]
        log(f"regime filter warm-up: {len(self.warm)} earlier 5m candles")

    def regime_block(self, i):
        """None if the market looks ranging enough to trade, else the reason to skip (uses candles up to bar i only)."""
        if not USE_REGIME_FILTER:
            return None
        seq = self.warm + self.bars[:i + 1]
        adx = adx_value(seq)
        if adx is None:
            return "ADX not available yet (not enough candles)"
        if adx >= ADX_THRESHOLD:
            return f"trending: ADX {adx:.1f} >= {ADX_THRESHOLD}"
        if USE_BW_FILTER:
            bw = bandwidth(seq)
            prev = [bandwidth(seq[:len(seq) - k]) for k in range(1, BW_LOOKBACK + 1)]
            prev = [x for x in prev if x]
            if bw and prev and bw > BW_EXPANSION * sum(prev) / len(prev):
                return f"volatility expanding: bandwidth {bw:.4f} > {BW_EXPANSION}x recent {sum(prev) / len(prev):.4f}"
        return None

    def _fetch_bars(self):
        today = self.now().strftime("%Y-%m-%d")
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval=TIMEFRAME,
                                 start_date=today, end_date=today, source=HISTORY_SOURCE)
        except Exception as e:
            log(f"history exception ({TIMEFRAME}): {e}")
            return []
        if not hasattr(df, "empty"):
            log(f"history error ({TIMEFRAME}): {df}")
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
            if t + timedelta(minutes=BAR_MINUTES) > now:
                continue   # candle not fully completed yet; no lookahead
            bars.append({"t": t, "o": float(row["open"]), "h": float(row["high"]),
                         "l": float(row["low"]), "c": float(row["close"])})
        bars.sort(key=lambda b: b["t"])
        return bars

    def refresh_bars(self):
        bars = self._fetch_bars()
        if bars:
            self.bars = bars

    # ---------- bar-driven signal ----------
    def process_new_bars(self):
        while self.processed_bars < len(self.bars):
            self.handle_new_bar(self.processed_bars)
            self.processed_bars += 1

    def in_time_window(self):
        return self.at(START_TIME) <= self.now() < self.at(NO_NEW_ENTRY_AFTER)

    def day_caps_reached(self):
        if self.trades_today >= MAX_TRADES_PER_DAY:
            return f"max trades/day ({MAX_TRADES_PER_DAY}) reached"
        if self.day_pnl <= -DAILY_MAX_LOSS:
            return f"daily max loss reached ({self.day_pnl:+.0f})"
        if DAILY_PROFIT_TARGET and self.day_pnl >= DAILY_PROFIT_TARGET:
            return f"daily profit target reached ({self.day_pnl:+.0f})"
        return None

    def handle_new_bar(self, i):
        bar = self.bars[i]
        rng = bar["h"] - bar["l"]
        if rng <= 0:
            return

        long_touch = bar["l"] <= self.levels["s3"] and bar["c"] > self.levels["s3"]
        short_touch = bar["h"] >= self.levels["r3"] and bar["c"] < self.levels["r3"]

        lower_wick_ratio = (min(bar["o"], bar["c"]) - bar["l"]) / rng
        upper_wick_ratio = (bar["h"] - max(bar["o"], bar["c"])) / rng

        long_signal = long_touch and lower_wick_ratio >= MIN_WICK_RATIO
        short_signal = short_touch and upper_wick_ratio >= MIN_WICK_RATIO

        if long_touch and not long_signal:
            log(f"S3 reversal at bar {i} ({bar['t']:%H:%M}) dropped: "
                f"lower wick ratio {lower_wick_ratio:.2f} < {MIN_WICK_RATIO}")
        if short_touch and not short_signal:
            log(f"R3 reversal at bar {i} ({bar['t']:%H:%M}) dropped: "
                f"upper wick ratio {upper_wick_ratio:.2f} < {MIN_WICK_RATIO}")

        if not (long_signal or short_signal) or (long_signal and short_signal):
            return

        if self.position:
            log(f"Signal at bar {i} ({bar['t']:%H:%M}) ignored: position already open")
            return
        why = self.regime_block(i)
        if why:
            log(f"Signal at bar {i} ({bar['t']:%H:%M}) skipped by regime filter: {why}")
            return
        if not self.in_time_window():
            return
        cap = self.day_caps_reached()
        if cap:
            if not self._cap_logged:
                log(f"No further entries today: {cap}")
                self._cap_logged = True
            return

        if long_signal:
            self.attempt_entry("CE", bar, i)
        else:
            self.attempt_entry("PE", bar, i)

    def attempt_entry(self, side, bar, i):
        entry_level = bar["c"]
        buffer = entry_level * STOP_BUFFER_PCT / 100.0
        if side == "CE":
            stop = self.levels["s4"] - buffer
            risk = entry_level - stop
        else:
            stop = self.levels["r4"] + buffer
            risk = stop - entry_level
        if risk <= 0:
            log(f"{side} signal at bar {i} skipped: non-positive risk distance "
                f"(entry {entry_level:.2f}, stop {stop:.2f})")
            return
        if risk / entry_level * 100.0 > STOP_MAX_PCT:
            log(f"{side} signal at bar {i} skipped: risk distance {risk:.2f} "
                f"({risk / entry_level * 100.0:.2f}%) exceeds STOP_MAX_PCT ({STOP_MAX_PCT}%)")
            return
        target = entry_level + REWARD_RISK * risk if side == "CE" else entry_level - REWARD_RISK * risk

        try:
            symbol = self.resolve_atm(side)
        except Exception as e:
            log(f"ATM resolution failed: {e}")
            return
        qty = self.lot * LOTS
        premium = self.buy(symbol, qty,
                            f"{side} Camarilla reversal | index entry {entry_level:.2f} "
                            f"stop {stop:.2f} target {target:.2f}")
        if premium is None:
            return
        self.position = {
            "side": side, "symbol": symbol, "qty": qty, "entry_premium": premium,
            "index_entry": entry_level, "stop": stop, "target": target,
        }
        self.trades_today += 1
        log(f"ENTER {side} | index stop {stop:.2f} target {target:.2f} (R:R {REWARD_RISK}) | "
            f"trade {self.trades_today}/{MAX_TRADES_PER_DAY}")

    # ---------- position management (index-points stop/target, checked on LTP) ----------
    def manage_position(self):
        if not self.position:
            return
        pos = self.position
        idx_ltp = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        if idx_ltp is None:
            return
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

    # ---------- main loop ----------
    def run(self):
        log(f"Strategy {STRATEGY} starting on {UNDERLYING}")
        try:
            a = self.c.analyzerstatus()
            log(f"Analyzer (paper) mode: {a.get('data', a)}")
        except Exception:
            pass
        self.wait_until(self.at(SESSION_START))
        self.setup_levels()
        self.setup_warmup()
        while True:
            if self.now() >= self.at(SQUARE_OFF_TIME):
                if self.position:
                    self.exit_position("square-off time")
                log("Square-off time reached; stopping for the day")
                break
            self.refresh_bars()
            self.process_new_bars()
            self.manage_position()
            self.sleep(POLL_SEC)
        log(f"Done for the day. Trades {self.trades_today} | day P&L {self.day_pnl:+.0f}")


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
