"""
All_in_one: one intraday strategy that picks its play by session and market condition (ATM CE/PE option buying)
--------------------------------------------------------------------------------------------------------------
SESSIONS (all settings): S1..S5 each have ON (trade or not), START / END (HH:MM) and PLAYS ("auto" or a list).
  default  S1 09:15-09:30 trend (EMA+RSI+price action; also builds the opening range)  S2 09:30-11:00 auto + orb
           S3 11:00-13:00 auto                                   S4 13:00-14:00 auto
           S5 14:00-15:00 auto + late_momentum                   square-off 15:15
  Outside an ON session no new trade is opened (an open trade is still managed).

RISK (one number): RISK_PER_TRADE rupees (e.g. 1000 ... 10000). The stop of a trade is RISK_PER_TRADE, the profit step
  TARGET_R x RISK_PER_TRADE, the daily loss limit DAILY_RISK_MULT x RISK_PER_TRADE. Size: RISK_SIZING "risk" ->
  lots = RISK_PER_TRADE / RISK_PER_LOT (RISK_PER_LOT = the rupee stop one lot is given, 1000 for NIFTY), capped by
  MAX_LOTS; "lots" -> always LOTS. Other risk checks: one position, trades per session / day, losses in a row, time
  left before square-off, room to the next pivot level.

MARKET CONDITION ENGINE (every completed CANDLE_MIN index candle, default 5 min; indicators warmed up with the previous day's candles):
  trend strength   ADX(14), EMA 9 / 21, efficiency ratio ER(10)
  volatility       ATR(5) / ATR(30) (expansion / contraction), today's range vs the 5-day average daily range
                   (morning / closing volatility), India VIX change over 15 min (sudden rise = spike; VIX_TREND_FILTER:
                   falling VIX confirms CE, rising VIX confirms PE, once the 15-min change passes VIX_TREND_MIN_PCT -
                   user's 2026-10-30 observation on job 194, VIX down while NIFTY ran up)
  momentum         RSI(14), last 3 candles' move
  options flow     PCR (open interest, strikes within PCR_WITHIN_POINTS) and its 30-min change; CE vs PE volume
                   traded in the last 15 min (from the option chain)
  liquidity        LIQUID_STRIKE: within LIQUID_STRIKE_BAND strikes of ATM, the strike with the most OI (or volume,
                   LIQUID_STRIKE_METRIC) on each side, re-read every candle - that is the strike actually bought,
                   not always ATM. LIQUID_MIN (optional) blocks an entry on a side when its best nearby strike is
                   still too thin. Re-checked at the start of every session (S1..S5) along with the rest of cond_text.
  regime           spike    VIX +VIX_SPIKE_PCT % in 15 min, or ATR ratio >= ATR_EXPANSION
                   quiet    ATR ratio <= ATR_QUIET (no trade) - a hard override, never scored (below)
                   trend    ADX >= ADX_TREND, ER >= ER_TREND, EMA 9/21 and the close agree (trend_up / trend_down)
                   range    ADX < ADX_RANGE or ER < ER_RANGE
                   mixed    otherwise (also: regime_confidence() is not defined for mixed - already the catch-all)
  confidence       trend/range/spike get a 0-100 regime_confidence (user, 2026-10-30): 4 criteria x 25 points each -
                   trend: ADX, EMA9/21 direction, EMA21 slope, price structure (HH/HL or LH/LL over STRUCT_BARS);
                   range: ADX, flat EMA21, ER, RSI exhaustion (>=60 or <=40); spike: abnormal candle range (ATR14 x
                   REGIME_SPIKE_ATR_MULT), sudden ATR expansion, RSI/VIX exhaustion, compression beforehand
                   (COMPRESSION_LOOKBACK). Below REGIME_MIN_CONFIDENCE the regime becomes 'uncertain' - a 5th
                   classification absent from the auto map below, so indicator disagreement skips the trade instead
                   of forcing one; cond_text() logs it as "uncertain (<was> NN% conf)".
  auto picks:      trend -> trend (pullback continuation) | range -> reversion (Bollinger bounce)
                   spike -> breakout (with the move; with a VIX spike only PE, research: VIX jumps come with falls)
                   mixed -> direction (rising ATM premium readings) | quiet / uncertain -> NO TRADE
  optional filters PCR_FILTER (CE needs PCR rising / PE falling over 30 min), FLOW_FILTER (CE needs more CE than PE
                   volume in the last 15 min and vice versa), VIX_SPIKE_NO_CE (no CE in a VIX spike), LIQUID_MIN
                   (skip a side when its best nearby strike's OI/volume is below this), VIX_TREND_FILTER (above)

PLAYS
  orb            Smart ORB (user, 2026-10-30). S2 only to arm, until ORB_LAST_ENTRY: a 5-min close beyond the OR +
                 ORB_BUFFER_PTS (OR width ORB_MIN..MAX_WIDTH_PTS) only arms a setup if the candle shows conviction
                 (body >= ORB_BODY_PCT of its range, EMA9/21 aligned, RSI past ORB_RSI_BUY/SELL, not an abnormal
                 ATR14 spike, regime not spike/uncertain) - the breakout candle itself is never bought. Price then
                 has ORB_RETEST_MAX_BARS candles to come back within ORB_RETEST_PTS of the level and hold (closing
                 the right side of it) before entry; a close back through the level by ORB_STOP_PTS invalidates the
                 setup first. A breakout-quality score (/100: trend 20, candle strength 20, RSI momentum 15, ATR
                 expansion 15, +30 once the retest holds; 80+ high-quality, 70-79 normal, 60-69 watch only, <60 no
                 trade - starting thresholds, not validated) gates entry at ORB_MIN_SCORE and sets the profit target
                 (ORB_TARGET_R_HQ or ORB_TARGET_R x RISK_PER_TRADE - minimum RR 1:1.5). Invalid once in a trade: a
                 close back through the breakout level by ORB_STOP_PTS.
  direction      ATM CE / PE readings every READING_INTERVAL_MIN on a clock grid from the session start; one premium
                 rising RISING_READINGS times and the other not -> buy it.
  trend          trend_up: the previous candle dipped to EMA 9, this one closes above EMA 9 and the previous high,
                 and (TREND_RSI_FILTER) RSI(14) > 50 -> CE (mirror PE, RSI < 50). The EMA+RSI+price-action combo also
                 drives S1 (09:15-09:30), where it runs on yesterday's warm-up indicators from the first candle.
                 Invalid: a close beyond EMA 21.
  reversion      range: previous close outside Bollinger(20, 2), this close back inside, RSI below 40 / above 60 ->
                 CE at the lower band / PE at the upper. Exit in profit at the middle band; invalid beyond the extreme.
  breakout       spike: close beyond the high / low of the previous BREAKOUT_BARS candles in the direction of the last
                 3 candles. Invalid: a close back inside by half an ATR.
  late_momentum  S5, once at LATE_ENTRY_TIME: the first half-hour return (09:15 open -> 09:45 close, vs the previous
                 close) is at least LATE_MIN_PCT % and the day still moves that way -> buy that side, hold to the end
                 (Gao, Han, Li, Zhou, Journal of Financial Economics 2018: first half-hour predicts the last one).
EXITS (every 15 s): stop -RISK_PER_TRADE (never more than the day's remaining loss budget); at each profit step hold
  with a floor (LOCK_PCT) while the play's condition still holds, else book; near the next pivot level while in
  profit; the play's invalidation (5-min close); square-off. Never overnight, never adds, never sells options.

Strike actually traded (user, 2026-10-30): every play used to buy the ATM strike regardless of OI. LIQUID_STRIKE now
picks, within a small band around ATM, whichever strike on the entry side carries the most OI (default) or volume -
better fills (tighter spread) without drifting to a far strike just because it holds old, stale OI.

S1 / VIX trend / ORB stop (user, 2026-10-30, from job 194's replay of 2026-10-30): S1 was pure discovery (no trade) -
now it trades the EMA+RSI+price-action 'trend' play (same play S2-S5 use on a trend_up/trend_down regime; RSI is new
everywhere, not just S1). VIX_TREND_FILTER adds the observed VIX-down/index-up (and reverse) relationship as a
same-direction confirmation across every session. orb's invalidation moved from the OR middle to a tight
ORB_STOP_PTS (10-12 pts) back through the breakout level itself, symmetric on both the OR-high/CE and OR-low/PE
sides, so a failed breakout is cut close to where it failed rather than given most of the range back.

Smart ORB + regime confidence (user, 2026-10-30, "compare and decide"): orb became a two-stage engine - a scored
breakout candle arms a setup, a held retest confirms it (never buys the breakout candle itself) - gated by a 0-100
breakout-quality score that also sets the profit target. The regime classifier keeps its existing thresholds and
names (changing spike/trend/range's tuned cutoffs felt riskier than scoring their existing evidence) but now also
carries a 0-100 confidence per regime and downgrades a weak read to 'uncertain', which picks no auto play - so
disagreeing indicators skip the trade instead of guessing. ORB itself is not mapped from 'breakout' regime/confidence
(it is a once-per-day, 09:15-09:30-range play, not a session-agnostic regime->play mapping like the others).

Evidence: see knowledge/strategy_registry.md (2026-10-30). Run first with OpenAlgo Analyzer (paper) mode ON.
"""

import json
import os
import time
from datetime import datetime, timedelta
from openalgo import api

from script_openalgo.strategy_lib import levels  # noqa: E402

# ======================= PARAMETERS =======================
API_KEY = os.getenv("OPENALGO_API_KEY", "PASTE_YOUR_API_KEY_HERE")
HOST = os.getenv("OPENALGO_HOST", "http://127.0.0.1:5000")
STRATEGY = "AllInOne"

UNDERLYING = "NIFTY"
INDEX_EXCHANGE = "NSE_INDEX"
OPT_EXCHANGE = "NFO"
VIX_SYMBOL = "INDIAVIX"
PRODUCT = "MIS"

# ---- sessions: ON / START / END / PLAYS ("auto" = by market condition; or a list: orb, direction, trend,
#      reversion, breakout, late_momentum; "none" = no trade) ----
S1_ON = True                 # 2026-10-30: EMA+RSI+price action ('trend') - was off (discovery only)
S1_START = "09:15"
S1_END = "09:30"
S1_PLAYS = "trend"
S2_ON = True
S2_START = "09:30"
S2_END = "11:00"
S2_PLAYS = "auto,orb"
S3_ON = True
S3_START = "11:00"
S3_END = "13:00"
S3_PLAYS = "auto"
S4_ON = True
S4_START = "13:00"
S4_END = "14:00"
S4_PLAYS = "auto"
S5_ON = True
S5_START = "14:00"
S5_END = "15:00"
S5_PLAYS = "auto,late_momentum"
SQUARE_OFF_TIME = "15:15"

# ---- risk: one rupee number drives the stop, the target, the daily limit and (optionally) the size ----
RISK_PER_TRADE = 1000       # rupees lost at the stop of one trade (e.g. 1000 ... 10000)
RISK_SIZING = "risk"        # "risk": lots = RISK_PER_TRADE / RISK_PER_LOT (max MAX_LOTS) | "lots": always LOTS
RISK_PER_LOT = 1000         # the rupee stop one lot gets (NIFTY ATM: ~15 premium points x 65)
LOTS = 1
MAX_LOTS = 10
TARGET_R = 1.0              # profit step = TARGET_R x RISK_PER_TRADE
LOCK_PCT = 50               # hold at a profit step: floor = step - (100 - LOCK_PCT) % of the step size
DAILY_RISK_MULT = 3         # daily loss limit = DAILY_RISK_MULT x RISK_PER_TRADE
MAX_TRADES_PER_SESSION = 3
MAX_TRADES = 6
MAX_CONSECUTIVE_LOSSES = 3
MIN_MINUTES_LEFT = 20       # no entry with less than this left before SQUARE_OFF_TIME
COOLDOWN_MIN = 5
SR_EXIT_PTS = 15            # exit near the next pivot level in the trade's direction while in profit; 0 = off
SR_MIN_ROOM = 30            # skip an entry when that level is closer than this (index points); 0 = off

# ---- market condition engine ----
ADX_TREND = 22
ADX_RANGE = 18
ER_TREND = 0.35
ER_RANGE = 0.25
ATR_EXPANSION = 1.6         # ATR(5) / ATR(30)
ATR_QUIET = 0.6
VIX_SPIKE_PCT = 3.0         # India VIX up this % within 15 min = spike
VIX_SPIKE_NO_CE = True      # no CE entries during a VIX spike
VIX_TREND_FILTER = True     # falling VIX confirms CE, rising VIX confirms PE, once |15-min change| >= VIX_TREND_MIN_PCT
VIX_TREND_MIN_PCT = 1.0     # below this the VIX move is noise - no filter either way
REGIME_MIN_CONFIDENCE = 50  # trend/range/spike regimes get a 0-100 confidence (4 criteria x 25, see regime_confidence);
                            # below this the regime downgrades to 'uncertain' - absent from choose()'s auto map, so
                            # no play is picked rather than forcing a trade on a low-conviction read; not validated
STRUCT_BARS = 4             # trend confidence: this many candles of strictly higher/lower highs+lows (price structure)
EMA21_SLOPE_BARS = 5        # trend/range confidence: EMA21 now vs this many candles ago
COMPRESSION_LOOKBACK = 6    # spike confidence: ATR ratio was <= 1.0 (contracting) at some point in this many candles
REGIME_SPIKE_ATR_MULT = 2.5 # spike confidence: this candle's range vs ATR14 counts as "abnormal"
PCR_WITHIN_POINTS = 250
PCR_FILTER = False          # CE needs PCR rising over 30 min, PE falling
FLOW_FILTER = False         # CE needs more CE than PE volume in the last 15 min, PE the opposite
LIQUID_STRIKE = True        # buy the most liquid nearby strike (OI/volume) instead of always ATM (see below)
LIQUID_STRIKE_BAND = 2      # how many strikes either side of ATM to consider
LIQUID_STRIKE_METRIC = "oi" # "oi" | "volume" - which field decides "most liquid"
LIQUID_MIN = 0              # skip an entry on a side when its best nearby strike's OI/volume is below this; 0 = off
WARMUP_CANDLES = 60         # indicator warm-up from the previous day: this many 5-min candles' worth of time
CANDLE_MIN = 5              # candle size of the engine and the plays: 1, 3 or 5 minutes (built from 1-min candles,
                            # so 3 min works live too); lookbacks (ADX 14, ATR 30, BB 20 ...) count candles
PLAN_BIAS_FILTER = True     # session plan: after a trending session only trade its direction (see plan_session)
DAY_TREND_FILTER = True     # no counter-trend entry while the DAY so far trends: day ER >= DAY_TREND_ER and the move
DAY_TREND_ER = 0.30         # from the open >= DAY_TREND_MOVE x the 5-day average range
DAY_TREND_MOVE = 0.40

# ---- plays ----
ORB_BUFFER_PTS = 5
ORB_STOP_PTS = 12           # orb invalidation: a close back through the breakout level (OR high for CE, OR low for
                            # PE) by this many points - tight, right at the level that was supposed to hold
ORB_LAST_ENTRY = "10:30"
ORB_MIN_WIDTH_PTS = 20
ORB_MAX_WIDTH_PTS = 200
ORB_NEEDS_ROOM = True
ORB_BODY_PCT = 0.60         # Smart ORB: breakout candle body must be at least this % of its high-low range
ORB_EMA_FILTER = True       # breakout needs EMA9/EMA21 aligned with the side (9>21 for CE, 9<21 for PE)
ORB_RSI_BUY = 55            # breakout RSI(14) must be above this for CE (ORB_RSI_FULL_CE = full quality-score marks)
ORB_RSI_SELL = 45           # ... below this for PE
ORB_RSI_FULL_CE = 70        # RSI at/above this scores full ORB momentum points (CE)
ORB_RSI_FULL_PE = 30        # RSI at/below this scores full ORB momentum points (PE)
ORB_ATR_SPIKE_MULT = 2.5    # skip the breakout candle if its range > this x ATR(14) (abnormal spike/wick, not a clean break)
ORB_RETEST = True           # require a retest that holds the broken level before entering (never the breakout candle)
ORB_RETEST_PTS = 10         # retest counts when price comes back within this of the level without closing through it
ORB_RETEST_MAX_BARS = 6     # cancel the setup if no retest confirms within this many candles
ORB_MIN_SCORE = 70          # breakout quality score (/100, see orb_score/sig_orb) needed to actually enter ("Normal"+)
ORB_TARGET_R = 1.5          # orb profit step = this x RISK_PER_TRADE for a Normal-quality setup (score 70-79) - min RR 1:1.5
ORB_TARGET_R_HQ = 2.0       # ... for a High-quality setup (score >= 80)
READING_INTERVAL_MIN = 5    # direction
RISING_READINGS = 3
CONTINUE_READINGS = 2
TREND_RSI_FILTER = True     # trend: also needs RSI(14) agreeing (>50 for CE, <50 for PE) - the "RSI" in EMA+RSI+
                            # price-action; applies wherever 'trend' fires, S1 included
BB_PERIOD = 20              # reversion
BB_STD = 2.0
BREAKOUT_BARS = 12          # breakout
OI_ZONE_PTS = 40            # oi_bounce: the previous candle came within this of the OI support / resistance
                            # (user's 29 Oct PE sales at the OI support were 18-48 pts above it)
OI_BREAK_PTS = 20           # oi_bounce: invalid when a 5-min close is this far through the level
OI_MIN_BUILD = 0            # oi_bounce: minimum OI added at the level since the first reading (contracts); 0 = any
OI_IN_TREND = False         # oi_bounce also in a trend, with the trend only (29 Oct pattern; 29 days: +5008 gross vs
                            # +6260 without, more trades and costs) - off
CHEAP_PCT = 0               # oi_bounce: the option bought must be at least this % below its recent high; 0 = off
CHEAP_LOOKBACK_MIN = 30     # "recent" = the last 30 minutes
SR_LEVELS = "pivots"        # exit / room levels: "pivots" | "oi" (OI-build support / resistance) | "both"
LATE_ENTRY_TIME = "14:45"   # late_momentum
LATE_MIN_PCT = 0.25
POLL_SEC = 15
# ==========================================================



def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {msg}", flush=True)


def hhmm(s):
    h, m = map(int, s.split(":"))
    return h, m


# ---------- indicators (pure, on lists of candles {o, h, l, c}) ----------
def ema(values, n):
    out, e, k = [], None, 2 / (n + 1)
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def true_ranges(bars):
    return [b["h"] - b["l"] if i == 0 else max(b["h"] - b["l"], abs(b["h"] - bars[i - 1]["c"]), abs(b["l"] - bars[i - 1]["c"]))
            for i, b in enumerate(bars)]


def atr(bars, n):
    tr = true_ranges(bars)
    return sum(tr[-n:]) / n if len(tr) >= n else None


def adx(bars, n=14):
    if len(bars) < 2 * n + 1:
        return None
    tr, pdm, mdm = [], [], []
    for i in range(1, len(bars)):
        up, dn = bars[i]["h"] - bars[i - 1]["h"], bars[i - 1]["l"] - bars[i]["l"]
        pdm.append(up if up > dn and up > 0 else 0.0)
        mdm.append(dn if dn > up and dn > 0 else 0.0)
        tr.append(max(bars[i]["h"] - bars[i]["l"], abs(bars[i]["h"] - bars[i - 1]["c"]), abs(bars[i]["l"] - bars[i - 1]["c"])))
    s_tr, s_p, s_m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs = []
    for i in range(n, len(tr)):
        s_tr, s_p, s_m = s_tr - s_tr / n + tr[i], s_p - s_p / n + pdm[i], s_m - s_m / n + mdm[i]
        pdi, mdi = (100 * s_p / s_tr, 100 * s_m / s_tr) if s_tr else (0, 0)
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0)
    if len(dxs) < n:
        return None
    a = sum(dxs[:n]) / n
    for d in dxs[n:]:
        a = (a * (n - 1) + d) / n
    return a


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    g = l_ = 0.0
    for a, b in zip(closes[:n], closes[1:n + 1]):
        g, l_ = g + max(b - a, 0), l_ + max(a - b, 0)
    ag, al = g / n, l_ / n
    for a, b in zip(closes[n:], closes[n + 1:]):
        ag, al = (ag * (n - 1) + max(b - a, 0)) / n, (al * (n - 1) + max(a - b, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def efficiency_ratio(closes, n=10):
    if len(closes) <= n:
        return None
    w = closes[-(n + 1):]
    path = sum(abs(b - a) for a, b in zip(w, w[1:]))
    return abs(w[-1] - w[0]) / path if path else 0.0


def bollinger(closes, n, k):
    if len(closes) < n:
        return None
    w = closes[-n:]
    m = sum(w) / n
    sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
    return m - k * sd, m, m + k * sd


class Strategy:
    def __init__(self, client, clock=datetime.now, sleeper=time.sleep):
        self.c = client
        self.now = clock
        self.sleep = sleeper
        self.warm, self.bars = [], []        # previous day's / today's completed 5-min candles
        self.seen_bar = None
        self.session = None                  # name of the ON session now (None = outside)
        self.reports = {}
        self.sess_trades = {}
        self.trades = 0
        self.day_pnl = 0.0
        self.losses_in_row = 0
        self.position = None
        self.cool_until = None
        self.pivots, self.prev_close, self.avg_range = None, None, None
        self.opening = None
        self.orb_done = self.late_done = False
        self.orb_pending = None              # armed breakout waiting for a retest: {side, level, bars, pre_score, parts}
        self.next_target_r = None            # one-shot TARGET_R override for the next enter() (Smart ORB quality score)
        self.vix_hist = []                   # (time, vix) per candle
        self.atr_hist = []                   # (time, atr_ratio) per candle - regime confidence (compression check)
        self.flow_hist = []                  # (time, pcr, ce_vol, pe_vol) per candle
        self.oi_open = {}                    # strike -> (CE OI, PE OI) at the first chain reading of the day
        self.chain = {}                      # {rows, atm_strike, step} from the latest option chain read
        self.cond = {}                       # latest market condition (shown on the Signals chart)
        self.plan = None                     # the current session's plan (plan_session)
        self.regime_count = {}
        self.state = {}
        self.levels = {}
        self.lot = None
        self.reset_readings()

    # ---------- time ----------
    def at(self, s):
        h, m = hhmm(s)
        return self.now().replace(hour=h, minute=m, second=0, microsecond=0)

    def session_table(self):
        return [("S1", S1_ON, S1_START, S1_END, S1_PLAYS), ("S2", S2_ON, S2_START, S2_END, S2_PLAYS),
                ("S3", S3_ON, S3_START, S3_END, S3_PLAYS), ("S4", S4_ON, S4_START, S4_END, S4_PLAYS),
                ("S5", S5_ON, S5_START, S5_END, S5_PLAYS)]

    def current_session(self):
        """(name, start, plays) of the session the clock is in (ON or not), or None between sessions."""
        now = self.now()
        for name, on, start, end, plays in self.session_table():
            if self.at(start) <= now < self.at(end):
                ps = {p.strip() for p in str(plays).split(",") if p.strip() and p.strip() != "none"}
                return name, start, ps if on else set()
        return None

    # ---------- market data ----------
    def ltp(self, symbol, exchange):
        for _ in range(2):
            try:
                r = self.c.quotes(symbol=symbol, exchange=exchange)
                if r.get("status") == "success":
                    p = float(r["data"].get("ltp") or 0)
                    if p > 0:
                        return p
            except Exception as e:
                log(f"quotes exception {symbol}: {e}")
            self.sleep(2)
        return None

    def nearest_expiry(self):
        r = self.c.expiry(symbol=UNDERLYING, exchange=OPT_EXCHANGE, instrumenttype="options")
        if r.get("status") != "success" or not r.get("data"):
            raise RuntimeError(f"expiry lookup failed: {r}")
        return r["data"][0].replace("-", "").upper()

    def atm(self):
        exp = self.nearest_expiry()
        out = {}
        for ot in ("CE", "PE"):
            r = self.c.optionsymbol(underlying=UNDERLYING, exchange=INDEX_EXCHANGE, expiry_date=exp, offset="ATM", option_type=ot)
            if r.get("status") != "success":
                raise RuntimeError(f"optionsymbol failed: {r}")
            out[ot] = r["symbol"]
            self.lot = int(r["lotsize"])
        return out

    @staticmethod
    def _rows(df, keep):
        rows = []
        for ts, row in df.iterrows():
            t = ts.to_pydatetime().replace(tzinfo=None)
            if keep(t):
                rows.append({"t": t, "o": float(row["open"]), "h": float(row["high"]), "l": float(row["low"]), "c": float(row["close"])})
        rows.sort(key=lambda b: b["t"])
        return rows

    @staticmethod
    def _resample(rows, minutes):
        """1-min candles -> CANDLE_MIN candles, buckets from 09:15 of each day (label = bucket start)."""
        if minutes == 1:
            return rows
        out = {}
        for r in rows:
            t = r["t"]
            base = t.replace(hour=9, minute=15, second=0, microsecond=0)
            if t < base:
                continue
            k = base + timedelta(minutes=(int((t - base).total_seconds()) // 60 // minutes) * minutes)
            b = out.get(k)
            if b is None:
                out[k] = {"t": k, "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"]}
            else:
                b["h"], b["l"], b["c"] = max(b["h"], r["h"]), min(b["l"], r["l"]), r["c"]
        return [out[k] for k in sorted(out)]

    def refresh_bars(self):
        today = self.now().date()
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="1m",
                                start_date=today.isoformat(), end_date=today.isoformat(), source="api")
        except Exception as e:
            log(f"history exception: {e}")
            return
        if hasattr(df, "iterrows") and not df.empty:
            now = self.now()
            m1 = self._rows(df, lambda t: t.date() == today and t + timedelta(minutes=1) <= now)
            self.bars = [b for b in self._resample(m1, CANDLE_MIN) if b["t"] + timedelta(minutes=CANDLE_MIN) <= now]

    def load_day_context(self):
        """Once a day: previous day's candles (indicator warm-up), close, pivot levels, 5-day average range."""
        today = self.now().date()
        try:
            df = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="1m",
                                start_date=(today - timedelta(days=7)).isoformat(),
                                end_date=(today - timedelta(days=1)).isoformat(), source="api")
            if hasattr(df, "iterrows") and not df.empty:
                warm = self._resample(self._rows(df, lambda t: t.date() < today), CANDLE_MIN)
                self.warm = warm[-max(1, WARMUP_CANDLES * 5 // CANDLE_MIN):]
        except Exception as e:
            log(f"warm-up candles unavailable ({e}); indicators start with today's candles")
        self.pivots = []
        try:
            d = self.c.history(symbol=UNDERLYING, exchange=INDEX_EXCHANGE, interval="D",
                               start_date=(today - timedelta(days=15)).isoformat(), end_date=today.isoformat(), source="api")
            d = d[[ix.date() < today for ix in d.index]]
            c = levels.cpr_from_daily(d)
            if c:
                self.prev_close = c["prev_close"]
                self.pivots = sorted({round(c[k], 2) for k in ("s3", "s2", "s1", "bc", "pivot", "tc", "r1", "r2", "r3",
                                                               "prev_high", "prev_low")})
                last5 = d.tail(5)
                self.avg_range = float((last5["high"] - last5["low"]).mean()) if len(last5) else None
        except Exception as e:
            log(f"daily levels unavailable ({e}); pivot exits / room checks off today")
        log(f"Day context: {len(self.warm)} warm-up candles | previous close {self.prev_close} | 5-day avg range "
            f"{self.avg_range if self.avg_range is None else round(self.avg_range, 1)} | pivots "
            + ", ".join(f"{x:.0f}" for x in self.pivots or []))

    def next_level(self, side, idx):
        pool = list(self.pivots or []) if SR_LEVELS in ("pivots", "both") else []
        if SR_LEVELS in ("oi", "both"):
            pool += [x for x in (self.cond.get("oi_support"), self.cond.get("oi_resistance")) if x]
        cand = [x for x in pool if (x > idx if side == "CE" else x < idx)]
        return (min(cand) if side == "CE" else max(cand)) if cand else None

    def best_strike(self, side):
        """LIQUID_STRIKE: within LIQUID_STRIKE_BAND strikes of ATM, the strike on `side` with the most OI/volume
        (LIQUID_STRIKE_METRIC), from the chain self.chain holds (fetched this candle in update_condition). Returns
        (value, strike, symbol, lotsize), or None when the chain is stale/empty or nothing in the band qualifies."""
        atm_k, step, rows = self.chain.get("atm_strike"), self.chain.get("step"), self.chain.get("rows")
        if atm_k is None or not step or not rows:
            return None
        leg = "ce" if side == "CE" else "pe"
        best = None
        for r in rows:
            if abs(r["strike"] - atm_k) > LIQUID_STRIKE_BAND * step:
                continue
            o = r.get(leg)
            if not o or not o.get("symbol"):
                continue
            val = (o.get("volume") if LIQUID_STRIKE_METRIC == "volume" else o.get("oi")) or 0
            if best is None or val > best[0]:
                best = (val, r["strike"], o["symbol"], o.get("lotsize"))
        return best

    def entry_symbol(self, side, play):
        """The option to buy: LIQUID_STRIKE picks the most liquid nearby strike (logged); otherwise, or when the
        chain has nothing in band, falls back to plain ATM (direction reuses the ATM symbols already read).
        self.lot is normally a side effect of atm() - the liquid-strike path never calls it, so it is set here too
        (bug found 2026-10-30: self.lot stayed None all day whenever the very first entry took this path, crashing
        qty = self.lot * lots in enter())."""
        if LIQUID_STRIKE:
            picked = self.best_strike(side)
            if picked:
                val, strike, sym, lotsize = picked
                if lotsize:
                    self.lot = int(lotsize)
                log(f"{play} {side}: strike {strike:.0f} picked for liquidity ({LIQUID_STRIKE_METRIC} {val:,.0f}, "
                    f"within {LIQUID_STRIKE_BAND} strikes of ATM)")
                return sym
        return (self.syms or self.atm())[side] if play == "direction" else self.atm()[side]

    # ---------- market condition engine ----------
    @staticmethod
    def _struct(bars, n):
        """Price structure over the last n candles: 'up' (strictly higher highs AND higher lows), 'down' (strictly
        lower highs AND lower lows), else None."""
        if len(bars) < n:
            return None
        w = bars[-n:]
        highs, lows = [x["h"] for x in w], [x["l"] for x in w]
        if all(b > a for a, b in zip(highs, highs[1:])) and all(b > a for a, b in zip(lows, lows[1:])):
            return "up"
        if all(b < a for a, b in zip(highs, highs[1:])) and all(b < a for a, b in zip(lows, lows[1:])):
            return "down"
        return None

    def regime_confidence(self, reg, allb, cond):
        """0-100 confidence in the already-chosen regime `reg` (user, 2026-10-30): 4 criteria x 25 points each, the
        same weighted-score idea as the ORB quality score. None for 'mixed'/'quiet' (no scoring - 'mixed' is already
        the catch-all low-conviction bucket, 'quiet' is a hard ATR-ratio override, neither needs a confidence read).
        The caller downgrades to 'uncertain' below REGIME_MIN_CONFIDENCE so a weak read does not force a trade."""
        ad, er, ar, r = cond.get("adx"), cond.get("er"), cond.get("atr_ratio"), cond.get("rsi")
        e9, e21, a14 = cond.get("ema9"), cond.get("ema21"), cond.get("atr14")
        e21_series = ema([b["c"] for b in allb], 21)
        slope = (e21_series[-1] - e21_series[-1 - EMA21_SLOPE_BARS]) if len(e21_series) > EMA21_SLOPE_BARS else None
        if reg in ("trend_up", "trend_down"):
            up = reg == "trend_up"
            struct = self._struct(allb, STRUCT_BARS)
            return ((25 if ad is not None and ad >= ADX_TREND else 0) + (25 if e9 and e21 and ((e9 > e21) == up) else 0)
                    + (25 if slope is not None and ((slope > 0) == up) else 0) + (25 if struct == ("up" if up else "down") else 0))
        if reg == "range":
            flat = slope is not None and a14 and abs(slope) < a14 * 0.1
            return ((25 if ad is not None and ad < ADX_RANGE else 0) + (25 if flat else 0)
                    + (25 if er is not None and er < ER_RANGE else 0) + (25 if r is not None and (r >= 60 or r <= 40) else 0))
        if reg == "spike":
            now = self.now()
            cutoff = now - timedelta(minutes=CANDLE_MIN * COMPRESSION_LOOKBACK)
            compressed = any(x <= 1.0 for t, x in self.atr_hist if cutoff <= t < now and x is not None)
            abnormal = a14 and allb[-1]["h"] - allb[-1]["l"] > REGIME_SPIKE_ATR_MULT * a14
            sudden = ar is not None and ar >= ATR_EXPANSION * 1.3
            exhaustion = (r is not None and (r >= 80 or r <= 20)) or cond.get("vix_spike")
            return (25 if abnormal else 0) + (25 if sudden else 0) + (25 if exhaustion else 0) + (25 if compressed else 0)
        return None

    def update_condition(self):
        """Indicators and regime from completed candles (warm-up + today), VIX and the option chain."""
        now, allb = self.now(), self.warm + self.bars
        closes = [b["c"] for b in allb]
        e9, e21 = ema(closes, 9), ema(closes, 21)
        a5, a30, a14 = atr(allb, 5), atr(allb, 30), atr(allb, 14)
        cond = {"adx": adx(allb), "er": efficiency_ratio(closes), "rsi": rsi(closes), "ema9": e9[-1] if e9 else None,
                "ema21": e21[-1] if e21 else None, "atr5": a5, "atr14": a14, "atr_ratio": (a5 / a30) if a5 and a30 else None,
                "close": closes[-1] if closes else None}
        if self.bars and self.avg_range:
            cond["day_range_vs_avg"] = (max(b["h"] for b in self.bars) - min(b["l"] for b in self.bars)) / self.avg_range
        self.atr_hist.append((now, cond["atr_ratio"]))          # regime confidence: compression-before-expansion
        v = self.ltp(VIX_SYMBOL, INDEX_EXCHANGE)
        if v:
            self.vix_hist.append((now, v))
            old = [x for t, x in self.vix_hist if t <= now - timedelta(minutes=15)]
            cond["vix"], cond["vix_chg15"] = v, ((v / old[-1] - 1) * 100 if old else None)
        try:
            ch = self.c.optionchain(underlying=UNDERLYING, exchange=INDEX_EXCHANGE, expiry_date=self.nearest_expiry(),
                                    strike_count=int(PCR_WITHIN_POINTS // 50) + 2)
            if ch and ch.get("status") == "success":
                atm_k = ch.get("atm_strike")
                rows = [r for r in ch.get("chain", []) if atm_k is None or abs(r["strike"] - atm_k) <= PCR_WITHIN_POINTS]
                ce_oi = sum(float((r.get("ce") or {}).get("oi") or 0) for r in rows)
                pe_oi = sum(float((r.get("pe") or {}).get("oi") or 0) for r in rows)
                vols = {float(r["strike"]): (float((r.get("ce") or {}).get("volume") or 0),
                                             float((r.get("pe") or {}).get("volume") or 0)) for r in rows}
                self.flow_hist.append((now, pe_oi / ce_oi if ce_oi else None, vols))
                # OI build-up per strike since the first reading of the day: where writers are adding
                spot = float(ch.get("underlying_ltp") or cond["close"] or 0)
                build = {}
                for r in ch.get("chain", []):
                    k = float(r["strike"])
                    oi = (float((r.get("ce") or {}).get("oi") or 0), float((r.get("pe") or {}).get("oi") or 0))
                    self.oi_open.setdefault(k, oi)
                    build[k] = (oi[0] - self.oi_open[k][0], oi[1] - self.oi_open[k][1])
                below = [k for k in build if k <= spot and build[k][1] > OI_MIN_BUILD]
                above = [k for k in build if k >= spot and build[k][0] > OI_MIN_BUILD]
                cond["oi_support"] = max(below, key=lambda k: build[k][1]) if below else None
                cond["oi_resistance"] = max(above, key=lambda k: build[k][0]) if above else None
                cond["oi_support_add"] = build[cond["oi_support"]][1] if cond["oi_support"] else None
                cond["oi_resistance_add"] = build[cond["oi_resistance"]][0] if cond["oi_resistance"] else None
                cond["pcr"] = self.flow_hist[-1][1]
                o30 = [x for x in self.flow_hist if x[0] <= now - timedelta(minutes=30) and x[1]]
                if o30 and cond["pcr"]:
                    cond["pcr_chg30"] = cond["pcr"] - o30[-1][1]
                o15 = [x for x in self.flow_hist if x[0] <= now - timedelta(minutes=15)]
                if o15:                                      # volume traded in the last 15 min, same strikes only
                    old = o15[-1][2]
                    common = [k for k in vols if k in old]
                    dce = sum(max(0.0, vols[k][0] - old[k][0]) for k in common)
                    dpe = sum(max(0.0, vols[k][1] - old[k][1]) for k in common)
                    cond["flow_ce_share"] = dce / (dce + dpe) if dce + dpe > 0 else None
                strikes = sorted({float(r["strike"]) for r in ch.get("chain", [])})
                step = min((b - a for a, b in zip(strikes, strikes[1:])), default=None)
                self.chain = {"rows": ch.get("chain", []), "atm_strike": atm_k, "step": step}
                if LIQUID_STRIKE:
                    for side in ("CE", "PE"):
                        best = self.best_strike(side)
                        if best:
                            val, bk, _, _ = best
                            cond[f"liquid_{side.lower()}_strike"], cond[f"liquid_{side.lower()}_val"] = bk, val
        except Exception as e:
            log(f"option chain unavailable ({e})")
        # regime
        reg = "mixed"
        spike = (cond.get("vix_chg15") or 0) >= VIX_SPIKE_PCT
        ar, ad, er = cond["atr_ratio"], cond["adx"], cond["er"]
        if spike or (ar is not None and ar >= ATR_EXPANSION):
            reg = "spike"
        elif ar is not None and ar <= ATR_QUIET:
            reg = "quiet"
        elif ad is not None and er is not None and ad >= ADX_TREND and er >= ER_TREND and cond["ema9"] and cond["ema21"]:
            up = cond["ema9"] > cond["ema21"] and cond["close"] > cond["ema9"]
            dn = cond["ema9"] < cond["ema21"] and cond["close"] < cond["ema9"]
            reg = "trend_up" if up else "trend_down" if dn else "mixed"
        elif (ad is not None and ad < ADX_RANGE) or (er is not None and er < ER_RANGE):
            reg = "range"
        cond["vix_spike"] = spike
        conf = self.regime_confidence(reg, allb, cond) if allb else None
        cond["regime_confidence"] = conf
        if conf is not None and conf < REGIME_MIN_CONFIDENCE:
            cond["regime_was"] = reg                 # kept for session reports / logs, not used by choose()
            reg = "uncertain"
        cond["regime"] = reg
        self.cond = cond
        self.regime_count[reg] = self.regime_count.get(reg, 0) + 1
        self.state = {k: round(v, 3) for k, v in cond.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        return cond

    def cond_text(self):
        c = self.cond

        def f(k, d=2):
            v = c.get(k)
            return "-" if v is None else f"{v:.{d}f}"

        def g(k):
            v = c.get(k)
            return "-" if v is None else f"{v:,.0f}"
        reg, conf_s = c.get('regime'), f(('regime_confidence'), 0)
        if reg == "uncertain" and c.get('regime_was'):
            reg_s = f"{reg} ({c['regime_was']} {conf_s}% conf)"
        elif c.get('regime_confidence') is not None:
            reg_s = f"{reg} ({conf_s}% conf)"
        else:
            reg_s = str(reg)
        return (f"regime {reg_s} | ADX {f('adx', 1)} ER {f('er')} RSI {f('rsi', 0)} ATR5/30 {f('atr_ratio')} "
                f"range/avg {f('day_range_vs_avg')} VIX {f('vix')} ({f('vix_chg15', 1)}%/15m) PCR {f('pcr')} "
                f"({f('pcr_chg30')}/30m) CE-flow {f('flow_ce_share')} | liquid CE {g('liquid_ce_strike')}/{g('liquid_ce_val')} "
                f"PE {g('liquid_pe_strike')}/{g('liquid_pe_val')}")

    def day_trend(self):
        """'CE' / 'PE' when the day so far is a clear trend (DAY_TREND_ER, DAY_TREND_MOVE), else None."""
        if not self.bars or not self.avg_range:
            return None
        closes = [self.bars[0]["o"]] + [b["c"] for b in self.bars]
        path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
        move = closes[-1] - closes[0]
        er = abs(move) / path if path else 0.0
        self.cond["day_er"], self.cond["day_move"] = er, move
        if er >= DAY_TREND_ER and abs(move) >= DAY_TREND_MOVE * self.avg_range:
            return "CE" if move > 0 else "PE"
        return None

    def filters_ok(self, side):
        c = self.cond
        if VIX_SPIKE_NO_CE and side == "CE" and c.get("vix_spike"):
            return "VIX spike: no CE"
        if VIX_TREND_FILTER and c.get("vix_chg15") is not None and abs(c["vix_chg15"]) >= VIX_TREND_MIN_PCT:
            want_ce = c["vix_chg15"] < 0
            if (side == "CE") != want_ce:
                return f"VIX {'falling' if want_ce else 'rising'} {c['vix_chg15']:+.1f}%/15m: no {side}"
        if PCR_FILTER and c.get("pcr_chg30") is not None and ((side == "CE") != (c["pcr_chg30"] > 0)):
            return f"PCR {'falling' if side == 'CE' else 'rising'} over 30 min"
        if FLOW_FILTER and c.get("flow_ce_share") is not None and ((side == "CE") != (c["flow_ce_share"] > 0.5)):
            return f"option volume flow against {side} (CE share {c['flow_ce_share']:.2f})"
        if LIQUID_STRIKE and LIQUID_MIN:
            val = c.get(f"liquid_{side.lower()}_val")
            if val is None or val < LIQUID_MIN:
                return f"{side} liquidity too low ({'-' if val is None else f'{val:,.0f}'} < {LIQUID_MIN:,.0f})"
        return None

    # ---------- session reports ----------
    def close_session(self, name, start, end):
        a, b = self.at(start), self.at(end)
        bars = [x for x in self.bars if a <= x["t"] and x["t"] + timedelta(minutes=CANDLE_MIN) <= b]
        rep = {"session": name, "trades": self.sess_trades.get(name, 0), "day_pnl": round(self.day_pnl, 2),
               "regimes": dict(self.regime_count)}
        if bars:
            move = bars[-1]["c"] - bars[0]["o"]
            rep.update(open=bars[0]["o"], high=max(x["h"] for x in bars), low=min(x["l"] for x in bars),
                       close=bars[-1]["c"], move=round(move, 2), bias="bullish" if move > 0 else "bearish" if move < 0 else "neutral")
        if name == "S1" or (self.opening is None and a <= self.at("09:15") and bars):
            gap = (bars[0]["o"] / self.prev_close - 1) * 100 if bars and self.prev_close else None
            if bars:
                self.opening = {"or_high": rep["high"], "or_low": rep["low"], "or_width": round(rep["high"] - rep["low"], 2),
                                "gap_pct": None if gap is None else round(gap, 2)}
                rep.update(self.opening)
        self.reports[name] = rep
        self.regime_count = {}
        log(f"SESSION REPORT {name} " + json.dumps(rep, default=str))

    def plan_session(self, name):
        """SESSION PLAN at the start of every session: the previous session's report + the current condition decide
        which sides this session may trade. Logged; used by enter() (PLAN_BIAS_FILTER)."""
        prev = None
        for n in ("S5", "S4", "S3", "S2", "S1"):
            if n < name and n in self.reports:
                prev = self.reports[n]
                break
        c = self.cond
        dom = max(prev["regimes"], key=prev["regimes"].get) if prev and prev.get("regimes") else None
        now_reg = c.get("regime")
        sides, note = {"CE", "PE"}, "both sides"
        if now_reg == "quiet":
            sides, note = set(), "quiet market: no trade until the condition changes"
        elif dom in ("trend_up", "trend_down") and now_reg in (dom, "mixed"):
            sides = {"CE"} if dom == "trend_up" else {"PE"}
            note = f"previous session trended {'up' if dom == 'trend_up' else 'down'}: only {next(iter(sides))}"
        elif prev and prev.get("bias") in ("bullish", "bearish") and now_reg in ("trend_up", "trend_down"):
            agree = (now_reg == "trend_up") == (prev["bias"] == "bullish")
            note = "trend agrees with the previous session's move" if agree else "trend against the previous session's move: both sides, careful"
        elif dom == "range" or now_reg == "range":
            note = "range: trade bounces at the levels, both sides"
        elif now_reg == "spike":
            note = "volatility spike: breakouts with the move" + (", no CE" if VIX_SPIKE_NO_CE and c.get("vix_spike") else "")
        dt = self.day_trend() if DAY_TREND_FILTER else None
        if dt and sides:
            sides = sides & {dt}
            note += f"; the day trends {'up' if dt == 'CE' else 'down'}: only {dt}"
        self.plan = {"session": name, "sides": sides, "prev_regime": dom, "prev_bias": (prev or {}).get("bias"),
                     "regime_now": now_reg, "note": note}
        dr = c.get("day_range_vs_avg")
        log(f"SESSION PLAN {name}: {note} | previous session {dom or '-'} / {(prev or {}).get('bias') or '-'} | now "
            f"{self.cond_text()} | day range {'-' if dr is None else f'{dr:.2f}'} x 5-day avg | gap "
            f"{(self.opening or {}).get('gap_pct')}% | OI support {c.get('oi_support')} resistance {c.get('oi_resistance')}")

    def ensure_opening(self):
        """The opening range from the 09:15-09:30 candles, also when S1 is off."""
        if self.opening is None and self.now() >= self.at("09:30"):
            bars = [x for x in self.bars if x["t"] < self.at("09:30")]
            if bars:
                hi, lo = max(x["h"] for x in bars), min(x["l"] for x in bars)
                gap = (bars[0]["o"] / self.prev_close - 1) * 100 if self.prev_close else None
                self.opening = {"or_high": hi, "or_low": lo, "or_width": round(hi - lo, 2), "gap_pct": None if gap is None else round(gap, 2)}
                log(f"Opening range {lo:.2f}-{hi:.2f} (width {hi - lo:.0f}) | gap {self.opening['gap_pct']}%")

    # ---------- risk ----------
    def lots(self):
        if RISK_SIZING == "risk":
            return max(1, min(MAX_LOTS, int(RISK_PER_TRADE // RISK_PER_LOT)))
        return LOTS

    def risk_block(self, side=None, idx=None):
        now = self.now()
        if self.position:
            return "a position is open"
        if self.cool_until and now < self.cool_until:
            return "cooldown"
        if self.trades >= MAX_TRADES:
            return f"max trades ({MAX_TRADES})"
        if self.sess_trades.get(self.session, 0) >= MAX_TRADES_PER_SESSION:
            return f"max trades in {self.session} ({MAX_TRADES_PER_SESSION})"
        if self.day_pnl <= -DAILY_RISK_MULT * RISK_PER_TRADE:
            return f"daily loss limit ({self.day_pnl:+.0f})"
        if self.losses_in_row >= MAX_CONSECUTIVE_LOSSES:
            return f"{self.losses_in_row} losses in a row"
        if (self.at(SQUARE_OFF_TIME) - now) < timedelta(minutes=MIN_MINUTES_LEFT):
            return f"less than {MIN_MINUTES_LEFT} min to square-off"
        if side and idx is not None and SR_MIN_ROOM:
            lvl = self.next_level(side, idx)
            if lvl is not None and abs(lvl - idx) < SR_MIN_ROOM:
                return f"no room: {'resistance' if side == 'CE' else 'support'} {lvl:.0f} only {abs(lvl - idx):.0f} pts away"
        return None

    # ---------- direction play ----------
    def reset_readings(self):
        self.readings, self.syms, self.next_reading = [], None, None

    @staticmethod
    def rising(v):
        return all(a < b for a, b in zip(v, v[1:]))

    def direction(self, n):
        w = self.readings[-(n + 1):]
        if len(w) < n + 1:
            return None
        ce, pe = self.rising([r["ce"] for r in w]), self.rising([r["pe"] for r in w])
        if ce and not pe:
            return "CE"
        if pe and not ce:
            return "PE"
        if ce and pe:
            d = w[-1]["idx"] - w[0]["idx"]
            return "CE" if d > 0 else "PE" if d < 0 else None
        return None

    def take_reading(self):
        if not self.syms:
            self.syms = self.atm()
        idx = self.ltp(UNDERLYING, INDEX_EXCHANGE)
        ce = self.ltp(self.syms["CE"], OPT_EXCHANGE)
        pe = self.ltp(self.syms["PE"], OPT_EXCHANGE)
        if None in (idx, ce, pe):
            return None
        self.readings.append({"t": self.now(), "idx": idx, "ce": ce, "pe": pe})
        return self.readings[-1]

    def reading_due(self, start):
        now = self.now()
        if self.next_reading is None:
            base = self.at(start)
            n = max(0, -(-int((now - base).total_seconds()) // (READING_INTERVAL_MIN * 60)))
            self.next_reading = base + timedelta(minutes=n * READING_INTERVAL_MIN)
        if now >= self.next_reading:
            self.next_reading += timedelta(minutes=READING_INTERVAL_MIN)
            return True
        return False

    # ---------- candle plays: return (side, why, invalidation) or None ----------
    @staticmethod
    def orb_band(score):
        return "high-quality" if score >= 80 else "normal" if score >= 70 else "watch only" if score >= 60 else "no trade"

    def orb_score(self, side, body_pct):
        """Breakout quality score /100 at the breakout candle (user, 2026-10-30): trend alignment 20, candle
        strength 20, RSI momentum 15, ATR expansion 15 - the 30-point retest leg is added separately once a retest
        actually holds (never scored in advance, since sig_orb never enters without one)."""
        c = self.cond
        e9, e21, r, ar = c.get("ema9"), c.get("ema21"), c.get("rsi"), c.get("atr_ratio")
        trend = 20 if e9 and e21 and ((side == "CE") == (e9 > e21)) else 0
        strength = round(max(0.0, min(1.0, body_pct)) * 20)
        if r is None:
            momentum = 0
        elif side == "CE":
            momentum = round(15 * max(0.0, min(1.0, (r - ORB_RSI_BUY) / max(1e-9, ORB_RSI_FULL_CE - ORB_RSI_BUY))))
        else:
            momentum = round(15 * max(0.0, min(1.0, (ORB_RSI_SELL - r) / max(1e-9, ORB_RSI_SELL - ORB_RSI_FULL_PE))))
        expansion = round(15 * max(0.0, min(1.0, ((ar or 0) - 1.0) / max(1e-9, ATR_EXPANSION - 1.0))))
        return trend + strength + momentum + expansion, {"trend": trend, "strength": strength, "momentum": momentum, "expansion": expansion}

    def sig_orb(self, b):
        """Smart ORB (user, 2026-10-30): a breakout candle only arms a setup - it must show conviction (body % of
        range, EMA9/21 aligned, RSI confirming, not an abnormal ATR14 spike) - the breakout candle itself is never
        bought. Price then has to retest the broken level and hold before entry. A breakout-quality score (/100:
        trend 20, candle strength 20, RSI 15, ATR expansion 15, +30 once the retest holds; bands 80+ high-quality,
        70-79 normal, 60-69 watch only, <60 no trade - starting thresholds, not statistically validated) gates the
        actual entry at ORB_MIN_SCORE and sets the profit target (ORB_TARGET_R_HQ for a high-quality score, else
        ORB_TARGET_R - minimum RR 1:1.5 either way)."""
        o = self.opening
        if not o or self.orb_done or self.now() > self.at(ORB_LAST_ENTRY):
            self.orb_pending = None
            return None
        if not (ORB_MIN_WIDTH_PTS <= o["or_width"] <= ORB_MAX_WIDTH_PTS):
            self.orb_done, self.orb_pending = True, None
            log(f"orb: OR width {o['or_width']:.0f} outside {ORB_MIN_WIDTH_PTS}-{ORB_MAX_WIDTH_PTS}; no ORB today")
            return None

        pend = self.orb_pending
        if pend:
            pend["bars"] += 1
            level, side = pend["level"], pend["side"]
            if (side == "CE" and b["c"] < level - ORB_STOP_PTS) or (side == "PE" and b["c"] > level + ORB_STOP_PTS):
                log(f"orb {side}: retest failed, close {b['c']:.2f} back through {level:.0f}; no ORB today")
                self.orb_done, self.orb_pending = True, None
                return None
            near = (b["l"] <= level + ORB_RETEST_PTS) if side == "CE" else (b["h"] >= level - ORB_RETEST_PTS)
            holds = (b["c"] > level) if side == "CE" else (b["c"] < level)
            if near and holds:
                score = pend["pre_score"] + 30
                band = self.orb_band(score)
                self.orb_done, self.orb_pending = True, None
                log(f"orb {side}: retest holds {level:.0f} (close {b['c']:.2f}) | quality {score}/100 ({band}) "
                    f"{pend['parts']} + retest 30")
                if score < ORB_MIN_SCORE:
                    log(f"orb {side} not taken: quality {score} < ORB_MIN_SCORE {ORB_MIN_SCORE} ({band})")
                    return None
                self.next_target_r = ORB_TARGET_R_HQ if score >= 80 else ORB_TARGET_R
                stop = level - ORB_STOP_PTS if side == "CE" else level + ORB_STOP_PTS
                return side, f"ORB retest holds {level:.0f}, quality {score}/100 ({band})", ("level", stop)
            if pend["bars"] >= ORB_RETEST_MAX_BARS:
                log(f"orb {side}: no retest of {level:.0f} within {ORB_RETEST_MAX_BARS} candles; no ORB today")
                self.orb_done, self.orb_pending = True, None
            return None

        side = "CE" if b["c"] > o["or_high"] + ORB_BUFFER_PTS else "PE" if b["c"] < o["or_low"] - ORB_BUFFER_PTS else None
        if not side:
            return None
        body_pct = abs(b["c"] - b["o"]) / (b["h"] - b["l"]) if b["h"] > b["l"] else 0.0
        if body_pct < ORB_BODY_PCT:
            log(f"orb {side} breakout skipped: body {body_pct:.0%} < {ORB_BODY_PCT:.0%} (weak candle)")
            return None
        c = self.cond
        e9, e21, r = c.get("ema9"), c.get("ema21"), c.get("rsi")
        if ORB_EMA_FILTER and e9 and e21 and ((side == "CE") != (e9 > e21)):
            log(f"orb {side} breakout skipped: EMA9/21 not aligned")
            return None
        if r is not None and ((side == "CE" and r < ORB_RSI_BUY) or (side == "PE" and r > ORB_RSI_SELL)):
            log(f"orb {side} breakout skipped: RSI {r:.0f} not confirming")
            return None
        atr14 = c.get("atr14")
        if atr14 and (b["h"] - b["l"]) > ORB_ATR_SPIKE_MULT * atr14:
            log(f"orb {side} breakout skipped: candle range {b['h'] - b['l']:.0f} > {ORB_ATR_SPIKE_MULT}x ATR14 (abnormal spike)")
            return None
        if c.get("regime") in ("spike", "uncertain"):
            log(f"orb {side} breakout skipped: {c.get('regime')} regime")
            return None
        score, parts = self.orb_score(side, body_pct)
        if not ORB_RETEST:
            self.orb_done = True
            self.next_target_r = ORB_TARGET_R_HQ if score >= 80 else ORB_TARGET_R
            stop = o["or_high"] - ORB_STOP_PTS if side == "CE" else o["or_low"] + ORB_STOP_PTS
            return side, f"close {b['c']:.2f} beyond OR {o['or_low']:.0f}-{o['or_high']:.0f}, quality {score}/70", ("level", stop)
        level = o["or_high"] if side == "CE" else o["or_low"]
        self.orb_pending = {"side": side, "level": level, "bars": 0, "pre_score": score, "parts": parts}
        log(f"orb {side}: breakout armed at {level:.0f} (close {b['c']:.2f}, body {body_pct:.0%}) | pre-score {score}/70 "
            f"{parts}; waiting for a retest")
        return None

    def sig_trend(self, b, p):
        c = self.cond
        e9, e21, rsi_v = c.get("ema9"), c.get("ema21"), c.get("rsi")
        if not e9 or not e21:
            return None
        if c["regime"] == "trend_up" and p["l"] <= e9 and b["c"] > e9 and b["c"] > p["h"]:
            if TREND_RSI_FILTER and (rsi_v is None or rsi_v <= 50):
                return None
            return "CE", f"trend up: pullback to EMA9 {e9:.0f}, close {b['c']:.2f} above the previous high, RSI {rsi_v:.0f}", ("ema21", None)
        if c["regime"] == "trend_down" and p["h"] >= e9 and b["c"] < e9 and b["c"] < p["l"]:
            if TREND_RSI_FILTER and (rsi_v is None or rsi_v >= 50):
                return None
            return "PE", f"trend down: pullback to EMA9 {e9:.0f}, close {b['c']:.2f} below the previous low, RSI {rsi_v:.0f}", ("ema21", None)
        return None

    def sig_reversion(self, b, p):
        closes = [x["c"] for x in self.warm + self.bars]
        bb_now, bb_prev = bollinger(closes, BB_PERIOD, BB_STD), bollinger(closes[:-1], BB_PERIOD, BB_STD)
        r = self.cond.get("rsi")
        if not bb_now or not bb_prev or r is None:
            return None
        lo, mid, hi = bb_now
        if p["c"] < bb_prev[0] and b["c"] > lo and r < 40:
            return "CE", f"range: back inside the lower band {lo:.0f} (RSI {r:.0f})", ("level", min(p["l"], b["l"]), "mid")
        if p["c"] > bb_prev[2] and b["c"] < hi and r > 60:
            return "PE", f"range: back inside the upper band {hi:.0f} (RSI {r:.0f})", ("level", max(p["h"], b["h"]), "mid")
        return None

    def sig_breakout(self, b):
        allb = self.warm + self.bars
        if len(allb) < BREAKOUT_BARS + 4:
            return None
        prev = allb[-(BREAKOUT_BARS + 1):-1]
        hi, lo = max(x["h"] for x in prev), min(x["l"] for x in prev)
        move3 = allb[-1]["c"] - allb[-4]["c"]
        half = (self.cond.get("atr5") or 0) / 2
        if b["c"] > hi and move3 > 0:
            return "CE", f"volatility breakout above {hi:.0f} ({BREAKOUT_BARS}-candle high)", ("level", hi - half)
        if b["c"] < lo and move3 < 0:
            return "PE", f"volatility breakout below {lo:.0f} ({BREAKOUT_BARS}-candle low)", ("level", lo + half)
        return None

    def sig_oi_bounce(self, b, p):
        """Bounce off the strike where writers added the most OI today: buy the (cheaper) option of the bounce."""
        c = self.cond
        s, r = c.get("oi_support"), c.get("oi_resistance")
        reg = c.get("regime")
        if reg == "trend_up":                                # in a trend only the pullback with the trend
            r = None
        elif reg == "trend_down":
            s = None
        if s and p["l"] <= s + OI_ZONE_PTS and b["c"] >= s - OI_BREAK_PTS / 2 and b["c"] > b["o"] and b["c"] > p["h"]:
            return "CE", f"bounce off OI support {s:.0f} (+{(c.get('oi_support_add') or 0) / 1e5:.1f}L PE OI today)", ("level", s - OI_BREAK_PTS)
        if r and p["h"] >= r - OI_ZONE_PTS and b["c"] <= r + OI_BREAK_PTS / 2 and b["c"] < b["o"] and b["c"] < p["l"]:
            return "PE", f"rejected at OI resistance {r:.0f} (+{(c.get('oi_resistance_add') or 0) / 1e5:.1f}L CE OI today)", ("level", r + OI_BREAK_PTS)
        return None

    def cheap_ok(self, sym):
        """CHEAP_PCT: the option is at least that % below its high of the last CHEAP_LOOKBACK_MIN minutes (bought after
        a pullback, not at the top; a day high would block every pullback in a trend)."""
        if not CHEAP_PCT:
            return None
        try:
            today = self.now().date().isoformat()
            df = self.c.history(symbol=sym, exchange=OPT_EXCHANGE, interval="1m", start_date=today, end_date=today, source="api")
            now = self.now()
            hi = max((float(r["high"]) for ts, r in df.iterrows()
                      if now - timedelta(minutes=CHEAP_LOOKBACK_MIN) <= ts.to_pydatetime().replace(tzinfo=None) <= now - timedelta(minutes=1)),
                     default=None) if hasattr(df, "iterrows") else None
            ltp = self.ltp(sym, OPT_EXCHANGE)
        except Exception:
            return None
        if ltp and hi and ltp > hi * (1 - CHEAP_PCT / 100):
            return f"not cheap: {sym} {ltp:.2f} is within {CHEAP_PCT}% of its {CHEAP_LOOKBACK_MIN}-min high {hi:.2f}"
        return None

    def sig_late(self):
        if self.late_done or self.now() < self.at(LATE_ENTRY_TIME) or not self.prev_close:
            return None
        self.late_done = True
        first = [x for x in self.bars if x["t"] < self.at("09:45")]
        if not first or not self.bars:
            return None
        r1 = (first[-1]["c"] / self.prev_close - 1) * 100
        rday = (self.bars[-1]["c"] / first[-1]["c"] - 1) * 100
        if abs(r1) < LATE_MIN_PCT or (r1 > 0) != (rday > 0):
            log(f"late_momentum: first half-hour {r1:+.2f}%, since then {rday:+.2f}%: no trade")
            return None
        side = "CE" if r1 > 0 else "PE"
        return side, f"first half-hour {r1:+.2f}% and the day still {'up' if r1 > 0 else 'down'} ({rday:+.2f}%)", ("none", None)

    def choose(self, plays):
        """The plays to try now: 'auto' maps the regime to a play; explicit plays are added."""
        reg = self.cond.get("regime")
        picked = []
        if "auto" in plays:
            trend = ["trend", "oi_bounce"] if OI_IN_TREND else ["trend"]
            picked += {"trend_up": trend, "trend_down": trend, "range": ["oi_bounce", "reversion"],
                       "spike": ["breakout"], "mixed": ["direction"]}.get(reg, [])
        picked += [p for p in ("orb", "late_momentum", "oi_bounce", "direction", "trend", "reversion", "breakout") if p in plays]
        out = []
        for p in picked:
            if p and p not in out:
                out.append(p)
        return out

    # ---------- orders ----------
    def fill_price(self, oid):
        try:
            self.sleep(1)
            r = self.c.orderstatus(order_id=oid, strategy=STRATEGY)
            return float(r.get("data", {}).get("average_price") or 0) or None
        except Exception:
            return None

    def enter(self, side, play, why, idx, inval):
        target_r = self.next_target_r if self.next_target_r is not None else TARGET_R
        self.next_target_r = None                            # one-shot: never leaks into an unrelated later trade
        block = self.risk_block(side, idx if (play != "orb" or ORB_NEEDS_ROOM) else None) or self.filters_ok(side)
        if not block and PLAN_BIAS_FILTER and self.plan and self.plan.get("session") == self.session and side not in self.plan["sides"]:
            block = f"session plan: {self.plan['note']}"
        if not block and DAY_TREND_FILTER:
            dt = self.day_trend()
            if dt and side != dt:
                block = f"the day trends {'up' if dt == 'CE' else 'down'} (ER {self.cond.get('day_er'):.2f}, move " \
                        f"{self.cond.get('day_move'):+.0f}): no {side}"
        if block:
            log(f"{play} {side} signal not taken: {block}")
            return
        try:
            sym = self.entry_symbol(side, play)
            if self.lot is None:                      # safety net: the chain row had no lotsize (should not happen)
                self.atm()
        except Exception as e:
            log(f"strike resolution failed: {e}")
            return
        if play == "oi_bounce":
            why_not = self.cheap_ok(sym)
            if why_not:
                log(f"oi_bounce {side} signal not taken: {why_not}")
                return
        lots = self.lots()
        qty = self.lot * lots
        r = self.c.placeorder(strategy=STRATEGY, symbol=sym, action="BUY", exchange=OPT_EXCHANGE,
                              price_type="MARKET", product=PRODUCT, quantity=qty)
        if r.get("status") != "success":
            log(f"BUY FAILED {sym}: {r}")
            return
        px = self.fill_price(r.get("orderid")) or self.ltp(sym, OPT_EXCHANGE)
        step = target_r * RISK_PER_TRADE
        self.position = {"symbol": sym, "side": side, "qty": qty, "entry": px, "play": play, "idx": idx, "inval": inval,
                         "step": step, "step_size": step, "floor": None}
        self.trades += 1
        self.sess_trades[self.session] = self.sess_trades.get(self.session, 0) + 1
        log(f"BUY {sym} qty {qty} ({lots} lot) @ {px} | {self.session} {play}: {why} | stop -{RISK_PER_TRADE} step +{step:.0f} "
            f"| trade {self.trades}/{MAX_TRADES} | {self.cond_text()}")

    def exit(self, why):
        pos = self.position
        r = self.c.placeorder(strategy=STRATEGY, symbol=pos["symbol"], action="SELL", exchange=OPT_EXCHANGE,
                              price_type="MARKET", product=PRODUCT, quantity=pos["qty"])
        if r.get("status") != "success":
            log(f"!!! SELL FAILED {pos['symbol']}: {r} - retrying on the next check; CHECK POSITION IN OPENALGO !!!")
            return False
        px = self.fill_price(r.get("orderid")) or self.ltp(pos["symbol"], OPT_EXCHANGE) or pos["entry"]
        p = (px - pos["entry"]) * pos["qty"]
        self.day_pnl += p
        self.losses_in_row = self.losses_in_row + 1 if p < 0 else 0
        log(f"SELL {pos['symbol']} qty {pos['qty']} @ {px} | {pos['play']}: {why} | P&L {p:+.0f} | day P&L {self.day_pnl:+.0f}")
        self.position = None
        self.cool_until = self.now() + timedelta(minutes=COOLDOWN_MIN)
        self.reset_readings()
        return True

    def still_valid(self, pos):
        """At a profit step: does the play's reason still hold? (hold vs book)"""
        if pos["play"] == "direction":
            if self.syms and (not self.readings or self.now() - self.readings[-1]["t"] >= timedelta(minutes=2)):
                self.take_reading()
            return self.direction(CONTINUE_READINGS) == pos["side"]
        if pos["play"] == "late_momentum":
            return True
        if not self.bars:
            return False
        b = self.bars[-1]
        if pos["play"] == "reversion":
            return False                                     # mean reversion: take the profit
        return (b["c"] > b["o"]) if pos["side"] == "CE" else (b["c"] < b["o"])

    def manage(self, new_bar):
        pos = self.position
        px = self.ltp(pos["symbol"], OPT_EXCHANGE)
        if px is None:
            return
        p = (px - pos["entry"]) * pos["qty"]
        self.levels = {"entry": pos["entry"], "pnl": round(p, 2)}
        if pos["floor"] is not None and p <= pos["floor"]:
            self.exit(f"locked profit: {p:+.0f} <= floor {pos['floor']:+.0f}")
            return
        cap = min(RISK_PER_TRADE, DAILY_RISK_MULT * RISK_PER_TRADE + self.day_pnl)
        if p <= -cap:
            self.exit(f"stop: {p:+.0f} (limit {cap:.0f})")
            return
        if p >= pos["step"]:
            if self.still_valid(pos):
                pos["floor"] = pos["step"] - pos["step_size"] * (100 - LOCK_PCT) / 100
                log(f"HOLD {pos['side']} at {p:+.0f}: {pos['play']} still holds; floor {pos['floor']:+.0f}")
                pos["step"] += pos["step_size"]
            else:
                self.exit(f"profit booked ({p:+.0f})")
                return
        if SR_EXIT_PTS and p > 0 and pos["play"] != "late_momentum":
            idx = self.ltp(UNDERLYING, INDEX_EXCHANGE)
            if idx is not None and (idx > pos["idx"] if pos["side"] == "CE" else idx < pos["idx"]):
                lvl = self.next_level(pos["side"], idx)
                if lvl is not None and abs(lvl - idx) <= SR_EXIT_PTS:
                    self.exit(f"{'resistance' if pos['side'] == 'CE' else 'support'} {lvl:.0f} near (index {idx:.2f}) at {p:+.0f}")
                    return
        if new_bar and self.bars:
            b, kind = self.bars[-1], pos["inval"][0]
            ce = pos["side"] == "CE"
            if kind == "level":
                lvl = pos["inval"][1]
                if (ce and b["c"] < lvl) or (not ce and b["c"] > lvl):
                    self.exit(f"invalid: 5-min close {b['c']:.2f} past {lvl:.0f}")
                    return
                if len(pos["inval"]) > 2 and p > 0:          # reversion: the middle band is the target
                    bb = bollinger([x["c"] for x in self.warm + self.bars], BB_PERIOD, BB_STD)
                    if bb and ((ce and b["c"] >= bb[1]) or (not ce and b["c"] <= bb[1])):
                        self.exit(f"middle band {bb[1]:.0f} reached at {p:+.0f}")
                        return
            elif kind == "ema21":
                e21 = self.cond.get("ema21")
                if e21 and ((ce and b["c"] < e21) or (not ce and b["c"] > e21)):
                    self.exit(f"trend invalid: 5-min close {b['c']:.2f} past EMA21 {e21:.0f}")

    # ---------- main loop ----------
    def run(self):
        log(f"Strategy {STRATEGY} starting on {UNDERLYING} | risk {RISK_PER_TRADE} per trade ({self.lots()} lot(s) by "
            f"{RISK_SIZING}) | sessions " + "; ".join(f"{n} {'ON' if on else 'off'} {a}-{b} {p}" for n, on, a, b, p in self.session_table()))
        try:
            log(f"Analyzer (paper) mode: {self.c.analyzerstatus().get('data')}")
        except Exception:
            pass
        while self.now() < self.at("09:15"):
            self.sleep(min(30, max(1, (self.at("09:15") - self.now()).total_seconds())))
        self.load_day_context()
        last_sess = None                                     # (name, start, end) of the session being reported
        while True:
            now = self.now()
            if now >= self.at(SQUARE_OFF_TIME):
                if self.position and not self.exit("square-off time"):
                    self.sleep(POLL_SEC)
                    continue
                self.refresh_bars()
                if last_sess:
                    self.close_session(*last_sess)
                break
            self.refresh_bars()
            self.ensure_opening()
            cur = self.current_session()
            name = cur[0] if cur else None
            if name != (last_sess[0] if last_sess else None):
                if last_sess:
                    self.close_session(*last_sess)
                last_sess = None
                if cur:
                    row = next(r for r in self.session_table() if r[0] == name)
                    last_sess = (name, row[2], row[3])
                    log(f"===== SESSION {name} {row[2]}-{row[3]} | {'plays: ' + ', '.join(sorted(cur[2])) if cur[2] else 'NO TRADE'} "
                        f"| day P&L {self.day_pnl:+.0f} =====")
                    if self.bars:
                        self.update_condition()
                    self.plan_session(name)
                self.session = name
                self.sess_trades.setdefault(name, 0)
                self.reset_readings()
            new_bar = bool(self.bars) and self.bars[-1]["t"] != self.seen_bar
            if new_bar:
                self.seen_bar = self.bars[-1]["t"]
                self.update_condition()
            if self.position:
                if self.position["play"] == "direction" and cur and self.reading_due(cur[1]):
                    self.take_reading()
                self.manage(new_bar)
            elif cur and cur[2]:
                plays = self.choose(cur[2])
                if self.orb_pending and "orb" not in plays:      # keep tracking a retest even past S2 / off-regime
                    plays = plays + ["orb"]
                if new_bar and len(self.bars) >= 2:
                    b, p = self.bars[-1], self.bars[-2]
                    for play in plays:
                        sig = {"orb": lambda: self.sig_orb(b) if (cur[0] == "S2" or self.orb_pending) else None,
                               "trend": lambda: self.sig_trend(b, p), "reversion": lambda: self.sig_reversion(b, p),
                               "breakout": lambda: self.sig_breakout(b), "late_momentum": self.sig_late,
                               "oi_bounce": lambda: self.sig_oi_bounce(b, p)}.get(play, lambda: None)()
                        if sig and not self.position:
                            self.enter(sig[0], play, sig[1], b["c"], sig[2])
                if not self.position and "direction" in plays and self.reading_due(cur[1]):
                    if self.risk_block() in (None, "cooldown"):
                        rd = self.take_reading()
                        side = self.direction(RISING_READINGS) if rd else None
                        if side:
                            self.enter(side, "direction", f"{side} premium rising {RISING_READINGS} readings", rd["idx"], ("none", None))
                elif "direction" not in plays and self.readings:
                    self.reset_readings()
            self.sleep(POLL_SEC)
        log(f"Done for the day. Trades {self.trades} | day P&L {self.day_pnl:+.0f}")


if __name__ == "__main__":
    Strategy(api(api_key=API_KEY, host=HOST)).run()
