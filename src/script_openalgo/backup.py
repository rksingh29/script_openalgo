"""Export / import the WHOLE workspace as one zip, so nothing has to be re-fetched or re-run on another machine.

Included (relative to the workspace folder):
    strategies/            every strategy and backtest file
    results/               every backtest result (trades, logs, summaries, multi-day reports)
    data_cache*/           every candle cache a workspace points to (index, option candles with OI, daily bars, chain snapshots)
    strategy_db/           the SQLite databases (copied consistently), workspaces.json, saved code versions, specs, inbox, job logs
    .claude/               agents, the strategy_builder skill and its knowledge files, orchestrator state and agent logs
    script_openalgo.toml   workspace settings (informational: the host of the other machine is NOT overwritten on import)
NOT included: .env (it holds your API key), backups/ (earlier exports), __pycache__, SQLite -wal/-shm files.

Every file is listed in MANIFEST.json with its size and SHA-256. Import verifies all of them BEFORE touching anything, makes a
safety backup of the current state first, never lets a path leave the workspace folder, and refuses unknown top-level folders.

    script-openalgo export [--out FILE.zip]
    script-openalgo import FILE.zip [--overwrite] [--no-safety-backup] [--dry-run]
"""
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime

from script_openalgo import __version__, config

FORMAT = 1
MANIFEST = "MANIFEST.json"
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIX = (".pyc", "-wal", "-shm", ".tmp")
MAX_TOTAL_BYTES = 40 * 1024 ** 3            # refuse archives that expand to more than this
RESERVED = {"strategies", "results", "strategy_db", ".claude", "src", "backups"}      # a candle cache may not use these names
ALLOWED_TOP = {"strategies", "results", "strategy_db", ".claude", "cache", "script_openalgo.toml"}   # "cache/<folder>" restores to <workspace>/<folder>


def _sha256(path, buf=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


def _walk(folder, arc_prefix):
    """(relative archive name, absolute path) for every file under folder, skipping caches and temp files."""
    if not os.path.isdir(folder):
        return
    for r, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f.endswith(SKIP_SUFFIX):
                continue
            ap = os.path.join(r, f)
            rel = os.path.relpath(ap, folder).replace(os.sep, "/")
            yield f"{arc_prefix}/{rel}", ap


def _workspace_entries(home):
    """[(db file, cache dir)] for every workspace in strategy_db/workspaces.json."""
    ws_file = os.path.join(home, "strategy_db", "workspaces.json")
    items = {"default": {"db": "strategy_db/default.db", "cache": "data_cache"}}
    if os.path.exists(ws_file):
        with open(ws_file, encoding="utf-8") as f:
            items = json.load(f).get("items", items)
    out = []
    for name, it in items.items():
        db = it["db"] if os.path.isabs(it["db"]) else os.path.join(home, it["db"])
        cache = it["cache"] if os.path.isabs(it["cache"]) else os.path.join(home, it["cache"])
        out.append((name, os.path.normpath(db), os.path.normpath(cache)))
    return out


def _consistent_db_copy(src, tmp_dir):
    """A consistent snapshot of a live SQLite file (handles WAL) via the SQLite backup API."""
    dst = os.path.join(tmp_dir, os.path.basename(src))
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    return dst


def _inside(home, path):
    """Path of `path` relative to `home` (with /) if it is inside it, else None."""
    try:
        if os.path.commonpath([home, path]) != home or os.path.normpath(path) == home:
            return None
    except ValueError:                                           # different drives
        return None
    return os.path.relpath(path, home).replace(os.sep, "/")


def _plan(home):
    """Everything an export would contain: (arcname, path, kind) plus notes about what was left out."""
    items, notes = [], []
    tmp = tempfile.mkdtemp(prefix="so_export_")
    home = os.path.normpath(home)
    entries = _workspace_entries(home)
    db_files = {db for _, db, _ in entries}
    for top in ("strategies", "results", ".claude"):
        for arc, ap in _walk(os.path.join(home, top), top):
            items.append((arc, ap, "file"))
    sdb = os.path.join(home, "strategy_db")
    for arc, ap in _walk(sdb, "strategy_db"):
        if ap.endswith(".db") and os.path.normpath(ap) in db_files:
            continue                                              # added below as a consistent copy
        items.append((arc, ap, "file"))
    for name, db, cache in entries:
        if os.path.exists(db):
            items.append(("strategy_db/" + os.path.basename(db), _consistent_db_copy(db, tmp) if os.path.getsize(db) else db, "db"))
        rel = _inside(home, cache)
        if rel and rel.split("/")[0] not in RESERVED:
            for arc, ap in _walk(cache, "cache/" + rel):
                items.append((arc, ap, "file"))
        elif os.path.isdir(cache):
            notes.append(f"workspace '{name}': candle cache {cache} is outside the workspace folder (or uses a reserved name) and was NOT included")
    cfg = os.path.join(home, config.CONFIG_NAME)
    if os.path.exists(cfg):
        items.append((config.CONFIG_NAME, cfg, "file"))
    # a cache directory is stored under data/<its folder name>; remember the mapping
    return items, notes, tmp


def preview(home=None):
    home = home or config.home()
    items, notes, tmp = _plan(home)
    groups = {}
    try:
        for arc, ap, _ in items:
            top = arc.split("/")[0]
            g = groups.setdefault(top, {"files": 0, "bytes": 0})
            g["files"] += 1
            g["bytes"] += os.path.getsize(ap)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"groups": groups, "files": sum(g["files"] for g in groups.values()),
            "bytes": sum(g["bytes"] for g in groups.values()), "notes": notes,
            "excluded": [".env (API key)", "backups/", "__pycache__"]}


def export_workspace(dest=None, home=None):
    """Write the zip. Returns {path, files, bytes, sha256_of_zip}. Default destination: <workspace>/backups/."""
    home = os.path.normpath(home or config.home())
    if dest is None:
        os.makedirs(config.backups_dir(), exist_ok=True)
        dest = os.path.join(config.backups_dir(), f"script_openalgo_backup_{datetime.now():%Y%m%d_%H%M%S}.zip")
    items, notes, tmp = _plan(home)
    files, total = {}, 0
    try:
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for arc, ap, kind in items:
                files[arc] = {"size": os.path.getsize(ap), "sha256": _sha256(ap), "kind": kind}
                total += files[arc]["size"]
                z.write(ap, arc)
            counts = {}
            for name, db, _ in _workspace_entries(home):
                if os.path.exists(db):
                    c = sqlite3.connect(db)
                    try:
                        counts[name] = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                                        for t in ("strategies", "runs", "jobs", "claude_calls") if _has_table(c, t)}
                    finally:
                        c.close()
            manifest = {"format": FORMAT, "package_version": __version__, "created": datetime.now().isoformat(timespec="seconds"),
                        "source_workspace": home, "notes": notes, "workspace_counts": counts, "total_bytes": total, "files": files}
            z.writestr(MANIFEST, json.dumps(manifest, indent=1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"path": dest, "files": len(files), "bytes": total, "zip_bytes": os.path.getsize(dest), "notes": notes,
            "workspace_counts": counts}


def _has_table(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


class ImportError_(Exception):
    pass


def _safe_target(home, arc):
    if not arc or arc.startswith(("/", "\\")) or ":" in arc.split("/")[0] or ".." in arc.replace("\\", "/").split("/"):
        raise ImportError_(f"unsafe path in archive: {arc!r}")
    top = arc.split("/")[0]
    if top not in ALLOWED_TOP:
        raise ImportError_(f"unexpected top-level entry in archive: {top!r}")
    parts = arc.split("/")
    if top == "cache":                                           # cache/<folder>/... -> <workspace>/<folder>/...
        if len(parts) < 3 or parts[1] in RESERVED:
            raise ImportError_(f"bad candle-cache path in archive: {arc!r}")
        parts = parts[1:]
    target = os.path.normpath(os.path.join(home, *parts))
    if os.path.commonpath([home, target]) != home:
        raise ImportError_(f"path escapes the workspace: {arc!r}")
    return target


def verify_archive(zip_path):
    """Check structure and every checksum without extracting. Returns the manifest or raises ImportError_."""
    try:
        z = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise ImportError_(f"not a valid zip file: {e}")
    with z:
        if MANIFEST not in z.namelist():
            raise ImportError_("MANIFEST.json missing: this is not a script_openalgo backup")
        manifest = json.loads(z.read(MANIFEST))
        if manifest.get("format", 0) > FORMAT:
            raise ImportError_(f"backup format {manifest.get('format')} is newer than this version supports ({FORMAT}); upgrade script_openalgo")
        total = sum(i.file_size for i in z.infolist())
        if total > MAX_TOTAL_BYTES:
            raise ImportError_("archive expands to an unreasonable size")
        names = set(z.namelist()) - {MANIFEST}
        if names != set(manifest["files"]):
            raise ImportError_(f"archive content does not match the manifest ({len(names ^ set(manifest['files']))} differences)")
        for arc, meta in manifest["files"].items():
            h = hashlib.sha256()
            with z.open(arc) as f:
                while chunk := f.read(1024 * 1024):
                    h.update(chunk)
            if h.hexdigest() != meta["sha256"]:
                raise ImportError_(f"checksum mismatch (corrupted or modified): {arc}")
    return manifest


def import_workspace(zip_path, home=None, overwrite=False, safety_backup=True, dry_run=False, guard=None):
    """Restore a backup into the workspace.

    Databases in the archive REPLACE the local ones (after the safety backup). Other files are copied; a file that already exists with
    different content is kept unless overwrite=True (it is reported as a conflict). workspaces.json is merged. The local
    script_openalgo.toml (host, settings) is never overwritten.
    guard: optional callable that raises if the app is busy (e.g. jobs running)."""
    home = os.path.normpath(home or config.home())
    manifest = verify_archive(zip_path)                        # nothing is touched before this passes
    targets = {arc: _safe_target(home, arc) for arc in manifest["files"]}
    report = {"verified_files": len(targets), "written": 0, "identical_skipped": 0, "conflicts": [], "databases_replaced": [],
              "safety_backup": None, "dry_run": dry_run, "source": {k: manifest.get(k) for k in ("created", "package_version", "workspace_counts")}}
    if guard:
        guard()
    if dry_run:
        for arc, t in targets.items():
            if os.path.exists(t) and _sha256(t) == manifest["files"][arc]["sha256"]:
                report["identical_skipped"] += 1
            elif os.path.exists(t) and not arc.endswith(".db") and arc != "strategy_db/workspaces.json" and arc != config.CONFIG_NAME:
                report["conflicts"].append(arc)
            else:
                report["written"] += 1
        return report
    if safety_backup and any(os.path.exists(os.path.join(home, d)) for d in ("strategies", "results", "strategy_db")):
        os.makedirs(config.backups_dir(), exist_ok=True)
        report["safety_backup"] = export_workspace(
            os.path.join(config.backups_dir(), f"pre_import_{datetime.now():%Y%m%d_%H%M%S}.zip"), home)["path"]
    with zipfile.ZipFile(zip_path) as z:
        for arc, target in targets.items():
            meta = manifest["files"][arc]
            os.makedirs(os.path.dirname(target), exist_ok=True)
            is_db = arc.endswith(".db")
            if arc == config.CONFIG_NAME and os.path.exists(target):
                continue                                        # keep this machine's host/settings
            if arc == "strategy_db/workspaces.json" and os.path.exists(target):
                _merge_workspaces(target, json.loads(z.read(arc)))
                report["written"] += 1
                continue
            if os.path.exists(target):
                if _sha256(target) == meta["sha256"]:
                    report["identical_skipped"] += 1
                    continue
                if not is_db and not overwrite:
                    report["conflicts"].append(arc)
                    continue
            tmp = target + ".importing"
            with z.open(arc) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            if _sha256(tmp) != meta["sha256"]:
                os.remove(tmp)
                raise ImportError_(f"write verification failed for {arc}")
            for ext in ("-wal", "-shm"):                        # stale journal files of a replaced database
                if is_db and os.path.exists(target + ext):
                    os.remove(target + ext)
            os.replace(tmp, target)
            report["written"] += 1
            if is_db:
                report["databases_replaced"].append(arc)
    _migrate_databases(home)
    return report


def _merge_workspaces(target, incoming):
    cur = json.load(open(target, encoding="utf-8"))
    for name, it in incoming.get("items", {}).items():
        cur.setdefault("items", {}).setdefault(name, it)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(cur, f, indent=1)


def _migrate_databases(home):
    """Open every restored database once so schema upgrades (older backups) are applied."""
    from script_openalgo.strategy_db import db
    for name, _, _ in _workspace_entries(home):
        try:
            db.connect(name).close()
        except Exception:
            pass
