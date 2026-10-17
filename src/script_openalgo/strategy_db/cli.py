"""
CLI used by the strategy-researcher agent (and by humans) to read/write the strategy DB.
The database enforces uniqueness; the agent cannot bypass it.

  python -m script_openalgo.strategy_db.cli list                      # compact list of everything known (names, aliases, family, status)
  python -m script_openalgo.strategy_db.cli add spec.json [--force-new] [--job N]
  python -m script_openalgo.strategy_db.cli merge <id> spec.json [--job N]
  python -m script_openalgo.strategy_db.cli count-new --job N          # how many were added by this job so far
  python -m script_openalgo.strategy_db.cli register <slug> "<Name>"   # register a hand-written <slug>.py from the project folder

Prints one JSON object per call.
"""
import argparse
import json
import sys

from . import db


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    a = sub.add_parser("add")
    a.add_argument("file")
    a.add_argument("--force-new", action="store_true")
    a.add_argument("--job", type=int)
    m = sub.add_parser("merge")
    m.add_argument("id", type=int)
    m.add_argument("file")
    m.add_argument("--job", type=int)
    r = sub.add_parser("register")
    r.add_argument("slug")
    r.add_argument("name")
    r.add_argument("--family", default="user")
    c = sub.add_parser("count-new")
    c.add_argument("--job", type=int, required=True)
    args = ap.parse_args()
    con = db.connect()
    if args.cmd == "list":
        out = []
        for r in db.rows(con, "SELECT id,name,aliases,family,status,testable FROM strategies"):
            out.append({"id": r["id"], "name": r["name"], "aliases": json.loads(r["aliases"]), "family": r["family"],
                        "status": r["status"], "testable": r["testable"]})
        dead = db.rows(con, "SELECT name FROM tombstones")
        print(json.dumps({"strategies": out, "previously_deleted": [d["name"] for d in dead],
                          "required_spec_fields": db.REQUIRED}))
    elif args.cmd in ("add", "merge"):
        with open(args.file, encoding="utf-8") as f:
            spec = json.load(f)
        if args.cmd == "add":
            res = db.add_strategy(con, spec, job_id=args.job, force_new=args.force_new)
        else:
            res = db.merge_into(con, args.id, spec, job_id=args.job)
        print(json.dumps(res))
    elif args.cmd == "register":
        print(json.dumps(db.register_user_strategy(con, args.slug, args.name, args.family)))
    else:
        n = con.execute("SELECT COUNT(*) FROM research_log WHERE job_id=? AND action='added'", (args.job,)).fetchone()[0]
        print(json.dumps({"added_so_far": n}))


if __name__ == "__main__":
    sys.exit(main())
