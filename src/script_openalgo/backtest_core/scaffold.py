"""Generate strategies/backtest_<slug>.py from a template (no AI agent needed) and check that FakeClient
supports everything the strategy calls on the client.

    python -m script_openalgo.backtest_core.scaffold <slug>            # write the backtest file (refuses to overwrite)
    python -m script_openalgo.backtest_core.scaffold <slug> --check    # only report client methods the FakeClient lacks
"""
import ast
import json
import os
import sys

from script_openalgo import config

ROOT = config.home()
STRAT_DIR = config.strategies_dir()
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", "backtest_template.py.txt")


def strategy_path(slug):
    return os.path.join(STRAT_DIR, slug + ".py")


def backtest_path(slug):
    return os.path.join(STRAT_DIR, "backtest_" + slug + ".py")


def write_backtest(slug, overwrite=False):
    if not os.path.exists(strategy_path(slug)):
        raise FileNotFoundError(f"{strategy_path(slug)} not found")
    p = backtest_path(slug)
    if os.path.exists(p) and not overwrite:
        return {"written": False, "reason": "already exists", "path": p}
    text = open(TEMPLATE, encoding="utf-8").read().replace("__SLUG__", slug)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return {"written": True, "path": p}


def client_methods(source):
    """Names of methods the strategy calls on its OpenAlgo client (found through `self.X = client` in __init__)."""
    tree = ast.parse(source)
    attrs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id == "client":
            for t in node.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) and t.value.id == "self":
                    attrs.add(t.attr)
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            v = node.func.value
            if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id == "self" and v.attr in attrs:
                used.add(node.func.attr)
            elif isinstance(v, ast.Name) and v.id == "client":
                used.add(node.func.attr)
    return used


def fake_client_methods():
    from script_openalgo.backtest_core.fake_client import FakeClient
    return {n for n in dir(FakeClient) if not n.startswith("_") and callable(getattr(FakeClient, n))}


def missing_client_methods(slug):
    used = client_methods(open(strategy_path(slug), encoding="utf-8").read())
    return sorted(used - fake_client_methods()), sorted(used)


if __name__ == "__main__":
    slug = sys.argv[1]
    missing, used = missing_client_methods(slug)
    out = {"client_methods_used": used, "missing_in_fake_client": missing}
    if "--check" not in sys.argv:
        out.update(write_backtest(slug))
    print(json.dumps(out))
    sys.exit(1 if missing else 0)
