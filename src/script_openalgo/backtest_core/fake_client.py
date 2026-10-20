"""Simulated OpenAlgo client backed by data_cache/. Implements only what strategies may call
(see .claude/skills/strategy_builder/knowledge/openalgo_api.md)."""
import re
import json
from datetime import date, timedelta

import pandas as pd

from . import data_loader as dl
from . import instruments


def _interval_minutes(interval):
    """'5m' -> 5, '1h' -> 60, 'D' -> 1440."""
    if interval.upper() == "D":
        return 1440
    m = re.match(r"^(\d+)([a-zA-Z]+)$", interval or "")
    if not m:
        raise ValueError(f"unsupported interval {interval!r}")
    n, unit = int(m.group(1)), m.group(2).lower()
    if unit == "m":
        return n
    if unit == "h":
        return n * 60
    raise ValueError(f"unsupported interval {interval!r}")


class FakeClient:
    def __init__(self, day, clock, slippage_pts=0.0, charges_per_order=0.0, instrument="NIFTY"):
        self.day = day
        self.clock = clock
        self.slippage = slippage_pts
        self.charges = charges_per_order
        self.instrument = instrument.upper()
        self.cfg = instruments.get(self.instrument)
        self.manifest = dl.load_manifest(day, self.instrument)
        if not self.manifest or not self.manifest.get("complete"):
            raise RuntimeError(f"{self.instrument} data for {day} is missing or incomplete; "
                               "run backtest_core.data_loader first")
        self.expiry_date = date.fromisoformat(self.manifest["expiry"])
        self.lot = int(self.manifest["lot_size"])
        self._series = {}
        self.orders = []           # dicts: id, time, cycle, symbol, action, qty, price
        self.strategy = None       # set by the runner, used only to tag orders with the cycle number

    # ---------- data ----------
    def _df(self, symbol):
        if symbol not in self._series:
            self._series[symbol] = dl.load_series(symbol, self.day)
        return self._series[symbol]

    def _price(self, symbol):
        """Close of the last COMPLETED 1m bar at sim-time (bar T is complete at T+1min). No lookahead."""
        df = self._df(symbol)
        if df is None or df.empty:
            return None
        cutoff = self.clock.now() - timedelta(minutes=1)
        sub = df.loc[:cutoff]
        if sub.empty:
            return None
        return float(sub["close"].iloc[-1])

    # ---------- OpenAlgo API surface ----------
    def quotes(self, symbol, exchange):
        df = self._df(symbol)
        if df is None or df.empty:
            return {"status": "error", "message": f"no data for {symbol} at {self.clock.now():%H:%M}"}
        cutoff = self.clock.now() - timedelta(minutes=1)
        sub = df.loc[:cutoff]
        if sub.empty:
            return {"status": "error", "message": f"no data for {symbol} at {self.clock.now():%H:%M}"}
        # open/high/low are session-to-date (from 09:15 up to the last completed 1m bar), matching what a
        # live quotes() call would show intraday; no lookahead since `sub` already stops at `cutoff`.
        data = {
            "ltp": float(sub["close"].iloc[-1]),
            "open": float(sub["open"].iloc[0]),
            "high": float(sub["high"].max()),
            "low": float(sub["low"].min()),
        }
        return {"status": "success", "data": data}

    def history(self, symbol, exchange, interval, start_date=None, end_date=None, source="api", **kw):
        """Resampled OHLCV candles for `symbol` on self.day. Only bars whose full period has already
        elapsed at sim-time are returned (no lookahead): a bar starting at T is included only once
        every 1-minute candle inside [T, T+interval) has completed."""
        if str(interval).upper() in ("D", "1D"):
            return self._daily(symbol, start_date, end_date)
        if end_date and date.fromisoformat(str(end_date)[:10]) < self.day:
            return self._history_before_today(symbol, interval, start_date, end_date)
        raw = self._df(symbol)
        if raw is None or raw.empty:
            return pd.DataFrame()
        cutoff = self.clock.now() - timedelta(minutes=1)   # last completed 1m bar
        sub = raw.loc[:cutoff]
        if sub.empty:
            return pd.DataFrame()
        bar_minutes = _interval_minutes(interval)
        if bar_minutes == 1:
            return sub[["open", "high", "low", "close", "volume"]].copy()
        origin = sub.index[0].normalize() + timedelta(hours=9, minutes=15)
        agg = sub.resample(f"{bar_minutes}min", origin=origin, label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna(subset=["open"])
        last_1m_end = sub.index[-1] + timedelta(minutes=1)   # end of the last available 1m bar
        agg = agg[agg.index + pd.Timedelta(minutes=bar_minutes) <= last_1m_end]
        return agg

    def _history_before_today(self, symbol, interval, start_date, end_date):
        """Intraday bars of earlier, cached days (indicator warm-up). Never includes the simulated day, so no lookahead;
        days missing from the cache are skipped."""
        bar_minutes = _interval_minutes(interval)
        d = date.fromisoformat(str(start_date)[:10]) if start_date else date.fromisoformat(str(end_date)[:10])
        last = date.fromisoformat(str(end_date)[:10])
        parts = []
        while d <= last:
            raw = dl.load_series(symbol, d) if d.weekday() < 5 else None
            if raw is not None and not raw.empty:
                if bar_minutes == 1:
                    parts.append(raw[["open", "high", "low", "close", "volume"]])
                else:
                    origin = raw.index[0].normalize() + timedelta(hours=9, minutes=15)
                    parts.append(raw.resample(f"{bar_minutes}min", origin=origin, label="left", closed="left").agg(
                        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna(subset=["open"]))
            d += timedelta(days=1)
        return pd.concat(parts) if parts else pd.DataFrame()

    def _daily(self, symbol, start_date=None, end_date=None):
        """Daily bars of the index. Only COMPLETED days are returned: the bar of the simulated day itself is not known yet."""
        if str(symbol).upper() != self.cfg["index_symbol"]:
            return pd.DataFrame()
        df = dl.load_daily(self.instrument)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df[df.index < pd.Timestamp(self.day)]
        if start_date:
            df = df[df.index >= pd.Timestamp(start_date)]
        if end_date:
            df = df[df.index <= pd.Timestamp(end_date)]
        df = df.copy()
        df.index.name = "timestamp"
        return df

    def optionchain(self, underlying, exchange, expiry_date=None, strike_count=None, **kw):
        """Option chain as of sim-time. Uses a saved real snapshot (data_cache/chain_*.json) if one exists at or before the
        simulated time; otherwise builds a PARTIAL chain (only the cached strikes) from the 1m option candles, whose `oi`
        and volume are the last completed bar's. Response fields follow OpenAlgo; `source` and `partial` say which one."""
        if str(underlying).upper() != self.cfg["index_symbol"]:
            return {"status": "error", "message": f"FakeClient holds {self.instrument} data, not {underlying}"}
        now = self.clock.now()
        snaps = [x for x in dl.chain_snapshots(self.instrument, self.day) if x[0] <= now]
        if snaps:
            with open(snaps[-1][1], encoding="utf-8") as f:
                data = json.load(f)
            data["source"], data["partial"] = "snapshot", False
        else:
            data = self._chain_from_bars(now)
            if data is None:
                return {"status": "error", "message": "no index price yet, cannot build a chain"}
        if strike_count:
            atm, step = data["atm_strike"], self.cfg["strike_step"]
            data["chain"] = [r for r in data["chain"] if abs(r["strike"] - atm) <= int(strike_count) * step]
        return data

    def _chain_from_bars(self, now):
        spot = self._price(self.cfg["index_symbol"])
        if spot is None:
            return None
        step = self.cfg["strike_step"]
        atm = self._atm(spot)
        cutoff = now - timedelta(minutes=1)

        def leg(strike, ot):
            sym = dl.option_symbol(self.instrument, self.expiry_date, strike, ot)
            df = self._df(sym)
            if df is None or df.empty:
                return None
            sub = df.loc[:cutoff]
            if sub.empty:
                return None
            k = int(round(abs(strike - atm) / step))
            itm = (strike < atm) if ot == "CE" else (strike > atm)
            label = "ATM" if k == 0 else f"{'ITM' if itm else 'OTM'}{k}"
            return {"symbol": sym, "label": label, "ltp": float(sub["close"].iloc[-1]), "open": float(sub["open"].iloc[0]),
                    "high": float(sub["high"].max()), "low": float(sub["low"].min()), "volume": float(sub["volume"].sum()),
                    "oi": float(sub["oi"].iloc[-1]), "lotsize": self.lot, "bid": None, "ask": None}

        rows = [{"strike": float(k), "ce": leg(k, "CE"), "pe": leg(k, "PE")} for k in sorted(self.manifest["strikes"])]
        e = self.expiry_date
        return {"status": "success", "underlying": self.cfg["index_symbol"], "underlying_symbol": self.cfg["index_symbol"],
                "underlying_exchange": self.cfg["index_exchange"], "underlying_ltp": spot, "expiry_date": dl.fmt_expiry(e),
                "atm_strike": float(atm), "chain": rows, "source": "cached_bars", "partial": True,
                "strikes_covered": [min(self.manifest["strikes"]), max(self.manifest["strikes"])]}

    def _atm(self, spot, option_type="CE"):
        """ATM strike: spot rounded to the strike step (as before for indices). If that strike is not in the cache (irregular
        stock strikes, e.g. OFSS lists 10200/10250 but not 10150), the nearest cached strike for this option type."""
        step = self.cfg["strike_step"]
        strike = int(round(spot / step) * step)
        if self._df(dl.option_symbol(self.instrument, self.expiry_date, strike, option_type)) is not None:
            return strike
        have = [int(k) for k in self.manifest.get("strikes", [])
                if self._df(dl.option_symbol(self.instrument, self.expiry_date, k, option_type)) is not None]
        return min(have, key=lambda k: (abs(k - spot), k)) if have else strike

    def expiry(self, symbol, exchange, instrumenttype):
        e = self.expiry_date
        return {"status": "success", "data": [f"{e.day:02d}-{dl.MONTHS[e.month - 1]}-{e.year % 100:02d}"]}

    def optionsymbol(self, underlying, exchange, expiry_date, offset, option_type):
        if underlying.upper() != self.cfg["index_symbol"]:
            return {"status": "error", "message": f"FakeClient holds {self.instrument} data, not {underlying}"}
        spot = self._price(self.cfg["index_symbol"])
        if spot is None:
            return {"status": "error", "message": f"no {underlying} price yet"}
        if offset != "ATM":
            return {"status": "error", "message": f"offset {offset} not supported by FakeClient"}
        strike = self._atm(spot, option_type)
        sym = dl.option_symbol(self.instrument, self.expiry_date, strike, option_type)
        if self._df(sym) is None:
            return {"status": "error", "message": f"{sym} not in data cache (strike outside fetched range)"}
        return {"status": "success", "symbol": sym, "exchange": self.cfg["opt_exchange"], "lotsize": self.lot,
                "underlying_ltp": spot}

    def placeorder(self, strategy, symbol, action, exchange, price_type, product, quantity, **kw):
        px = self._price(symbol)
        if px is None:
            return {"status": "error", "message": f"no price for {symbol}"}
        px += self.slippage if action == "BUY" else -self.slippage
        oid = f"SIM{len(self.orders) + 1:05d}"
        cost = px * int(quantity) * self.cfg.get("slippage_pct", 0) / 100      # estimated spread, booked in net P&L only
        self.orders.append({"id": oid, "time": self.clock.now(), "cycle": getattr(self.strategy, "cycle", 0),
                            "symbol": symbol, "action": action, "qty": int(quantity), "price": round(px, 2),
                            "cost": round(cost, 2)})
        return {"status": "success", "orderid": oid}

    def orderstatus(self, order_id, strategy):
        for o in self.orders:
            if o["id"] == order_id:
                return {"status": "success", "data": {"average_price": o["price"], "order_status": "complete"}}
        return {"status": "error", "message": "order not found"}

    def analyzerstatus(self):
        return {"status": "success", "data": {"analyze_mode": True, "mode": "backtest"}}
