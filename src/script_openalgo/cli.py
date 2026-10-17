"""script-openalgo command line.

    script-openalgo [--host URL] [--home DIR] <command> ...

    init [DIR]         create a workspace (config, folders, agents, skill, example strategies)
    doctor             check the setup and whether the OpenAlgo server is reachable
    fetch              fetch one day of candles            (data_loader options: --instrument --date --chain --force)
    backfill           fetch the whole listed life of the nearest contract
    multiday           run strategies over many cached days
    verify             script review of one backtest result
    bias-report        CPR / PCR bias of backtested trades
    scaffold SLUG      generate strategies/backtest_<SLUG>.py
    orchestrate        run the build/test pipeline for one strategy
    webapp             start the web app (http://127.0.0.1:5055)
    db                 strategy database commands (list, add, merge, register ...)
    sync               register strategy files and import result folders into the web app's database
    export / import    back up / restore EVERYTHING in the workspace as one zip

--host and --home apply to every command and to the processes they start. Defaults: host http://127.0.0.1:5000, home = the nearest
folder (from here upwards) that has script_openalgo.toml, else the current folder.
"""
import argparse
import os
import runpy
import sys

from script_openalgo import __version__, config

MODULES = {
    "fetch": "script_openalgo.backtest_core.data_loader",
    "backfill": "script_openalgo.backtest_core.backfill",
    "multiday": "script_openalgo.backtest_core.multiday",
    "verify": "script_openalgo.backtest_core.verify",
    "bias-report": "script_openalgo.backtest_core.bias_report",
    "scaffold": "script_openalgo.backtest_core.scaffold",
    "orchestrate": "script_openalgo.orchestrator",
    "webapp": "script_openalgo.webapp.app",
    "db": "script_openalgo.strategy_db.cli",
}


def _run_module(name, args):
    sys.argv = [name, *args]
    runpy.run_module(MODULES[name.split()[-1]] if name.split()[-1] in MODULES else name, run_name="__main__", alter_sys=True)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    p = argparse.ArgumentParser(prog="script-openalgo", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, add_help=True)
    p.add_argument("--version", action="version", version=f"script_openalgo {__version__}")
    p.add_argument("--host", help="OpenAlgo server URL, e.g. http://192.168.1.20:5000 (default: OPENALGO_HOST, workspace config, then http://127.0.0.1:5000)")
    p.add_argument("--home", help="workspace folder (default: nearest folder with script_openalgo.toml, else the current folder)")
    p.add_argument("command", nargs="?", help="one of: init doctor sync " + " ".join(MODULES) + " export import")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    # global options may also appear after the command: pull them out first
    pre, rest, i = [], [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("--host", "--home") and i + 1 < len(argv):
            pre += [a, argv[i + 1]]
            i += 2
        elif a.startswith(("--host=", "--home=")):
            pre.append(a)
            i += 1
        else:
            rest.append(a)
            i += 1
    args = p.parse_args(pre + rest)
    if args.home:
        config.set_home(args.home)
    if args.host:
        config.set_host(args.host)
    cmd, tail = args.command, args.rest
    if not cmd:
        p.print_help()
        return 0

    if cmd == "init":
        from script_openalgo import workspace
        ip = argparse.ArgumentParser(prog="script-openalgo init")
        ip.add_argument("folder", nargs="?", default=".")
        ip.add_argument("--no-examples", action="store_true", help="do not copy the example strategies")
        ia = ip.parse_args(tail)
        r = workspace.init_workspace(ia.folder, args.host, not ia.no_examples)
        print(f"Workspace ready: {r['folder']}")
        for x in r["created"]:
            print("  created", x)
        for x in r["kept"]:
            print("  kept   ", x)
        print(f"\nNext: cd \"{r['folder']}\" ; copy .env.example to .env and put your OpenAlgo API key in it ;\n"
              f"      script-openalgo doctor ;   script-openalgo --host http://<openalgo-host>:5000 fetch --instrument ALL")
        return 0
    if cmd == "doctor":
        from script_openalgo import workspace
        lines, ok = workspace.doctor(args.host)
        print("\n".join(lines))
        return 0 if ok else 1
    if cmd == "sync":
        from script_openalgo.strategy_db import db
        con = db.connect()
        r = db.sync_from_disk(con)
        con.close()
        print(f"Registered {r['registered']} strategies, imported {r['runs_imported']} results into the database of {config.home()}")
        return 0
    if cmd == "export":
        from script_openalgo import backup
        ep = argparse.ArgumentParser(prog="script-openalgo export")
        ep.add_argument("--out", help="zip file to write (default: <workspace>/backups/script_openalgo_backup_<time>.zip)")
        ea = ep.parse_args(tail)
        r = backup.export_workspace(ea.out)
        print(f"Exported {r['files']} files ({r['bytes'] / 1024 / 1024:.1f} MB, zip {r['zip_bytes'] / 1024 / 1024:.1f} MB) to {r['path']}")
        for n in r["notes"]:
            print("  note:", n)
        print("The API key (.env) is NOT included. Counts:", r["workspace_counts"])
        return 0
    if cmd == "import":
        from script_openalgo import backup
        ip = argparse.ArgumentParser(prog="script-openalgo import")
        ip.add_argument("zip")
        ip.add_argument("--overwrite", action="store_true", help="replace local files that differ (databases are always replaced)")
        ip.add_argument("--no-safety-backup", action="store_true")
        ip.add_argument("--dry-run", action="store_true", help="verify and report only; change nothing")
        ia = ip.parse_args(tail)
        try:
            r = backup.import_workspace(ia.zip, overwrite=ia.overwrite, safety_backup=not ia.no_safety_backup, dry_run=ia.dry_run)
        except backup.ImportError_ as e:
            print("Import refused:", e)
            return 1
        print(("DRY RUN (nothing changed): " if r["dry_run"] else "Imported: ") + f"{r['verified_files']} files verified; "
              f"{r['written']} written, {r['identical_skipped']} already identical, {len(r['conflicts'])} conflicts")
        for c in r["conflicts"][:20]:
            print("  conflict (kept local; use --overwrite to replace):", c)
        if r["databases_replaced"]:
            print("  databases replaced:", ", ".join(r["databases_replaced"]))
        if r["safety_backup"]:
            print("  safety backup of the previous state:", r["safety_backup"])
        return 0
    if cmd in MODULES:
        os.environ.setdefault("PYTHONUTF8", "1")
        _run_module(f"script-openalgo {cmd}", tail)
        return 0
    p.error(f"unknown command {cmd!r}")


if __name__ == "__main__":
    sys.exit(main())
