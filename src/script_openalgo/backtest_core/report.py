"""Turn the fake client's orders + strategy log into trades.csv, summary.json and a printed summary."""
import csv
import json
import os
import re
from collections import defaultdict


def build_trades(orders, charges_per_order=0.0):
    """Pair each BUY with the later SELL(s) of the same symbol, FIFO per (cycle, symbol).
    A single BUY may be closed by more than one SELL (e.g. a partial target exit followed by a
    later final exit, as in scale-out strategies); these are combined into one trade row with a
    quantity-weighted exit price so each entry still shows as exactly one trade."""
    open_legs = defaultdict(list)
    trades = []
    for o in orders:
        key = (o["cycle"], o["symbol"])
        if o["action"] == "BUY":
            open_legs[key].append({"entry_time": o["time"], "entry_price": o["price"],
                                   "qty": o["qty"], "remaining": o["qty"], "cost": o.get("cost", 0.0),
                                   "exit_notional": 0.0, "exit_qty": 0, "exit_time": None})
        else:
            qty_left = o["qty"]
            while qty_left > 0 and open_legs[key]:
                leg = open_legs[key][0]
                take = min(qty_left, leg["remaining"])
                leg["remaining"] -= take
                leg["exit_qty"] += take
                leg["exit_notional"] += take * o["price"]
                leg["exit_time"] = o["time"]
                leg["cost"] += o.get("cost", 0.0) * take / o["qty"] if o["qty"] else 0.0
                qty_left -= take
                if leg["remaining"] == 0:
                    open_legs[key].pop(0)
                    avg_exit = leg["exit_notional"] / leg["exit_qty"]
                    gross = (avg_exit - leg["entry_price"]) * leg["qty"]
                    trades.append({"cycle": key[0], "symbol": key[1], "qty": leg["qty"],
                                   "entry_time": leg["entry_time"].strftime("%H:%M:%S"),
                                   "entry_price": leg["entry_price"],
                                   "exit_time": leg["exit_time"].strftime("%H:%M:%S"),
                                   "exit_price": round(avg_exit, 2),
                                   "gross_pnl": round(gross, 2),
                                   "net_pnl": round(gross - 2 * charges_per_order - leg["cost"], 2),
                                   **({"spread_cost": round(leg["cost"], 2)} if leg["cost"] else {})})
            # a leftover qty_left here (SELL bigger than any open BUY) would mean an order bug
            # upstream; nothing sensible to pair it with, so it is silently dropped like before.
    unclosed = [(k, leg) for k, legs in open_legs.items() for leg in legs]
    return trades, unclosed


def summarize(trades, orders, log_lines, unclosed):
    pnls = [t["gross_pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    cum = peak = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    per_cycle = defaultdict(float)
    for t in trades:
        per_cycle[t["cycle"]] += t["gross_pnl"]
    buys_per_cycle = defaultdict(int)
    for o in orders:
        if o["action"] == "BUY":
            buys_per_cycle[o["cycle"]] += 1
    reasons = defaultdict(int)
    for line in log_lines:
        # "EXIT ALL (cycle N): <reason>" (nifty_direction_ce_pe) or "EXIT <SIDE> <reason> | ..."
        # (vwap_reclaim_scalp-style full/final exits; partial exits are intentionally excluded).
        m = re.search(r"EXIT ALL \(cycle \d+\): ([^|(]+)", line) or re.search(r"\bEXIT (?:CE|PE) ([^|(]+)", line)
        if m:
            reasons[m.group(1).strip()] += 1
    return {
        "trades": len(trades),
        "cycles_with_trades": len(per_cycle),
        "reversals": sum(max(0, n - 1) for n in buys_per_cycle.values()),
        "gross_pnl": round(sum(pnls), 2),
        "net_pnl": round(sum(t["net_pnl"] for t in trades), 2),
        **({"spread_cost": round(sum(t.get("spread_cost", 0) for t in trades), 2)} if any(t.get("spread_cost") for t in trades) else {}),
        "win_rate": round(len(wins) / len(pnls), 3) if pnls else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "max_drawdown": round(dd, 2),
        "pnl_per_cycle": {str(k): round(v, 2) for k, v in sorted(per_cycle.items())},
        "stop_reasons": dict(reasons),
        "unclosed_positions": [f"{k[1]} (cycle {k[0]})" for k, _ in unclosed],
    }


def write_results(out_dir, day, trades, summary, log_lines):
    os.makedirs(out_dir, exist_ok=True)
    if trades:
        with open(os.path.join(out_dir, "trades.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(dict.fromkeys(k for t in trades for k in t)), restval="")
            w.writeheader()
            w.writerows(trades)
    params = {}
    try:                                                   # options chosen for this run in the web app (STRATEGY_PARAMS)
        params = json.loads(os.environ.get("STRATEGY_PARAMS") or "{}") or {}
    except ValueError:
        pass
    with open(os.path.join(out_dir, "summary.json"), "w") as f:
        json.dump({"date": day.isoformat(), **summary, **({"params": params} if params else {})}, f, indent=1)
    with open(os.path.join(out_dir, "log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))


def print_summary(day, trades, summary):
    print(f"\n===== BACKTEST RESULT {day} =====")
    for t in trades:
        print(f"  cycle {t['cycle']} {t['symbol']} {t['entry_time']}@{t['entry_price']} -> "
              f"{t['exit_time']}@{t['exit_price']}  P&L {t['gross_pnl']:+.0f}")
    for k, v in summary.items():
        print(f"  {k}: {v}")
