"""Claude usage tracking and budget gate, shared by every process that calls Claude (orchestrator, researcher).

Every Claude call is reserved here first (rolling-window budget + minimum gap between calls) and finished afterwards with the
usage the CLI reports (tokens, cost, duration). The budget is an ESTIMATE of your plan's allowance: Claude does not tell us the
remaining quota, only the "limit reached" message when it is hit (handled separately by the hold/resume logic).

Settings (table `settings`): claude_max_per_hour, claude_max_per_day, claude_min_gap_sec, claude_max_cost_day (0 = off).
A call has a weight: 1 for an agent step, 3 for a researcher run (many web searches).
"""
import json
from datetime import datetime, timedelta

from . import db

DEFAULTS = {"claude_max_per_hour": 12, "claude_max_per_day": 60, "claude_min_gap_sec": 20, "claude_max_cost_day": 0}


def settings(con):
    out = {}
    for k, v in DEFAULTS.items():
        raw = db.get_setting(con, k, None)
        try:
            out[k] = type(v)(float(raw)) if raw is not None else v
        except ValueError:
            out[k] = v
    return out


def save_settings(con, values):
    for k in DEFAULTS:
        if k in values and values[k] is not None:
            v = float(values[k])
            if v < 0:
                raise ValueError(f"{k} must be >= 0")
            db.set_setting(con, k, int(v) if k != "claude_max_cost_day" else v)


def _window(con, since):
    return db.rows(con, "SELECT ts, weight, cost_usd FROM claude_calls WHERE ts > ? AND status != 'refused' ORDER BY ts",
                   (since.isoformat(timespec="seconds"),))


def _wait_for(rows, key, current, cap, add, span):
    """Seconds until enough old rows have left the rolling window for `current + add` to fit under `cap`."""
    if cap <= 0 or current + add <= cap:
        return 0.0
    need = current + add - cap
    freed = 0.0
    for r in rows:                                   # oldest first
        freed += float(r[key] or 0)
        if freed >= need:
            return max(0.0, (datetime.fromisoformat(r["ts"]) + span - datetime.now()).total_seconds())
    return span.total_seconds()


def check(con, weight=1):
    """(ok, wait_seconds, reason). Does not record anything."""
    s = settings(con)
    now = datetime.now()
    hour = _window(con, now - timedelta(hours=1))
    day = _window(con, now - timedelta(days=1))
    waits = []
    h_used, d_used = sum(r["weight"] for r in hour), sum(r["weight"] for r in day)
    w = _wait_for(hour, "weight", h_used, s["claude_max_per_hour"], weight, timedelta(hours=1))
    if w:
        waits.append((w, f"hourly budget {h_used}/{s['claude_max_per_hour']}"))
    w = _wait_for(day, "weight", d_used, s["claude_max_per_day"], weight, timedelta(days=1))
    if w:
        waits.append((w, f"daily budget {d_used}/{s['claude_max_per_day']}"))
    cost = sum(float(r["cost_usd"] or 0) for r in day)
    if s["claude_max_cost_day"] and cost >= s["claude_max_cost_day"]:
        w = _wait_for(day, "cost_usd", cost, s["claude_max_cost_day"], 0.01, timedelta(days=1))
        waits.append((w, f"daily cost {cost:.2f}/{s['claude_max_cost_day']}"))
    last = con.execute("SELECT MAX(ts) FROM claude_calls WHERE status != 'refused'").fetchone()[0]
    if last and s["claude_min_gap_sec"]:
        gap = (datetime.fromisoformat(last) + timedelta(seconds=s["claude_min_gap_sec"]) - now).total_seconds()
        if gap > 0:
            waits.append((gap, "minimum gap between calls"))
    if not waits:
        return True, 0.0, ""
    w, reason = max(waits)
    return False, w, reason


def reserve(con, source, agent, strategy=None, job_id=None, weight=1):
    """Atomically check the budget and record the call as started. Returns (ok, wait_seconds, reason, call_id)."""
    con.commit()
    con.execute("BEGIN IMMEDIATE")
    try:
        ok, wait, reason = check(con, weight)
        if not ok:
            con.execute("ROLLBACK")
            return False, wait, reason, None
        cur = con.execute("INSERT INTO claude_calls(ts,source,agent,strategy,job_id,status,weight) VALUES(?,?,?,?,?,?,?)",
                          (db.now(), source, agent, strategy, job_id, "started", weight))
        cid = cur.lastrowid
        con.execute("COMMIT")
        return True, 0.0, "", cid
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise


def finish(con, call_id, envelope=None, status="ok", limit_hit=False, note=""):
    """Record the outcome and the usage the CLI reported (`--output-format json` envelope)."""
    u = (envelope or {}).get("usage") or {}
    con.execute("UPDATE claude_calls SET status=?, duration_ms=?, cost_usd=?, input_tokens=?, output_tokens=?, "
                "cache_read=?, cache_creation=?, limit_hit=?, note=? WHERE id=?",
                (status, (envelope or {}).get("duration_ms"), (envelope or {}).get("total_cost_usd"),
                 u.get("input_tokens"), u.get("output_tokens"), u.get("cache_read_input_tokens"),
                 u.get("cache_creation_input_tokens"), 1 if limit_hit else 0, note[:300], call_id))
    con.commit()


def usage(con):
    now = datetime.now()
    s = settings(con)
    hour, day = _window(con, now - timedelta(hours=1)), _window(con, now - timedelta(days=1))
    tok = con.execute("SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), COALESCE(SUM(cache_read),0) "
                      "FROM claude_calls WHERE ts > ?", ((now - timedelta(days=1)).isoformat(timespec="seconds"),)).fetchone()
    recent = db.rows(con, "SELECT ts,source,agent,strategy,status,duration_ms,cost_usd,input_tokens,output_tokens,limit_hit "
                          "FROM claude_calls ORDER BY id DESC LIMIT 12")
    ok, wait, reason = check(con, 1)
    return {"settings": s, "hour": sum(r["weight"] for r in hour), "day": sum(r["weight"] for r in day),
            "cost_day": round(sum(float(r["cost_usd"] or 0) for r in day), 3),
            "tokens_day": {"input": tok[0], "output": tok[1], "cache_read": tok[2]},
            "has_capacity": ok, "wait_seconds": round(wait), "reason": reason, "recent": recent}
