"""Create and check a workspace folder: where your candles, results, strategies and databases live.

`script-openalgo init <folder>` copies the packaged agents, skill and example strategies into the workspace (the Claude CLI
discovers agents from <workspace>/.claude/agents), writes script_openalgo.toml, and never overwrites an existing file.
"""
import os
import shutil
import sys
from importlib import resources

from script_openalgo import __version__, config

ASSETS = resources.files("script_openalgo") / "assets"


def _copy_tree(src, dst, overwrite=False):
    """Copy a packaged directory (importlib.resources Traversable) to disk. Returns (copied, skipped)."""
    copied = skipped = 0
    os.makedirs(dst, exist_ok=True)
    for entry in src.iterdir():
        target = os.path.join(dst, entry.name)
        if entry.is_dir():
            c, s = _copy_tree(entry, target, overwrite)
            copied, skipped = copied + c, skipped + s
        elif os.path.exists(target) and not overwrite:
            skipped += 1
        else:
            with entry.open("rb") as f, open(target, "wb") as out:
                shutil.copyfileobj(f, out)
            copied += 1
    return copied, skipped


def init_workspace(folder, host=None, examples=True):
    folder = os.path.abspath(os.path.expanduser(folder))
    os.makedirs(folder, exist_ok=True)
    out = {"folder": folder, "created": [], "kept": []}
    cfg = os.path.join(folder, config.CONFIG_NAME)
    existed = os.path.exists(cfg)
    config.write_default_config(folder, host)
    (out["kept"] if existed else out["created"]).append(config.CONFIG_NAME)
    for d in ("strategies", "results", "data_cache", "strategy_db", "backups"):
        os.makedirs(os.path.join(folder, d), exist_ok=True)
    envx = os.path.join(folder, ".env.example")
    if not os.path.exists(envx):
        with open(envx, "w", encoding="utf-8") as f:
            f.write("# Copy to .env and fill in. Never share or commit .env.\nOPENALGO_API_KEY=your-openalgo-api-key\n")
        out["created"].append(".env.example")
    for name, dst in (("agents", ".claude/agents"), ("skills", ".claude/skills")):
        c, k = _copy_tree(ASSETS / name, os.path.join(folder, *dst.split("/")))
        out["created"].append(f"{dst} ({c} files)")
        if k:
            out["kept"].append(f"{dst} ({k} existing files kept)")
    if examples:
        sdir = os.path.join(folder, "strategies")
        if not any(f.endswith(".py") for f in os.listdir(sdir)):
            c, _ = _copy_tree(ASSETS / "strategies", sdir)
            out["created"].append(f"strategies/ ({c} example strategies)")
        else:
            out["kept"].append("strategies/ (not empty: examples not copied)")
    return out


def doctor(host_override=None):
    """Checks that do not need the OpenAlgo server, then one that does. Returns (list of lines, all_ok)."""
    import requests
    lines, ok = [], True

    def say(good, text):
        nonlocal ok
        ok = ok and good
        lines.append(("[ok]   " if good else "[FAIL] ") + text)

    say(sys.version_info >= (3, 11), f"Python {sys.version.split()[0]} (needs 3.11+)")
    say(True, f"script_openalgo {__version__}")
    home = config.home()
    say(os.path.exists(os.path.join(home, config.CONFIG_NAME)), f"workspace {home}" + ("" if os.path.exists(os.path.join(home, config.CONFIG_NAME)) else "  (no script_openalgo.toml: run `script-openalgo init .`)"))
    say(os.path.isdir(os.path.join(home, ".claude", "agents")), "agents installed in <workspace>/.claude/agents" if os.path.isdir(os.path.join(home, ".claude", "agents")) else "agents not installed in the workspace (run init); only needed for the AI steps")
    say(bool(config.api_key()), "API key found (environment or .env)" if config.api_key() else "no API key: set OPENALGO_API_KEY or create .env")
    h = host_override or config.host()
    try:
        r = requests.post(f"{h}/api/v1/ping", json={"apikey": config.api_key() or ""}, timeout=8)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200 and j.get("status") == "success":
            say(True, f"OpenAlgo reachable at {h} (broker: {j.get('data', {}).get('broker')})")
        else:
            say(False, f"OpenAlgo at {h} answered {r.status_code}: {str(j.get('message') or r.text)[:100]} (wrong API key?)")
    except requests.RequestException as e:
        say(False, f"cannot reach OpenAlgo at {h}: {type(e).__name__}. Check the address, that the server is running and the firewall; set it with --host or OPENALGO_HOST")
    claude = shutil.which("claude")
    lines.append(("[ok]   " if claude else "[info] ") + ("Claude CLI found (AI steps available)" if claude else "Claude CLI not found: the AI steps (researcher, strategy-writer) need `claude` installed and logged in; data, backtests and the web app work without it"))
    return lines, ok
