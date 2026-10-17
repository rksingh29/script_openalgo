#!/usr/bin/env python
"""Build the script_openalgo package with `uv build` and PROVE the result is complete.

    python build_all.py                      # sync assets, check, uv build, inspect, install test in a clean venv
    python build_all.py --skip-install-test  # same without the clean-venv install test
    python build_all.py --check-only         # sync assets and run the checks, do not build

Steps
  1. sync   copy the agents, the skill (with cleaned knowledge seeds) and the example strategies from this workspace into
            src/script_openalgo/assets/ so they ship INSIDE the package (tar.gz and wheel)
  2. check  compile everything; scan the files that will be packaged for API keys / secrets
  3. build  `uv build` -> dist/script_openalgo-<version>.tar.gz and .whl
  4. inspect  assert the required files are in BOTH archives and that nothing private is
  5. install test  a fresh uv venv, install the wheel, run the CLI: init, doctor against an unreachable server (must fail cleanly,
            no OpenAlgo server needed), export + import round trip
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "src", "script_openalgo")
ASSETS = os.path.join(PKG, "assets")
DIST = os.path.join(HERE, "dist")

REQUIRED = [                                  # must be present in BOTH the tar.gz and the wheel (package-relative)
    "__init__.py", "cli.py", "config.py", "backup.py", "workspace.py", "orchestrator.py",
    "backtest_core/data_loader.py", "backtest_core/multiday.py", "backtest_core/templates/backtest_template.py.txt",
    "strategy_db/db.py", "strategy_lib/levels.py", "webapp/app.py", "webapp/static/index.html",
    "assets/agents/strategy-researcher.md", "assets/agents/strategy-writer.md", "assets/agents/backtest-writer.md",
    "assets/agents/backtest-reviewer.md", "assets/agents/data-downloader.md",
    "assets/skills/strategy_builder/SKILL.md", "assets/skills/strategy_builder/knowledge/openalgo_api.md",
    "assets/strategies/nifty_direction_ce_pe.py", "assets/strategies_meta.json",
]
FORBIDDEN_FRAGMENTS = ["/.env", "data_cache", "/results/", "strategy_db/default.db", "__pycache__", ".pyc", "/backups/"]
SECRET_RE = re.compile(r"""(?i)(api_?key|apikey|OPENALGO_API_KEY)\s*[=:]\s*['"]?[0-9a-f]{24,}['"]?""")


def say(msg=""):
    print(msg, flush=True)


def run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def sync_assets():
    """Copy agents / skill / example strategies from the workspace into the package. Single source of truth stays the workspace."""
    shutil.rmtree(ASSETS, ignore_errors=True)
    os.makedirs(ASSETS)
    src_agents = os.path.join(HERE, ".claude", "agents")
    for f in glob.glob(os.path.join(src_agents, "*.md")):
        os.makedirs(os.path.join(ASSETS, "agents"), exist_ok=True)
        shutil.copy(f, os.path.join(ASSETS, "agents", os.path.basename(f)))
    skill_src = os.path.join(HERE, ".claude", "skills", "strategy_builder")
    skill_dst = os.path.join(ASSETS, "skills", "strategy_builder")
    os.makedirs(os.path.join(skill_dst, "knowledge"), exist_ok=True)
    shutil.copy(os.path.join(skill_src, "SKILL.md"), skill_dst)
    for f in glob.glob(os.path.join(skill_src, "knowledge", "*.md")):
        text = open(f, encoding="utf-8").read()
        name = os.path.basename(f)
        if name == "strategy_registry.md":            # results of THIS user's runs are not a seed
            text = "# Strategy registry (one line per strategy; append results, newest last)\n\n| Strategy file | Backtest file | Key params | Last backtest | Status |\n|---|---|---|---|---|\n"
        text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("User decision:"))
        with open(os.path.join(skill_dst, "knowledge", name), "w", encoding="utf-8", newline="\n") as out:
            out.write(text + "\n")
    os.makedirs(os.path.join(ASSETS, "strategies"), exist_ok=True)
    n = 0
    for f in glob.glob(os.path.join(HERE, "strategies", "*.py")):
        if not os.path.basename(f).startswith("backtest_"):      # backtests are regenerated from the template
            shutil.copy(f, os.path.join(ASSETS, "strategies", os.path.basename(f)))
            n += 1
    write_strategy_meta()
    say(f"[sync]   assets: {len(glob.glob(os.path.join(ASSETS, 'agents', '*.md')))} agents, 1 skill + "
        f"{len(glob.glob(os.path.join(skill_dst, 'knowledge', '*.md')))} knowledge files, {n} example strategies")


def write_strategy_meta():
    """assets/strategies_meta.json: name, rules, sources and tags of the bundled example strategies (no runs, no status), so a new
    workspace shows them properly in the web app. Taken from this workspace's database."""
    import json
    import sqlite3
    db = os.path.join(HERE, "strategy_db", "default.db")
    meta = {}
    if os.path.exists(db):
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        have = {os.path.basename(f)[:-3] for f in glob.glob(os.path.join(ASSETS, "strategies", "*.py"))}
        for r in con.execute("SELECT * FROM strategies"):
            if r["slug"] in have:
                meta[r["slug"]] = {"name": r["name"], "aliases": json.loads(r["aliases"] or "[]"), "family": r["family"],
                                   "spec": json.loads(r["spec"] or "{}"), "signature": r["signature"], "sources": json.loads(r["sources"] or "[]"),
                                   "popularity": r["popularity"], "data_needs": r["data_needs"], "testable": r["testable"],
                                   "testable_reason": r["testable_reason"], "origin": r["origin"], "tags": json.loads(r["tags"] or "[]")}
        con.close()
    with open(os.path.join(ASSETS, "strategies_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    say(f"[sync]   strategy descriptions for {len(meta)} bundled strategies")


def packaged_files():
    for r, ds, fs in os.walk(PKG):
        ds[:] = [d for d in ds if d != "__pycache__"]
        for f in fs:
            if not f.endswith(".pyc"):
                yield os.path.join(r, f)


def check():
    r = run([sys.executable, "-m", "compileall", "-q", PKG])
    if r.returncode:
        say(r.stdout + r.stderr)
        sys.exit("[check]  compile failed")
    real_key = None
    envp = os.path.join(HERE, ".env")
    if os.path.exists(envp):
        for line in open(envp, encoding="utf-8"):
            if line.startswith("OPENALGO_API_KEY="):
                real_key = line.split("=", 1)[1].strip().strip('"').strip("'")
    bad = []
    for p in packaged_files():
        try:
            text = open(p, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        if real_key and real_key in text:
            bad.append((p, "contains your real API key"))
        for m in SECRET_RE.finditer(text):
            bad.append((p, f"looks like a hard-coded key: {m.group(0)[:30]}..."))
    if bad:
        for p, why in bad:
            say(f"  SECRET? {os.path.relpath(p, HERE)}: {why}")
        sys.exit("[check]  refusing to build: remove the secrets above")
    say(f"[check]  compiled; {sum(1 for _ in packaged_files())} files scanned, no secrets found")


def build():
    shutil.rmtree(DIST, ignore_errors=True)
    r = run(["uv", "build", "--out-dir", DIST], cwd=HERE)
    say((r.stdout + r.stderr).strip())
    if r.returncode:
        sys.exit("[build]  uv build failed")
    files = sorted(os.listdir(DIST))
    say("[build]  " + ", ".join(f"{f} ({os.path.getsize(os.path.join(DIST, f)) / 1024:.0f} KB)" for f in files))
    sdist = next(f for f in files if f.endswith(".tar.gz"))
    wheel = next(f for f in files if f.endswith(".whl"))
    return os.path.join(DIST, sdist), os.path.join(DIST, wheel)


def inspect(sdist, wheel):
    with tarfile.open(sdist) as t:
        sd = [m.name for m in t.getmembers() if m.isfile()]
    with zipfile.ZipFile(wheel) as z:
        wh = z.namelist()
    ok = True
    for label, names, prefix in (("tar.gz", sd, "/src/script_openalgo/"), ("wheel", wh, "script_openalgo/")):
        missing = [r for r in REQUIRED if not any(n.endswith(prefix + r) or n.endswith("/" + r) and prefix in n for n in names)]
        forbidden = [n for n in names for frag in FORBIDDEN_FRAGMENTS if frag in n.replace("\\", "/") and "script_openalgo" in n and not n.endswith(".md")]
        agents = [n for n in names if "/assets/agents/" in n]
        skills = [n for n in names if "/assets/skills/" in n]
        strat = [n for n in names if "/assets/strategies/" in n]
        say(f"[inspect] {label}: {len(names)} files | agents {len(agents)} | skill files {len(skills)} | example strategies {len(strat)}"
            + (f" | MISSING {missing}" if missing else " | all required files present")
            + (f" | FORBIDDEN {forbidden[:3]}" if forbidden else " | nothing private"))
        ok = ok and not missing and not forbidden
    if not ok:
        sys.exit("[inspect] the archives are incomplete or contain private files")
    return sd, wh


def install_test(wheel):
    tmp = tempfile.mkdtemp(prefix="so_install_")
    venv = os.path.join(tmp, "venv")
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"          # never let uv pick a beta interpreter
    r = run(["uv", "venv", "--python", pyver, venv])
    if r.returncode:
        sys.exit("[install] uv venv failed: " + r.stderr)
    py = os.path.join(venv, "Scripts" if os.name == "nt" else "bin", "python.exe" if os.name == "nt" else "python")
    r = run(["uv", "pip", "install", "--python", py, wheel])
    if r.returncode:
        sys.exit("[install] pip install failed:\n" + r.stderr[-1500:])
    say("[install] wheel installed into a clean venv (no OpenAlgo server software, no workspace data)")
    cli = [py, "-m", "script_openalgo"]
    ws = os.path.join(tmp, "ws")
    env = {k: v for k, v in os.environ.items() if k not in ("OPENALGO_HOST", "OPENALGO_API_KEY", "SCRIPT_OPENALGO_HOME", "BACKTEST_CACHE_DIR", "STRATEGY_DB_PATH")}
    env["PYTHONUTF8"] = "1"
    checks = []

    def step(name, cond, detail=""):
        checks.append(cond)
        say(f"[install]   {'ok  ' if cond else 'FAIL'} {name}" + (f"  ({detail})" if detail else ""))

    r = run(cli + ["--version"], env=env)
    step("--version", r.returncode == 0 and "script_openalgo" in r.stdout, r.stdout.strip())
    r = run(cli + ["--host", "http://10.255.255.1:5000", "init", ws], env=env)
    step("init creates a workspace", r.returncode == 0 and os.path.exists(os.path.join(ws, "script_openalgo.toml")), r.stderr.strip()[:120])
    toml = open(os.path.join(ws, "script_openalgo.toml"), encoding="utf-8").read() if os.path.exists(os.path.join(ws, "script_openalgo.toml")) else ""
    step("the --host given to init is saved in the workspace config", "10.255.255.1" in toml)
    step("agents copied to <workspace>/.claude/agents", len(glob.glob(os.path.join(ws, ".claude", "agents", "*.md"))) >= 5, f"{len(glob.glob(os.path.join(ws, '.claude', 'agents', '*.md')))} agents")
    step("skill + knowledge copied", os.path.exists(os.path.join(ws, ".claude", "skills", "strategy_builder", "SKILL.md")))
    step("example strategies copied", len(glob.glob(os.path.join(ws, "strategies", "*.py"))) >= 5, f"{len(glob.glob(os.path.join(ws, 'strategies', '*.py')))} files")
    r = run(cli + ["--home", ws, "--host", "http://127.0.0.1:9", "doctor"], env=env)
    step("doctor with an unreachable server fails cleanly (no traceback)", r.returncode == 1 and "Traceback" not in r.stdout + r.stderr and "cannot reach OpenAlgo" in r.stdout, "exit " + str(r.returncode))
    r = run(cli + ["--home", ws, "multiday", "--strategy", "nifty_direction_ce_pe"], env=env)
    step("multiday with no cached data gives a clear message, not a crash", "Traceback" not in r.stderr and ("no usable days" in r.stdout + r.stderr or "no usable days" in r.stderr + r.stdout), (r.stdout + r.stderr).strip()[-120:])
    out = os.path.join(tmp, "b.zip")
    r = run(cli + ["--home", ws, "export", "--out", out], env=env)
    step("export", r.returncode == 0 and os.path.exists(out), r.stdout.strip()[-100:])
    ws2 = os.path.join(tmp, "ws2")
    os.makedirs(ws2)
    r = run(cli + ["--home", ws2, "import", out], env=env)
    step("import into an empty workspace", r.returncode == 0 and "written" in r.stdout, r.stdout.strip()[-100:])
    shutil.rmtree(tmp, ignore_errors=True)
    if not all(checks):
        sys.exit("[install] some checks failed")
    say(f"[install] all {len(checks)} checks passed")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-install-test", action="store_true")
    ap.add_argument("--check-only", action="store_true")
    a = ap.parse_args()
    if not shutil.which("uv"):
        sys.exit("uv not found. Install it from https://docs.astral.sh/uv/ and run again.")
    sync_assets()
    check()
    if a.check_only:
        return
    sdist, wheel = build()
    inspect(sdist, wheel)
    if not a.skip_install_test:
        install_test(wheel)
    say(f"\nDone. Install on another machine with:  pip install {os.path.relpath(wheel, HERE)}")


if __name__ == "__main__":
    main()
