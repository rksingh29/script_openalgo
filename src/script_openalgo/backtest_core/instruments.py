"""Per-instrument settings. Values were verified against OpenAlgo on 2026-10-17
(see .claude/skills/strategy_builder/knowledge/openalgo_api.md). Lot size is NOT stored here:
it changes over time and is read from the optionsymbol API per date."""

INSTRUMENTS = {
    "NIFTY": {
        "index_symbol": "NIFTY",
        "index_exchange": "NSE_INDEX",
        "opt_exchange": "NFO",
        "strike_step": 50,
        "strike_buffer": 2,        # extra strikes beyond the day's range, each side
        "expiry_cycle": "weekly",
        "max_expiry_gap_days": 8,  # weekly expiries: a nearest-listed expiry further out than this means
                                   # the expiry actually used that day has probably expired
    },
    "BANKNIFTY": {
        "index_symbol": "BANKNIFTY",
        "index_exchange": "NSE_INDEX",
        "opt_exchange": "NFO",
        "strike_step": 100,
        "strike_buffer": 2,
        "expiry_cycle": "monthly",
        "max_expiry_gap_days": 35,  # monthly expiries only (as listed on 2026-10-17)
    },
}


def get(name):
    try:
        return INSTRUMENTS[name.upper()]
    except KeyError:
        raise SystemExit(f"unknown instrument {name}; known: {', '.join(INSTRUMENTS)}")
