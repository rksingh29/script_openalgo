"""
Strategy Lab web app (local only).

    python -m webapp.app            # http://127.0.0.1:5055

Binds to 127.0.0.1. Mutating requests need the X-Requested-With header (blocks cross-site form posts) and a
localhost Host header. The OpenAlgo API key is read from the environment / .env by child processes only.
"""
import csv
import glob
import json
import os
import re
import shutil
import sys
from datetime import date

import pandas as pd

from flask import Flask, abort, jsonify, request, send_from_directory

from script_openalgo.backtest_core import instruments  # noqa: E402
from script_openalgo import backup as backup_mod, config  # noqa: E402
from script_openalgo.strategy_db import db, gate, library  # noqa: E402
from script_openalgo.webapp import jobs  # noqa: E402

app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 ** 3                  # backups can be large; imports are verified before anything is applied
PORT = int(os.environ.get("STRATEGY_LAB_PORT", "5055"))
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


def ws():
    return db.workspaces()["active"]


@app.before_request
def guard():
    if request.host not in ALLOWED_HOSTS:
        abort(403)
    if request.method != "GET" and request.headers.get("X-Requested-With") != "strategy-lab":
        abort(403)


def conn():
    return db.connect(ws())


def err(msg, code=400):
    return jsonify({"error": msg}), code


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------- state ----------------
PHASE_PCT = {"starting": 3, "strategy": 10, "data": 30, "backtest": 55, "review": 80, "learn": 95}    # test phases, rough share done

@app.get("/api/state")
def state():
    con = conn()
    stats = db.strategy_stats(con)
    q_order = {r["id"]: i + 1 for i, r in enumerate(db.queue(con))}
    strategies = []
    for r in db.rows(con, "SELECT id,slug,name,family,status,phase,status_note,pinned,origin,file,testable,"
                          "testable_reason,popularity,created,updated,archived_at,aliases,tags,parent_id,code_edited_at "
                          "FROM strategies ORDER BY id DESC"):
        r["aliases"] = json.loads(r["aliases"] or "[]")
        r["tags"] = json.loads(r["tags"] or "[]")
        r["stats"] = stats.get(r["id"])
        r["queue_pos"] = q_order.get(r["id"])
        if r["status"] == "in_progress":                 # progress + whether a job is really running (else it is stuck)
            j = con.execute("SELECT id,kind,started,phase FROM jobs WHERE strategy_id=? AND status='running' "
                            "ORDER BY id DESC", (r["id"],)).fetchone()
            r["job"] = dict(j) if j else None
            r["pct"] = PHASE_PCT.get(r["phase"]) if j else None
        strategies.append(r)
    js = db.rows(con, "SELECT id,kind,strategy_id,label,status,phase,started,ended,exit_code,note,questions "
                      "FROM jobs ORDER BY id DESC LIMIT 25")
    for j in js:
        j["questions"] = json.loads(j["questions"] or "[]")
    out = {"workspace": ws(), "workspaces": db.workspaces()["items"], "strategies": strategies, "jobs": js,
           "auto_run": db.get_setting(con, "auto_run", "0") == "1", "key_present": jobs.key_present(),
           "paused_until": jobs.paused_until(con), "usage": gate.usage(con),
           "llm_review": db.get_setting(con, "llm_review", "0") == "1",
           "instruments": list(instruments.INSTRUMENTS),
           "research_log": db.rows(con, "SELECT ts,action,detail FROM research_log ORDER BY id DESC LIMIT 15")}
    con.close()
    return jsonify(out)


@app.get("/api/strategies/<int:sid>")
def strategy_detail(sid):
    con = conn()
    r = con.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    if not r:
        return err("not found", 404)
    d = dict(r)
    for k in ("aliases", "spec", "sources", "tags"):
        d[k] = json.loads(d[k] or "null")
    if d.get("parent_id"):
        pr = con.execute("SELECT id,name FROM strategies WHERE id=?", (d["parent_id"],)).fetchone()
        d["parent"] = dict(pr) if pr else None
    d["runs"] = db.rows(con, "SELECT * FROM runs WHERE strategy_id=? ORDER BY day DESC, id DESC", (sid,))
    d["jobs"] = db.rows(con, "SELECT id,kind,label,status,phase,started,ended,note FROM jobs WHERE strategy_id=? "
                             "ORDER BY id DESC LIMIT 10", (sid,))
    con.close()
    return jsonify(d)


# ---------------- results ----------------
@app.get("/api/results")
def results():
    """All runs, with strategy names; the UI groups by strategy or by date."""
    con = conn()
    rs = db.rows(con, "SELECT r.*, s.name, s.slug, s.archived_at FROM runs r JOIN strategies s ON s.id=r.strategy_id "
                      "ORDER BY r.day DESC, r.id DESC")
    con.close()
    return jsonify({"runs": rs})


@app.get("/api/runs/<int:rid>")
def run_detail(rid):
    con = conn()
    r = con.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    con.close()
    if not r:
        return err("not found", 404)
    d = dict(r)
    rd = d.get("result_dir")
    d["trade_rows"], d["log_tail"], d["summary"] = [], "", None
    if rd and os.path.isdir(rd) and os.path.commonpath([os.path.abspath(rd), db.RESULTS_DIR]) == db.RESULTS_DIR:
        tp = os.path.join(rd, "trades.csv")
        if os.path.exists(tp):
            d["trade_rows"] = list(csv.DictReader(open(tp, encoding="utf-8")))
        sp = os.path.join(rd, "summary.json")
        if os.path.exists(sp):
            d["summary"] = json.load(open(sp, encoding="utf-8"))
        lp = os.path.join(rd, "log.txt")
        if os.path.exists(lp):
            lines = [l for l in open(lp, encoding="utf-8").read().splitlines() if "P&L " not in l or "TOTAL" not in l]
            d["log_tail"] = "\n".join(lines[-60:])
    return jsonify(d)


@app.get("/api/runs/<int:rid>/chart")
def run_chart(rid):
    d = run_detail(rid)
    if isinstance(d, tuple):
        return d
    r = d.get_json()
    from script_openalgo.webapp import chart
    return jsonify(chart.build(db.ws_paths(ws())[1], r["instrument"], r["day"], r["trade_rows"]))


@app.post("/api/runs/<int:rid>/delete")
def run_delete(rid):
    con = conn()
    ok = db.delete_run(con, rid, delete_dir=bool((request.json or {}).get("delete_files")))
    con.close()
    return jsonify({"deleted": ok})


@app.post("/api/runs/delete")
def runs_delete_many():
    b = request.json or {}
    ids = [int(i) for i in (b.get("ids") or []) if str(i).isdigit()][:5000]
    con = conn()
    n = sum(bool(db.delete_run(con, rid, delete_dir=bool(b.get("delete_files")))) for rid in ids)
    con.close()
    return jsonify({"deleted": n})


def _delete_job(con, j):
    """Remove a finished job row and its log files (only files inside the workspace's log folder)."""
    lp = j["log_path"]
    try:
        events = json.loads(j["meta"] or "{}").get("events")        # signal sessions keep their events next to the log
    except (ValueError, TypeError):
        events = None
    if lp and os.path.commonpath([os.path.abspath(lp), os.path.abspath(jobs.LOG_DIR)]) == os.path.abspath(jobs.LOG_DIR):
        ev_ok = events and os.path.commonpath([os.path.abspath(events), os.path.abspath(jobs.LOG_DIR)]) == os.path.abspath(jobs.LOG_DIR)
        for p in (lp, lp[:-4] + ".exit" if lp.endswith(".log") else None, events if ev_ok else None):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    con.execute("DELETE FROM jobs WHERE id=?", (j["id"],))


FINISHED = ("ok", "failed", "cancelled", "interrupted")      # needs_input / limit / running jobs are still in use


@app.post("/api/jobs/<int:jid>/delete")
def job_delete(jid):
    con = conn()
    j = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not j:
        con.close()
        return err("not found", 404)
    if j["status"] not in FINISHED:
        con.close()
        return err(f"job is {j['status']}; stop it (or answer it) first", 409)
    _delete_job(con, j)
    con.commit()
    con.close()
    return jsonify({"deleted": 1})


@app.post("/api/jobs/clear")
def jobs_clear():
    con = conn()
    rows = con.execute(f"SELECT * FROM jobs WHERE status IN ({','.join('?' * len(FINISHED))})", FINISHED).fetchall()
    for j in rows:
        _delete_job(con, j)
    con.commit()
    con.close()
    return jsonify({"deleted": len(rows)})


# ---------------- instruments: search OpenAlgo, add / edit / remove ----------------
def _inst_row(name, c):
    return {"name": name, "source": c.get("source"), "kind": c.get("kind"), "company": c.get("name") or name,
            "exchange": c.get("index_exchange"), "strike_step": c.get("strike_step"), "expiry_cycle": c.get("expiry_cycle"),
            "min_underlying_bars": c.get("min_underlying_bars"), "min_option_coverage": c.get("min_option_coverage"),
            "slippage_pct": c.get("slippage_pct", 0), "daily_max_loss": (c.get("strategy_overrides") or {}).get("DAILY_MAX_LOSS"),
            "lot_size": c.get("lot_size_seen"), "added": c.get("added"), "note": c.get("note"),
            "cached_days": len(glob.glob(os.path.join(cache_dir(), f"manifest_{name}_*.json")))}


@app.get("/api/instruments")
def instruments_list():
    instruments.reload()
    return jsonify({"items": [_inst_row(k, v) for k, v in instruments.INSTRUMENTS.items()], "file": instruments.user_file()})


@app.get("/api/instruments/search")
def instruments_search():
    from script_openalgo.backtest_core import discovery
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return err("type at least 2 letters")
    try:
        return jsonify({"items": discovery.search(q)})
    except (RuntimeError, OSError, ValueError) as e:
        return err(f"OpenAlgo search failed: {e}", 502)
    except SystemExit as e:                                  # API key missing
        return err(str(e), 502)


def _num(v, lo, hi, name):
    if v in (None, ""):
        return None
    x = float(v)
    if not lo <= x <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return x


@app.post("/api/instruments/add")
def instruments_add():
    from script_openalgo.backtest_core import discovery
    b = request.json or {}
    sym = (b.get("symbol") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9&-]{1,30}", sym):
        return err("invalid symbol")
    try:
        c = discovery.add(sym, min_option_coverage=_num(b.get("min_option_coverage"), 0.1, 1, "coverage"),
                          slippage_pct=_num(b.get("slippage_pct"), 0, 20, "spread %"),
                          daily_max_loss=_num(b.get("daily_max_loss"), 0, 10_000_000, "daily max loss"))
    except ValueError as e:
        return err(str(e))
    except (RuntimeError, OSError) as e:
        return err(str(e), 502)
    except SystemExit as e:
        return err(str(e), 502)
    return jsonify({"added": _inst_row(sym, c)})


@app.post("/api/instruments/<name>/update")
def instruments_update(name):
    name = name.upper()
    if name not in instruments.INSTRUMENTS:
        return err("unknown instrument", 404)
    b = request.json or {}
    c = {k: v for k, v in instruments.INSTRUMENTS[name].items() if k != "source"}
    try:
        for k, lo, hi in (("min_option_coverage", 0.1, 1), ("slippage_pct", 0, 20), ("strike_step", 0.05, 100000)):
            v = _num(b.get(k), lo, hi, k)
            if v is not None:
                c[k] = int(v) if k == "strike_step" and v.is_integer() else v
        if "daily_max_loss" in b:
            ov = dict(c.get("strategy_overrides") or {})
            v = _num(b.get("daily_max_loss"), 0, 10_000_000, "daily max loss")
            if v is None:
                ov.pop("DAILY_MAX_LOSS", None)
            else:
                ov["DAILY_MAX_LOSS"] = v
            c["strategy_overrides"] = ov
    except ValueError as e:
        return err(str(e))
    instruments.save_user(name, c)                           # a built-in edited here gets a workspace override
    return jsonify({"item": _inst_row(name, instruments.INSTRUMENTS[name])})


@app.post("/api/instruments/<name>/delete")
def instruments_delete(name):
    name = name.upper()
    c = instruments.INSTRUMENTS.get(name)
    if not c:
        return err("unknown instrument", 404)
    if c.get("source") != "user":
        return err("built-in instruments cannot be removed", 409)
    instruments.remove_user(name)
    return jsonify({"removed": name, "builtin_restored": name in instruments.INSTRUMENTS})


# ---------------- strategy options (module-level UPPER_CASE constants) chosen per run in the UI ----------------
HIDDEN_PARAMS = {"API_KEY", "HOST", "STRATEGY", "UNDERLYING", "INDEX_EXCHANGE", "OPT_EXCHANGE", "PRODUCT", "TIMEFRAME",
                 "BAR_MINUTES", "HISTORY_SOURCE", "SESSION_START", "VIX_SYMBOL", "VIX_EXCHANGE", "POLL_SEC"}


def strategy_params(slug):
    """[{name, value, type, comment, toggle}] for the strategy's simple module constants (bool/int/float/str), in file
    order. `toggle`: a number whose comment says "0 = off" (shown as a checkbox + value)."""
    import ast
    path = os.path.join(db.STRATEGIES_DIR, slug + ".py")
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines()
    out = []
    for node in ast.parse(src).body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        name = node.targets[0].id
        if not name.isupper() or name in HIDDEN_PARAMS or not isinstance(node.value, ast.Constant):
            continue
        v = node.value.value
        if not isinstance(v, (bool, int, float, str)):
            continue
        line = lines[node.lineno - 1]
        comment = line.split("#", 1)[1].strip().rstrip("=").strip() if "#" in line else ""
        k = node.lineno                                             # continuation comment lines right below
        while k < len(lines) and lines[k].strip().startswith("#") and "----" not in lines[k] and "====" not in lines[k]:
            comment += " " + lines[k].strip().lstrip("#").strip()
            k += 1
        typ = "bool" if isinstance(v, bool) else "int" if isinstance(v, int) else "float" if isinstance(v, float) else "str"
        out.append({"name": name, "value": v, "type": typ, "comment": comment[:300],
                    "toggle": typ in ("int", "float") and "0 = off" in comment})
    return out


def parse_run_params(slug, text):
    """'NAME=value, ...' from the UI -> dict, checked against the strategy file. Raises ValueError with a message."""
    from script_openalgo.signals import parse_params
    params = parse_params(text or "")
    known = {p["name"] for p in strategy_params(slug)}
    unknown = [k for k in params if k not in known]
    if unknown:
        raise ValueError(f"{', '.join(unknown)}: not an option of {slug}")
    return params


@app.get("/api/strategies/<int:sid>/params")
def strategy_params_api(sid):
    con = conn()
    s = con.execute("SELECT slug FROM strategies WHERE id=?", (sid,)).fetchone()
    con.close()
    if not s or not os.path.exists(os.path.join(db.STRATEGIES_DIR, s["slug"] + ".py")):
        return err("strategy or its code file not found", 404)
    inst = (request.args.get("instrument") or "").upper()
    ov = (instruments.INSTRUMENTS.get(inst) or {}).get("strategy_overrides", {}) if inst else {}
    items = strategy_params(s["slug"])
    for p in items:
        if p["name"] in ov:
            p["instrument_value"] = ov[p["name"]]                   # e.g. BANKNIFTY sets CONFIRM_BARS=0
    return jsonify({"items": items, "instrument": inst})


@app.post("/api/signals/clear")
def signals_clear():
    """Delete every finished signal session (with its log and events files). Running sessions are kept."""
    con = conn()
    rows = con.execute("SELECT * FROM jobs WHERE kind='signal' AND status IN ('ok', 'failed', 'cancelled', 'interrupted')").fetchall()
    for j in rows:
        _delete_job(con, j)
    con.commit()
    con.close()
    return jsonify({"deleted": len(rows)})


# ---------------- strategy selector + market news ----------------
@app.get("/api/selector/settings")
def selector_settings():
    con = conn()
    out = {"instruments": json.loads(db.get_setting(con, "selector_instruments", "[]") or "[]"),
           "max_picks": int(db.get_setting(con, "selector_max_picks", "8") or 8),
           "news_enabled": db.get_setting(con, "news_enabled", "0") == "1", "news_slots": list(jobs.NEWS_SLOTS),
           "all_instruments": list(instruments.INSTRUMENTS)}
    con.close()
    return jsonify(out)


@app.post("/api/selector/settings")
def selector_settings_save():
    b = request.json or {}
    insts = [i for i in (b.get("instruments") or []) if i in instruments.INSTRUMENTS]
    try:
        mx = max(1, min(8, int(b.get("max_picks") or 8)))
    except (TypeError, ValueError):
        return err("max picks must be 1-8")
    con = conn()
    db.set_setting(con, "selector_instruments", json.dumps(insts))
    db.set_setting(con, "selector_max_picks", str(mx))
    if "news_enabled" in b:
        db.set_setting(con, "news_enabled", "1" if b.get("news_enabled") else "0")
    con.close()
    return jsonify({"ok": True})


@app.post("/api/selector/run")
def selector_run():
    """Picks for today (live day labels from OpenAlgo) or for a cached past day (rehearsal)."""
    from datetime import datetime as _dt
    from script_openalgo import journal, selector
    b = request.json or {}
    insts = [i for i in (b.get("instruments") or []) if i in instruments.INSTRUMENTS]
    if not insts:
        return err("select at least one instrument")
    day = (b.get("day") or "").strip() or None
    stage = int(b.get("stage") or (1 if _dt.now().strftime("%H:%M") < "09:46" else 2))
    con = conn()
    try:
        selector.build_days(con, quiet=True)
        journal.update_quietly(con)
        labels_by, problems = None, {}
        if not day:
            labels_by = {}
            for i in insts:
                try:
                    labels_by[i] = selector.live_labels(i)
                except (Exception, SystemExit) as e:          # no data yet, broker logged out, ...
                    labels_by[i] = {}
                    problems[i] = str(e)[:160]
        res = selector.pick(con, insts, day, stage if day else stage, int(b.get("max_picks") or 8), labels_by)
        res["problems"] = problems
        res["live"] = not day
        res["evaluated"] = {k: v[:12] for k, v in res["evaluated"].items()}
        if not day:
            db.set_setting(con, "selector_last", json.dumps({"at": db.now(), "stage": stage, "picks": res["picks"]}))
        return jsonify(res)
    finally:
        con.close()


@app.post("/api/selector/backtest")
def selector_backtest():
    from script_openalgo import journal, selector
    b = request.json or {}
    insts = [i for i in (b.get("instruments") or []) if i in instruments.INSTRUMENTS]
    if not insts:
        return err("select at least one instrument")
    con = conn()
    try:
        selector.build_days(con, quiet=True)
        journal.update_quietly(con)
        return jsonify(selector.backtest(con, insts, int(b.get("max_picks") or 8)))
    finally:
        con.close()


@app.get("/api/news")
def news_list():
    """News files of the last days (newest first) and the state of today's slots."""
    d = jobs.news_dir(ws())
    items = []
    if os.path.isdir(d):
        for f in sorted(glob.glob(os.path.join(d, "*.json")), reverse=True)[:9]:
            try:
                items.append(json.load(open(f, encoding="utf-8")) | {"file": os.path.basename(f)})
            except (ValueError, OSError):
                items.append({"file": os.path.basename(f), "error": "not valid JSON"})
    con = conn()
    running = [dict(r) for r in con.execute("SELECT id, label, status, started FROM jobs WHERE kind='news' ORDER BY id DESC LIMIT 3")]
    enabled = db.get_setting(con, "news_enabled", "0") == "1"
    con.close()
    return jsonify({"items": items, "jobs": running, "enabled": enabled, "slots": list(jobs.NEWS_SLOTS)})


@app.post("/api/news/run")
def news_run():
    try:
        return jsonify({"job_id": jobs.start_news(ws())})
    except RuntimeError as e:
        return err(str(e), 409)


# ---------------- signals: run a strategy live or as a replay; its orders become signals; "act" sends one ----------------
MAX_SIGNAL_SESSIONS = 24     # running sessions at once (each strategy x instrument uses one; the Live desk can use up to 3 x 8)
MAX_GROUP = 8                # strategies in one multi-strategy run


def _signal_job(jid):
    con = conn()
    j = con.execute("SELECT * FROM jobs WHERE id=? AND kind='signal'", (jid,)).fetchone()
    con.close()
    return dict(j) if j else None


def _signal_events(j):
    p = json.loads(j["meta"] or "{}").get("events")
    if not p or not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass                                            # a line still being written
    return out


def _openalgo_mode():
    from script_openalgo.backtest_core import data_loader as dl
    try:
        code, b = dl._post("/api/v1/analyzer")
        if code == 200 and b.get("status") == "success":
            return {"ok": True, "analyze_mode": bool(b["data"].get("analyze_mode")), "mode": b["data"].get("mode")}
        return {"ok": False, "message": b.get("message") or f"HTTP {code}"}
    except BaseException as e:                                  # server down, key missing (SystemExit)
        return {"ok": False, "message": str(e)[:200]}


@app.get("/api/signals/mode")
def signals_mode():
    con = conn()
    allow = db.get_setting(con, "allow_live_orders", "0") == "1"
    con.close()
    return jsonify({**_openalgo_mode(), "allow_live_orders": allow})


@app.post("/api/signals/live-orders")
def signals_live_orders():
    b = request.json or {}
    on = bool(b.get("on"))
    if on and b.get("confirm") != "LIVE":
        return err("type LIVE to allow real orders")
    con = conn()
    db.set_setting(con, "allow_live_orders", "1" if on else "0")
    con.close()
    return jsonify({"allow_live_orders": on})


@app.get("/api/signals/sessions")
def signals_sessions():
    con = conn()
    rows = db.rows(con, "SELECT j.id, j.status, j.label, j.started, j.ended, j.meta, s.name FROM jobs j "
                        "LEFT JOIN strategies s ON s.id=j.strategy_id WHERE j.kind='signal' ORDER BY j.id DESC LIMIT 60")
    con.close()
    for r in rows:
        m = json.loads(r.pop("meta") or "{}")
        r.update({k: m.get(k) for k in ("mode", "instrument", "date", "start", "speed", "slug", "params", "group")})
        r["acted"] = len(m.get("acted", {}))
    return jsonify({"items": rows})


class SignalStartError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def _signal_common(b):
    """Instrument / mode / replay day settings shared by every session of a start request (validated once)."""
    from datetime import date as _date
    from script_openalgo.backtest_core import data_loader as dl
    inst = (b.get("instrument") or "NIFTY").upper()
    if inst not in instruments.INSTRUMENTS:
        raise SignalStartError("unknown instrument")
    mode = "replay" if b.get("mode") == "replay" else "live"
    common = {"inst": inst, "mode": mode}
    if mode == "replay":
        day, start = (b.get("date") or "").strip(), (b.get("time") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise SignalStartError("choose the replay date")
        if start and not re.fullmatch(r"\d{2}:\d{2}", start):
            raise SignalStartError("time must be HH:MM")
        try:
            speed = float(b.get("speed") or 30)
        except ValueError:
            raise SignalStartError("bad speed")
        mf = dl.load_manifest(_date.fromisoformat(day), inst)
        if not (mf and mf.get("complete")):
            raise SignalStartError(f"no complete cached data for {inst} on {day}: fetch or backfill it in the Data tab first")
        common.update({"day": day, "start": start, "speed": speed})
    elif not jobs.key_present():
        raise SignalStartError("OPENALGO_API_KEY not found (environment or .env)")
    return common


def _signal_strategy(sid, params_text):
    con = conn()
    s = con.execute("SELECT id, slug, name FROM strategies WHERE id=?", (sid,)).fetchone()
    con.close()
    if not s or not os.path.exists(os.path.join(db.STRATEGIES_DIR, s["slug"] + ".py")):
        raise SignalStartError(f"strategy {sid} not found or its code file is missing")
    try:
        params = parse_run_params(s["slug"], params_text or "")
    except ValueError as e:
        raise SignalStartError(f"options of {s['name']}: {e}")
    return s, params


def _launch_signal(s, params, common, group=None, desk=None):
    inst, mode = common["inst"], common["mode"]
    events = os.path.join(jobs.LOG_DIR, ws(), "signals_{JOB_ID}.jsonl")
    cmd = [sys.executable, "-u", "-m", "script_openalgo.signals", "--strategy", s["slug"], "--instrument", inst, "--events", events]
    if params:
        cmd += ["--set", ", ".join(f"{k}={v}" for k, v in params.items())]
    meta = {"mode": mode, "instrument": inst, "slug": s["slug"], "params": params}
    if group:
        meta["group"] = group
    if desk:
        meta["desk"] = desk
    if mode == "replay":
        cmd += ["--date", common["day"], "--speed", str(common["speed"])] + (["--from", common["start"]] if common["start"] else [])
        meta.update({"date": common["day"], "start": common["start"], "speed": common["speed"]})
    label = f"signals {s['slug']} {inst} " + (f"replay {meta['date']} from {meta['start'] or 'open'} x{meta['speed']:g}"
                                              if mode == "replay" else "LIVE") + \
        (f" [{', '.join(f'{k}={v}' for k, v in params.items())}]" if params else "") + (f" (group {group})" if group else "")
    jid = jobs._launch(ws(), "signal", s["id"], label, cmd, meta=meta)
    con = conn()                                                # store the real events path now that the job id is known
    con.execute("UPDATE jobs SET meta=? WHERE id=?", (json.dumps(dict(meta, events=events.replace("{JOB_ID}", str(jid)))), jid))
    con.commit()
    con.close()
    return jid


def _running_signals():
    con = conn()
    n = con.execute("SELECT COUNT(*) FROM jobs WHERE kind='signal' AND status='running'").fetchone()[0]
    con.close()
    return n


@app.post("/api/signals/start")
def signals_start():
    b = request.json or {}
    try:
        sid = int(b.get("strategy_id"))
    except (TypeError, ValueError):
        return err("choose a strategy")
    try:
        common = _signal_common(b)
        s, params = _signal_strategy(sid, b.get("params"))
        if _running_signals() >= MAX_SIGNAL_SESSIONS:
            raise SignalStartError(f"{MAX_SIGNAL_SESSIONS} signal sessions are running; stop some first", 409)
        return jsonify({"job_id": _launch_signal(s, params, common)})
    except SignalStartError as e:
        return err(str(e), e.code)


@app.post("/api/signals/start-group")
def signals_start_group():
    """Several strategies in one run: one session each (same instrument, mode, day, time, speed), linked by a group id.
    Body: {strategy_ids: [..], params: {strategy_id: "NAME=value, ..."}, instrument, mode, date, time, speed}."""
    import uuid
    b = request.json or {}
    try:
        ids = list(dict.fromkeys(int(x) for x in (b.get("strategy_ids") or [])))
    except (TypeError, ValueError):
        return err("bad strategy list")
    if len(ids) < 2:
        return err("choose at least two strategies (one strategy: use the normal start)")
    if len(ids) > MAX_GROUP:
        return err(f"at most {MAX_GROUP} strategies in one run")
    try:
        common = _signal_common(b)
        chosen = [_signal_strategy(sid, (b.get("params") or {}).get(str(sid))) for sid in ids]   # validate all first
        if _running_signals() + len(chosen) > MAX_SIGNAL_SESSIONS:
            raise SignalStartError(f"this run needs {len(chosen)} sessions; {_running_signals()} of {MAX_SIGNAL_SESSIONS} are in use", 409)
        group = uuid.uuid4().hex[:8]
        jids = [_launch_signal(s, params, common, group) for s, params in chosen]
        return jsonify({"group": group, "job_ids": jids})
    except SignalStartError as e:
        return err(str(e), e.code)


def _group_jobs(group):
    con = conn()
    rows = [dict(r) for r in con.execute("SELECT j.*, s.name FROM jobs j LEFT JOIN strategies s ON s.id=j.strategy_id "
                                         "WHERE j.kind='signal' ORDER BY j.id").fetchall()]
    con.close()
    return [r for r in rows if json.loads(r["meta"] or "{}").get("group") == group]


@app.get("/api/signals/group/<group>")
def signals_group_view(group):
    """Merged view of a group: the underlying's candles and VIX once, every member's signals (tagged), per-member status,
    paper P&L inputs and indicator states (the page draws one member's indicators at a time)."""
    js = _group_jobs(group)
    if not js:
        return err("not found", 404)
    members, bars, vix, start, signals = [], [], [], {}, []
    for j in js:
        ev = _signal_events(j)
        scores = _signal_scores(j, ev)
        meta = json.loads(j["meta"] or "{}")
        acted = meta.get("acted", {})
        b = [e for e in ev if e["type"] == "bar"]
        if len(b) > len(bars):
            bars = b
            vix = [{"t": e["t"], "c": e["c"]} for e in ev if e["type"] == "vix"]
            start = next((e for e in ev if e["type"] == "start"), {})
        end = next((e for e in reversed(ev) if e["type"] in ("end", "error")), None)
        sig = [dict(e, acted=acted.get(str(e["id"])), score=scores.get(str(e["id"])), job_id=j["id"], slug=meta.get("slug"), name=j["name"])
               for e in ev if e["type"] == "signal"]
        signals += sig
        members.append({"id": j["id"], "slug": meta.get("slug"), "name": j["name"], "status": j["status"], "params": meta.get("params") or {},
                        "clock": next((e["t"] for e in reversed(ev) if e["type"] == "clock"), None), "end": end,
                        "states": [{"t": e["t"], "v": e["v"]} for e in ev if e["type"] == "state"],
                        "logs": [e for e in ev if e["type"] == "log"][-40:], "n_signals": len(sig)})
    signals.sort(key=lambda e: (e.get("date", ""), e["t"]))
    m0 = json.loads(js[0]["meta"] or "{}")
    status = "running" if any(m["status"] == "running" for m in members) else members[0]["status"]
    return jsonify({"group": group, "status": status, "meta": {k: m0.get(k) for k in ("mode", "instrument", "date", "start", "speed")},
                    "start": start, "bars": bars, "vix": vix, "signals": signals, "members": members})


@app.post("/api/signals/group/<group>/stop")
def signals_group_stop(group):
    n = sum(bool(jobs.cancel(ws(), j["id"])) for j in _group_jobs(group) if j["status"] == "running")
    return jsonify({"stopped": n})


@app.post("/api/signals/group/<group>/delete")
def signals_group_delete(group):
    js = _group_jobs(group)
    if any(j["status"] == "running" for j in js):
        return err("stop the run first", 409)
    con = conn()
    for j in js:
        _delete_job(con, j)
    con.commit()
    con.close()
    return jsonify({"deleted": len(js)})


def _signal_scores(j, ev):
    """Journal score of every BUY signal of a session, computed once and kept in the job meta. Replay sessions are scored
    against journal trades of EARLIER days only; live sessions use the session's own candles and VIX for the features."""
    from script_openalgo import journal
    from script_openalgo.backtest_core import data_loader as dl
    meta = json.loads(j["meta"] or "{}")
    scores = dict(meta.get("scores") or {})
    todo = [e for e in ev if e["type"] == "signal" and e.get("action") == "BUY" and str(e["id"]) not in scores]
    if not todo:
        return scores
    inst = meta.get("instrument") or "NIFTY"
    cfg = instruments.get(inst)
    cache = db.ws_paths(ws())[1]
    frames = None
    day = meta.get("date")
    if meta.get("mode") == "live":
        day = next((e.get("date") for e in ev if e["type"] == "start"), None) or db.now()[:10]

        def frame(rows, ohlc):
            if not rows:
                return None
            idx = pd.to_datetime([f"{day} {r['t']}" for r in rows])
            cols = {k: [r[k] for r in rows] for k in ("o", "h", "l", "c")} if ohlc else {k: [r["c"] for r in rows] for k in ("o", "h", "l", "c")}
            return pd.DataFrame({"open": cols["o"], "high": cols["h"], "low": cols["l"], "close": cols["c"],
                                 "volume": 0, "oi": 0}, index=idx)
        frames = {cfg["index_symbol"]: frame([e for e in ev if e["type"] == "bar"], True),
                  "INDIAVIX": frame([e for e in ev if e["type"] == "vix"], False)}
    try:
        mf = dl.load_manifest(date.fromisoformat(day), inst) or {}
    except (TypeError, ValueError):
        mf = {}
    con = conn()
    try:
        for e in todo:
            feats = journal.signal_features(cache, inst, day, e["t"], e["symbol"], mf.get("days_to_expiry"), frames)
            scores[str(e["id"])] = journal.score_signal(con, meta.get("slug"), inst, day, "CE" if e["symbol"].endswith("CE") else "PE", feats)
        m = json.loads(con.execute("SELECT meta FROM jobs WHERE id=?", (j["id"],)).fetchone()["meta"] or "{}")
        m["scores"] = scores
        con.execute("UPDATE jobs SET meta=? WHERE id=?", (json.dumps(m), j["id"]))
        con.commit()
    except Exception as e:                                    # a score problem must never break the signals page
        scores["_error"] = str(e)[:200]
    finally:
        con.close()
    return scores


@app.get("/api/signals/<int:jid>")
def signals_view(jid):
    j = _signal_job(jid)
    if not j:
        return err("not found", 404)
    ev = _signal_events(j)
    scores = _signal_scores(j, ev)
    meta = json.loads(_signal_job(jid)["meta"] or "{}")
    acted = meta.get("acted", {})
    end = next((e for e in reversed(ev) if e["type"] in ("end", "error")), None)
    log_tail = ""
    if j["status"] != "running" and not end and j.get("log_path") and os.path.exists(j["log_path"]):
        log_tail = open(j["log_path"], encoding="utf-8", errors="replace").read()[-1500:]
    return jsonify({"id": jid, "status": j["status"], "label": j["label"],
                    "meta": {k: v for k, v in meta.items() if k not in ("acted", "events")},
                    "start": next((e for e in ev if e["type"] == "start"), {}),
                    "clock": next((e["t"] for e in reversed(ev) if e["type"] == "clock"), None),
                    "bars": [e for e in ev if e["type"] == "bar"],
                    "vix": [{"t": e["t"], "c": e["c"]} for e in ev if e["type"] == "vix"],
                    "states": [{"t": e["t"], "v": e["v"]} for e in ev if e["type"] == "state"],
                    "signals": [dict(e, acted=acted.get(str(e["id"])), score=scores.get(str(e["id"]))) for e in ev if e["type"] == "signal"],
                    "logs": [e for e in ev if e["type"] == "log"][-80:], "end": end, "log_tail": log_tail})


@app.post("/api/signals/<int:jid>/stop")
def signals_stop(jid):
    if not _signal_job(jid):
        return err("not found", 404)
    return jsonify({"stopped": jobs.cancel(ws(), jid)})


@app.post("/api/signals/<int:jid>/act")
def signals_act(jid):
    """Send one LIVE signal to OpenAlgo as a market order. Paper (Analyzer) mode only, unless live orders are allowed."""
    from script_openalgo.backtest_core import data_loader as dl
    j = _signal_job(jid)
    if not j:
        return err("not found", 404)
    meta = json.loads(j["meta"] or "{}")
    if meta.get("mode") != "live":
        return err("replay signals are historical; only live signals can be acted on", 409)
    try:
        sig_id = int((request.json or {}).get("signal_id"))
    except (TypeError, ValueError):
        return err("signal_id required")
    sig = next((e for e in _signal_events(j) if e["type"] == "signal" and e["id"] == sig_id), None)
    if not sig:
        return err("signal not found", 404)
    if str(sig_id) in meta.get("acted", {}):
        return err("this signal was already sent", 409)
    mode = _openalgo_mode()
    if not mode.get("ok"):
        return err(f"cannot read OpenAlgo's mode: {mode.get('message')}", 502)
    con = conn()
    allow = db.get_setting(con, "allow_live_orders", "0") == "1"
    con.close()
    if not mode["analyze_mode"] and not allow:
        return err("OpenAlgo is in LIVE mode (Analyzer off). Switch Analyzer on in OpenAlgo, or allow live orders on this page first.", 409)
    code, b = dl._post("/api/v1/placeorder", strategy=f"StrategyLab-{meta.get('slug', '')}"[:40], symbol=sig["symbol"],
                       action=sig["action"], exchange=sig.get("exchange") or "NFO", pricetype="MARKET",
                       product=sig.get("product") or "MIS", quantity=sig["qty"])
    if not (code == 200 and b.get("status") == "success"):
        return err(f"order rejected by OpenAlgo: {b.get('message') or code}", 502)
    rec = {"at": db.now(), "orderid": b.get("orderid"), "paper": mode["analyze_mode"]}
    con = conn()
    m = json.loads(con.execute("SELECT meta FROM jobs WHERE id=?", (jid,)).fetchone()["meta"] or "{}")
    m.setdefault("acted", {})[str(sig_id)] = rec
    con.execute("UPDATE jobs SET meta=? WHERE id=?", (json.dumps(m), jid))
    con.commit()
    con.close()
    return jsonify(rec)


# ---------------- Live desk: the chosen strategies on all selected instruments, one feed ----------------
MAX_DESK_INSTRUMENTS = 4
_mode_cache = {"at": 0.0, "val": None}


def _cached_mode():
    import time as _t
    if _t.time() - _mode_cache["at"] > 15:
        _mode_cache.update(at=_t.time(), val=_openalgo_mode())
    return _mode_cache["val"]


def _desk(con):
    return json.loads(db.get_setting(con, "live_desk", "{}") or "{}")


def _market_state(now=None):
    from datetime import datetime as _dt
    now = now or _dt.now()
    if now.weekday() >= 5:
        return "closed (weekend)"
    t = now.strftime("%H:%M")
    return "pre-open" if t < "09:15" else "open" if t < "15:30" else "closed"


@app.post("/api/live/start")
def live_start():
    """Body: {picks: [{instrument, strategy_id, params?}], mode: live|replay, date, time, speed, min_score}."""
    import uuid
    b = request.json or {}
    picks = b.get("picks") or []
    by = {}
    for p in picks:
        inst = str(p.get("instrument") or "").upper()
        try:
            sid = int(p.get("strategy_id"))
        except (TypeError, ValueError):
            return err("bad strategy in the picks")
        by.setdefault(inst, []).append((sid, p.get("params") or ""))
    if not by:
        return err("choose at least one strategy on one instrument")
    if len(by) > MAX_DESK_INSTRUMENTS:
        return err(f"at most {MAX_DESK_INSTRUMENTS} instruments on the Live desk")
    if any(len(v) > MAX_GROUP for v in by.values()):
        return err(f"at most {MAX_GROUP} strategies per instrument")
    con = conn()
    old = _desk(con)
    con.close()
    if old.get("jobs") and any((_signal_job(j) or {}).get("status") == "running" for j in old["jobs"]):
        return err("the Live desk is already running; stop it first", 409)
    try:
        commons = {inst: _signal_common({**b, "instrument": inst}) for inst in by}
        chosen = {inst: [_signal_strategy(sid, prm) for sid, prm in v] for inst, v in by.items()}
        need = sum(len(v) for v in chosen.values())
        if _running_signals() + need > MAX_SIGNAL_SESSIONS:
            raise SignalStartError(f"the desk needs {need} sessions; {_running_signals()} of {MAX_SIGNAL_SESSIONS} are in use", 409)
    except SignalStartError as e:
        return err(str(e), e.code)
    desk = uuid.uuid4().hex[:8]
    groups, jids = {}, []
    for inst, lst in chosen.items():
        groups[inst] = uuid.uuid4().hex[:8]
        for s, params in lst:
            jids.append(_launch_signal(s, params, commons[inst], groups[inst], desk))
    state = {"id": desk, "started": db.now(), "mode": "replay" if b.get("mode") == "replay" else "live", "date": b.get("date"),
             "time": b.get("time"), "speed": b.get("speed"), "instruments": list(chosen), "groups": groups, "jobs": jids,
             "min_score": int(b.get("min_score") or 0)}
    con = conn()
    db.set_setting(con, "live_desk", json.dumps(state))
    con.close()
    return jsonify(state)


@app.post("/api/live/stop")
def live_stop():
    con = conn()
    d = _desk(con)
    con.close()
    n = sum(bool(jobs.cancel(ws(), j)) for j in d.get("jobs", []) if (_signal_job(j) or {}).get("status") == "running")
    return jsonify({"stopped": n})


def _pnl(sigs):
    open_, pnl = {}, 0.0
    for s in sigs:
        if s.get("price") is None:
            continue
        q = open_.setdefault(s["symbol"], [])
        if s["action"] == "BUY":
            q.append([s["qty"], s["price"], s])
        else:
            left = s["qty"]
            while left > 0 and q:
                t = min(left, q[0][0])
                pnl += (s["price"] - q[0][1]) * t
                q[0][0] -= t
                left -= t
                if not q[0][0]:
                    q.pop(0)
    return round(pnl, 1), [{"symbol": k, "qty": sum(x[0] for x in v), "entry": v[0][1], "entry_id": v[0][2]["id"]} for k, v in open_.items() if v]


@app.get("/api/live/feed")
def live_feed():
    """Everything the Live page shows, for all instruments of the desk: signals (with journal scores), per-instrument
    status (last candle, VIX, sessions), market state and OpenAlgo mode."""
    con = conn()
    d = _desk(con)
    con.close()
    out = {"desk": d, "market": _market_state(), "now": db.now(), "openalgo": _cached_mode(), "instruments": {}, "signals": []}
    if not d.get("jobs"):
        return jsonify(out)
    for jid in d["jobs"]:
        j = _signal_job(jid)
        if not j:
            continue
        ev = _signal_events(j)
        scores = _signal_scores(j, ev)
        meta = json.loads(_signal_job(jid)["meta"] or "{}")
        inst = meta.get("instrument")
        acted = meta.get("acted", {})
        sigs = [dict(e, job_id=jid, slug=meta.get("slug"), name=j.get("name"), instrument=inst, acted=acted.get(str(e["id"])),
                     score=scores.get(str(e["id"]))) for e in ev if e["type"] == "signal"]
        pnl, opn = _pnl(sigs)
        bars = [e for e in ev if e["type"] == "bar"]
        vix = [e for e in ev if e["type"] == "vix"]
        end = next((e for e in reversed(ev) if e["type"] in ("end", "error")), None)
        row = out["instruments"].setdefault(inst, {"group": d["groups"].get(inst), "last_bar": None, "last_close": None, "vix": None,
                                                    "clock": None, "sessions": []})
        if bars and (row["last_bar"] is None or bars[-1]["t"] > row["last_bar"]):
            row["last_bar"], row["last_close"] = bars[-1]["t"], bars[-1]["c"]
        if vix:
            row["vix"] = vix[-1]["c"]
        clock = next((e["t"] for e in reversed(ev) if e["type"] == "clock"), None)
        if clock and (row["clock"] is None or clock > row["clock"]):
            row["clock"] = clock
        row["sessions"].append({"job_id": jid, "slug": meta.get("slug"), "name": j.get("name"), "status": j["status"], "signals": len(sigs),
                                "pnl": pnl, "open": opn, "error": end.get("message") if end and end.get("type") == "error" else None,
                                "params": meta.get("params") or {}})
        out["signals"] += sigs
    out["signals"].sort(key=lambda s: (s.get("date", ""), s["t"], s["job_id"], s["id"]), reverse=True)
    return jsonify(out)


# ---------------- actions ----------------
# ---------------- trade manager (auto-trade) ----------------
def _trader_cfg(b):
    """Validated manager config from the Trade page: instruments -> strategy ids -> slugs, limits as numbers."""
    from script_openalgo import trader
    cfg = dict(trader.DEFAULTS)
    con = conn()
    insts = {}
    try:
        for inst, ic in (b.get("instruments") or {}).items():
            inst = str(inst).upper()
            if inst not in instruments.INSTRUMENTS:
                raise ValueError(f"unknown instrument {inst}")
            slugs = []
            for sid in ic.get("strategies") or []:
                r = con.execute("SELECT slug FROM strategies WHERE id=?", (int(sid),)).fetchone()
                if not r or not os.path.exists(os.path.join(db.STRATEGIES_DIR, r["slug"] + ".py")):
                    raise ValueError(f"strategy {sid} not found or its code file is missing")
                slugs.append(r["slug"])
            if slugs:
                insts[inst] = {"strategies": slugs[:MAX_GROUP], "lots": max(1, int(ic.get("lots") or 1))}
    finally:
        con.close()
    if not insts:
        raise ValueError("choose at least one strategy on one instrument")
    if len(insts) > MAX_DESK_INSTRUMENTS:
        raise ValueError(f"at most {MAX_DESK_INSTRUMENTS} instruments")
    cfg["instruments"] = insts
    for k in ("max_margin_per_trade", "max_lots", "min_score", "max_trades_per_day", "max_daily_loss", "max_consecutive_losses",
              "max_open_positions", "hard_stop_pct"):
        if b.get(k) not in (None, ""):
            v = float(b[k])
            if v < 0:
                raise ValueError(f"{k} cannot be negative")
            cfg[k] = int(v) if k != "max_margin_per_trade" else v
    if b.get("sides") in ("CE", "PE", "both"):
        cfg["sides"] = b["sides"]
    if b.get("sizing") in ("lots", "margin"):
        cfg["sizing"] = b["sizing"]
    if cfg["sizing"] == "margin" and not cfg["max_margin_per_trade"]:
        raise ValueError("sizing by margin needs max margin per trade")
    sq = str(b.get("square_off") or "")
    if re.fullmatch(r"\d\d:\d\d", sq) and "09:30" <= sq <= "15:20":
        cfg["square_off"] = sq
    if not cfg["max_daily_loss"]:
        raise ValueError("a max daily loss is required for auto-trade")
    return cfg


def _trader_active(con):
    """The running auto session (None if none); a session whose processes have all ended is closed here."""
    from script_openalgo import trader
    trader.ensure(con)
    r = con.execute("SELECT * FROM trader_sessions WHERE mode='auto' AND status='running' ORDER BY started DESC LIMIT 1").fetchone()
    if not r:
        return None
    jids = json.loads(db.get_setting(con, f"trader_jobs_{r['id']}", "[]") or "[]")
    alive = [j for j in jids if (con.execute("SELECT status FROM jobs WHERE id=?", (j,)).fetchone() or {"status": ""})["status"] == "running"]
    if not alive:
        con.execute("UPDATE trader_sessions SET status='done', ended=? WHERE id=?", (db.now(), r["id"]))
        con.commit()
        return None
    return dict(r)


@app.get("/api/trader/settings")
def trader_settings():
    from script_openalgo import trader, costs
    con = conn()
    out = {"defaults": trader.DEFAULTS, "saved": json.loads(db.get_setting(con, "trader_form", "{}") or "{}"),
           "costs": costs.settings(con), "allow_live_orders": db.get_setting(con, "allow_live_orders", "0") == "1"}
    con.close()
    return jsonify(out)


@app.post("/api/trader/costs")
def trader_costs_save():
    from script_openalgo import costs
    b = request.json or {}
    try:
        vals = {k: float(b[k]) for k in costs.DEFAULTS if k != "broker" and b.get(k) not in (None, "")}
    except (TypeError, ValueError):
        return err("cost rates must be numbers")
    con = conn()
    db.set_setting(con, "costs", json.dumps(vals))
    out = costs.settings(con)
    con.close()
    return jsonify(out)


@app.post("/api/trader/start")
def trader_start():
    """Start auto-trade for today: one process per instrument. Paper (OpenAlgo Analyzer ON) unless the user has turned on
    allow_live_orders AND types LIVE for this start."""
    import uuid
    from script_openalgo import trader
    b = request.json or {}
    try:
        cfg = _trader_cfg(b)
    except (ValueError, TypeError) as e:
        return err(str(e))
    con = conn()
    busy = _trader_active(con)
    allow = db.get_setting(con, "allow_live_orders", "0") == "1"
    con.close()
    if busy:
        return err("auto-trade is already running; stop it first", 409)
    mode = _openalgo_mode()
    if not mode.get("ok"):
        return err(f"OpenAlgo not reachable: {mode.get('message')}", 503)
    paper = bool(mode.get("analyze_mode"))
    if not paper and (not allow or b.get("confirm") != "LIVE"):
        return err("OpenAlgo is in LIVE mode (real orders). Turn on 'allow live orders' on the Signals page and type LIVE to "
                   "start, or switch OpenAlgo to Analyzer (paper).", 403)
    if _market_state() not in ("open", "pre-open"):
        return err(f"market is {_market_state()}: use Simulate a day instead")
    today = date.today().isoformat()
    sid = f"auto-{today}-{uuid.uuid4().hex[:6]}"
    con = conn()
    trader.ensure(con)
    con.execute("INSERT INTO trader_sessions VALUES(?,?,?,?,?,?,?,?)",
                (sid, today, "auto", "running", json.dumps(cfg), db.now(), None, "paper (Analyzer)" if paper else "LIVE"))
    db.set_setting(con, f"trader_stop_{sid}", "")
    con.close()
    jids = []
    for inst in cfg["instruments"]:
        cmd = [sys.executable, "-u", "-m", "script_openalgo.trader", "auto", "--session", sid, "--instrument", inst]
        jids.append(jobs._launch(ws(), "trader", None, f"auto-trade {inst} ({sid}, {'paper' if paper else 'LIVE'})", cmd,
                                 meta={"session": sid, "instrument": inst}))
    con = conn()
    db.set_setting(con, f"trader_jobs_{sid}", json.dumps(jids))
    db.set_setting(con, "trader_form", json.dumps({k: v for k, v in b.items() if k != "confirm"}))
    con.close()
    return jsonify({"session": sid, "jobs": jids, "paper": paper})


@app.post("/api/trader/stop")
def trader_stop():
    """how=stop: no new entries, open positions keep their exits (strategy, hard stop, square-off), then the manager ends.
    how=flatten: close all open positions now, then end. how=kill: end the processes at once (positions NOT closed)."""
    from script_openalgo import trader
    how = (request.json or {}).get("how", "stop")
    if how not in ("stop", "flatten", "kill"):
        return err("how must be stop, flatten or kill")
    con = conn()
    s = _trader_active(con)
    if not s:
        con.close()
        return err("auto-trade is not running", 404)
    trader.event(con, s["id"], "info", f"user: {how}")
    killed = 0
    if how == "kill":
        jids = json.loads(db.get_setting(con, f"trader_jobs_{s['id']}", "[]") or "[]")
        killed = sum(bool(jobs.cancel(ws(), j)) for j in jids)
        con.execute("UPDATE trader_sessions SET status='killed', ended=? WHERE id=?", (db.now(), s["id"]))
        con.commit()
    else:
        db.set_setting(con, f"trader_stop_{s['id']}", how)
    con.close()
    return jsonify({"session": s["id"], "how": how, "killed": killed})


@app.post("/api/trader/simulate")
def trader_simulate():
    """What the manager would have done on a cached day (a job; the result is a 'sim' session in the ledger)."""
    import uuid
    b = request.json or {}
    try:
        cfg = _trader_cfg(b)
        day = date.fromisoformat(b.get("date") or "").isoformat()
    except (ValueError, TypeError) as e:
        return err(str(e) or "bad date")
    sid = f"sim-{day}-{uuid.uuid4().hex[:6]}"
    path = os.path.join(jobs.LOG_DIR, ws(), f"trader_{sid}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    cmd = [sys.executable, "-u", "-m", "script_openalgo.trader", "simulate", "--day", day, "--config", path, "--session", sid]
    jid = jobs._launch(ws(), "tradersim", None, f"trade manager simulation {day}", cmd, meta={"session": sid, "date": day})
    con = conn()
    db.set_setting(con, "trader_form", json.dumps({k: v for k, v in b.items() if k != "confirm"}))
    con.close()
    return jsonify({"session": sid, "job": jid})


@app.get("/api/trader/status")
def trader_status():
    """The running (or a chosen, or the latest) session: config, trades, events, limits used; plus the session list."""
    from script_openalgo import trader
    con = conn()
    trader.ensure(con)
    active = _trader_active(con)
    sid = request.args.get("session") or (active or {}).get("id")
    if not sid:
        r = con.execute("SELECT id FROM trader_sessions ORDER BY started DESC LIMIT 1").fetchone()
        sid = r["id"] if r else None
    out = {"active": active, "market": _market_state(), "openalgo": _cached_mode(), "session": None,
           "sessions": db.rows(con, "SELECT id, day, mode, status, started, ended, note FROM trader_sessions ORDER BY started DESC LIMIT 40")}
    s = con.execute("SELECT * FROM trader_sessions WHERE id=?", (sid,)).fetchone() if sid else None
    if s:
        s = dict(s)
        s["config"] = json.loads(s["config"] or "{}")
        s["stop_flag"] = db.get_setting(con, f"trader_stop_{sid}", "") or None
        book = trader.Book(con, sid).summary()
        s["book"] = {"entries": book["entries"], "open": len(book["open"]), "net": book["net"], "streak": book["streak"]}
        s["trades"] = db.rows(con, "SELECT * FROM trader_trades WHERE session=? ORDER BY id", (sid,))
        s["events"] = db.rows(con, "SELECT ts, instrument, strategy, kind, message FROM trader_events WHERE session=? ORDER BY id DESC LIMIT 300", (sid,))
        jids = json.loads(db.get_setting(con, f"trader_jobs_{sid}", "[]") or "[]")
        s["jobs"] = db.rows(con, f"SELECT id, label, status FROM jobs WHERE id IN ({','.join('?' * len(jids))})", jids) if jids else []
        out["session"] = s
    con.close()
    return jsonify(out)


@app.get("/api/trader/review")
def trader_review():
    from script_openalgo import trader
    con = conn()
    r = trader.review(con, request.args.get("day") or date.today().isoformat(), request.args.get("mode") or None)
    con.close()
    return jsonify(r)


@app.post("/api/trader/sessions/<sid>/delete")
def trader_session_delete(sid):
    from script_openalgo import trader
    con = conn()
    trader.ensure(con)
    s = con.execute("SELECT status FROM trader_sessions WHERE id=?", (sid,)).fetchone()
    if not s or s["status"] == "running":
        con.close()
        return err("no such session" if not s else "stop the session first", 404 if not s else 409)
    for t in ("trader_trades", "trader_events"):
        con.execute(f"DELETE FROM {t} WHERE session=?", (sid,))
    con.execute("DELETE FROM trader_sessions WHERE id=?", (sid,))
    con.commit()
    con.close()
    return jsonify({"deleted": sid})


@app.post("/api/research")
def research():
    b = request.json or {}
    mode = b.get("mode", "daily")
    count = 10 if mode == "seed" else 1
    if b.get("count"):
        count = max(1, min(20, int(b["count"])))
    try:
        jid = jobs.start_research(ws(), count, mode)
    except RuntimeError as e:
        return err(str(e), 409)
    return jsonify({"job_id": jid})


@app.post("/api/strategies/<int:sid>/run")
def run_strategy(sid):
    b = request.json or {}
    inst = (b.get("instrument") or "NIFTY").upper()
    if inst not in instruments.INSTRUMENTS:
        return err("unknown instrument")
    day = b.get("date") or None
    con = conn()
    srow = con.execute("SELECT slug FROM strategies WHERE id=?", (sid,)).fetchone()
    con.close()
    params = None
    if (b.get("params") or "").strip():
        if not srow or not os.path.exists(os.path.join(db.STRATEGIES_DIR, srow["slug"] + ".py")):
            return err("strategy code file not found; options need the code file")
        try:
            params = parse_run_params(srow["slug"], b["params"]) or None
        except ValueError as e:
            return err(f"options: {e}")
    if b.get("from") or b.get("to") or b.get("days"):          # several days at once (no Claude involved)
        try:
            jid = jobs.start_multiday(ws(), sid, inst, b.get("from") or None, b.get("to") or None,
                                      int(b["days"]) if b.get("days") else None, bool(b.get("allow_far")), params=params)
        except RuntimeError as e:
            return err(str(e), 409)
        return jsonify({"job_id": jid})
    try:
        jid = jobs.start_test(ws(), sid, day, inst, params=params)
    except RuntimeError as e:
        return err(str(e), 409)
    return jsonify({"job_id": jid, "queued": jid is None})


@app.post("/api/jobs/<int:jid>/answer")
def answer(jid):
    text = ((request.json or {}).get("text") or "").strip()
    if not text:
        return err("empty answer")
    con = conn()
    j = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    con.close()
    if not j or j["status"] != "needs_input":
        return err("job is not waiting for input", 409)
    cmd_inst = "NIFTY"
    try:
        new = jobs.start_test(ws(), j["strategy_id"], None, cmd_inst, answer=text,
                              params=json.loads(j["meta"] or "{}").get("params") or None)
    except RuntimeError as e:
        return err(str(e), 409)
    return jsonify({"job_id": new})


@app.post("/api/strategies/<int:sid>/stop")
def stop_strategy(sid):
    """Stop whatever is running for this strategy and put it back in the queue (also clears a stuck in_progress)."""
    n = 0
    con = conn()
    ids = [r["id"] for r in con.execute("SELECT id FROM jobs WHERE strategy_id=? AND status='running'", (sid,))]
    con.close()
    for jid in ids:
        n += bool(jobs.cancel(ws(), jid))
    con = conn()
    con.execute("UPDATE strategies SET status='queued', phase='', status_note=?, updated=? WHERE id=? AND status='in_progress'",
                ("stopped by user" if n else "reset (nothing was running)", db.now(), sid))
    con.commit()
    con.close()
    return jsonify({"stopped": n})


@app.post("/api/jobs/<int:jid>/cancel")
def cancel(jid):
    return jsonify({"cancelled": jobs.cancel(ws(), jid)})


@app.get("/api/jobs/<int:jid>/log")
def job_log(jid):
    con = conn()
    j = con.execute("SELECT log_path FROM jobs WHERE id=?", (jid,)).fetchone()
    con.close()
    if not j or not j["log_path"] or not os.path.exists(j["log_path"]):
        return jsonify({"log": ""})
    txt = open(j["log_path"], encoding="utf-8", errors="replace").read()
    return jsonify({"log": txt[-6000:]})


@app.post("/api/strategies/<int:sid>/archive")
def archive(sid):
    con = conn()
    db.archive(con, sid, bool((request.json or {}).get("on", True)))
    con.close()
    return jsonify({"ok": True})


@app.post("/api/strategies/<int:sid>/pin")
def pin(sid):
    con = conn()
    con.execute("UPDATE strategies SET pinned = 1 - pinned WHERE id=?", (sid,))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.post("/api/strategies/<int:sid>/requeue")
def requeue(sid):
    con = conn()
    db.set_status(con, sid, "queued", "", "re-queued by user")
    con.execute("DELETE FROM jobs WHERE strategy_id=? AND kind='test' AND status='failed'", (sid,))
    con.execute("UPDATE jobs SET meta=json_set(COALESCE(meta,'{}'), '$.held_done', 1) WHERE strategy_id=? AND status='limit'", (sid,))   # cancel a hold
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.post("/api/strategies/<int:sid>/delete")
def delete(sid):
    b = request.json or {}
    if b.get("confirm") != "DELETE":
        return err("confirmation missing")
    con = conn()
    res = db.delete_strategy(con, sid, delete_result_dirs=bool(b.get("delete_files")))
    con.close()
    return jsonify(res)


@app.post("/api/settings/claude")
def claude_settings():
    b = request.json or {}
    con = conn()
    try:
        gate.save_settings(con, {"claude_max_per_hour": b.get("max_per_hour"), "claude_max_per_day": b.get("max_per_day"),
                                 "claude_min_gap_sec": b.get("min_gap_sec"), "claude_max_cost_day": b.get("max_cost_day")})
        if "llm_review" in b:
            db.set_setting(con, "llm_review", "1" if b["llm_review"] else "0")
    except (ValueError, TypeError) as e:
        con.close()
        return err(str(e))
    con.close()
    return jsonify({"ok": True})


@app.post("/api/sync")
def sync_disk():
    """Re-read strategy files and result folders from the workspace into the database (e.g. after command-line runs)."""
    con = conn()
    try:
        out = db.sync_from_disk(con, db.ws_paths()[1])
        from script_openalgo import journal
        j = journal.update_quietly(con)                      # keep the trade journal in step with the runs
        return jsonify(dict(out, journal_trades_added=(j or {}).get("trades_added", 0)))
    finally:
        con.close()


@app.post("/api/auto")
def auto():
    con = conn()
    db.set_setting(con, "auto_run", "1" if (request.json or {}).get("on") else "0")
    con.close()
    return jsonify({"ok": True})


# ---------------- data (candle cache) ----------------
def cache_dir():
    return db.ws_paths()[1]


@app.get("/api/data")
def data_list():
    out = []
    for p in sorted(glob.glob(os.path.join(cache_dir(), "manifest_*.json")), reverse=True):
        try:
            m = json.load(open(p, encoding="utf-8"))
        except ValueError:
            continue
        files = [os.path.join(cache_dir(), f"{name}_{m['date']}.parquet") for name in m.get("series", {})]
        size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
        out.append({"instrument": m.get("instrument", "NIFTY"), "date": m["date"], "expiry": m.get("expiry"),
                    "series": len(m.get("series", {})), "complete": m.get("complete"), "issues": m.get("issues", [])[:3],
                    "warnings": m.get("warnings", [])[:3], "dte": m.get("days_to_expiry"), "nearest": m.get("nearest", True),
                    "backfilled": bool(m.get("backfilled")), "unlisted": len(m.get("unlisted", [])),
                    "size_kb": round(size / 1024)})
    return jsonify({"cache_dir": cache_dir(), "items": out})


@app.post("/api/data/fetch")
def data_fetch():
    b = request.json or {}
    inst = (b.get("instrument") or "NIFTY").upper()
    if inst != "ALL" and inst not in instruments.INSTRUMENTS:
        return err("unknown instrument")
    try:
        jid = jobs.start_fetch(ws(), inst, b.get("date") or None)
    except RuntimeError as e:
        return err(str(e), 409)
    return jsonify({"job_id": jid})


@app.post("/api/data/backfill")
def data_backfill():
    inst = ((request.json or {}).get("instrument") or "NIFTY").upper()
    if inst != "ALL" and inst not in instruments.INSTRUMENTS:
        return err("unknown instrument")
    try:
        jid = jobs.start_backfill(ws(), inst)
    except RuntimeError as e:
        return err(str(e), 409)
    return jsonify({"job_id": jid})


@app.post("/api/data/delete")
def data_delete():
    b = request.json or {}
    inst, day = (b.get("instrument") or "").upper(), b.get("date") or ""
    if inst not in instruments.INSTRUMENTS or not (len(day) == 10 and day[4] == "-" and day[7] == "-"):
        return err("bad instrument/date")
    mp = os.path.join(cache_dir(), f"manifest_{inst}_{day}.json")
    if not os.path.exists(mp) and inst == "NIFTY":                      # legacy name from before BANKNIFTY support
        mp = os.path.join(cache_dir(), f"manifest_{day}.json")
    if not os.path.exists(mp):
        return err("no such cached day", 404)
    m = json.load(open(mp, encoding="utf-8"))
    removed = 0
    for name in m.get("series", {}):
        f = os.path.join(cache_dir(), f"{name}_{day}.parquet")
        if os.path.exists(f) and os.path.dirname(os.path.abspath(f)) == os.path.abspath(cache_dir()):
            os.remove(f)
            removed += 1
    os.remove(mp)
    return jsonify({"files_removed": removed})


# ---------------- library: tags, rules, rename, copy, code ----------------
def _res(result):
    return (jsonify(result), 400) if "error" in result else jsonify(result)


@app.get("/api/tags")
def tags_all():
    con = conn()
    out = library.all_tags(con)
    con.close()
    return jsonify(out)


@app.post("/api/strategies/<int:sid>/tags")
def strategy_tags(sid):
    con = conn()
    tags = library.set_tags(con, sid, (request.json or {}).get("tags", []))
    con.close()
    return jsonify({"tags": tags})


@app.post("/api/strategies/<int:sid>/spec")
def strategy_spec(sid):
    con = conn()
    res = library.update_spec(con, sid, (request.json or {}).get("fields", {}))
    con.close()
    return _res(res)


@app.post("/api/strategies/<int:sid>/rename")
def strategy_rename(sid):
    con = conn()
    res = library.rename(con, sid, (request.json or {}).get("name"))
    con.close()
    return _res(res)


@app.post("/api/strategies/<int:sid>/copy")
def strategy_copy(sid):
    con = conn()
    res = library.copy_strategy(con, sid, (request.json or {}).get("name"))
    con.close()
    return _res(res)


@app.get("/api/strategies/<int:sid>/code")
def code_get(sid):
    con = conn()
    res = library.read_code(con, sid)
    con.close()
    return _res(res)


@app.post("/api/strategies/<int:sid>/code")
def code_save(sid):
    b = request.json or {}
    if b.get("kind") not in ("strategy", "backtest") or not isinstance(b.get("text"), str):
        return err("kind and text required")
    con = conn()
    row = con.execute("SELECT status FROM strategies WHERE id=?", (sid,)).fetchone()
    if not row:
        con.close()
        return err("not found", 404)
    if row["status"] == "in_progress":
        con.close()
        return err("a test is running for this strategy; wait or stop it before editing", 409)
    res = library.save_code(con, sid, b["kind"], b["text"])
    con.close()
    return _res(res)


@app.get("/api/strategies/<int:sid>/code/version")
def code_version(sid):
    con = conn()
    res = library.read_version(con, sid, request.args.get("name", ""))
    con.close()
    return _res(res)


@app.post("/api/strategies/<int:sid>/code/restore")
def code_restore(sid):
    con = conn()
    res = library.restore_version(con, sid, (request.json or {}).get("name", ""))
    con.close()
    return _res(res)


# ---------------- backup: export / import everything ----------------
def _backup_name_ok(name):
    return bool(name) and os.path.basename(name) == name and name.endswith(".zip") and not name.startswith(".")


def _busy_guard():
    """Refuse to import while any job is running (the files and databases would change under it)."""
    for wsn in db.workspaces()["items"]:
        c = db.connect(wsn)
        try:
            if jobs.running(c):
                raise backup_mod.ImportError_(f"jobs are running in workspace '{wsn}'; wait for them or stop them first")
        finally:
            c.close()


@app.get("/api/backup/preview")
def backup_preview():
    return jsonify(backup_mod.preview())


@app.get("/api/backup/list")
def backup_list():
    d = config.backups_dir()
    items = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d), reverse=True):
            if _backup_name_ok(f):
                st = os.stat(os.path.join(d, f))
                items.append({"name": f, "bytes": st.st_size, "created": st.st_mtime})
    return jsonify({"folder": d, "items": items})


@app.post("/api/backup/export")
def backup_export():
    try:
        r = backup_mod.export_workspace()
    except Exception as e:                                          # report, never crash the page
        return err(f"export failed: {e}", 500)
    return jsonify({"name": os.path.basename(r["path"]), "files": r["files"], "bytes": r["bytes"], "zip_bytes": r["zip_bytes"],
                    "notes": r["notes"], "counts": r["workspace_counts"]})


@app.get("/api/backup/download/<name>")
def backup_download(name):
    if not _backup_name_ok(name) or not os.path.exists(os.path.join(config.backups_dir(), name)):
        abort(404)
    return send_from_directory(config.backups_dir(), name, as_attachment=True)


@app.post("/api/backup/delete")
def backup_delete():
    name = (request.json or {}).get("name", "")
    p = os.path.join(config.backups_dir(), name)
    if not _backup_name_ok(name) or not os.path.exists(p):
        return err("no such backup", 404)
    os.remove(p)
    return jsonify({"deleted": name})


@app.post("/api/backup/import")
def backup_import():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".zip"):
        return err("choose a .zip backup file")
    os.makedirs(config.backups_dir(), exist_ok=True)
    tmp = os.path.join(config.backups_dir(), f".incoming_{os.getpid()}.zip")
    f.save(tmp)
    try:
        rep = backup_mod.import_workspace(tmp, overwrite=request.form.get("overwrite") == "1",
                                          dry_run=request.form.get("dry_run") == "1", guard=_busy_guard)
    except backup_mod.ImportError_ as e:
        return err(str(e), 400)
    except Exception as e:
        return err(f"import failed: {e}", 500)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return jsonify(rep)


# ---------------- workspaces ----------------
@app.post("/api/workspaces")
def ws_add():
    b = request.json or {}
    try:
        db.add_workspace((b.get("name") or "").strip(), (b.get("db") or None), (b.get("cache") or None))
        if b.get("activate"):
            db.set_active(b["name"].strip())
    except ValueError as e:
        return err(str(e))
    return jsonify({"ok": True})


@app.post("/api/workspaces/activate")
def ws_activate():
    try:
        db.set_active((request.json or {}).get("name"))
    except ValueError as e:
        return err(str(e))
    jobs.recover(ws())
    return jsonify({"ok": True})


def main():
    con = conn()
    db.seed_existing(con)
    sy = db.sync_from_disk(con)
    if sy["registered"] or sy["runs_imported"]:
        print(f"Synced from disk: {sy['registered']} strategies registered, {sy['runs_imported']} results imported")
    con.close()
    jobs.recover(ws())
    jobs.AutoRunner(ws).start()
    print(f"Strategy Lab on http://127.0.0.1:{PORT}  (workspace: {ws()})")
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
