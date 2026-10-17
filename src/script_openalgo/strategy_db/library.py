"""Strategy library operations: tags, edit rules, rename, copy, and code files with version history.

Code files live in the strategies/ folder as <slug>.py (strategy) and backtest_<slug>.py (its backtest). Every save keeps the
previous text in strategy_db/versions/<slug>/ so edits are reversible. Only files for slugs registered in the
database can be touched, and only in the strategies folder.
"""
import ast
import json
import os
import re
import shutil
from datetime import datetime

from . import db

ROOT = db.ROOT
STRAT_DIR = db.STRATEGIES_DIR
VERSIONS_DIR = os.path.join(db.DB_DIR, "versions")
MAX_CODE_BYTES = 300_000
SLUG_RE = re.compile(r"^[a-z_][a-z0-9_]{0,60}$")

SUGGESTED_TAGS = [
    "bullish", "bearish", "neutral", "both-sides", "hedge",                                # market view
    "scalp", "intraday", "trend", "mean-reversion", "breakout", "momentum", "reversal",    # style
    "options-buying", "index", "nifty", "banknifty",                                       # instrument / vehicle
    "low-risk", "high-risk", "experimental", "favourite",                                  # your own judgement
]
EDITABLE_SPEC = ["family", "timeframe", "indicators", "entry_trigger", "exit_type", "entry_long", "entry_short",
                 "exit", "stop", "target", "filters", "params", "notes", "testable_reason"]


# ---------------- tags ----------------
def set_tags(con, sid, tags):
    tags = db.normalize_tags(tags)
    con.execute("UPDATE strategies SET tags=?, updated=? WHERE id=?", (json.dumps(tags), db.now(), sid))
    con.commit()
    return tags


def all_tags(con):
    counts = {}
    for r in con.execute("SELECT tags FROM strategies WHERE archived_at IS NULL").fetchall():
        for t in json.loads(r["tags"] or "[]"):
            counts[t] = counts.get(t, 0) + 1
    return {"counts": counts, "suggested": SUGGESTED_TAGS}


# ---------------- rules / name ----------------
def update_spec(con, sid, fields):
    """Edit the stored rules. Researched strategies get their signature recomputed and may not collide."""
    r = con.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    if not r:
        return {"error": "not found"}
    spec = json.loads(r["spec"] or "{}")
    for k, v in fields.items():
        if k in EDITABLE_SPEC:
            spec[k] = v
    if isinstance(spec.get("indicators"), str):
        spec["indicators"] = [i.strip() for i in spec["indicators"].split(",") if i.strip()]
    sig = r["signature"]
    if r["origin"] == "researcher":
        sig = db.make_signature(spec)
        clash = con.execute("SELECT name FROM strategies WHERE signature=? AND id<>?", (sig, sid)).fetchone()
        if clash:
            return {"error": f"after this edit the strategy would duplicate '{clash['name']}'. "
                             "Change the entry trigger / exit type / indicators, or copy it instead."}
    fam = db.norm(spec.get("family") or r["family"])
    con.execute("UPDATE strategies SET spec=?, signature=?, family=?, updated=? WHERE id=?",
                (json.dumps(spec), sig, fam, db.now(), sid))
    con.commit()
    return {"ok": True}


def rename(con, sid, name):
    name = (name or "").strip()
    if not name or len(name) > 80:
        return {"error": "name must be 1-80 characters"}
    con.execute("UPDATE strategies SET name=?, updated=? WHERE id=?", (name, db.now(), sid))
    con.commit()
    return {"ok": True}


# ---------------- code files ----------------
def _slug(con, sid):
    r = con.execute("SELECT slug FROM strategies WHERE id=?", (sid,)).fetchone()
    if not r or not SLUG_RE.match(r["slug"]):
        return None
    return r["slug"]


def code_path(slug, kind):
    if kind == "strategy":
        return os.path.join(STRAT_DIR, slug + ".py")
    if kind == "backtest":
        return os.path.join(STRAT_DIR, "backtest_" + slug + ".py")
    raise ValueError("kind must be strategy or backtest")


def read_code(con, sid):
    slug = _slug(con, sid)
    if not slug:
        return {"error": "not found"}
    out = {"slug": slug}
    for kind in ("strategy", "backtest"):
        p = code_path(slug, kind)
        out[kind] = {"file": os.path.basename(p), "exists": os.path.exists(p),
                     "text": open(p, encoding="utf-8").read() if os.path.exists(p) else ""}
    out["versions"] = list_versions(slug)
    return out


def list_versions(slug):
    d = os.path.join(VERSIONS_DIR, slug)
    if not os.path.isdir(d):
        return []
    res = []
    for f in sorted(os.listdir(d), reverse=True):
        m = re.match(r"^(strategy|backtest)_(\d{8}_\d{6})\.py$", f)
        if m:
            res.append({"kind": m.group(1), "name": f,
                        "time": datetime.strptime(m.group(2), "%Y%m%d_%H%M%S").isoformat(timespec="seconds")})
    return res


def check_code(kind, text):
    """Returns (error, warnings). Syntax errors block saving; contract problems are warnings."""
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        return f"syntax error line {e.lineno}: {e.msg}", []
    warnings = []
    if kind == "strategy":
        cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Strategy"]
        if not cls:
            warnings.append("no `class Strategy` found: the backtest imports `Strategy` from this file")
        else:
            init = [n for n in cls[0].body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
            args = {a.arg for a in init[0].args.args} if init else set()
            if not {"client", "clock", "sleeper"} <= args:
                warnings.append("Strategy.__init__ should accept client, clock and sleeper (strategy contract)")
        if re.search(r"\bapi_key\s*=\s*['\"][0-9a-fA-F]{20,}['\"]", text):
            warnings.append("looks like a hard-coded API key: use the OPENALGO_API_KEY environment variable")
    return None, warnings


def save_code(con, sid, kind, text):
    slug = _slug(con, sid)
    if not slug:
        return {"error": "not found"}
    if len(text.encode("utf-8")) > MAX_CODE_BYTES:
        return {"error": "file too large"}
    err, warnings = check_code(kind, text)
    if err:
        return {"error": err}
    p = code_path(slug, kind)
    if os.path.exists(p):
        _backup(slug, kind, p)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if kind == "strategy":                       # old runs measured the previous code: stats ignore them from now on
        con.execute("UPDATE strategies SET code_edited_at=?, file=?, updated=? WHERE id=?",
                    (db.now(), slug + ".py", db.now(), sid))
        con.commit()
    return {"ok": True, "warnings": warnings}


def _backup(slug, kind, path):
    d = os.path.join(VERSIONS_DIR, slug)
    os.makedirs(d, exist_ok=True)
    shutil.copyfile(path, os.path.join(d, f"{kind}_{datetime.now():%Y%m%d_%H%M%S}.py"))


def read_version(con, sid, name):
    slug = _slug(con, sid)
    if not slug or not re.match(r"^(strategy|backtest)_\d{8}_\d{6}\.py$", name or ""):
        return {"error": "bad version"}
    p = os.path.join(VERSIONS_DIR, slug, name)
    if not os.path.exists(p):
        return {"error": "version not found"}
    return {"text": open(p, encoding="utf-8").read()}


def restore_version(con, sid, name):
    v = read_version(con, sid, name)
    if "error" in v:
        return v
    return save_code(con, sid, name.split("_")[0], v["text"])


# ---------------- copy ----------------
def _copy_files(old, new):
    src = code_path(old, "strategy")
    shutil.copyfile(src, code_path(new, "strategy"))
    bsrc = code_path(old, "backtest")
    if os.path.exists(bsrc):
        text = open(bsrc, encoding="utf-8").read().replace(old, new)     # module name, result-folder prefix, usage text
        with open(code_path(new, "backtest"), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)


def copy_strategy(con, sid, new_name):
    """Independent copy under a new name: rules, tags and code files (backtest re-pointed to the new module)."""
    r = con.execute("SELECT * FROM strategies WHERE id=?", (sid,)).fetchone()
    if not r:
        return {"error": "not found"}
    new_name = (new_name or "").strip()
    if not new_name or len(new_name) > 80:
        return {"error": "name must be 1-80 characters"}
    slug = db.slugify(new_name)
    if not SLUG_RE.match(slug):
        return {"error": "name does not make a valid file name"}
    if con.execute("SELECT 1 FROM strategies WHERE slug=?", (slug,)).fetchone() or \
            os.path.exists(code_path(slug, "strategy")) or os.path.exists(code_path(slug, "backtest")):
        return {"error": f"'{slug}' already exists; choose another name"}
    has_file = os.path.exists(code_path(r["slug"], "strategy"))
    if has_file:
        _copy_files(r["slug"], slug)
    spec = json.loads(r["spec"] or "{}")
    spec["name"] = new_name
    cur = con.execute(
        "INSERT INTO strategies(slug,name,aliases,family,spec,signature,sources,popularity,data_needs,testable,"
        "testable_reason,status,origin,file,created,updated,tags,parent_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (slug, new_name, "[]", r["family"], json.dumps(spec), "copy-" + slug, r["sources"], r["popularity"],
         r["data_needs"], r["testable"], r["testable_reason"], "queued", "copy", slug + ".py" if has_file else None,
         db.now(), db.now(), r["tags"], sid))
    con.commit()
    return {"ok": True, "id": cur.lastrowid, "slug": slug}
