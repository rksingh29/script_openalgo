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


def apply_strategy_overrides(strat, instrument, user_overrides=None):
    """Point a strategy module at `instrument` for a backtest: underlying symbol/exchange plus the instrument's resized
    constants. The module's original values are remembered on first use and restored first, so running several instruments
    in one process does not leak one instrument's values into the next. User PARAM_OVERRIDES win."""
    cfg = get(instrument)
    orig = getattr(strat, "_orig_params", None)
    if orig is None:
        orig = {}
        strat._orig_params = orig
    wanted = dict(cfg.get("strategy_overrides", {}))
    wanted["UNDERLYING"] = cfg["index_symbol"]
    if hasattr(strat, "INDEX_EXCHANGE"):
        wanted["INDEX_EXCHANGE"] = cfg["index_exchange"]
    for k in list(orig):                                   # undo what an earlier instrument set
        setattr(strat, k, orig[k])
    for k, v in wanted.items():
        if not hasattr(strat, k) or k in (user_overrides or {}):
            continue
        orig.setdefault(k, getattr(strat, k))
        setattr(strat, k, v)
    for k, v in (user_overrides or {}).items():
        setattr(strat, k, v)
