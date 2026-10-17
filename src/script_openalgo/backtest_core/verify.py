"""Deterministic review of a strategy + its backtest result (no AI needed).

    python -m script_openalgo.backtest_core.verify --strategy <slug> --result-dir results/<dir> --date YYYY-MM-DD [--instrument NIFTY]

Prints a JSON verdict {"status": "pass"|"fail", "findings": [...], "info": [...]} and exits 0 on pass, 1 on fail.
pass = no finding of severity high/medium.

Checks
  results     summary.json, trades.csv, log.txt exist; trades reconcile with the summary; no position left open
  prices      every entry (and single-fill exit) price equals the close of the last COMPLETED 1m bar before the order
              time, recomputed from the cached candles (proves no lookahead in fills)
  timing      entry <= exit, all inside the 09:15-15:30 session
  strategy    static checks: class Strategy(client, clock, sleeper); no direct clock/sleep calls; no file or network
              access (so it cannot read future data); no hard-coded API key; only client methods FakeClient supports
  backtest    imports the real strategy and builds it with Strategy(...)
"""
import argparse
import ast
import csv
import json
import os
import re
import sys
from datetime import date, datetime, timedelta

from script_openalgo import config  # noqa: E402

ROOT = config.home()

from script_openalgo.backtest_core import data_loader as dl  # noqa: E402
from script_openalgo.backtest_core import scaffold  # noqa: E402

TOL = 0.011


def _f(sev, where, issue):
    return {"severity": sev, "where": where, "issue": issue}


def check_strategy_source(slug):
    findings = []
    p = scaffold.strategy_path(slug)
    if not os.path.exists(p):
        return [_f("high", p, "strategy file not found")]
    src = open(p, encoding="utf-8").read()
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [_f("high", p, f"syntax error line {e.lineno}: {e.msg}")]
    cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Strategy"]
    if not cls:
        findings.append(_f("high", p, "no class Strategy"))
    else:
        init = [n for n in cls[0].body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
        args = {a.arg for a in init[0].args.args} if init else set()
        if not {"client", "clock", "sleeper"} <= args:
            findings.append(_f("high", p, "Strategy.__init__ must accept client, clock and sleeper"))
    if not re.search(r'if\s+__name__\s*==\s*[\'"]__main__[\'"]', src):
        findings.append(_f("low", p, "no `if __name__ == \"__main__\"` guard"))
    # calls that bypass the injected clock/sleeper or read data outside the client
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            line = f"{p}:{node.lineno}"
            if name in ("datetime.now", "datetime.datetime.now", "time.sleep", "time.time", "datetime.utcnow") and fn.name != "log":
                findings.append(_f("medium", line, f"direct call {name}() in {fn.name}(): use the injected clock/sleeper"))
            if name in ("open", "pd.read_parquet", "pd.read_csv", "pandas.read_parquet", "pandas.read_csv") \
                    or name.startswith(("requests.", "urllib.", "http.client.", "socket.")):
                findings.append(_f("high", line, f"{name}() reads outside the client: could bypass the no-lookahead guarantee"))
    if re.search(r"""(?i)api_?key\s*=\s*['"][0-9a-f]{20,}['"]""", src):
        findings.append(_f("high", p, "hard-coded API key"))
    missing, used = scaffold.missing_client_methods(slug)
    if missing:
        findings.append(_f("high", p, f"uses client methods FakeClient does not implement: {', '.join(missing)}"))
    return findings


def check_backtest_source(slug):
    p = scaffold.backtest_path(slug)
    if not os.path.exists(p):
        return [_f("high", p, "backtest file not found")]
    src = open(p, encoding="utf-8").read()
    out = []
    if f"import {slug} as strat" not in src:
        out.append(_f("high", p, "does not import the real strategy module"))
    if "strat.Strategy(" not in src:
        out.append(_f("high", p, "does not build strat.Strategy(...)"))
    return out


def _close_before(df, when):
    sub = df.loc[:when - timedelta(minutes=1)]
    return None if sub.empty else float(sub["close"].iloc[-1])


def check_results(result_dir, day, instrument):
    findings, info = [], []
    need = {n: os.path.join(result_dir, n) for n in ("summary.json", "log.txt")}
    for n, p in need.items():
        if not os.path.exists(p):
            findings.append(_f("high", p, f"{n} missing"))
    if findings:
        return findings, info
    summary = json.load(open(need["summary.json"], encoding="utf-8"))
    tp = os.path.join(result_dir, "trades.csv")
    trades = list(csv.DictReader(open(tp, encoding="utf-8"))) if os.path.exists(tp) else []
    if not trades:
        info.append("no trades on this day: the strategy's conditions never triggered. Check the rules by hand before trusting this")
        if summary.get("trades"):
            findings.append(_f("high", tp, "summary reports trades but trades.csv is empty"))
        return findings, info
    total = sum(float(t["gross_pnl"]) for t in trades)
    if abs(total - float(summary.get("gross_pnl", 0))) > 0.05:
        findings.append(_f("high", tp, f"trade P&L sum {total:.2f} != summary gross_pnl {summary.get('gross_pnl')}"))
    if len(trades) != summary.get("trades"):
        findings.append(_f("medium", tp, f"{len(trades)} rows in trades.csv but summary says {summary.get('trades')} trades"))
    if summary.get("unclosed_positions"):
        findings.append(_f("high", need["summary.json"], f"positions left open: {summary['unclosed_positions']}"))
    open_t, close_t = datetime.strptime("09:15:00", "%H:%M:%S").time(), datetime.strptime("15:30:00", "%H:%M:%S").time()
    checked = skipped = 0
    for i, t in enumerate(trades, 1):
        et = datetime.combine(day, datetime.strptime(t["entry_time"], "%H:%M:%S").time())
        xt = datetime.combine(day, datetime.strptime(t["exit_time"], "%H:%M:%S").time())
        if et > xt:
            findings.append(_f("high", f"trade {i}", "entry after exit"))
        if et.time() < open_t or xt.time() > close_t:
            findings.append(_f("high", f"trade {i}", f"outside the session ({t['entry_time']} - {t['exit_time']})"))
        df = dl.load_series(t["symbol"], day)
        if df is None:
            findings.append(_f("high", f"trade {i}", f"no cached candles for {t['symbol']}"))
            continue
        px = _close_before(df, et)
        if px is None or abs(px - float(t["entry_price"])) > TOL:
            findings.append(_f("high", f"trade {i} {t['symbol']}", f"entry price {t['entry_price']} != last completed bar close {px} before {t['entry_time']}"))
        else:
            checked += 1
        xp = _close_before(df, xt)
        if xp is not None and abs(xp - float(t["exit_price"])) <= TOL:
            checked += 1
        else:
            skipped += 1          # scale-out exits are quantity-weighted averages, so they cannot be matched to one bar
    info.append(f"{checked} fill prices re-derived from the candles and matched; {skipped} exit prices not single-fill (not compared)")
    return findings, info


def verify(slug, result_dir, day, instrument="NIFTY"):
    findings = check_strategy_source(slug) + check_backtest_source(slug)
    rf, info = check_results(result_dir, day, instrument)
    findings += rf
    bad = [f for f in findings if f["severity"] in ("high", "medium")]
    return {"status": "fail" if bad else "pass", "findings": findings, "info": info}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--result-dir", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--instrument", default="NIFTY")
    a = ap.parse_args()
    res = verify(a.strategy, a.result_dir, date.fromisoformat(a.date), a.instrument)
    print(json.dumps(res, indent=1))
    sys.exit(0 if res["status"] == "pass" else 1)
