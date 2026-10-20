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


# ---------------- signals: run a strategy live or as a replay; its orders become signals; "act" sends one ----------------
MAX_SIGNAL_SESSIONS = 3


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
                        "LEFT JOIN strategies s ON s.id=j.strategy_id WHERE j.kind='signal' ORDER BY j.id DESC LIMIT 20")
    con.close()
    for r in rows:
        m = json.loads(r.pop("meta") or "{}")
        r.update({k: m.get(k) for k in ("mode", "instrument", "date", "start", "speed", "slug")})
        r["acted"] = len(m.get("acted", {}))
    return jsonify({"items": rows})


@app.post("/api/signals/start")
def signals_start():
    from datetime import date as _date
    from script_openalgo.backtest_core import data_loader as dl
    b = request.json or {}
    try:
        sid = int(b.get("strategy_id"))
    except (TypeError, ValueError):
        return err("choose a strategy")
    inst = (b.get("instrument") or "NIFTY").upper()
    if inst not in instruments.INSTRUMENTS:
        return err("unknown instrument")
    mode = "replay" if b.get("mode") == "replay" else "live"
    con = conn()
    s = con.execute("SELECT id, slug, name FROM strategies WHERE id=?", (sid,)).fetchone()
    n_run = con.execute("SELECT COUNT(*) FROM jobs WHERE kind='signal' AND status='running'").fetchone()[0]
    con.close()
    if not s or not os.path.exists(os.path.join(db.STRATEGIES_DIR, s["slug"] + ".py")):
        return err("strategy not found or its code file is missing")
    if n_run >= MAX_SIGNAL_SESSIONS:
        return err(f"{n_run} signal sessions are running; stop one first", 409)
    events = os.path.join(jobs.LOG_DIR, ws(), "signals_{JOB_ID}.jsonl")
    cmd = [sys.executable, "-u", "-m", "script_openalgo.signals", "--strategy", s["slug"], "--instrument", inst, "--events", events]
    meta = {"mode": mode, "instrument": inst, "slug": s["slug"]}
    if mode == "replay":
        day, start = (b.get("date") or "").strip(), (b.get("time") or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return err("choose the replay date")
        if start and not re.fullmatch(r"\d{2}:\d{2}", start):
            return err("time must be HH:MM")
        try:
            speed = float(b.get("speed") or 30)
        except ValueError:
            return err("bad speed")
        mf = dl.load_manifest(_date.fromisoformat(day), inst)
        if not (mf and mf.get("complete")):
            return err(f"no complete cached data for {inst} on {day}: fetch or backfill it in the Data tab first")
        cmd += ["--date", day, "--speed", str(speed)] + (["--from", start] if start else [])
        meta.update({"date": day, "start": start, "speed": speed})
    elif not jobs.key_present():
        return err("OPENALGO_API_KEY not found (environment or .env)")
    label = f"signals {s['slug']} {inst} " + (f"replay {meta['date']} from {meta['start'] or 'open'} x{meta['speed']:g}"
                                              if mode == "replay" else "LIVE")
    jid = jobs._launch(ws(), "signal", sid, label, cmd, meta=meta)
    con = conn()                                                # store the real events path now that the job id is known
    con.execute("UPDATE jobs SET meta=? WHERE id=?", (json.dumps(dict(meta, events=events.replace("{JOB_ID}", str(jid)))), jid))
    con.commit()
    con.close()
    return jsonify({"job_id": jid})


@app.get("/api/signals/<int:jid>")
def signals_view(jid):
    j = _signal_job(jid)
    if not j:
        return err("not found", 404)
    ev = _signal_events(j)
    meta = json.loads(j["meta"] or "{}")
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
                    "signals": [dict(e, acted=acted.get(str(e["id"]))) for e in ev if e["type"] == "signal"],
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


# ---------------- actions ----------------
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
    if b.get("from") or b.get("to") or b.get("days"):          # several days at once (no Claude involved)
        try:
            jid = jobs.start_multiday(ws(), sid, inst, b.get("from") or None, b.get("to") or None,
                                      int(b["days"]) if b.get("days") else None, bool(b.get("allow_far")))
        except RuntimeError as e:
            return err(str(e), 409)
        return jsonify({"job_id": jid})
    try:
        jid = jobs.start_test(ws(), sid, day, inst)
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
        new = jobs.start_test(ws(), j["strategy_id"], None, cmd_inst, answer=text)
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
        return jsonify(db.sync_from_disk(con, db.ws_paths()[1]))
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
