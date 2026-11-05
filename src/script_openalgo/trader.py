"""Trade manager: money management + automatic option BUYING on top of the strategies (paper via OpenAlgo Analyzer).

    python -m script_openalgo.trader auto --session S --instrument NIFTY      # one process per instrument (web app starts it)
    python -m script_openalgo.trader simulate --day 2026-10-23 --config cfg.json   # what the manager would have done that day

The strategies decide WHEN (their own entries and exits). The manager decides WHETHER and HOW MUCH:
  - entries only on the allowed side(s) (CE / PE / both) and strategies, with a journal score >= min_score
  - limits for the whole session (all instruments): max trades per day, max daily net loss (realised, after costs), max
    consecutive losses, max open positions; Stop auto-trade (no new entries) and Flatten all (close everything now)
  - size: fixed lots per trade, or as many lots as fit in max_margin_per_trade (premium x lot size), checked against the
    available funds (OpenAlgo `funds`) in auto mode
  - protection: a hard stop at -hard_stop_pct of the premium, a profit lock (once a trade's gross P&L first reaches
    profit_lock_trigger, a floor at profit_lock_floor_pct % of it; 0 = off), and square-off at SQUARE_OFF (15:15)
  - every order's result is read back (`orderstatus`); rejected / unfilled orders are recorded, never assumed
  - costs (brokerage + statutory, costs.py) are deducted from every trade's P&L
  - position transfer (user, 2026-11-02): on start, AutoManager.reconcile_open_positions() picks up any trade still
    'open' in the ledger for this instrument today, so restarting for a code deployment never silently forgets a
    real open position - it is then managed by the manager's generic protections above (not the strategy's own
    exit signal, which does not resume for that trade; see the method's docstring)
Ledger: tables trader_sessions, trader_trades, trader_events in the workspace DB.
"""
import argparse
import json
import os
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta

from script_openalgo import config
from script_openalgo.strategy_db import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS trader_sessions(
  id TEXT PRIMARY KEY, day TEXT, mode TEXT, status TEXT, config TEXT, started TEXT, ended TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS trader_trades(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, day TEXT, mode TEXT, instrument TEXT, strategy TEXT, side TEXT, symbol TEXT,
  lots INTEGER, qty INTEGER, entry_time TEXT, entry_price REAL, entry_order TEXT, exit_time TEXT, exit_price REAL, exit_order TEXT,
  status TEXT, entry_reason TEXT, exit_reason TEXT, score INTEGER, score_note TEXT, gross REAL, costs REAL, net REAL, created TEXT);
CREATE TABLE IF NOT EXISTS trader_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, ts TEXT, instrument TEXT, strategy TEXT, kind TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS tt_session ON trader_trades(session);
CREATE INDEX IF NOT EXISTS te_session ON trader_events(session);
"""
DEFAULTS = {
    "instruments": {},           # {"NIFTY": {"strategies": ["parabolic_sar_flip_scalp"], "lots": 1}}
    "sides": "both",             # CE | PE | both
    "sizing": "lots",            # lots | margin
    "max_margin_per_trade": 0,   # rupees, used when sizing = margin
    "max_lots": 5,               # safety cap on lots per trade (fixed lots and margin sizing)
    "min_score": 0,              # journal score needed to enter (0 = no score filter)
    "max_trades_per_day": 6,
    "max_daily_loss": 2000,      # rupees, net after costs, realised
    "max_consecutive_losses": 3,
    "max_open_positions": 3,
    "hard_stop_pct": 25,         # exit if the premium falls this % below the entry
    "profit_lock_trigger": 0,    # rupees: once an open trade's gross P&L first reaches this, lock a floor at
                                  # profit_lock_floor_pct % of it (exit if P&L falls back to the floor); 0 = off
    "profit_lock_floor_pct": 50, # the floor, as a % of profit_lock_trigger (e.g. trigger 1000, 50% -> floor 500)
    "square_off": "15:15",
}
_lock = threading.Lock()


def ensure(con):
    con.executescript(SCHEMA)


def cfg_with_defaults(cfg):
    out = dict(DEFAULTS)
    out.update(cfg or {})
    return out


def event(con, session, kind, message, instrument=None, strategy=None, ts=None):
    with _lock:
        con.execute("INSERT INTO trader_events(session, ts, instrument, strategy, kind, message) VALUES(?,?,?,?,?,?)",
                    (session, ts or db.now(), instrument, strategy, kind, str(message)[:500]))
        con.commit()


# ---------------- the decision rules (shared by auto and simulation) ----------------
class Book:
    """Session state from the ledger: trades today, realised net, losing streak, open positions."""

    def __init__(self, con, session):
        self.con, self.session = con, session

    def trades(self):
        return db.rows(self.con, "SELECT * FROM trader_trades WHERE session=? AND status IN ('open','closed') ORDER BY id", (self.session,))

    def summary(self):
        tr = self.trades()
        closed = [t for t in tr if t["status"] == "closed"]
        streak = 0
        for t in reversed(closed):
            if (t["net"] or 0) < 0:
                streak += 1
            else:
                break
        return {"entries": len(tr), "open": [t for t in tr if t["status"] == "open"], "net": round(sum(t["net"] or 0 for t in closed), 2),
                "streak": streak}


def check_entry(cfg, book, stop_flag, side, score):
    """None if the entry is allowed, else the reason it is refused."""
    s = book.summary()
    if stop_flag:
        return f"auto-trade stopped ({stop_flag})"
    if cfg["sides"] in ("CE", "PE") and side != cfg["sides"]:
        return f"side {side} not allowed (only {cfg['sides']})"
    if s["entries"] >= cfg["max_trades_per_day"]:
        return f"max trades per day reached ({cfg['max_trades_per_day']})"
    if s["net"] <= -abs(cfg["max_daily_loss"]):
        return f"daily loss limit reached (net {s['net']:+.0f} <= -{cfg['max_daily_loss']})"
    if s["streak"] >= cfg["max_consecutive_losses"]:
        return f"{s['streak']} losses in a row (limit {cfg['max_consecutive_losses']})"
    if len(s["open"]) >= cfg["max_open_positions"]:
        return f"{len(s['open'])} positions open (limit {cfg['max_open_positions']})"
    if cfg["min_score"] and (score is None or score < cfg["min_score"]):
        return f"journal score {score} below the minimum {cfg['min_score']}"
    return None


def size_lots(cfg, inst_cfg, premium, lot, mult=1, base=1):
    """Lots for one entry: fixed lots (x `base`, the strategy's own normal entry size in lots -- e.g. 2 for a strategy
    whose LOTS constant is 2 by design -- x `mult`, how many multiples of that normal size THIS signal is, e.g. 2 for
    a doubled reversal leg), or what fits in max_margin_per_trade; capped by max_lots. 0 = cannot afford."""
    if cfg["sizing"] == "margin" and cfg["max_margin_per_trade"]:
        n = int(cfg["max_margin_per_trade"] // max(1e-9, premium * lot))
    else:
        n = int(inst_cfg.get("lots") or 1) * max(1, int(base)) * max(1, int(mult))
    return max(0, min(n, int(cfg["max_lots"] or n)))


def sells_options(slug):
    """True for a strategy that opens SHORT option positions (OPTION_SELLING = True): the manager buys options only, and
    would read its SELL as an exit, so such strategies are refused."""
    import os
    import re
    p = os.path.join(config.strategies_dir(), slug + ".py")
    try:
        return bool(re.search(r"^OPTION_SELLING\s*=\s*True", open(p, encoding="utf-8").read(), re.M))
    except OSError:
        return False


def _lots_per(slug):
    """The strategy's LOTS constant (its normal entry size), so a bigger order (reversal leg) is seen as a multiple."""
    import importlib
    sys.path.insert(0, config.strategies_dir())
    try:
        return max(1, int(getattr(importlib.import_module(slug), "LOTS", 1) or 1))
    except Exception:
        return 1


def journal_score(con, slug, instrument, day, t, symbol, frames=None, dte=None):
    from script_openalgo import journal
    try:
        feats = journal.signal_features(db.ws_paths()[1], instrument, day, t, symbol, dte, frames)
        sc = journal.score_signal(con, slug, instrument, day, "CE" if symbol.endswith("CE") else "PE", feats)
        return sc.get("score"), sc.get("note", "")
    except Exception as e:                                   # no score must not stop a trade decision: treated as unknown
        return None, f"score unavailable: {e}"


def charges(instrument, entry, exit_, qty, con):
    from script_openalgo import costs
    from script_openalgo.backtest_core import instruments
    return costs.round_trip(entry, exit_, qty, instruments.get(instrument).get("opt_exchange", "NFO"), costs.settings(con))


# ---------------- simulation of a day ----------------
def contract_params(inst_cfg):
    """The Trade page's per-instrument contract choice as run options: EXPIRY (next week, monthly, ...) and STRIKE_OFFSET.
    Empty = each strategy's saved options (or the file's ATM / nearest expiry)."""
    from script_openalgo.backtest_core import instruments as I
    out = {}
    if inst_cfg.get("expiry"):
        out[I.EXPIRY_KEY] = I.expiry_choice(inst_cfg["expiry"])
    if inst_cfg.get("strike"):
        out[I.STRIKE_KEY] = I.strike_offset(inst_cfg["strike"])
    return out


def _strategy_signals(slug, instrument, day, params=None):
    """Run the strategy on the cached day (FakeClient) and return (signals, logs) in time order: signals are what it
    WANTED to do; logs are its own narrative (the same lines the Signals tab shows), for the manager's log in
    simulate() - matching AutoManager.run(), which captures this narrative into trader_events for live auto mode."""
    import io
    import contextlib
    from script_openalgo import signals as sig

    out, logs = [], []

    class Collect:
        def emit(self, kind, **data):
            if kind == "signal":
                out.append(data)
            elif kind == "log":
                logs.append(data)
    sig.PARAMS.clear()
    sig.PARAMS.update(params or {})                          # the instrument's expiry / strike choice
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            sig.run_replay(slug, instrument, day, None, 0, Collect())
        except SystemExit:
            pass
        finally:
            sig.PARAMS.clear()
    return out, logs


def simulate(con, day, cfg, session=None, quiet=False):
    """What the manager would have done on a cached `day`: every strategy's signals in time order through the same rules,
    sizing, hard stop (from the option's 1m candles), square-off and costs. Writes a 'sim' session to the ledger."""
    ensure(con)
    cfg = cfg_with_defaults(cfg)
    session = session or f"sim-{day}-{datetime.now():%H%M%S}"
    con.execute("INSERT OR REPLACE INTO trader_sessions VALUES(?,?,?,?,?,?,?,?)",
                (session, day, "sim", "running", json.dumps(cfg), db.now(), None, ""))
    con.commit()
    try:
        return _simulate_run(con, day, cfg, session, quiet)
    except Exception as e:
        con.execute("UPDATE trader_sessions SET status='failed', ended=?, note=? WHERE id=?",
                    (db.now(), str(e)[:300], session))
        con.commit()
        raise


def _simulate_run(con, day, cfg, session, quiet):
    from script_openalgo.backtest_core import data_loader as dl
    book = Book(con, session)
    stream = []
    for inst, ic in cfg["instruments"].items():
        for slug in ic.get("strategies", []):
            if sells_options(slug):
                event(con, session, "error", f"{slug} sells options: the manager buys options only; not simulated", inst, slug)
                continue
            sigs, logs = _strategy_signals(slug, inst, date.fromisoformat(day), contract_params(ic))
            for s in sigs:
                stream.append((s["t"], inst, slug, s))
            for l in logs:
                event(con, session, "log", l["msg"], inst, slug, ts=f"{day}T{l['t']}")
    stream.sort(key=lambda x: x[0])
    mf = {inst: dl.load_manifest(date.fromisoformat(day), inst) or {} for inst in cfg["instruments"]}
    open_pos = {}                                            # (inst, slug, symbol) -> position dict (see BUY branch below)
    sq = cfg["square_off"]

    def opt_candles(symbol):
        return dl.load_series(symbol, date.fromisoformat(day))

    def close_trade(key, t, price, reason, sell_strategy_qty=None):
        """sell_strategy_qty: the strategy's own requested SELL quantity for this tranche (its own units, same space
        as its BUY qty) -- None = force-close everything still open (hard stop / square-off), always final. A
        strategy that scales out (partial then final SELL, e.g. vwap_standard_deviation_band_reversion_scalp) is
        tracked across both SELLs and combined into one trade row with a quantity-weighted exit price, the same way
        backtest_core/report.py already does for backtests -- the manager used to treat the FIRST SELL as closing
        the whole position, silently losing the later leg's result."""
        pos = open_pos[key]
        tid = pos["id"]
        tr = con.execute("SELECT * FROM trader_trades WHERE id=?", (tid,)).fetchone()
        if sell_strategy_qty is None:
            take_real = pos["qty"]
            final = True
        else:
            take_strategy = min(sell_strategy_qty, pos["remaining_strategy_qty"])
            final = take_strategy >= pos["remaining_strategy_qty"]
            take_real = pos["qty"] if final else max(1, min(pos["qty"] - 1, round(tr["qty"] * take_strategy / pos["strategy_qty"])))
            pos["remaining_strategy_qty"] -= take_strategy
        pos["exit_notional"] += price * take_real
        pos["exit_qty_done"] += take_real
        pos["qty"] -= take_real
        if not final:
            event(con, session, "log", f"{tr['symbol']} partial exit @ {price:.2f} qty {take_real} (more to come)", tr["instrument"], tr["strategy"], ts=f"{day}T{t}")
            return
        avg_exit = pos["exit_notional"] / pos["exit_qty_done"]
        gross = round((avg_exit - tr["entry_price"]) * tr["qty"], 2)
        c = charges(tr["instrument"], tr["entry_price"], avg_exit, tr["qty"], con)
        con.execute("UPDATE trader_trades SET exit_time=?, exit_price=?, exit_order='SIM', status='closed', exit_reason=?, gross=?, costs=?, net=? WHERE id=?",
                    (t, round(avg_exit, 2), reason, gross, c, round(gross - c, 2), tid))
        con.commit()
        event(con, session, "exit", f"{tr['symbol']} {reason} @ {avg_exit:.2f} net {gross - c:+.0f}", tr["instrument"], tr["strategy"], ts=f"{day}T{t}")
        del open_pos[key]

    def stop_check(upto):
        """Hard stops and square-off of open positions between their last check and `upto` (HH:MM:SS)."""
        for key, pos in list(open_pos.items()):
            tr = con.execute("SELECT * FROM trader_trades WHERE id=?", (pos["id"],)).fetchone()
            df = opt_candles(tr["symbol"])
            if df is None:
                continue
            lvl = tr["entry_price"] * (1 - cfg["hard_stop_pct"] / 100)
            import pandas as pd
            seg = df.loc[pd.Timestamp(f"{day} {tr['entry_time'][:5]}") + pd.Timedelta(minutes=1): pd.Timestamp(f"{day} {min(upto, sq + ':00')[:5]}")]
            hit = seg[seg["low"] <= lvl]
            if cfg["hard_stop_pct"] and len(hit):
                t0 = hit.index[0]
                close_trade(key, (t0 + pd.Timedelta(minutes=1)).strftime("%H:%M:%S"), min(lvl, float(hit["open"].iloc[0])), f"hard stop -{cfg['hard_stop_pct']}%")
            elif upto >= sq + ":00":
                last = df.loc[: pd.Timestamp(f"{day} {sq}") - pd.Timedelta(minutes=1)]
                close_trade(key, sq + ":00", float(last["close"].iloc[-1]), "square-off")

    for t, inst, slug, s in stream:
        stop_check(t)
        key = (inst, slug, s["symbol"])
        if s["action"] == "BUY":
            if t >= sq + ":00":
                continue
            score, note = journal_score(con, slug, inst, day, t, s["symbol"], dte=mf[inst].get("days_to_expiry"))
            why = check_entry(cfg, book, None, "CE" if s["symbol"].endswith("CE") else "PE", score)
            lot = int(mf[inst].get("lot_size") or s["qty"])
            base = _lots_per(slug)
            mult = max(1, s["qty"] // (lot * base))
            lots = size_lots(cfg, cfg["instruments"][inst], s["price"], lot, mult, base) if not why else 0
            if not why and lots == 0:
                why = f"cannot afford 1 lot within the margin limit ({s['price']} x {lot})"
            if why:
                event(con, session, "skip", f"{s['symbol']} at {t}: {why}", inst, slug, ts=f"{day}T{t}")
                continue
            cur = con.execute("INSERT INTO trader_trades(session, day, mode, instrument, strategy, side, symbol, lots, qty, entry_time, entry_price, "
                              "entry_order, status, entry_reason, score, score_note, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              (session, day, "sim", inst, slug, "CE" if s["symbol"].endswith("CE") else "PE", s["symbol"], lots, lots * lot, t,
                               s["price"], "SIM", "open", f"{slug} signal", score, note, db.now()))
            con.commit()
            open_pos[key] = {"id": cur.lastrowid, "qty": lots * lot, "strategy_qty": s["qty"], "remaining_strategy_qty": s["qty"],
                             "exit_notional": 0.0, "exit_qty_done": 0}
            event(con, session, "entry", f"BUY {lots} lot(s) {s['symbol']} @ {s['price']} (score {score})", inst, slug, ts=f"{day}T{t}")
        elif key in open_pos:
            close_trade(key, t, s["price"], "strategy exit", s["qty"])
    stop_check("23:59:59")
    s = book.summary()
    con.execute("UPDATE trader_sessions SET status='done', ended=?, note=? WHERE id=?",
                (db.now(), f"{s['entries']} trades, net {s['net']:+.0f}", session))
    con.commit()
    if not quiet:
        print(f"simulation {session}: {s['entries']} trades, net {s['net']:+.0f}")
    return session


# ---------------- automatic trading (live data, orders through OpenAlgo) ----------------
class ManagedClient:
    """Wraps the strategy's client: BUY orders go through the manager's checks and sizing; SELLs close the managed position.
    A refused BUY returns an error, so the strategy simply has no position (consistent)."""

    def __init__(self, inner, mgr, slug, strat_module):
        self._inner, self.mgr, self.slug, self.strat = inner, mgr, slug, strat_module
        self._fills = {}
        self._n = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def placeorder(self, strategy=None, symbol=None, action=None, exchange=None, price_type="MARKET", product="MIS", quantity=0, **kw):
        if str(action).upper() == "BUY":
            return self.mgr.enter(self, symbol, exchange, product, int(quantity))
        return self.mgr.leave(self, symbol, exchange, product, int(quantity), "strategy exit")

    def orderstatus(self, order_id=None, strategy=None, **kw):
        if order_id in self._fills:
            return {"status": "success", "data": {"average_price": self._fills[order_id], "order_status": "complete"}}
        return self._inner.orderstatus(order_id=order_id, strategy=strategy, **kw)


class AutoManager:
    def __init__(self, session, instrument, cfg, con):
        from openalgo import api
        from script_openalgo.backtest_core import instruments
        self.session, self.inst, self.cfg, self.con = session, instrument, cfg_with_defaults(cfg), con
        self.icfg = instruments.get(instrument)
        self.api = api(api_key=config.api_key(), host=config.host())
        self.day = date.today().isoformat()
        self.book = Book(con, session)
        self.positions = {}                                  # (slug, symbol) -> position dict (see enter()/_close())
        self.lock = threading.Lock()
        self.reconcile_open_positions()

    def reconcile_open_positions(self):
        """Pick up any position still 'open' in the ledger for this instrument today, from an earlier session that
        was restarted (a code deployment, a crash) without being flattened first - so a real position is never
        silently forgotten. Trade-manager only: Signals sessions never place real orders, so there is nothing to
        transfer there. Limitation: only the manager's own generic protections (hard stop %, profit lock,
        square-off, flatten) apply to a transferred position from here on - the strategy that originally opened it
        starts fresh in the new session and will not resume its own strategy-specific exit signal for that trade.
        A scale-out strategy's own partial-exit progress (if any had already happened before the restart) is not
        recoverable either: the position is treated as fully open at its original entry quantity."""
        rows = db.rows(self.con, "SELECT * FROM trader_trades WHERE instrument=? AND day=? AND status='open'",
                       (self.inst, self.day))
        opt_exchange = self.icfg.get("opt_exchange", "NFO")
        for tr in rows:
            self.positions[(tr["strategy"], tr["symbol"])] = {"id": tr["id"], "exchange": opt_exchange, "product": "MIS", "qty": tr["qty"],
                                                               "strategy_qty": tr["qty"], "remaining_strategy_qty": tr["qty"],
                                                               "exit_notional": 0.0, "exit_qty_done": 0}
            self.log("info", f"transferred open position {tr['symbol']} (trade #{tr['id']}, entry {tr['entry_price']}) from an "
                     f"earlier session - managed by hard stop / profit lock / square-off from here on, not its own strategy signal",
                     tr["strategy"])

    def flag(self):
        return db.get_setting(self.con, f"trader_stop_{self.session}", "") or None

    def log(self, kind, msg, slug=None):
        event(self.con, self.session, kind, msg, self.inst, slug)

    def quote(self, symbol, exchange):
        try:
            r = self.api.quotes(symbol=symbol, exchange=exchange)
            return float(r["data"]["ltp"]) if r.get("status") == "success" else None
        except Exception:
            return None

    def _order(self, symbol, exchange, action, qty, product):
        """Place a MARKET order and read back its status; returns (order_id, avg_price) or raises RuntimeError."""
        r = self.api.placeorder(strategy=f"StrategyLabTrader-{self.inst}"[:40], symbol=symbol, action=action, exchange=exchange,
                                price_type="MARKET", product=product, quantity=qty)
        if r.get("status") != "success":
            raise RuntimeError(f"order rejected: {r.get('message') or r}")
        oid = r.get("orderid")
        for _ in range(15):                                  # up to ~15 s for the fill
            time.sleep(1)
            st = self.api.orderstatus(order_id=oid, strategy=f"StrategyLabTrader-{self.inst}"[:40])
            d = st.get("data") or {}
            s = str(d.get("order_status") or "").lower()
            if s in ("complete", "completed", "filled"):
                return oid, float(d.get("average_price") or 0) or self.quote(symbol, exchange)
            if s in ("rejected", "cancelled", "canceled"):
                raise RuntimeError(f"order {oid} {s}: {d.get('rejection_reason') or d}")
        raise RuntimeError(f"order {oid} not confirmed filled within 15 s (check OpenAlgo)")

    def enter(self, client, symbol, exchange, product, qty):
        with self.lock:
            side = "CE" if symbol.endswith("CE") else "PE"
            price = self.quote(symbol, exchange)
            if price is None:
                self.log("skip", f"{symbol}: no quote", client.slug)
                return {"status": "error", "message": "no quote"}
            score, note = journal_score(self.con, client.slug, self.inst, self.day, datetime.now().strftime("%H:%M:%S"), symbol)
            why = check_entry(self.cfg, self.book, self.flag(), side, score)
            lots_per = max(1, int(getattr(client.strat, "LOTS", 1) or 1))
            client._lot = getattr(client, "_lot", None) or max(1, qty // lots_per)     # lot size from the first (normal) entry
            lot = client._lot
            mult = max(1, qty // (lot * lots_per))
            lots = size_lots(self.cfg, self.cfg["instruments"].get(self.inst, {}), price, lot, mult, lots_per) if not why else 0
            if not why and lots == 0:
                why = f"cannot afford 1 lot within the margin limit ({price} x {lot})"
            if not why:
                try:
                    f = self.api.funds()
                    cash = float((f.get("data") or {}).get("availablecash") or 0)
                    if cash and cash < price * lot * lots * 1.05:
                        why = f"available funds {cash:.0f} below {price * lot * lots:.0f}"
                except Exception:
                    pass
            if why:
                self.log("skip", f"{symbol}: {why}", client.slug)
                return {"status": "error", "message": why}
            try:
                oid, fill = self._order(symbol, exchange, "BUY", lot * lots, product)
            except RuntimeError as e:
                self.log("error", f"BUY {symbol}: {e}", client.slug)
                return {"status": "error", "message": str(e)}
            cur = self.con.execute("INSERT INTO trader_trades(session, day, mode, instrument, strategy, side, symbol, lots, qty, entry_time, entry_price, "
                                   "entry_order, status, entry_reason, score, score_note, created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (self.session, self.day, "auto", self.inst, client.slug, side, symbol, lots, lot * lots,
                                    datetime.now().strftime("%H:%M:%S"), fill, oid, "open", f"{client.slug} signal", score, note, db.now()))
            self.con.commit()
            self.positions[(client.slug, symbol)] = {"id": cur.lastrowid, "exchange": exchange, "product": product, "qty": lot * lots,
                                                      "strategy_qty": qty, "remaining_strategy_qty": qty,
                                                      "exit_notional": 0.0, "exit_qty_done": 0}
            client._fills[oid] = fill
            self.log("entry", f"BUY {lots} lot(s) {symbol} @ {fill} (score {score})", client.slug)
            return {"status": "success", "orderid": oid}

    def leave(self, client, symbol, exchange, product, qty, reason):
        """qty: the strategy's own requested SELL quantity (its units) -- may be only part of the managed position
        (a scale-out, e.g. vwap_standard_deviation_band_reversion_scalp's 1SD partial then VWAP-target final exit).
        Only a true final tranche closes the managed position and finalizes the trader_trades row; a partial places
        a real partial SELL and keeps the rest of the position open for the strategy's later exit signal."""
        with self.lock:
            p = self.positions.get((client.slug, symbol))
            if not p:                                        # already closed by the manager (hard stop / flatten)
                client._n += 1
                oid = f"MGR{client._n:05d}"
                client._fills[oid] = self.quote(symbol, exchange) or 0
                return {"status": "success", "orderid": oid}
            r = self._close(p, symbol, reason, client, strategy_qty=qty)
            if r.get("status") == "success" and not r.get("partial"):
                self.positions.pop((client.slug, symbol), None)
            return r

    def _close(self, p, symbol, reason, client=None, strategy_qty=None):
        """strategy_qty: the strategy's own requested SELL quantity for this tranche (its units) -- None = force-
        close everything still open (hard stop / square-off / profit lock / flatten), always a final, full exit.
        A partial tranche places a real partial SELL, updates the position's running weighted-average exit price
        and remaining quantity, and leaves the position open (caller must not pop it) for the next tranche."""
        tr = self.con.execute("SELECT * FROM trader_trades WHERE id=?", (p["id"],)).fetchone()
        if strategy_qty is None:
            take_real, final = p["qty"], True
        else:
            take_strategy = min(strategy_qty, p["remaining_strategy_qty"])
            final = take_strategy >= p["remaining_strategy_qty"]
            take_real = p["qty"] if final else max(1, min(p["qty"] - 1, round(tr["qty"] * take_strategy / p["strategy_qty"])))
        try:
            oid, fill = self._order(symbol, p["exchange"], "SELL", take_real, p["product"])
        except RuntimeError as e:
            p["fails"] = p.get("fails", 0) + 1
            if p["fails"] in (1, 2) or p["fails"] % 30 == 0:         # first failures, then every ~5 minutes of retries
                self.log("error", f"SELL {symbol} FAILED (attempt {p['fails']}): {e} -- CHECK THE POSITION IN OPENALGO",
                         client.slug if client else None)
            return {"status": "error", "message": str(e)}
        if strategy_qty is not None:
            p["remaining_strategy_qty"] -= take_strategy
        p["exit_notional"] += fill * take_real
        p["exit_qty_done"] += take_real
        p["qty"] -= take_real
        if client:
            client._fills[oid] = fill
        if not final:
            self.log("exit", f"PARTIAL SELL {symbol} {reason} @ {fill} qty {take_real} (more to come)", tr["strategy"])
            return {"status": "success", "orderid": oid, "partial": True}
        avg_exit = p["exit_notional"] / p["exit_qty_done"]
        gross = round((avg_exit - tr["entry_price"]) * tr["qty"], 2)
        c = charges(self.inst, tr["entry_price"], avg_exit, tr["qty"], self.con)
        self.con.execute("UPDATE trader_trades SET exit_time=?, exit_price=?, exit_order=?, status='closed', exit_reason=?, gross=?, costs=?, net=? WHERE id=?",
                         (datetime.now().strftime("%H:%M:%S"), round(avg_exit, 2), oid, reason, gross, c, round(gross - c, 2), p["id"]))
        self.con.commit()
        self.log("exit", f"SELL {symbol} {reason} @ {avg_exit:.2f} net {gross - c:+.0f}", tr["strategy"])
        return {"status": "success", "orderid": oid}

    def guard(self, strategy_threads):
        """Every 10 s: flatten on request, hard stops, square-off, daily loss limit -> flatten. Returns (ending the process)
        when flattened / squared off, or when stopped (or every strategy finished) with no position left open."""
        while True:
            time.sleep(10)
            try:
                fl, now = self.flag(), datetime.now().strftime("%H:%M")
                s = self.book.summary()
                loss = s["net"] <= -abs(self.cfg["max_daily_loss"])
                flatten = fl == "flatten" or now >= self.cfg["square_off"] or loss
                with self.lock:
                    for key, p in list(self.positions.items()):
                        tr = self.con.execute("SELECT * FROM trader_trades WHERE id=?", (p["id"],)).fetchone()
                        ltp = self.quote(key[1], p["exchange"])
                        hard = ltp is not None and self.cfg["hard_stop_pct"] and ltp <= tr["entry_price"] * (1 - self.cfg["hard_stop_pct"] / 100)
                        locked = False
                        if ltp is not None and self.cfg["profit_lock_trigger"]:
                            gross = (ltp - tr["entry_price"]) * tr["qty"]
                            if p.get("lock_floor") is None:
                                if gross >= self.cfg["profit_lock_trigger"]:
                                    p["lock_floor"] = self.cfg["profit_lock_trigger"] * self.cfg["profit_lock_floor_pct"] / 100
                                    self.log("info", f"{key[1]}: profit lock armed, {gross:+.0f} >= {self.cfg['profit_lock_trigger']} "
                                             f"-> floor {p['lock_floor']:+.0f}", key[0])
                            elif gross <= p["lock_floor"]:
                                locked = True
                        if flatten or hard or locked:
                            self.positions.pop(key, None)
                            why = "flatten (user)" if fl == "flatten" else "square-off" if now >= self.cfg["square_off"] else \
                                "daily loss limit" if loss else f"hard stop -{self.cfg['hard_stop_pct']}%" if hard else \
                                f"profit lock: floor {p['lock_floor']:+.0f}"
                            r = self._close(p, key[1], why)
                            if r.get("status") != "success":
                                self.positions[key] = p              # retried on the next pass
                    idle = fl == "stop" or not any(t.is_alive() for t in strategy_threads)
                    if not self.positions and (flatten or idle):
                        return
            except Exception as e:
                self.log("error", f"guard: {e}")

    def run(self):
        from script_openalgo import signals as sig
        slugs = self.cfg["instruments"].get(self.inst, {}).get("strategies", [])
        threads = []
        refused = [x for x in slugs if sells_options(x)]
        for x in refused:
            self.log("error", f"{x} sells options: the manager buys options only; not started", x)
        slugs = [x for x in slugs if x not in refused]
        cp = contract_params(self.cfg["instruments"].get(self.inst, {}))
        sig.PARAMS.update(cp)                                # one process per instrument: applies to all its strategies
        if cp:
            self.log("info", "contract: " + ", ".join(f"{k}={v}" for k, v in cp.items()))
        for slug in slugs:
            def go(slug=slug):
                try:
                    strat = sig._load_strategy(slug, self.inst)
                    strat.log = lambda m, slug=slug: event(self.con, self.session, "log", m, self.inst, slug)
                    s = strat.Strategy(ManagedClient(self.api, self, slug, strat), clock=datetime.now, sleeper=time.sleep)
                    s.run()
                    self.log("info", "strategy finished for the day", slug)
                except Exception as e:
                    self.log("error", f"strategy crashed: {e} | {traceback.format_exc()[-600:]}", slug)
            t = threading.Thread(target=go, daemon=True)        # daemon: the process ends with the guard
            t.start()
            threads.append(t)
        self.log("info", f"started: {', '.join(slugs)} (session {self.session})")
        self.guard(threads)
        self.log("info", "auto-trade ended: " + (self.flag() or "strategies finished / square-off") + "; no open positions")



# ---------------- daily review ----------------
def review(con, day, mode=None):
    """The day's managed trades (auto and/or sim): totals, per strategy and instrument, exit reasons, why entries were
    refused, and plain observations for the user's review. Read-only."""
    ensure(con)
    q = "SELECT * FROM trader_trades WHERE day=? AND status='closed'" + (" AND mode=?" if mode else "")
    tr = db.rows(con, q, (day, mode) if mode else (day,))
    sess = [r["id"] for r in db.rows(con, "SELECT id FROM trader_sessions WHERE day=?" + (" AND mode=?" if mode else ""),
                                     (day, mode) if mode else (day,))]
    skips = {}
    if sess:
        for e in db.rows(con, f"SELECT message FROM trader_events WHERE kind='skip' AND session IN ({','.join('?' * len(sess))})", sess):
            why = e["message"].split(": ", 1)[-1]
            why = why.split(" (")[0]
            skips[why] = skips.get(why, 0) + 1

    def agg(rows):
        n = len(rows)
        wins = [r for r in rows if (r["net"] or 0) > 0]
        return {"trades": n, "wins": len(wins), "win_rate": round(len(wins) / n, 2) if n else None,
                "gross": round(sum(r["gross"] or 0 for r in rows), 2), "costs": round(sum(r["costs"] or 0 for r in rows), 2),
                "net": round(sum(r["net"] or 0 for r in rows), 2)}
    by_strat, by_inst, reasons = {}, {}, {}
    for r in tr:
        by_strat.setdefault(r["strategy"], []).append(r)
        by_inst.setdefault(r["instrument"], []).append(r)
        reasons[r["exit_reason"]] = reasons.get(r["exit_reason"], 0) + 1
    tot = agg(tr)
    notes = []
    if tot["trades"] and tot["costs"] > 0.3 * abs(tot["gross"] or 1):
        notes.append(f"costs ate {tot['costs']:.0f} of {tot['gross']:+.0f} gross: fewer, larger-edge trades help more than more trades")
    hs = [r for r in tr if str(r["exit_reason"]).startswith("hard stop")]
    if hs:
        notes.append(f"{len(hs)} trade(s) hit the hard stop ({sum(r['net'] for r in hs):+.0f}): check the entries' scores and times")
    lo = [r for r in tr if r["score"] is not None and r["score"] < 45]
    if lo:
        notes.append(f"{len(lo)} trade(s) entered with a journal score below 45 (net {sum(r['net'] for r in lo):+.0f}); "
                     "a minimum score would have skipped them")
    for k, v in sorted(by_strat.items(), key=lambda kv: agg(kv[1])["net"]):
        a = agg(v)
        if a["net"] < 0 and a["trades"] >= 2:
            notes.append(f"{k}: {a['trades']} trades, net {a['net']:+.0f} today (one day is not evidence; compare with its journal record)")
    return {"day": day, "mode": mode or "all", "total": tot, "by_strategy": {k: agg(v) for k, v in by_strat.items()},
            "by_instrument": {k: agg(v) for k, v in by_inst.items()}, "exit_reasons": reasons, "refused": skips,
            "trades": tr, "notes": notes}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m script_openalgo.trader", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("auto"); a1.add_argument("--session", required=True); a1.add_argument("--instrument", required=True)
    a2 = sub.add_parser("simulate"); a2.add_argument("--day", required=True); a2.add_argument("--config", required=True)
    a2.add_argument("--session")
    a3 = sub.add_parser("review"); a3.add_argument("--day", default=date.today().isoformat()); a3.add_argument("--mode")
    a = ap.parse_args(argv)
    con = db.connect()
    ensure(con)
    if a.cmd == "auto":
        row = con.execute("SELECT config FROM trader_sessions WHERE id=?", (a.session,)).fetchone()
        if not row:
            raise SystemExit(f"unknown session {a.session}")
        con.close()                                          # strategy threads share one connection: allow that
        import sqlite3
        con = sqlite3.connect(os.environ.get("STRATEGY_DB_PATH") or db.ws_paths()[0], timeout=30, check_same_thread=False)
        con.row_factory = sqlite3.Row
        AutoManager(a.session, a.instrument.upper(), json.loads(row[0]), con).run()
    elif a.cmd == "simulate":
        simulate(con, a.day, json.load(open(a.config, encoding="utf-8")), a.session)
    else:
        r = review(con, a.day, a.mode)
        print(json.dumps({k: v for k, v in r.items() if k != "trades"}, indent=1, default=str))
    con.close()


if __name__ == "__main__":
    main()
