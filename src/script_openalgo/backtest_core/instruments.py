"""Per-instrument settings. Values were verified against OpenAlgo on 2026-10-17 (indices) and 2026-10-20 (stocks)
(see .claude/skills/strategy_builder/knowledge/openalgo_api.md). Lot size is NOT stored here:
it changes over time and is read from the optionsymbol API per date.

`index_symbol` / `index_exchange` are the UNDERLYING's symbol and exchange (the names date from index-only days): for a
stock they are e.g. RELIANCE / NSE. `strike_step` is a hint: stock strikes can be irregular (OFSS lists 10200, 10250,
10300 but not 10150), so strikes that OpenAlgo does not know are recorded as `unlisted` instead of missing data.
`min_underlying_bars`: stock 1m candles end at 15:14 (360 bars), index candles at 15:29 (375 bars).
`slippage_pct`: estimated cost per fill as % of the option premium (half the bid-ask spread). It is booked as a cost in
net_pnl only; fill prices and gross P&L stay equal to the candle closes so the fill check still works. Indices: 0.
`strategy_overrides`: module constants of a strategy that must be resized for this instrument (applied by the backtest
runner only when the strategy has that constant and the user did not override it in PARAM_OVERRIDES).

More instruments can be added from the web app (Instruments tab: search OpenAlgo, add). They are stored per workspace in
strategy_db/instruments.json and merged over the built-in table below by reload() (called on import). Built-ins cannot be
removed; a user entry with the same name replaces a built-in's settings."""
import json
import os

BUILTIN = {
    "NIFTY": {
        "kind": "index",
        "index_symbol": "NIFTY",
        "index_exchange": "NSE_INDEX",
        "opt_exchange": "NFO",
        "strike_step": 50,
        "strike_buffer": 2,        # extra strikes beyond the day's range, each side
        "expiry_cycle": "weekly",
        "max_expiry_gap_days": 8,  # weekly expiries: a nearest-listed expiry further out than this means
                                   # the expiry actually used that day has probably expired
        "min_underlying_bars": 370,
        "min_option_coverage": 0.90,
    },
    "BANKNIFTY": {
        "kind": "index",
        "index_symbol": "BANKNIFTY",
        "index_exchange": "NSE_INDEX",
        "opt_exchange": "NFO",
        "strike_step": 100,
        "strike_buffer": 2,
        "expiry_cycle": "monthly",
        "max_expiry_gap_days": 35,  # monthly expiries only (as listed on 2026-10-17)
        "min_underlying_bars": 370,
        "min_option_coverage": 0.90,
        # parabolic_sar_flip_scalp: enter on the flip candle on BANKNIFTY (user decision 2026-10-21; 12-day test:
        # +23054 with CONFIRM_BARS=0 vs -1538 with 2). Applies to any strategy that has a CONFIRM_BARS constant.
        "strategy_overrides": {"CONFIRM_BARS": 0},
    },
    "RELIANCE": {                  # lot 500, strikes every 10 around 1200 (2026-10-20); liquid options
        "kind": "stock",
        "index_symbol": "RELIANCE",
        "index_exchange": "NSE",
        "opt_exchange": "NFO",
        "strike_step": 10,
        "strike_buffer": 3,
        "expiry_cycle": "monthly",
        "max_expiry_gap_days": 35,
        "min_underlying_bars": 355,
        "min_option_coverage": 0.90,
        "strategy_overrides": {"PCR_WITHIN_POINTS": 15},
        "slippage_pct": 0.5,           # ESTIMATE of half the bid-ask spread per fill, % of premium (net P&L only)
    },
    "OFSS": {                      # lot 100, strikes 50 apart near the money and 100 further out (2026-10-20); THIN options
        "kind": "stock",
        "index_symbol": "OFSS",
        "index_exchange": "NSE",
        "opt_exchange": "NFO",
        "strike_step": 50,
        "strike_buffer": 3,
        "expiry_cycle": "monthly",
        "max_expiry_gap_days": 35,
        "min_underlying_bars": 355,
        "min_option_coverage": 0.60,   # far strikes trade in bursts; below this a core strike makes the day incomplete
        "strategy_overrides": {"DAILY_MAX_LOSS": 6000, "PCR_WITHIN_POINTS": 120},
        "slippage_pct": 1.5,           # ESTIMATE: thin options, wide spreads (net P&L only)
    },
}

INSTRUMENTS = {}          # built-ins + the workspace's added instruments; mutated in place by reload()
FIELDS = ("kind", "index_symbol", "index_exchange", "opt_exchange", "strike_step", "strike_buffer", "expiry_cycle",
          "max_expiry_gap_days", "min_underlying_bars", "min_option_coverage", "slippage_pct", "strategy_overrides",
          "name", "lot_size_seen", "added", "note")


def user_file():
    from script_openalgo import config
    return os.path.join(config.strategy_db_dir(), "instruments.json")


def _load_user():
    p = user_file()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return {k.upper(): v for k, v in data.items() if isinstance(v, dict) and v.get("index_symbol")}
    except (OSError, ValueError):
        return {}


def reload():
    """Rebuild INSTRUMENTS from the built-ins and the workspace file (same dict object, so importers see the change)."""
    INSTRUMENTS.clear()
    INSTRUMENTS.update({k: dict(v, source="builtin") for k, v in BUILTIN.items()})
    for k, v in _load_user().items():
        INSTRUMENTS[k] = dict(v, source="user")
    return INSTRUMENTS


def save_user(name, cfg):
    """Add or replace an instrument in the workspace file and reload."""
    name = name.upper()
    data = _load_user()
    data[name] = {k: cfg[k] for k in FIELDS if k in cfg}
    p = user_file()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    reload()
    return INSTRUMENTS[name]


def remove_user(name):
    """Remove an added instrument (built-ins stay; their user override, if any, is dropped). Cached data is not touched."""
    name = name.upper()
    data = _load_user()
    if name not in data:
        return False
    del data[name]
    with open(user_file(), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    reload()
    return True


reload()


def get(name):
    try:
        return INSTRUMENTS[name.upper()]
    except KeyError:
        raise SystemExit(f"unknown instrument {name}; known: {', '.join(INSTRUMENTS)}")


def is_stock(name):
    return get(name).get("kind") == "stock"


STRIKE_KEY = "STRIKE_OFFSET"      # run option for every strategy: which strike its offset="ATM" requests get
STRIKE_CHOICES = ["ATM"] + [f"OTM{n}" for n in range(1, 6)] + [f"ITM{n}" for n in range(1, 6)]
EXPIRY_KEY = "EXPIRY"             # run option for every strategy: which expiry its expiry() list starts with
# current = nearest; next = next week (the expiry after the nearest); week2 / week3 = two / three after the nearest;
# monthly = the nearest MONTHLY expiry (the last listed expiry of its month); next_month = the monthly after that;
# auto = next on the nearest expiry's own day, else current. (Monthly-only instruments: every expiry is a monthly.)
EXPIRY_CHOICES = ["current", "next", "week2", "week3", "monthly", "next_month", "auto"]
EXPIRY_LABELS = {"current": "current (nearest)", "next": "next week", "week2": "week + 1 (2 after the nearest)",
                 "week3": "week + 2 (3 after the nearest)", "monthly": "monthly (nearest monthly)",
                 "next_month": "next month (monthly)", "auto": "auto: next week on expiry day"}


def expiry_options(instrument=None):
    """[(value, label)] of the expiry choices that make sense for the instrument's expiry cycle. Monthly-only instruments
    (BANKNIFTY, stocks): every listed expiry is a monthly, so next = next month, week2 = month + 2, and monthly /
    next_month would only repeat current / next."""
    cycle = (INSTRUMENTS.get(str(instrument or "").upper()) or {}).get("expiry_cycle", "weekly")
    if cycle == "monthly":
        return [("current", "current month"), ("next", "next month"), ("week2", "month + 2"),
                ("auto", "auto: next month on expiry day")]
    return [(v, EXPIRY_LABELS[v]) for v in EXPIRY_CHOICES]


def expiry_choice(value):
    v = str(value or "current").strip().lower()
    if v not in EXPIRY_CHOICES:
        raise ValueError(f"expiry {value!r}: use one of {', '.join(EXPIRY_CHOICES)}")
    return v


def strike_offset(value):
    """Normalise a strike choice: 'ATM', 'OTM1'..'OTM5' (ATM+n: away from the money), 'ITM1'..'ITM5'. ValueError if bad."""
    v = str(value or "ATM").strip().upper().replace(" ", "")
    v = {"ATM+0": "ATM"}.get(v, v)
    if v.startswith("ATM+"):
        v = "OTM" + v[4:]
    elif v.startswith("ATM-"):
        v = "ITM" + v[4:]
    if v not in STRIKE_CHOICES:
        raise ValueError(f"strike {value!r}: use one of {', '.join(STRIKE_CHOICES)}")
    return v


def defaults_file():
    from script_openalgo import config
    return os.path.join(config.strategy_db_dir(), "strategy_params.json")


def load_strategy_defaults():
    """{slug: {instrument: {NAME: value}}}: options the user saved as the standard for a strategy on an instrument
    (e.g. a rupee MAX_LOSS sized for RELIANCE). Workspace file strategy_db/strategy_params.json (git-tracked)."""
    p = defaults_file()
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def strategy_defaults(slug, instrument):
    return dict((load_strategy_defaults().get(slug) or {}).get(instrument.upper()) or {})


def save_strategy_defaults(slug, instrument, params):
    """Replace the saved options of `slug` on `instrument` ({} removes them)."""
    data = load_strategy_defaults()
    per = data.setdefault(slug, {})
    if params:
        per[instrument.upper()] = params
    else:
        per.pop(instrument.upper(), None)
    if not per:
        data.pop(slug, None)
    p = defaults_file()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    return strategy_defaults(slug, instrument)


class StrikeClient:
    """Wraps a strategy's client so every strategy can trade another strike / expiry without code changes:
    optionsymbol(offset="ATM") asks for the chosen strike (OTMn / ITMn), and expiry() lists the chosen expiry first
    (strategies take the first entry): "next" drops the nearest expiry, "auto" drops it only on its own expiry day.
    Everything else passes through."""

    def __init__(self, inner, offset, expiry="current"):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_offset", offset)
        object.__setattr__(self, "_expiry", expiry)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)

    def optionsymbol(self, *args, **kw):
        if str(kw.get("offset", "ATM")).upper() == "ATM":
            kw["offset"] = self._offset
        return self._inner.optionsymbol(*args, **kw)

    def expiry(self, *args, **kw):
        r = self._inner.expiry(*args, **kw)
        if self._expiry == "current" or not isinstance(r, dict) or r.get("status") != "success":
            return r
        raw = list(r.get("data") or [])
        if not raw:
            return r
        from datetime import date, datetime

        def parse(x):
            try:
                return datetime.strptime(str(x).replace("-", "").upper(), "%d%b%y").date()
            except ValueError:
                return None
        pairs = sorted(((parse(x), x) for x in raw), key=lambda p: (p[0] is None, p[0] or date.max))
        dates = [d for d, _ in pairs]
        today = getattr(self._inner, "day", None)            # FakeClient: the simulated day; live: today
        cached = isinstance(today, date)
        today = today if cached else date.today()
        monthly = [i for i, d in enumerate(dates) if d and not any(e and e > d and (e.year, e.month) == (d.year, d.month) for e in dates)]
        ch = self._expiry
        if ch == "auto":
            idx = 1 if dates[0] == today else 0
        elif ch in ("next", "week2", "week3"):
            idx = {"next": 1, "week2": 2, "week3": 3}[ch]
        elif ch == "monthly":
            idx = monthly[0] if monthly else None
        else:                                                # next_month
            idx = monthly[1] if len(monthly) > 1 else None
        if idx is None or idx >= len(pairs):
            where = " in the data cache (fetch: data_loader --next-expiry)" if cached else ""
            return {"status": "error", "message": f"EXPIRY={ch}: that expiry is not available{where} (listed: {', '.join(raw[:6])})"}
        return dict(r, data=[x for _, x in pairs[idx:]])


def _install_strike(strat, offset, expiry="current"):
    """Make strat.Strategy(client, ...) wrap its client in StrikeClient when a strike / expiry is chosen (idempotent)."""
    cls = getattr(strat, "Strategy", None)
    if cls is None:
        return
    if not hasattr(cls, "_orig_init"):
        cls._orig_init = cls.__init__

        def init(self, client, *a, **kw):
            off, exp = getattr(strat, "_strike_offset", "ATM"), getattr(strat, "_expiry_choice", "current")
            cls._orig_init(self, StrikeClient(client, off, exp) if (off != "ATM" or exp != "current") else client, *a, **kw)
        cls.__init__ = init
    strat._strike_offset = offset
    strat._expiry_choice = expiry


def apply_strategy_overrides(strat, instrument, user_overrides=None):
    """Point a strategy module at `instrument`: underlying symbol/exchange, the instrument's resized constants, then the
    options the user SAVED for this strategy on this instrument (strategy_params.json), then user PARAM_OVERRIDES, then
    the options chosen for this run (STRATEGY_PARAMS, last). STRIKE_OFFSET (any of these) picks the strike for the
    strategy's ATM requests. The module's original values are remembered on first use and restored first, so running
    several instruments in one process does not leak one instrument's values into the next."""
    cfg = get(instrument)
    orig = getattr(strat, "_orig_params", None)
    if orig is None:
        orig = {}
        strat._orig_params = orig
    user_overrides = dict(user_overrides or {})
    saved = strategy_defaults(getattr(strat, "__name__", ""), instrument)
    run_params = env_params()
    strike, expiry = "ATM", "current"
    for src in (saved, user_overrides, run_params):          # later wins
        try:
            if STRIKE_KEY in src:
                strike = strike_offset(src.pop(STRIKE_KEY))
            if EXPIRY_KEY in src:
                expiry = expiry_choice(src.pop(EXPIRY_KEY))
        except ValueError as e:
            raise SystemExit(str(e))
    wanted = dict(cfg.get("strategy_overrides", {}))
    wanted["UNDERLYING"] = cfg["index_symbol"]
    if hasattr(strat, "INDEX_EXCHANGE"):
        wanted["INDEX_EXCHANGE"] = cfg["index_exchange"]
    if hasattr(strat, "OPT_EXCHANGE"):
        wanted["OPT_EXCHANGE"] = cfg.get("opt_exchange", "NFO")          # BFO for SENSEX / BANKEX options
    if hasattr(strat, "STRIKE_STEP"):
        wanted["STRIKE_STEP"] = cfg["strike_step"]                      # strategies that walk strikes themselves
    wanted.update({k: v for k, v in saved.items() if hasattr(strat, k)})  # the user's saved options for this instrument
    for k in list(orig):                                   # undo what an earlier instrument set
        setattr(strat, k, orig[k])
    for k, v in wanted.items():
        if not hasattr(strat, k) or k in user_overrides:
            continue
        orig.setdefault(k, getattr(strat, k))
        setattr(strat, k, v)
    for k, v in user_overrides.items():
        orig.setdefault(k, getattr(strat, k, None))
        setattr(strat, k, v)
    for k, v in run_params.items():                        # options chosen for this run in the web app win
        if not hasattr(strat, k):
            raise SystemExit(f"unknown strategy parameter {k} (STRATEGY_PARAMS)")
        orig.setdefault(k, getattr(strat, k))
        setattr(strat, k, v)
    _install_strike(strat, strike, expiry)
    strat._run_params = run_params
    strat._saved_params = {k: v for k, v in saved.items() if hasattr(strat, k)}
    LAST_APPLIED.clear()
    LAST_APPLIED.update(saved=strat._saved_params, strike=strike, expiry=expiry)


LAST_APPLIED = {}          # what the last apply_strategy_overrides used (saved options, strike): stored in summary.json


def env_params():
    """Strategy options chosen for one run in the web app: JSON in the STRATEGY_PARAMS environment variable."""
    raw = os.environ.get("STRATEGY_PARAMS") or ""
    if not raw.strip():
        return {}
    try:
        d = json.loads(raw)
    except ValueError:
        raise SystemExit("STRATEGY_PARAMS is not valid JSON")
    return {str(k): v for k, v in d.items()} if isinstance(d, dict) else {}
