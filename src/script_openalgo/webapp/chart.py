"""Chart data for one test run: the index for the day plus, per trade, the option's 1-minute bars with entry/exit,
best/worst point and what-if numbers (tighter stop, later entry). Reads only the candle cache, never OpenAlgo."""
import os

STOP_PCTS = (3, 5, 8, 12)          # stop-loss as % of the entry premium
DELAYS = (1, 2, 3, 5, 10)          # enter this many minutes later, same exit
CONTEXT_MIN = 15                   # minutes of bars shown before entry / after exit


def _mins(t):
    p = [int(x) for x in str(t).split(":")]
    return p[0] * 60 + p[1] + (p[2] / 60 if len(p) > 2 else 0)


def _hhmm(m):
    return "%02d:%02d" % (int(m) // 60, int(m) % 60)


def _bars(cache, name, day):
    import pandas as pd
    p = os.path.join(cache, f"{name}_{day}.parquet")
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)
    if "dt" in df.columns:
        df = df.set_index("dt")
    df.index = pd.to_datetime(df.index)
    return df[["open", "high", "low", "close"]].sort_index()


def _rows(df):
    return [[_hhmm(t.hour * 60 + t.minute), float(r.open), float(r.high), float(r.low), float(r.close)]
            for t, r in zip(df.index, df.itertuples())]


def _trade(df, tr):
    qty, ep, xp = float(tr["qty"]), float(tr["entry_price"]), float(tr["exit_price"])
    pnl = float(tr["gross_pnl"])
    d = 1 if abs(pnl - (xp - ep) * qty) <= abs(pnl + (xp - ep) * qty) else -1          # long or short
    t0, t1 = _mins(tr["entry_time"]), _mins(tr["exit_time"])
    mins = df.index.hour * 60 + df.index.minute
    seg = df[(mins >= int(t0)) & (mins <= int(t1))]
    out = {"symbol": tr["symbol"], "qty": qty, "entry_time": tr["entry_time"], "exit_time": tr["exit_time"],
           "entry": ep, "exit": xp, "pnl": pnl, "dir": d}
    show = df[(mins >= int(t0) - CONTEXT_MIN) & (mins <= int(t1) + CONTEXT_MIN)]
    out["bars"] = _rows(show)
    if len(seg):
        fav = (seg["high"] if d == 1 else seg["low"])
        adv = (seg["low"] if d == 1 else seg["high"])
        fi = fav.idxmax() if d == 1 else fav.idxmin()
        ai = adv.idxmin() if d == 1 else adv.idxmax()
        out["mfe"], out["mfe_t"] = round(d * (float(fav[fi]) - ep) * qty, 2), _hhmm(fi.hour * 60 + fi.minute)
        out["mae"], out["mae_t"] = round(d * (float(adv[ai]) - ep) * qty, 2), _hhmm(ai.hour * 60 + ai.minute)
        out["gave_back"] = round(out["mfe"] - pnl, 2)
    # tighter stop: first bar after entry whose adverse extreme crosses the stop; filled at the stop (or the open if it gapped)
    stops = []
    for pct in STOP_PCTS:
        lvl = ep * (1 - d * pct / 100)
        hit = None
        for t, r in seg.iterrows():
            if t.hour * 60 + t.minute <= int(t0):
                continue                                           # the entry bar itself: order of events unknown
            if (d == 1 and r["low"] <= lvl) or (d == -1 and r["high"] >= lvl):
                hit = (t, min(lvl, r["open"]) if d == 1 else max(lvl, r["open"]))
                break
        stops.append({"pct": pct, "level": round(lvl, 2),
                      "pnl": round(d * (hit[1] - ep) * qty, 2) if hit else pnl,
                      "hit_time": _hhmm(hit[0].hour * 60 + hit[0].minute) if hit else None})
    out["stops"] = stops
    # later entry: buy at the close of the bar N minutes after the real entry, keep the same exit
    delays = []
    for n in DELAYS:
        if int(t0) + n >= int(t1):
            break
        row = df[mins == int(t0) + n]
        if len(row):
            px = float(row["close"].iloc[0])
            delays.append({"min": n, "price": px, "pnl": round(d * (xp - px) * qty, 2)})
    out["delays"] = delays
    return out


def build(cache, instrument, day, trade_rows):
    idx = _bars(cache, instrument.upper(), day)
    res = {"day": day, "instrument": instrument, "index": _rows(idx) if idx is not None else [], "trades": []}
    for tr in trade_rows:
        try:
            df = _bars(cache, tr["symbol"], day)
            if df is not None:
                res["trades"].append(_trade(df, tr))
        except (KeyError, ValueError):
            continue
    return res
