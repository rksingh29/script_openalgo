"""
Strategy database (SQLite) with uniqueness rules, queue, runs, jobs and workspaces.

A *workspace* = one SQLite file + one candle-cache directory. Several can exist (e.g. a new one when the
data grows); the active one is stored in strategy_db/workspaces.json. Processes started by the web app get
STRATEGY_DB_PATH / BACKTEST_CACHE_DIR in their environment so they use the same workspace.
"""
import difflib
import glob as glob_mod
import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
from datetime import datetime

from script_openalgo import config  # noqa: E402

ROOT = config.home()                                      # the workspace folder
DB_DIR = config.strategy_db_dir()
WS_FILE = os.path.join(DB_DIR, "workspaces.json")
RESULTS_DIR = config.results_dir()
STRATEGIES_DIR = config.strategies_dir()                  # <slug>.py and backtest_<slug>.py live here
SPECS_DIR = os.path.join(DB_DIR, "specs")

STATUSES = ["queued", "in_progress", "tested", "failed", "untestable", "needs_review"]
TESTABLE_WEIGHT = {"yes": 1.0, "adapt": 0.5, "no": 0.0}
SIMILARITY = 0.86
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS strategies(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL, aliases TEXT DEFAULT '[]',
  family TEXT, spec TEXT DEFAULT '{}', signature TEXT UNIQUE NOT NULL,
  sources TEXT DEFAULT '[]', popularity INTEGER DEFAULT 1, data_needs TEXT DEFAULT '',
  testable TEXT DEFAULT 'yes', testable_reason TEXT DEFAULT '',
  status TEXT DEFAULT 'queued', phase TEXT DEFAULT '', status_note TEXT DEFAULT '',
  pinned INTEGER DEFAULT 0, origin TEXT DEFAULT 'researcher', file TEXT,
  created TEXT, updated TEXT, archived_at TEXT,
  tags TEXT DEFAULT '[]', parent_id INTEGER, code_edited_at TEXT);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, strategy_id INTEGER NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
  instrument TEXT, day TEXT, executor TEXT DEFAULT 'option', trades INTEGER, gross_pnl REAL, win_rate REAL,
  max_dd REAL, verdict TEXT, result_dir TEXT, job_id INTEGER, created TEXT,
  code_hash TEXT, dte INTEGER, nearest INTEGER DEFAULT 1, params TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, strategy_id INTEGER, label TEXT, status TEXT,
  phase TEXT DEFAULT '', pid INTEGER, log_path TEXT, cmd TEXT, started TEXT, ended TEXT,
  exit_code INTEGER, note TEXT DEFAULT '', questions TEXT DEFAULT '[]', meta TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS tombstones(signature TEXT PRIMARY KEY, slug TEXT, name TEXT, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS research_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job_id INTEGER, action TEXT, strategy_id INTEGER, detail TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS ignored_results(path TEXT PRIMARY KEY, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS claude_calls(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, agent TEXT, strategy TEXT, job_id INTEGER,
  status TEXT, weight INTEGER DEFAULT 1, duration_ms INTEGER, cost_usd REAL, input_tokens INTEGER, output_tokens INTEGER,
  cache_read INTEGER, cache_creation INTEGER, limit_hit INTEGER DEFAULT 0, note TEXT);
CREATE INDEX IF NOT EXISTS calls_ts ON claude_calls(ts);
CREATE INDEX IF NOT EXISTS runs_strategy ON runs(strategy_id);
CREATE INDEX IF NOT EXISTS runs_day ON runs(day);
"""


def now():
    return datetime.now().isoformat(timespec="seconds")


# ---------------- workspaces ----------------
def _ws_load():
    if os.path.exists(WS_FILE):
        with open(WS_FILE, encoding="utf-8") as f:
            return json.load(f)
    ws = {"active": "default", "items": {"default": {"db": "strategy_db/default.db", "cache": "data_cache"}}}
    _ws_save(ws)
    return ws


def _ws_save(ws):
    os.makedirs(DB_DIR, exist_ok=True)
    with open(WS_FILE, "w", encoding="utf-8") as f:
        json.dump(ws, f, indent=1)


def workspaces():
    return _ws_load()


def _abs(p):
    return p if os.path.isabs(p) else os.path.join(ROOT, p)


def ws_paths(name=None):
    ws = _ws_load()
    name = name or ws["active"]
    it = ws["items"][name]
    return _abs(it["db"]), _abs(it["cache"])


def add_workspace(name, db=None, cache=None):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name or ""):
        raise ValueError("workspace name: letters, digits, _ and - only (max 40)")
    ws = _ws_load()
    if name in ws["items"]:
        raise ValueError("workspace already exists")
    ws["items"][name] = {"db": db or f"strategy_db/{name}.db", "cache": cache or f"data_cache_{name}"}
    _ws_save(ws)
    os.makedirs(_abs(ws["items"][name]["cache"]), exist_ok=True)
    connect(name).close()
    return ws


def set_active(name):
    ws = _ws_load()
    if name not in ws["items"]:
        raise ValueError("unknown workspace")
    ws["active"] = name
    _ws_save(ws)


def connect(workspace=None):
    """Connection to the workspace DB. STRATEGY_DB_PATH (set for child processes) wins."""
    path = os.environ.get("STRATEGY_DB_PATH") if workspace is None else None
    path = path or ws_paths(workspace)[0]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def normalize_tags(tags):
    out = []
    for t in tags or []:
        t = re.sub(r"[^a-z0-9_-]+", "-", str(t).lower().strip()).strip("-")[:30]
        if t and t not in out:
            out.append(t)
    return out[:20]


def _migrate(con):
    """Add columns introduced after the first release; give old rows a default tag from their family."""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(strategies)").fetchall()}
    for col, ddl in (("tags", "TEXT DEFAULT '[]'"), ("parent_id", "INTEGER"), ("code_edited_at", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE strategies ADD COLUMN {col} {ddl}")
    jcols = {r["name"] for r in con.execute("PRAGMA table_info(jobs)").fetchall()}
    if "meta" not in jcols:
        con.execute("ALTER TABLE jobs ADD COLUMN meta TEXT DEFAULT '{}'")
    rcols = {r["name"] for r in con.execute("PRAGMA table_info(runs)").fetchall()}
    for col, ddl in (("code_hash", "TEXT"), ("dte", "INTEGER"), ("nearest", "INTEGER DEFAULT 1"), ("params", "TEXT")):
        if col not in rcols:
            con.execute(f"ALTER TABLE runs ADD COLUMN {col} {ddl}")
    for r in con.execute("SELECT id, family, origin FROM strategies WHERE tags IS NULL OR tags='[]'").fetchall():
        con.execute("UPDATE strategies SET tags=? WHERE id=?",
                    (json.dumps(normalize_tags([r["family"], r["origin"]])), r["id"]))
    con.commit()


def rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql, args).fetchall()]


# ---------------- uniqueness ----------------
def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).strip()


def slugify(s):
    s = re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")
    if not s or s[0].isdigit():
        s = "s_" + s
    s = s[:50]
    if s in sys.stdlib_module_names or s in ("backtest_core", "strategy_db", "webapp", "strategies"):
        s += "_strat"                      # must not shadow a standard-library or project module
    return s


def make_signature(spec):
    """Hash of the normalised idea: family | sorted indicator names | entry trigger | exit type | timeframe."""
    parts = [norm(spec.get("family")),
             ",".join(sorted(norm(i) for i in spec.get("indicators", []))),
             norm(spec.get("entry_trigger")), norm(spec.get("exit_type")), norm(spec.get("timeframe"))]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


REQUIRED = ["name", "family", "timeframe", "indicators", "entry_trigger", "exit_type", "entry_long", "entry_short"]


def validate(spec):
    missing = [k for k in REQUIRED if not spec.get(k)]
    if missing:
        return f"missing fields: {', '.join(missing)}"
    if not isinstance(spec.get("indicators"), list):
        return "indicators must be a list of names"
    if spec.get("testable", "yes") not in TESTABLE_WEIGHT:
        return "testable must be yes, adapt or no"
    return None


def add_strategy(con, spec, job_id=None, force_new=False):
    """Insert a researched strategy or merge it into an existing one. Returns a result dict."""
    err = validate(spec)
    if err:
        return {"result": "rejected", "reason": err}
    sig = make_signature(spec)
    name = spec["name"].strip()
    slug = slugify(name)
    with _lock:
        t = con.execute("SELECT * FROM tombstones WHERE signature=? OR slug=?", (sig, slug)).fetchone()
        if t:
            _log(con, job_id, "skipped_deleted", None, f"{name}: previously deleted")
            return {"result": "tombstoned", "reason": "this strategy was deleted earlier; not re-adding"}
        existing = con.execute("SELECT * FROM strategies WHERE signature=?", (sig,)).fetchone()
        if not existing:
            for r in con.execute("SELECT * FROM strategies").fetchall():
                names = [norm(r["name"])] + [norm(a) for a in json.loads(r["aliases"] or "[]")]
                if norm(name) in names or slug == r["slug"]:
                    existing = r
                    break
        if existing:
            _merge(con, existing, spec)
            _log(con, job_id, "merged", existing["id"], f"{name} -> {existing['name']}")
            return {"result": "merged", "id": existing["id"], "slug": existing["slug"]}
        if not force_new:
            similar = []
            for r in con.execute("SELECT * FROM strategies WHERE family=?", (norm(spec["family"]),)).fetchall():
                names = [r["name"]] + json.loads(r["aliases"] or "[]")
                ratio = max(difflib.SequenceMatcher(None, norm(name), norm(n)).ratio() for n in names)
                if ratio >= SIMILARITY:
                    similar.append({"id": r["id"], "name": r["name"], "similarity": round(ratio, 2)})
            if similar:
                return {"result": "possible_duplicate", "candidates": similar,
                        "hint": "If it is the same idea, call add with the existing id via `merge`; "
                                "if it really differs, re-run add with --force-new"}
        base, n = slug, 2
        while con.execute("SELECT 1 FROM strategies WHERE slug=?", (slug,)).fetchone() or \
                os.path.exists(os.path.join(STRATEGIES_DIR, slug + ".py")):
            slug = f"{base}_{n}"
            n += 1
        testable = spec.get("testable", "yes")
        status = "untestable" if testable == "no" else "queued"
        cur = con.execute(
            "INSERT INTO strategies(slug,name,aliases,family,spec,signature,sources,popularity,data_needs,testable,"
            "testable_reason,status,origin,created,updated,tags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (slug, name, json.dumps(spec.get("aliases", [])), norm(spec["family"]), json.dumps(spec), sig,
             json.dumps(spec.get("sources", [])), int(spec.get("popularity", 1) or 1), spec.get("data_needs", ""),
             testable, spec.get("testable_reason", ""), status, "researcher", now(), now(),
             json.dumps(normalize_tags(list(spec.get("tags", [])) + [spec["family"]]))))
        con.commit()
        _log(con, job_id, "added", cur.lastrowid, f"{name} ({status})")
        return {"result": "added", "id": cur.lastrowid, "slug": slug, "status": status}


def merge_into(con, strategy_id, spec, job_id=None):
    r = con.execute("SELECT * FROM strategies WHERE id=?", (strategy_id,)).fetchone()
    if not r:
        return {"result": "rejected", "reason": "unknown id"}
    _merge(con, r, spec)
    _log(con, job_id, "merged", strategy_id, f"{spec.get('name')} -> {r['name']}")
    return {"result": "merged", "id": strategy_id, "slug": r["slug"]}


def _merge(con, r, spec):
    aliases = json.loads(r["aliases"] or "[]")
    for a in [spec.get("name")] + spec.get("aliases", []):
        if a and norm(a) != norm(r["name"]) and norm(a) not in [norm(x) for x in aliases]:
            aliases.append(a)
    sources = json.loads(r["sources"] or "[]")
    urls = {s.get("url") for s in sources}
    for s in spec.get("sources", []):
        if s.get("url") not in urls:
            sources.append(s)
    pop = min(5, max(r["popularity"], len(sources)))
    con.execute("UPDATE strategies SET aliases=?, sources=?, popularity=?, updated=? WHERE id=?",
                (json.dumps(aliases), json.dumps(sources), pop, now(), r["id"]))
    con.commit()


def _log(con, job_id, action, sid, detail):
    con.execute("INSERT INTO research_log(ts,job_id,action,strategy_id,detail) VALUES(?,?,?,?,?)",
                (now(), job_id, action, sid, detail))
    con.commit()


def log(con, job_id, action, sid, detail):
    _log(con, job_id, action, sid, detail)


# ---------------- queue ----------------
def queue(con):
    """Queued strategies in test order: pinned first, then popularity x testability, then oldest."""
    rs = rows(con, "SELECT * FROM strategies WHERE status='queued' AND archived_at IS NULL")
    for r in rs:
        r["priority"] = r["popularity"] * TESTABLE_WEIGHT.get(r["testable"], 0.5)
    rs.sort(key=lambda r: (-r["pinned"], -r["priority"], r["id"]))
    return rs


def next_queued(con):
    q = queue(con)
    return q[0] if q else None


def set_status(con, sid, status, phase="", note=""):
    con.execute("UPDATE strategies SET status=?, phase=?, status_note=?, updated=? WHERE id=?",
                (status, phase, note, now(), sid))
    con.commit()


# ---------------- runs ----------------
def add_run(con, sid, instrument, day, summary, verdict, result_dir, job_id=None, executor="option",
            code_hash=None, dte=None, nearest=1, params=None):
    """`params`: strategy options chosen for this run (None/{} = the strategy's defaults). Runs with options are kept
    out of the strategy's headline stats so variants do not mix with the default results."""
    params = params if params is not None else summary.get("params")
    con.execute("INSERT INTO runs(strategy_id,instrument,day,executor,trades,gross_pnl,win_rate,max_dd,verdict,"
                "result_dir,job_id,created,code_hash,dte,nearest,params) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, instrument, day, executor, summary.get("trades"), summary.get("gross_pnl"),
                 summary.get("win_rate"), summary.get("max_drawdown"), verdict, result_dir, job_id, now(),
                 code_hash, dte, 1 if nearest else 0, json.dumps(params, sort_keys=True) if params else None))
    con.commit()


def strategy_stats(con):
    """Per strategy and instrument: days tested, total/avg P&L, win-day rate, worst day.
    Only the LATEST run per (strategy, instrument, day, executor) counts, so re-running a day replaces it. Runs made with
    options chosen in the web app (runs.params) are variants: they are listed but never counted here."""
    out = {}
    for r in rows(con, "SELECT r.strategy_id, r.instrument, r.day, SUM(r.gross_pnl) pnl FROM runs r "
                       "JOIN strategies s ON s.id=r.strategy_id "
                       "WHERE r.id IN (SELECT MAX(id) FROM runs WHERE COALESCE(params, '') IN ('', '{}') GROUP BY strategy_id, instrument, day, executor) "
                       "AND r.created >= COALESCE(s.code_edited_at, '') AND COALESCE(r.nearest, 1) = 1 "
                       "GROUP BY r.strategy_id, r.instrument, r.day"):
        s = out.setdefault(r["strategy_id"], {}).setdefault(
            r["instrument"], {"days": 0, "total": 0.0, "wins": 0, "worst": None})
        s["days"] += 1
        s["total"] += r["pnl"] or 0
        s["wins"] += 1 if (r["pnl"] or 0) > 0 else 0
        s["worst"] = r["pnl"] if s["worst"] is None else min(s["worst"], r["pnl"] or 0)
    for r in rows(con, "SELECT r.strategy_id, r.instrument, r.day, SUM(r.gross_pnl) pnl FROM runs r "
                       "JOIN strategies s ON s.id=r.strategy_id "
                       "WHERE r.id IN (SELECT MAX(id) FROM runs WHERE COALESCE(params, '') IN ('', '{}') GROUP BY strategy_id, instrument, day, executor) "
                       "AND r.created >= COALESCE(s.code_edited_at, '') AND r.nearest = 0 "
                       "GROUP BY r.strategy_id, r.instrument, r.day"):
        s = out.setdefault(r["strategy_id"], {}).setdefault(
            r["instrument"], {"days": 0, "total": 0.0, "wins": 0, "worst": None})
        s["far_days"] = s.get("far_days", 0) + 1
        s["far_total"] = round(s.get("far_total", 0) + (r["pnl"] or 0), 2)
    for per in out.values():
        for s in per.values():
            if s["days"] == 0:
                s.update({"avg": None, "win_day_rate": None, "low_sample": True, "total": 0.0})
                continue
            s["avg"] = round(s["total"] / s["days"], 2)
            s["total"] = round(s["total"], 2)
            s["win_day_rate"] = round(s["wins"] / s["days"], 2)
            s["low_sample"] = s["days"] < 20
    return out


# ---------------- delete / archive ----------------
def archive(con, sid, on=True):
    con.execute("UPDATE strategies SET archived_at=?, updated=? WHERE id=?",
                (now() if on else None, now(), sid))
    con.commit()


def delete_strategy(con, sid, delete_result_dirs=False):
    """Hard delete. Keeps a tombstone so the researcher does not re-add it. Never touches strategy .py files."""
    r = con.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    if not r:
        return {"deleted": False}
    removed_dirs = []
    if delete_result_dirs:
        import shutil
        for run in con.execute("SELECT result_dir FROM runs WHERE strategy_id=?", (sid,)).fetchall():
            d = run["result_dir"]
            if d and os.path.isdir(d) and os.path.commonpath([os.path.abspath(d), RESULTS_DIR]) == RESULTS_DIR \
                    and os.path.abspath(d) != RESULTS_DIR:
                shutil.rmtree(d, ignore_errors=True)
                removed_dirs.append(d)
    con.execute("INSERT OR REPLACE INTO tombstones(signature,slug,name,deleted_at) VALUES(?,?,?,?)",
                (r["signature"], r["slug"], r["name"], now()))
    con.execute("DELETE FROM runs WHERE strategy_id=?", (sid,))
    con.execute("DELETE FROM strategies WHERE id=?", (sid,))
    con.commit()
    return {"deleted": True, "result_dirs_removed": len(removed_dirs)}


def delete_run(con, run_id, delete_dir=False):
    r = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        return False
    d = r["result_dir"]
    if delete_dir and d and os.path.isdir(d) and os.path.commonpath([os.path.abspath(d), RESULTS_DIR]) == RESULTS_DIR \
            and os.path.abspath(d) != RESULTS_DIR:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    elif d:                                       # files kept: remember, so the disk sync does not import the run again
        con.execute("INSERT OR REPLACE INTO ignored_results(path, deleted_at) VALUES(?,?)",
                    (os.path.normcase(os.path.abspath(d)), now()))
    con.execute("DELETE FROM runs WHERE id=?", (run_id,))
    con.commit()
    return True


# ---------------- settings ----------------
def get_setting(con, key, default=None):
    r = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(con, key, value):
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
    con.commit()


# ---------------- seed: the user's existing strategy ----------------
def seed_existing(con):
    """Register nifty_direction_ce_pe (user's own strategy) and import its existing result folders."""
    if con.execute("SELECT 1 FROM strategies WHERE slug='nifty_direction_ce_pe'").fetchone():
        return
    if not os.path.exists(os.path.join(STRATEGIES_DIR, "nifty_direction_ce_pe.py")):
        return
    spec = {"name": "Nifty Direction CE/PE", "family": "momentum", "timeframe": "5m",
            "indicators": ["option premium readings"], "entry_trigger": "3 rising readings", "exit_type": "target/stop/reversal",
            "entry_long": "ATM CE premium rises 3 consecutive 5-min readings while PE does not",
            "entry_short": "ATM PE premium rises 3 consecutive readings while CE does not",
            "notes": "User's own strategy; see nifty_direction_ce_pe.py docstring"}
    sig = "user-nifty_direction_ce_pe"
    cur = con.execute(
        "INSERT INTO strategies(slug,name,aliases,family,spec,signature,sources,popularity,data_needs,testable,status,"
        "origin,file,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("nifty_direction_ce_pe", "Nifty Direction CE/PE", "[]", "momentum", json.dumps(spec), sig, "[]", 3,
         "ohlcv_1m", "yes", "tested", "user", "nifty_direction_ce_pe.py", now(), now()))
    sid = cur.lastrowid
    con.commit()
    import glob
    latest = {}                                    # keep only the newest result folder per (instrument, day)
    for d in sorted(glob.glob(os.path.join(RESULTS_DIR, "nifty_direction_ce_pe_*"))):
        sj = os.path.join(d, "summary.json")
        if not os.path.exists(sj):
            continue
        s = json.load(open(sj, encoding="utf-8"))
        inst = "BANKNIFTY" if "BANKNIFTY" in os.path.basename(d) else "NIFTY"
        latest[(inst, s.get("date"))] = (d, s)
    for (inst, day), (d, s) in latest.items():
        add_run(con, sid, inst, day, s, "reviewed" if "114806" in d else "unreviewed", d)


def register_user_strategy(con, slug, name, family="user", notes=""):
    """Register a hand-written strategy file <slug>.py (project root) so it shows in the app and can be run/queued."""
    slug = slugify(slug)
    if not os.path.exists(os.path.join(STRATEGIES_DIR, slug + ".py")):
        return {"result": "rejected", "reason": f"{slug}.py not found in the strategies folder"}
    if con.execute("SELECT 1 FROM strategies WHERE slug=?", (slug,)).fetchone():
        return {"result": "exists", "slug": slug}
    spec = {"name": name, "family": family, "notes": notes or "User-provided strategy; see the file docstring"}
    cur = con.execute(
        "INSERT INTO strategies(slug,name,aliases,family,spec,signature,sources,popularity,data_needs,testable,status,"
        "origin,file,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (slug, name, "[]", norm(family), json.dumps(spec), "user-" + slug, "[]", 3, "ohlcv_1m", "yes", "queued",
         "user", slug + ".py", now(), now()))
    con.commit()
    return {"result": "added", "id": cur.lastrowid, "slug": slug}


# ---------------- sync: strategy files and result folders on disk -> database ----------------
def _result_dir_re():
    from script_openalgo.backtest_core import instruments
    instruments.reload()
    names = "|".join(sorted((re.escape(n) for n in instruments.INSTRUMENTS), key=len, reverse=True))
    return re.compile(r"^(?P<slug>.+?)_(?P<inst>" + names + r")_(?P<day>\d{4}-\d{2}-\d{2})_(?P<t>\d{6})$")


RESULT_DIR_RE = _result_dir_re()
LEGACY_DIR_RE = re.compile(r"^(?P<slug>.+?)_(?P<day>\d{4}-\d{2}-\d{2})_(?P<t>\d{6})$")       # early runs had no instrument in the name


def _bundled_meta():
    """Description of the packaged example strategies (name, rules, sources, tags), written by build_all.py."""
    try:
        from importlib import resources
        p = resources.files("script_openalgo") / "assets" / "strategies_meta.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except Exception:
        return {}


def _manifest_flags(cache_dir, instrument, day):
    p = os.path.join(cache_dir, f"manifest_{instrument}_{day}.json")
    if os.path.exists(p):
        try:
            m = json.load(open(p, encoding="utf-8"))
            return m.get("days_to_expiry"), 1 if m.get("nearest", True) else 0
        except (OSError, ValueError):
            pass
    return None, 1


def sync_from_disk(con, cache_dir=None):
    """Make the database reflect what is in the workspace folders, so command-line runs show up in the web app:
    1. every strategies/<slug>.py not yet in the database is registered (rules/tags from the bundled description when it is
       one of the packaged examples);
    2. result folders and results/multiday/*.json that are not yet recorded become runs (the newest result per strategy,
       instrument and day; code hash, days to expiry and nearest-expiry label included when known).
    Safe to run any number of times. Returns counts."""
    cache_dir = cache_dir or ws_paths()[1]
    out = {"registered": 0, "runs_imported": 0}
    meta = _bundled_meta()
    slugs = []
    if os.path.isdir(STRATEGIES_DIR):
        slugs = sorted(f[:-3] for f in os.listdir(STRATEGIES_DIR)
                       if f.endswith(".py") and not f.startswith(("backtest_", "_")) and f != "__init__.py")
    have = {r["slug"] for r in con.execute("SELECT slug FROM strategies").fetchall()}
    for slug in slugs:
        if slug in have:
            continue
        m = meta.get(slug)
        sig = (m or {}).get("signature") or "disk-" + slug
        if con.execute("SELECT 1 FROM strategies WHERE signature=?", (sig,)).fetchone():
            sig = "disk-" + slug
        if m:
            spec = m.get("spec") or {}
            con.execute(
                "INSERT INTO strategies(slug,name,aliases,family,spec,signature,sources,popularity,data_needs,testable,testable_reason,"
                "status,origin,file,created,updated,tags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (slug, m["name"], json.dumps(m.get("aliases", [])), m.get("family") or "user", json.dumps(spec), sig,
                 json.dumps(m.get("sources", [])), m.get("popularity", 1), m.get("data_needs", ""), m.get("testable", "yes"),
                 m.get("testable_reason", ""), "queued", m.get("origin", "researcher"), slug + ".py", now(), now(),
                 json.dumps(normalize_tags(m.get("tags", [])))))
        else:
            name = slug.replace("_", " ").title()
            con.execute(
                "INSERT INTO strategies(slug,name,aliases,family,spec,signature,sources,popularity,data_needs,testable,status,origin,"
                "file,created,updated,tags) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (slug, name, "[]", "user", json.dumps({"name": name, "notes": "found in the strategies folder"}), sig, "[]", 1,
                 "ohlcv_1m", "yes", "queued", "user", slug + ".py", now(), now(), json.dumps(["user"])))
        out["registered"] += 1
    con.commit()
    ids = {r["slug"]: r["id"] for r in con.execute("SELECT id, slug FROM strategies").fetchall()}
    known = {os.path.normcase(os.path.abspath(r["result_dir"])) for r in con.execute("SELECT result_dir FROM runs WHERE result_dir IS NOT NULL")}
    known |= {r["path"] for r in con.execute("SELECT path FROM ignored_results")}           # runs the user deleted

    # newest result folder per (slug, instrument, day)
    best = {}
    result_re = _result_dir_re()                     # rebuilt each time: instruments can be added while the app runs
    if os.path.isdir(RESULTS_DIR):
        for d in os.listdir(RESULTS_DIR):
            full = os.path.join(RESULTS_DIR, d)
            m = result_re.match(d)
            inst = m.group("inst") if m else "NIFTY"
            m = m or LEGACY_DIR_RE.match(d)
            if not m or not os.path.exists(os.path.join(full, "summary.json")) or m.group("slug") not in ids:
                continue
            key = (m.group("slug"), inst, m.group("day"))
            if key not in best or os.path.getmtime(full) > os.path.getmtime(best[key]):
                best[key] = full
    # what the multi-day reports say about those folders (verdict, code hash)
    info = {}
    mdir = os.path.join(RESULTS_DIR, "multiday")
    if os.path.isdir(mdir):
        for f in sorted(glob_mod.glob(os.path.join(mdir, "*.json"))):
            if os.path.basename(f).startswith("_"):
                continue
            try:
                rep = json.load(open(f, encoding="utf-8"))
                for r in rep.get("days", []):
                    if r.get("result_dir"):
                        info[os.path.normcase(os.path.abspath(r["result_dir"]))] = (r.get("verdict"), rep.get("code_hash"))
            except (OSError, ValueError, AttributeError):
                continue
    touched = set()
    for (slug, inst, day), full in sorted(best.items(), key=lambda kv: kv[0][2]):
        norm = os.path.normcase(os.path.abspath(full))
        if norm in known:
            continue
        try:
            summ = json.load(open(os.path.join(full, "summary.json"), encoding="utf-8"))
        except (OSError, ValueError):
            continue
        verdict, chash = info.get(norm, ("imported", None))
        dte, nearest = _manifest_flags(cache_dir, inst, day)
        add_run(con, ids[slug], inst, day, summ, verdict, full, None, code_hash=chash, dte=dte, nearest=nearest)
        touched.add(ids[slug])
        out["runs_imported"] += 1
    for sid in touched:
        con.execute("UPDATE strategies SET status='tested', updated=? WHERE id=? AND status IN ('queued','untestable')", (now(), sid))
    con.commit()
    return out
