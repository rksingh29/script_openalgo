"""Tiny launcher so a job survives a web-app restart.

    python runner.py <exit_code_file> <command...>

Runs the command (stdout/stderr are inherited, i.e. they go straight to the job log file the web app opened for us),
then writes the exit code to <exit_code_file>. The web app re-attaches to the log and exit-code file after a restart.
"""
import subprocess
import sys

if __name__ == "__main__":
    exit_file, cmd = sys.argv[1], sys.argv[2:]
    try:
        code = subprocess.call(cmd)
    except OSError as e:
        print(f"[runner] could not start command: {e}", flush=True)
        code = 127
    with open(exit_file, "w") as f:
        f.write(str(code))
    sys.exit(code)
