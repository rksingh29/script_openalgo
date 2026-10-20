"""Signal session: run a strategy's real code and record its orders as SIGNALS instead of sending them.

    python -m script_openalgo.signals --strategy <slug> --instrument NIFTY --events <file.jsonl>                 # live
    python -m script_openalgo.signals --strategy <slug> --instrument NIFTY --events <f> --date 2026-10-16 --from 10:30 --speed 30
    ... --set "CONFIRM_BARS=2, USE_REGIME_FILTER=true"      # strategy constants for this session only

Live:   prices and candles come from OpenAlgo (real time). placeorder() is NOT sent: it becomes a signal, filled on paper at
        the current LTP so the strategy keeps managing its position. The web app's "Act" button sends a signal for real.
Replay: the strategy runs on the cached candles of --date (FakeClient, no lookahead). Up to --from it runs at full speed;
        from then on simulated time passes --speed times faster than real time, so the chart builds up from that moment.

Events (one JSON object per line, appended and flushed):
  {"type": "start", ...}, {"type": "bar", "t": "HH:MM", "o","h","l","c"}, {"type": "signal", "id", "t", "action", "symbol",
  "qty", "exchange", "price", "underlying"}, {"type": "log", "t", "msg"}, {"type": "clock", "t"},
  {"type": "state", "t", "v": {name: value}} (the strategy's own values: indicators, levels, stop/target; when they change), {"type": "end"|"error", ...}
"""
import argparse
import importlib
import json
import sys
import time
import traceback
from datetime import date, datetime, timedelta

from script_openalgo import config


class Events:
    def __init__(self, path):
        self.f = open(path, "a", encoding="utf-8")

    def emit(self, kind, **data):
        self.f.write(json.dumps({"type": kind, **data}, default=str) + "\n")
        self.f.flush()


def _state_emitter(ev, holder, now):
    last = {"v": None}

    def emit_state():
        if holder.get("s") is None:
            return
        try:
            snap = snapshot(holder["s"])
        except Exception:                                          # never let a display helper stop the strategy
            return
        if snap != last["v"]:
            ev.emit("state", t=now().strftime("%H:%M"), v=snap)
            last["v"] = snap
    return emit_state


class SignalRecorder:
    """Wraps a client: every placeorder() becomes a signal event and a paper fill at the current price."""

    def __init__(self, inner, ev, clock, price_of, underlying):
        self._inner, self._ev, self._clock, self._price_of, self._underlying = inner, ev, clock, price_of, underlying
        self._fills, self._n = {}, 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def placeorder(self, strategy=None, symbol=None, action=None, exchange=None, price_type="MARKET", product="MIS",
                   quantity=0, **kw):
        px = self._price_of(symbol, exchange)
        self._n += 1
        oid = f"SIG{self._n:05d}"
        self._fills[oid] = px
        self._ev.emit("signal", id=self._n, t=self._clock().strftime("%H:%M:%S"), date=self._clock().date().isoformat(),
                      action=str(action).upper(), symbol=symbol, exchange=exchange, qty=int(quantity), product=product,
                      price=px, underlying=self._price_of(*self._underlying))
        return {"status": "success", "orderid": oid}

    def orderstatus(self, order_id=None, strategy=None, **kw):
        if order_id in self._fills:
            return {"status": "success", "data": {"average_price": self._fills[order_id], "order_status": "complete"}}
        return self._inner.orderstatus(order_id=order_id, strategy=strategy, **kw)


SKIP_ATTRS = {"c", "now", "sleep", "bars", "ha_bars", "htf_bars", "warm", "lot", "processed_bars", "option_sym"}


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 4)
    if isinstance(v, (list, tuple)) and v:                       # indicator series aligned with bars: latest value
        for x in reversed(v[-5:]):
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                return round(float(x), 4)
    return None


def snapshot(s):
    """The strategy's current values: numbers, latest value of number lists, numbers inside small dicts (levels, position
    stop/target), and flags. Only attributes the strategy keeps on `self` are visible."""
    out = {}
    for k, v in vars(s).items():
        if k.startswith("_") or k in SKIP_ATTRS:
            continue
        if isinstance(v, dict):
            for k2, v2 in list(v.items())[:12]:
                n = _num(v2)
                if n is not None:
                    out[f"{k}.{k2}"] = n
            continue
        n = _num(v)
        if n is not None:
            out[k] = n
        elif isinstance(v, bool):
            out[k] = v
    return out


PARAMS = {}          # --set KEY=VALUE overrides for this session only (module constants of the strategy)


def parse_params(text):
    """'CONFIRM_BARS=2, USE_REGIME_FILTER=true' -> {"CONFIRM_BARS": 2, "USE_REGIME_FILTER": True}."""
    out = {}
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"'{part}': use NAME=value")
        k, v = (x.strip() for x in part.split("=", 1))
        if not k.isidentifier() or not k.isupper():
            raise ValueError(f"'{k}': parameter names are UPPER_CASE constants of the strategy")
        low = v.lower()
        if low in ("true", "false"):
            out[k] = low == "true"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v.strip("'\"")
    return out


def _load_strategy(slug, instrument):
    sys.path.insert(0, config.strategies_dir())
    strat = importlib.import_module(slug)
    unknown = [k for k in PARAMS if not hasattr(strat, k)]
    if unknown:
        raise SystemExit(f"unknown strategy parameter(s) {unknown} for {slug}")
    from script_openalgo.backtest_core import instruments
    instruments.apply_strategy_overrides(strat, instrument, PARAMS)
    return strat


def run_replay(slug, instrument, day, start, speed, ev):
    from script_openalgo.backtest_core.fake_client import FakeClient
    from script_openalgo.backtest_core.instruments import get
    from script_openalgo.backtest_core.sim_clock import SimClock
    strat = _load_strategy(slug, instrument)
    cfg = get(instrument)
    clock = SimClock(day)
    fake = FakeClient(day, clock, 0.0, 0.0, instrument)              # raises if the day is not cached / incomplete
    under = (cfg["index_symbol"], cfg["index_exchange"])
    start_dt = datetime.combine(day, datetime.strptime(start, "%H:%M").time()) if start else clock.now()
    raw = fake._df(cfg["index_symbol"])
    state = {"bar": 0, "last_clock": None}
    holder = {}
    emit_state = _state_emitter(ev, holder, clock.now)

    def emit_bars():
        cutoff = clock.now() - timedelta(minutes=1)                    # completed 1m bars only
        sub = raw.loc[:cutoff]
        for t, r in sub.iloc[state["bar"]:].iterrows():
            ev.emit("bar", t=t.strftime("%H:%M"), o=float(r["open"]), h=float(r["high"]), l=float(r["low"]), c=float(r["close"]))
        state["bar"] = len(sub)
        tm = clock.now().strftime("%H:%M")
        if tm != state["last_clock"]:
            ev.emit("clock", t=tm, paced=clock.now() >= start_dt)
            state["last_clock"] = tm
            emit_state()

    def sleeper(seconds):
        before = clock.now()
        clock.sleep(seconds)
        emit_bars()
        if speed > 0 and clock.now() > start_dt:                       # pace only after the chosen start time
            paced = (clock.now() - max(before, start_dt)).total_seconds()
            time.sleep(max(0.0, paced) / speed)

    def log(msg):
        ev.emit("log", t=clock.now().strftime("%H:%M:%S"), msg=str(msg))

    strat.log = log
    client = SignalRecorder(fake, ev, clock.now, lambda s, e: fake._price(s), under)
    s = strat.Strategy(client, clock=clock.now, sleeper=sleeper)
    holder["s"] = s
    fake.strategy = s
    ev.emit("start", mode="replay", strategy=slug, instrument=instrument, date=day.isoformat(), start=start, speed=speed,
            underlying=cfg["index_symbol"], lot=fake.lot, expiry=fake.expiry_date.isoformat(), params=PARAMS)
    s.run()


def run_live(slug, instrument, ev):
    from openalgo import api
    from script_openalgo.backtest_core.instruments import get
    strat = _load_strategy(slug, instrument)
    cfg = get(instrument)
    key = config.api_key()
    if not key:
        raise SystemExit("OPENALGO_API_KEY not found (environment or .env)")
    real = api(api_key=key, host=config.host())
    under = (cfg["index_symbol"], cfg["index_exchange"])
    state = {"last": None, "next_fetch": 0.0}
    holder = {}
    emit_state = _state_emitter(ev, holder, datetime.now)

    def price_of(symbol, exchange):
        try:
            r = real.quotes(symbol=symbol, exchange=exchange)
            return float(r["data"]["ltp"]) if r.get("status") == "success" else None
        except Exception:
            return None

    def emit_bars(force=False):
        if not force and time.time() < state["next_fetch"]:
            return
        state["next_fetch"] = time.time() + 30
        today = date.today().isoformat()
        try:
            df = real.history(symbol=under[0], exchange=under[1], interval="1m", start_date=today, end_date=today)
        except Exception as e:
            ev.emit("log", t=datetime.now().strftime("%H:%M:%S"), msg=f"[signals] candle fetch failed: {e}")
            return
        if not hasattr(df, "iterrows") or df.empty:
            return
        now = datetime.now()
        for ts, r in df.iterrows():
            t = ts.to_pydatetime().replace(tzinfo=None)
            if t + timedelta(minutes=1) > now or (state["last"] and t <= state["last"]):
                continue                                               # unfinished bar, or already sent
            ev.emit("bar", t=t.strftime("%H:%M"), o=float(r["open"]), h=float(r["high"]), l=float(r["low"]), c=float(r["close"]))
            state["last"] = t
        ev.emit("clock", t=now.strftime("%H:%M"), paced=True)
        emit_state()

    def sleeper(seconds):
        end = time.time() + max(0.0, seconds)
        while time.time() < end:                                       # stay responsive to candle updates
            time.sleep(min(5.0, max(0.0, end - time.time())))
            emit_bars()

    def log(msg):
        ev.emit("log", t=datetime.now().strftime("%H:%M:%S"), msg=str(msg))

    strat.log = log
    client = SignalRecorder(real, ev, datetime.now, price_of, under)
    s = strat.Strategy(client, clock=datetime.now, sleeper=sleeper)
    holder["s"] = s
    ev.emit("start", mode="live", strategy=slug, instrument=instrument, date=date.today().isoformat(),
            underlying=under[0], host=config.host(), params=PARAMS)
    emit_bars(force=True)
    s.run()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--instrument", default="NIFTY")
    ap.add_argument("--events", required=True)
    ap.add_argument("--date", help="replay this cached day (default: live)")
    ap.add_argument("--from", dest="start", help="HH:MM: replay at full speed until this time, then paced")
    ap.add_argument("--speed", type=float, default=30.0, help="replay speed (simulated seconds per real second; 0 = no pause)")
    ap.add_argument("--set", dest="params", default="", help="strategy constants for this session, e.g. 'CONFIRM_BARS=2, LOTS=1'")
    a = ap.parse_args()
    ev = Events(a.events)
    try:
        PARAMS.update(parse_params(a.params))
        if a.date:
            run_replay(a.strategy, a.instrument.upper(), date.fromisoformat(a.date), a.start, a.speed, ev)
        else:
            run_live(a.strategy, a.instrument.upper(), ev)
        ev.emit("end", reason="strategy finished for the day")
    except SystemExit as e:
        ev.emit("error", message=str(e))
        raise
    except Exception as e:
        ev.emit("error", message=f"{type(e).__name__}: {e}", trace=traceback.format_exc()[-1500:])
        raise


if __name__ == "__main__":
    main()
