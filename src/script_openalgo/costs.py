"""Trading costs of an option round trip (Dhan by default): brokerage per executed order + statutory charges.

Defaults (verify against a Dhan contract note; rates change with budgets and exchange circulars):
  brokerage 20 per executed order (Dhan, F&O), STT 0.1 % of the SELL premium, exchange transaction charge on premium
  (NSE options 0.03503 %, BSE options 0.0325 %), SEBI fee 10 per crore of premium, stamp duty 0.003 % of the BUY premium,
  GST 18 % on brokerage + exchange charge + SEBI fee.
The user can change every rate in the web app (setting `costs`, JSON); `breakdown()` uses the saved values.
"""
import json

DEFAULTS = {
    "broker": "Dhan",
    "brokerage_per_order": 20.0,
    "stt_sell_pct": 0.1,
    "exch_pct_nse": 0.03503,
    "exch_pct_bse": 0.0325,
    "sebi_per_crore": 10.0,
    "stamp_buy_pct": 0.003,
    "gst_pct": 18.0,
}


def settings(con=None):
    """Saved cost settings merged over the defaults."""
    out = dict(DEFAULTS)
    if con is not None:
        from script_openalgo.strategy_db import db
        try:
            out.update(json.loads(db.get_setting(con, "costs", "{}") or "{}"))
        except ValueError:
            pass
    return out


def breakdown(buy_price, sell_price, qty, exchange="NFO", cfg=None, orders=2):
    """Costs in rupees of buying `qty` at `buy_price` and selling at `sell_price` (`orders` executed orders in total)."""
    c = cfg or DEFAULTS
    buy_t, sell_t = float(buy_price) * qty, float(sell_price) * qty
    turnover = buy_t + sell_t
    brokerage = c["brokerage_per_order"] * orders
    stt = sell_t * c["stt_sell_pct"] / 100
    exch = turnover * (c["exch_pct_bse"] if str(exchange).upper().startswith("B") else c["exch_pct_nse"]) / 100
    sebi = turnover * c["sebi_per_crore"] / 1e7
    stamp = buy_t * c["stamp_buy_pct"] / 100
    gst = (brokerage + exch + sebi) * c["gst_pct"] / 100
    total = brokerage + stt + exch + sebi + stamp + gst
    return {"brokerage": round(brokerage, 2), "stt": round(stt, 2), "exchange": round(exch, 2), "sebi": round(sebi, 4),
            "stamp": round(stamp, 2), "gst": round(gst, 2), "total": round(total, 2)}


def round_trip(buy_price, sell_price, qty, exchange="NFO", cfg=None, orders=2):
    return breakdown(buy_price, sell_price, qty, exchange, cfg, orders)["total"]
