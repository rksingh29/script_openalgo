"""
Orchestrator for the strategy_builder workflow.

  python .claude/orchestrator.py --strategy nifty_direction_ce_pe [--date YYYY-MM-DD]
         [--spec spec.md] [--answer "text"] [--restart] [--no-agents]

Phases: strategy -> data -> backtest -> review -> learn -> done
State is kept in .claude/state/<strategy>.json so a run can pause and resume.

Asking for input: when blocked, the orchestrator asks the user.
  * interactive terminal: it prompts on stdin;
  * run from a Claude session (no TTY): it saves the questions, prints a line starting with
    NEEDS_INPUT and exits with code 3. Claude asks the user, then re-runs with --answer "...".

Agents (.claude/agents/*.md) are run with `claude -p --agent <name>` and must end with a JSON line.
Needs env OPENALGO_API_KEY for data fetching (never printed or stored).
"""
import argparse
import glob
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta

from script_openalgo import config  # noqa: E402
from script_openalgo.backtest_core import scaffold, verify  # noqa: E402

ROOT = config.home()                       # the workspace: agents are discovered from <workspace>/.claude
STATE_DIR = config.state_dir()

STRAT_DIR = config.strategies_dir()      # strategy and backtest files live here
KNOW = config.knowledge_dir()
PHASES = ["strategy", "data", "backtest", "review", "learn", "done"]
MAX_REVIEW_ROUNDS = 3
AGENT_TIMEOUT = 1800
ALLOWED_TOOLS = "Read Write Edit Bash Glob Grep"
RESULT_SCHEMA = json.dumps({
    "type": "object",
    "properties": {"status": {"enum": ["done", "pass", "fail", "needs_input"]},
                   "questions": {"type": "array", "items": {"type": "string"}},
                   "findings": {"type": "array", "items": {"type": "object"}},
                   "artifacts": {"type": "array", "items": {"type": "string"}}},
    "required": ["status", "questions"]})
NEEDS_INPUT_EXIT = 3


def log(msg):
    print(f"[orchestrator] {msg}", flush=True)


# ---------------- state ----------------
class State:
    def __init__(self, name, restart=False):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.path = os.path.join(STATE_DIR, f"{name}.json")
        self.d = {"phase": "strategy", "date": None, "answers": [], "pending": None, "review_round": 0,
                  "findings": [], "result_dir": None, "fresh": None, "verdict": None}
        if os.path.exists(self.path) and not restart:
            self.d.update(json.load(open(self.path)))

    def save(self):
        json.dump(self.d, open(self.path, "w"), indent=1, default=str)

    def __getitem__(self, k):
        return self.d[k]

    def __setitem__(self, k, v):
        self.d[k] = v
        self.save()


def append_knowledge(fname, text):
    with open(os.path.join(KNOW, fname), "a", encoding="utf-8") as f:
        f.write(f"\n## {date.today().isoformat()}\n{text}\n")


# ---------------- user input ----------------
def ask_user(st, questions, context=""):
    """Returns answer text, or exits with code 3 when no TTY is available."""
    if st["fresh"] is not None:            # answer supplied via --answer on resume
        v, st["fresh"] = st["fresh"], None
        return v
    if st["pending"] is None:
        st["pending"] = {"questions": questions, "context": context}
    if sys.stdin.isatty():
        try:
            print(f"\n{context}")
            ans = [input(f"  ? {q}\n  > ") for q in questions]
            text = record_answer(st, "\n".join(ans))
            st["fresh"] = None
            return text
        except EOFError:                   # stdin is not really interactive
            pass
    print("NEEDS_INPUT " + json.dumps({"questions": questions, "context": context,
                                       "resume": "re-run the same command with --answer \"<user's answer>\""}))
    sys.exit(NEEDS_INPUT_EXIT)


def record_answer(st, text):
    p = st["pending"] or {"questions": []}
    st["answers"] = st["answers"] + [{"q": p["questions"], "a": text, "at": datetime.now().isoformat(timespec="seconds")}]
    st["pending"] = None
    st["fresh"] = text
    append_knowledge("lessons.md", f"User decision: Q: {' / '.join(p['questions'])} A: {text}")
    return text


def last_answer(st):
    return st["answers"][-1]["a"] if st["answers"] else None


# ---------------- agents ----------------
def claude_exe():
    """Resolve the real claude.exe behind the npm .cmd shim (the shim mangles JSON quotes in arguments)."""
    w = shutil.which("claude") or "claude"
    exe = os.path.join(os.path.dirname(w), "node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
    return exe if os.path.exists(exe) else w


def _parse_result(out):
    """Last JSON object in the output that has a "status" key (may span lines)."""
    dec = json.JSONDecoder()
    for m in reversed(list(re.finditer(r'\{\s*"status"', out))):
        try:
            return dec.raw_decode(out[m.start():])[0]
        except ValueError:
            continue
    return None


CURRENT = {"strategy": None}


def _gate_reserve(name):
    """Reserve a Claude call in the shared budget (strategy_db/gate.py). Short waits are slept off; a long wait ends this
    process with exit code 5 and a BUDGET_HOLD line, so the web app holds the job and resumes it when the budget frees up."""
    from script_openalgo.strategy_db import db, gate
    con = db.connect()
    jid = int(os.environ["JOB_ID"]) if os.environ.get("JOB_ID", "").isdigit() else None
    while True:
        ok, wait, reason, cid = gate.reserve(con, "orchestrator", name, strategy=CURRENT["strategy"], job_id=jid)
        if ok:
            return con, cid
        if wait <= 90:
            log(f"budget gate: waiting {wait:.0f}s ({reason})")
            time.sleep(wait + 1)
            continue
        until = (datetime.now() + timedelta(seconds=wait + 30)).isoformat(timespec="minutes")
        print(f"BUDGET_HOLD until={until} reason={reason}", flush=True)
        con.close()
        sys.exit(5)


def run_agent(name, prompt, _retry=True):
    con, call_id = _gate_reserve(name)
    log(f"agent {name} ...")
    full = prompt + "\n\nIMPORTANT: your final message MUST end with the JSON result object described in your instructions."
    cmd = [claude_exe(), "-p", full, "--agent", name, "--permission-mode", "acceptEdits",
           "--output-format", "json", "--json-schema", RESULT_SCHEMA,
           "--allowedTools", *ALLOWED_TOOLS.split()]
    from script_openalgo.strategy_db import gate
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=AGENT_TIMEOUT)
    except subprocess.TimeoutExpired:
        gate.finish(con, call_id, None, "timeout", False, "agent timed out")
        con.close()
        raise
    out = (r.stdout or "") + (r.stderr if r.returncode else "")
    env = None
    try:                                            # --output-format json: {"result": "<agent text>", "usage": {...}, ...}
        env = json.loads(r.stdout)
    except (ValueError, TypeError):
        pass
    limit = bool(re.search(r"hit your (?:session |usage |weekly |5-hour )?limit|usage limit reached", out, re.I))
    gate.finish(con, call_id, env if isinstance(env, dict) else None, "ok" if r.returncode == 0 else "failed", limit,
                "" if r.returncode == 0 else out[-200:])
    con.close()
    logs = os.path.join(STATE_DIR, "agent_logs")
    os.makedirs(logs, exist_ok=True)
    with open(os.path.join(logs, f"{datetime.now():%Y%m%d_%H%M%S}_{name}.txt"), "w", encoding="utf-8") as f:
        f.write(f"PROMPT:\n{full}\n\nEXIT {r.returncode}\n\nOUTPUT:\n{out}\n\nSTDERR:\n{r.stderr or ''}")
    res = None
    if isinstance(env, dict):
        res = _parse_result(env.get("result", "")) or None
        if res is None and env.get("structured_output"):
            res = env["structured_output"]
        out = env.get("result", out)
    res = res or _parse_result(out)
    if res is not None:
        return res
    if _retry and r.returncode == 0:
        log(f"agent {name} gave no JSON result; retrying once")
        return run_agent(name, prompt, _retry=False)
    if r.returncode == 0 and out.strip():           # still prose: surface it to the user
        return {"status": "needs_input", "questions": ["Agent reply had no JSON result: " + out.strip()[-600:]]}
    raise RuntimeError(f"agent {name} failed (exit {r.returncode}). Tail:\n{out[-800:]}")


def agent_with_input(st, name, prompt):
    """Run an agent; if it needs input, ask the user and rerun with the answer (loop)."""
    while True:
        res = run_agent(name, prompt + (f"\n\nUser answers so far: {json.dumps(st['answers'][-3:])}" if st["answers"] else ""))
        if res.get("status") != "needs_input":
            return res
        ask_user(st, res.get("questions", ["(agent asked for input)"]), f"Agent {name} needs input")


# ---------------- phases ----------------
def sh(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)


def phase_strategy(st, a):
    f = os.path.join(STRAT_DIR, f"{a.strategy}.py")
    if not os.path.exists(f):
        if not a.spec:
            ask_user(st, [f"{a.strategy}.py does not exist. Describe the strategy (or pass --spec file)."], "Strategy missing")
            spec = last_answer(st)
        else:
            spec = open(a.spec, encoding="utf-8").read()
        if a.no_agents:
            sys.exit("strategy file missing and --no-agents given")
        res = agent_with_input(st, "strategy-writer", f"Mode: write. File: strategies/{a.strategy}.py\nSpec:\n{spec}")
        log(f"strategy-writer: {res.get('status')} {res.get('artifacts')}")
    sys.path.insert(0, STRAT_DIR)
    mod = importlib.import_module(a.strategy)
    if not hasattr(mod, "Strategy"):
        sys.exit(f"{a.strategy}.py has no Strategy class (contract violation)")
    import inspect
    params = list(inspect.signature(mod.Strategy.__init__).parameters)
    if not {"client", "clock", "sleeper"} <= set(params):
        sys.exit(f"Strategy.__init__ must accept client, clock, sleeper (has {params})")
    if a.audit and not a.no_agents:
        res = agent_with_input(st, "strategy-writer", f"Mode: audit. File: strategies/{a.strategy}.py")
        log(f"audit: {res}")
    log("strategy OK (contract check passed)")


def phase_data(st, a):
    from script_openalgo.backtest_core import data_loader as dl
    if st["fresh"]:                                  # a date given via --answer at the data gate
        try:
            st["date"] = date.fromisoformat(st["fresh"].strip()).isoformat()
            st["fresh"] = None
        except ValueError:
            pass
    day = date.fromisoformat(st["date"]) if st["date"] else (date.fromisoformat(a.date) if a.date else dl.previous_trading_day())
    st["date"] = day.isoformat()
    log(f"target date {day}")
    m = dl.load_manifest(day, a.instrument)
    if not (m and m.get("complete")):
        m = dl.ensure_data(day, a.instrument)
    index_ok = m["series"].get("NIFTY", {}).get("rows", 0) >= dl.MIN_BARS_INDEX
    if not m["complete"] and not a.no_agents and index_ok:
        res = agent_with_input(st, "data-downloader", f"Target date: {day}. Manifest issues: {m['issues'][:10]}")
        m = dl.load_manifest(day, a.instrument)
        log(f"data-downloader: {res.get('status')} complete={res.get('complete')}")
    while not (m and m.get("complete")):
        ans = ask_user(st, [f"Data for {day} is incomplete: {(m or {}).get('issues', [])[:5]}. "
                            "Give another date (YYYY-MM-DD) or type 'abort'."], "Data gate failed")
        if ans.strip().lower().startswith("abort"):
            sys.exit("aborted by user at data gate")
        try:
            day = date.fromisoformat(ans.strip())
        except ValueError:
            continue
        st["date"] = day.isoformat()
        m = dl.load_manifest(day, a.instrument) or dl.ensure_data(day, a.instrument)
    log("data gate passed (manifest complete)")


def run_backtest(st, a):
    bt = os.path.join(STRAT_DIR, f"backtest_{a.strategy}.py")
    r = sh([sys.executable, bt, "--date", st["date"], "--instrument", a.instrument, "--no-fetch"], timeout=600)
    tail = (r.stdout or "")[-1500:]
    if r.returncode != 0:
        return False, (r.stderr or tail)[-1500:]
    dirs = sorted(glob.glob(os.path.join(ROOT, "results", f"{a.strategy}_{a.instrument.upper()}_{st['date']}_*")), key=os.path.getmtime)
    st["result_dir"] = dirs[-1] if dirs else None
    return True, tail


def phase_backtest(st, a):
    bt = os.path.join(STRAT_DIR, f"backtest_{a.strategy}.py")
    if not os.path.exists(bt):
        missing, used = scaffold.missing_client_methods(a.strategy)
        if not missing:                                  # the normal case: no AI needed, the file comes from a template
            scaffold.write_backtest(a.strategy)
            log(f"backtest file generated from the template (strategies/{os.path.basename(bt)}); no agent needed")
        else:
            if a.no_agents:
                sys.exit(f"{bt} missing and the strategy uses client methods FakeClient lacks: {missing}")
            log(f"strategy uses client methods FakeClient lacks: {missing}; asking backtest-writer to extend it")
            res = agent_with_input(st, "backtest-writer", f"Strategy file: strategies/{a.strategy}.py. Target date: {st['date']}. "
                                   f"FakeClient lacks these client methods: {missing}. Implement them in backtest_core/fake_client.py, "
                                   f"then create strategies/{os.path.basename(bt)} with `python -m script_openalgo.backtest_core.scaffold {a.strategy}`.")
            log(f"backtest-writer: {res.get('status')}")
    ok, out = run_backtest(st, a)
    if not ok:
        if a.no_agents:
            sys.exit(f"backtest failed:\n{out}")
        res = agent_with_input(st, "backtest-writer", f"The backtest crashed. Fix it.\nError:\n{out}")
        ok, out = run_backtest(st, a)
        if not ok:
            ask_user(st, [f"Backtest still failing:\n{out[-600:]}\nHow should I proceed?"], "Backtest failure")
            sys.exit("re-run after answering")
    log("backtest ran; results: " + str(st["result_dir"]))


def phase_review(st, a):
    """Deterministic review first (backtest_core/verify.py: free, instant). The AI reviewer is optional (--llm-review)."""
    while True:
        res = verify.verify(a.strategy, st["result_dir"], date.fromisoformat(st["date"]), a.instrument)
        for line in res["info"]:
            log("note: " + line)
        st["findings"] = res["findings"]
        if res["status"] == "pass":
            if a.llm_review and not a.no_agents:
                r2 = agent_with_input(st, "backtest-reviewer",
                                      f"Review strategies/{a.strategy}.py and strategies/backtest_{a.strategy}.py. Latest results: {st['result_dir']}. "
                                      f"Date {st['date']}. The deterministic checks (prices re-derived from candles, P&L reconciliation, static "
                                      f"contract checks) already PASSED; focus on strategy logic: lookahead, rule fidelity to the spec, obvious bugs.")
                if r2.get("status") != "pass":
                    res = {"status": "fail", "findings": r2.get("findings", []), "info": []}
                    st["findings"] = res["findings"]
                else:
                    st["verdict"] = "llm"
                    log("reviewer: PASS (llm)")
                    return
            else:
                st["verdict"] = "script"
                log("reviewer: PASS (script)")
                return
        st["review_round"] = st["review_round"] + 1
        log(f"reviewer: FAIL (round {st['review_round']}): {json.dumps(st['findings'])[:700]}")
        if a.no_agents:
            sys.exit("review failed and --no-agents is set; see findings above")
        if st["review_round"] >= MAX_REVIEW_ROUNDS:
            ans = ask_user(st, [f"Review still failing after {MAX_REVIEW_ROUNDS} rounds. Findings: {json.dumps(st['findings'])[:800]}. "
                                "Type 'accept' to continue anyway or give guidance."], "Review loop limit")
            if ans.strip().lower().startswith("accept"):
                st["verdict"] = "accepted-with-findings"
                return
            st["review_round"] = 0
        sp = scaffold.strategy_path(a.strategy)
        strat_f = [f for f in st["findings"] if str(f.get("where", "")).startswith(sp)]
        other_f = [f for f in st["findings"] if f not in strat_f]
        if strat_f:
            agent_with_input(st, "strategy-writer", f"Mode: audit. File: strategies/{a.strategy}.py. Fix ONLY these findings: {json.dumps(strat_f)}")
        if other_f:
            agent_with_input(st, "backtest-writer", f"Fix these review findings: {json.dumps(other_f)}")
        ok, out = run_backtest(st, a)
        if not ok:
            log(f"backtest crashed after fix:\n{out}")


def phase_learn(st, a):
    s = {}
    if st["result_dir"] and os.path.exists(os.path.join(st["result_dir"], "summary.json")):
        s = json.load(open(os.path.join(st["result_dir"], "summary.json")))
    line = (f"| {a.strategy}.py | backtest_{a.strategy}.py | run {datetime.now():%Y-%m-%d %H:%M} on {st['date']}: "
            f"trades {s.get('trades')}, gross P&L {s.get('gross_pnl')}, win rate {s.get('win_rate')}, "
            f"max DD {s.get('max_drawdown')} | {st['result_dir'] and os.path.basename(st['result_dir'])} | "
            f"{st.d.get('verdict') or 'UNREVIEWED'} |")
    with open(os.path.join(KNOW, "strategy_registry.md"), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    log("registry updated")


PHASE_FN = {"strategy": phase_strategy, "data": phase_data, "backtest": phase_backtest,
            "review": phase_review, "learn": phase_learn}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True, help="strategy module name, e.g. nifty_direction_ce_pe")
    ap.add_argument("--date")
    ap.add_argument("--instrument", default="NIFTY", help="NIFTY or BANKNIFTY")
    ap.add_argument("--spec", help="file with a plain-English strategy spec (only if the strategy file is missing)")
    ap.add_argument("--answer", help="answer to the pending NEEDS_INPUT question")
    ap.add_argument("--restart", action="store_true", help="ignore saved state")
    ap.add_argument("--no-agents", action="store_true", help="deterministic steps only; skip Claude agents")
    ap.add_argument("--audit", action="store_true", help="also run the strategy-writer audit")
    ap.add_argument("--llm-review", action="store_true", help="also run the AI reviewer after the script review passes (uses more Claude)")
    a = ap.parse_args()

    CURRENT["strategy"] = a.strategy
    st = State(a.strategy, a.restart)
    if a.answer is not None:
        if st["pending"] is None:
            log("no pending question; ignoring --answer")
        else:
            record_answer(st, a.answer)
    if st["phase"] == "done":
        st["phase"], st["review_round"] = "strategy", 0     # new run
    for ph in PHASES[PHASES.index(st["phase"]):-1]:
        st["phase"] = ph
        log(f"=== phase: {ph} ===")
        PHASE_FN[ph](st, a)
    st["phase"] = "done"
    log("DONE")


if __name__ == "__main__":
    main()
