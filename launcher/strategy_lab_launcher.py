"""Strategy Lab launcher: a small Windows GUI (tkinter) around the script-openalgo command line.

  Setup    create the venv if it does not exist, install / update the script_openalgo wheel, create the workspace, doctor
  Web app  start / stop the Strategy Lab web app, open it in the browser, live log
  Data     what is cached per instrument; fetch a day, backfill, next-expiry options, option-chain snapshot, sync,
           rebuild the journal, backup / restore

Built into one exe by launcher/build_exe.py (PyInstaller); the package file (script_openalgo-*.whl / .tar.gz) is kept
NEXT TO the exe, so a new version is installed by dropping the new file there (Update package). Settings are kept in
%APPDATA%\\StrategyLab\\launcher.json.
The launcher never trades and never touches the API key (the workspace's .env / OPENALGO_API_KEY are used as they are).
"""
import glob
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import date, timedelta
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP = "Strategy Lab launcher"
CFG_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "StrategyLab")
CFG_FILE = os.path.join(CFG_DIR, "launcher.json")
INSTRUMENTS = ["NIFTY", "BANKNIFTY", "SENSEX", "RELIANCE", "OFSS"]
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
KEY_NAME = "OPENALGO_API_KEY"


def app_dir():
    """The folder of the launcher: next to the exe (frozen) or of this script. The package files live here:
    script_openalgo-<version>-py3-none-any.whl / script_openalgo-<version>.tar.gz, optionally a wheelhouse folder."""
    return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


def defaults():
    """Everything next to the launcher: <folder>\\venv, <folder>\\my_workspace, package files in <folder>."""
    here = app_dir()
    return {"python": "",                                 # base Python used to create the venv (empty = auto)
            "venv": os.path.join(here, "venv"),
            "workspace": os.path.join(here, "my_workspace"),
            "source": here,                               # folder with the package files (or a repo: its dist is searched too)
            "host": "http://127.0.0.1:5000",
            "port": "5055",
            "lan": "0"}                                   # "1" = other devices on the network may open the web app


def env_file(workspace):
    return os.path.join(workspace, ".env")


def read_key(workspace, name=KEY_NAME):
    """A value (OPENALGO_API_KEY by default) from <workspace>\\.env (what Strategy Lab reads), else None. Never logged."""
    try:
        with open(env_file(workspace), encoding="utf-8") as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and k.strip() == name:
                    return v.strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def write_key(workspace, key, name=KEY_NAME):
    """Set (or with key=None remove) a value (OPENALGO_API_KEY by default) in <workspace>\\.env, keeping its other lines."""
    p = env_file(workspace)
    lines = []
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip().partition("=")[0].strip() != name]
    if key:
        lines.append(f"{name}={key}")
    os.makedirs(workspace, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


def bundle_dir():
    """Optional `wheelhouse` folder next to the launcher (dependency wheels from build_exe.py --wheelhouse) for an
    OFFLINE install on the Python version written in its bundle.json. None if there is none."""
    d = os.path.join(app_dir(), "wheelhouse")
    return d if os.path.isdir(d) else None


def bundle_info():
    d = bundle_dir()
    try:
        with open(os.path.join(d, "bundle.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return {}


def wheel_version(path):
    """(0, 1, 1) from script_openalgo-0.1.1-py3-none-any.whl (unknown parts -> 0)."""
    name = os.path.basename(path)
    for ext in (".tar.gz", ".whl", ".zip"):
        if name.endswith(ext):
            name = name[: -len(ext)]
    try:
        return tuple(int(x) if x.isdigit() else 0 for x in name.split("-")[1].split("."))
    except IndexError:
        return (0,)


def python_tag(py):
    """'3.14' for a python.exe (None if it cannot be run)."""
    if not py:
        return None
    try:
        out = subprocess.run([py, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
                             capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout.strip()
        return out or None
    except (OSError, subprocess.SubprocessError):
        return None


def load_cfg():
    """Saved settings over the defaults (the launcher's own folder); empty saved values fall back to the default."""
    cfg = defaults()
    try:
        with open(CFG_FILE, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if v not in (None, "") or k == "python"})
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg(cfg):
    os.makedirs(CFG_DIR, exist_ok=True)
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=1)


def find_python(cfg):
    """A real Python to create the venv with (the exe itself cannot): the setting, else py -3, else python on PATH."""
    if cfg.get("python") and os.path.exists(cfg["python"]):
        return cfg["python"]
    for cmd in (["py", "-3", "-c", "import sys; print(sys.executable)"], ["python", "-c", "import sys; print(sys.executable)"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW).stdout.strip()
            if out and os.path.exists(out) and "WindowsApps" not in out:
                return out
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP)
        self.geometry("980x680")
        self.minsize(820, 560)
        self.cfg = load_cfg()
        self.q = queue.Queue()                        # (widget, text) lines from worker threads
        self.task = None                              # the running Setup / Data command (one at a time)
        self.web = None                               # the web app process started here
        self.vars = {k: tk.StringVar(value=v) for k, v in self.cfg.items()}
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=6, pady=6)
        self.tab_setup(nb)
        self.tab_web(nb)
        self.tab_data(nb)
        self.status = tk.StringVar()
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=8, pady=(0, 6))
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(150, self.pump)
        self.after(500, self.refresh_status)

    # ---------------- paths ----------------
    def v(self, k):
        return self.vars[k].get().strip()

    def vpy(self):
        return os.path.join(self.v("venv"), "Scripts", "python.exe")

    def cli(self, *args):
        """script-openalgo inside the venv, with this workspace and OpenAlgo host."""
        return [self.vpy(), "-m", "script_openalgo", "--home", self.v("workspace"), "--host", self.v("host"), *args]

    def venv_env(self, **extra):
        """Environment with the venv first on PATH (+ VIRTUAL_ENV), so `python` / `script-openalgo` in anything the web
        app starts (jobs, Claude agents) is the venv's, never another Python that lacks script_openalgo."""
        scripts = os.path.dirname(self.vpy())
        path = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p and os.path.normcase(p) != os.path.normcase(scripts)]
        return dict(os.environ, PATH=os.pathsep.join([scripts] + path), VIRTUAL_ENV=self.v("venv"),
                    PYTHONUTF8="1", PYTHONUNBUFFERED="1", **extra)

    def wheels(self):
        """Candidate package files: next to the launcher, in its wheelhouse, and in the repo's dist (if the repo exists)."""
        out, seen = [], set()
        for folder, where in ((app_dir(), "next to the launcher"), (bundle_dir(), "wheelhouse"),
                              (os.path.join(self.v("source"), "dist"), "repo dist")):
            if not folder:
                continue
            for pat in ("script_openalgo-*.whl", "script_openalgo-*.tar.gz"):
                for w in glob.glob(os.path.join(folder, pat)):
                    if os.path.normcase(os.path.abspath(w)) not in seen:
                        seen.add(os.path.normcase(os.path.abspath(w)))
                        out.append((w, where))
        return out

    def wheel(self):
        """The newest version; for the same version a .whl before a .tar.gz (no build step), then the newest file."""
        c = self.wheels()
        if not c:
            return None
        return max(c, key=lambda x: (wheel_version(x[0]), x[0].endswith(".whl"), os.path.getmtime(x[0])))[0]

    def install_cmds(self, py_for_tag, extra):
        """pip install of the chosen wheel: OFFLINE from the bundled wheelhouse when it matches this Python, else online
        (PyPI) with the wheelhouse as an extra source."""
        w, d, info = self.wheel(), bundle_dir(), bundle_info()
        if not w:
            return None, None
        base = [self.vpy(), "-m", "pip", "install", *extra]
        if d and info.get("python") and info["python"] == python_tag(py_for_tag):
            return base + ["--no-index", "--find-links", d, w], base + ["--find-links", d, w]
        return base + (["--find-links", d] if d else []) + [w], None

    def persist(self):
        self.cfg.update({k: self.v(k) for k in self.vars})
        save_cfg(self.cfg)

    # ---------------- output ----------------
    def make_log(self, parent, height=14):
        fr = ttk.Frame(parent)
        t = tk.Text(fr, height=height, wrap="none", font=("Consolas", 9), bg="#111", fg="#ddd", insertbackground="#ddd")
        sb = ttk.Scrollbar(fr, command=t.yview)
        t.configure(yscrollcommand=sb.set, state="disabled")
        t.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return fr, t

    def write(self, widget, text):
        self.q.put((widget, text))

    def later(self, fn):
        """Run fn on the Tk thread (worker threads must not call Tk themselves)."""
        self.q.put((None, fn))

    def pump(self):
        try:
            while True:
                w, text = self.q.get_nowait()
                if w is None:
                    text()
                    continue
                w.configure(state="normal")
                w.insert("end", text if text.endswith("\n") else text + "\n")
                if int(w.index("end-1c").split(".")[0]) > 4000:
                    w.delete("1.0", "1000.0")
                w.see("end")
                w.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self.pump)

    # ---------------- running commands ----------------
    def run(self, title, cmds, log, done=None, cwd=None):
        """Run commands one after the other in a thread (stop at the first failure), streaming output into `log`."""
        if self.task and self.task.poll() is None:
            messagebox.showinfo(APP, "Another command is still running. Wait for it, or press Stop.")
            return
        self.persist()
        wd = cwd or (self.v("workspace") if os.path.isdir(self.v("workspace")) else None)   # Tk values: read here, not in the thread
        env = self.venv_env() if os.path.exists(self.vpy()) else dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")

        def work():
            ok = True
            for cmd in cmds:
                self.write(log, f"\n> {title}: {' '.join(cmd)}")
                try:
                    self.task = subprocess.Popen(cmd, cwd=wd,
                                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                                 text=True, encoding="utf-8", errors="replace", env=env, creationflags=NO_WINDOW)
                except OSError as e:
                    self.write(log, f"cannot start: {e}")
                    ok = False
                    break
                for line in self.task.stdout:
                    self.write(log, line.rstrip("\n"))
                code = self.task.wait()
                self.write(log, f"[exit code {code}]")
                if code != 0:
                    ok = False
                    break
            self.write(log, f"=== {title}: {'done' if ok else 'FAILED'} ===")
            self.task = None
            if done:
                self.later(lambda: done(ok))
            self.later(self.refresh_status)
        threading.Thread(target=work, daemon=True).start()

    def stop_task(self):
        if self.task and self.task.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.task.pid)], capture_output=True, creationflags=NO_WINDOW)

    # ---------------- Setup tab ----------------
    def tab_setup(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="  Setup  ")
        form = ttk.LabelFrame(tab, text="Folders and server", padding=8)
        form.pack(fill="x")
        rows = [("python", "Base Python (empty = auto: py -3 / python)", "file"), ("venv", "Virtual environment", "dir"),
                ("workspace", "Workspace (data, results, strategies)", "dir"), ("source", "Package folder / source repo", "dir"),
                ("host", "OpenAlgo server", None), ("port", "Web app port", None)]
        for i, (k, label, kind) in enumerate(rows):
            ttk.Label(form, text=label).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(form, textvariable=self.vars[k], width=70).grid(row=i, column=1, sticky="we", padx=4)
            if kind:
                ttk.Button(form, text="…", width=3, command=lambda k=k, kind=kind: self.browse(k, kind)).grid(row=i, column=2)
        ttk.Button(form, text="Use this folder for all", command=self.reset_paths).grid(row=0, column=3, padx=4)
        form.columnconfigure(1, weight=1)
        # ---- OpenAlgo API key (stored ONLY in <workspace>\\.env, where Strategy Lab reads it) ----
        kf = ttk.LabelFrame(tab, text="OpenAlgo API key (OpenAlgo web page > API Key)", padding=8)
        kf.pack(fill="x", pady=(6, 0))
        self.key_var, self.key_show = tk.StringVar(), tk.BooleanVar(value=False)
        self.key_entry = ttk.Entry(kf, textvariable=self.key_var, width=60, show="•")
        self.key_entry.pack(side="left", padx=(0, 4))
        ttk.Checkbutton(kf, text="show", variable=self.key_show,
                        command=lambda: self.key_entry.configure(show="" if self.key_show.get() else "•")).pack(side="left")
        ttk.Button(kf, text="Save key", command=self.save_key).pack(side="left", padx=4)
        ttk.Button(kf, text="Remove key", command=self.remove_key).pack(side="left", padx=2)
        ttk.Button(kf, text="Test (doctor)", command=lambda: self.run("doctor", [self.cli("doctor")], self.setup_log)).pack(side="left", padx=2)
        self.key_info = tk.StringVar()
        ttk.Label(kf, textvariable=self.key_info, foreground="#245").pack(side="left", padx=8)
        # ---- LAN access (other devices on the network): only with an access token ----
        lf = ttk.LabelFrame(tab, text="Network access (open Strategy Lab from a phone / another PC on this network)", padding=8)
        lf.pack(fill="x", pady=(6, 0))
        self.lan_var = tk.BooleanVar(value=self.cfg.get("lan") == "1")
        ttk.Checkbutton(lf, text="allow other devices", variable=self.lan_var, command=self.toggle_lan).pack(side="left")
        ttk.Button(lf, text="New access token", command=self.new_token).pack(side="left", padx=6)
        self.lan_info = tk.StringVar()
        ttk.Label(lf, textvariable=self.lan_info, foreground="#245").pack(side="left", padx=8)
        ttk.Button(lf, text="Copy link", command=self.copy_lan_link).pack(side="right")
        self.setup_info = tk.StringVar()
        ttk.Label(tab, textvariable=self.setup_info, foreground="#245", justify="left").pack(fill="x", pady=6)
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        ttk.Button(bar, text="1. Create venv (if missing) + install", command=self.create_venv).pack(side="left", padx=2)
        ttk.Button(bar, text="Update package (newest wheel)", command=self.update_pkg).pack(side="left", padx=2)
        ttk.Button(bar, text="Create workspace (if missing)", command=self.init_ws).pack(side="left", padx=2)
        ttk.Button(bar, text="Doctor", command=lambda: self.run("doctor", [self.cli("doctor")], self.setup_log)).pack(side="left", padx=2)
        ttk.Button(bar, text="Save settings", command=lambda: (self.persist(), self.refresh_status())).pack(side="left", padx=2)
        ttk.Button(bar, text="Stop", command=self.stop_task).pack(side="right", padx=2)
        fr, self.setup_log = self.make_log(tab)
        fr.pack(fill="both", expand=True, pady=(6, 0))

    def reset_paths(self):
        d = defaults()
        if not messagebox.askyesno(APP, "Use the launcher's folder for everything?\n\n" + "\n".join(
                f"{k}: {d[k]}" for k in ("venv", "workspace", "source"))):
            return
        for k in ("venv", "workspace", "source"):
            self.vars[k].set(d[k])
        self.persist()
        self.refresh_status()

    def lan_hosts(self):
        try:
            return [ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if not ip.startswith("127.")]
        except OSError:
            return []

    def toggle_lan(self):
        if self.lan_var.get():
            if not messagebox.askyesno(APP, "Allow other devices on this network to open Strategy Lab?\n\nStrategy Lab has no login: "
                                       "anyone with the access token can start auto-trade and send REAL orders. Use it only on your "
                                       "own network, keep the token private. Windows may ask to allow Python through the firewall "
                                       "(choose Private networks)."):
                self.lan_var.set(False)
                return
            if not read_key(self.v("workspace"), "STRATEGY_LAB_TOKEN"):
                self.new_token(ask=False)
        self.cfg["lan"] = "1" if self.lan_var.get() else "0"
        self.persist()
        self.refresh_status()
        if self.web and self.web.poll() is None:
            messagebox.showinfo(APP, "Restart the web app (Web app tab: Stop, Start) to apply.")

    def new_token(self, ask=True):
        import secrets
        if ask and read_key(self.v("workspace"), "STRATEGY_LAB_TOKEN") and not messagebox.askyesno(
                APP, "Replace the access token? Devices that used the old one must log in again."):
            return
        write_key(self.v("workspace"), secrets.token_urlsafe(12), "STRATEGY_LAB_TOKEN")
        self.write(self.setup_log, f"new access token saved in {env_file(self.v('workspace'))} (Copy link to send it to your device)")
        self.refresh_status()

    def lan_link(self):
        tok, ips = read_key(self.v("workspace"), "STRATEGY_LAB_TOKEN"), self.lan_hosts()
        ip = next((i for i in ips if i.startswith("192.168.") and not i.startswith("192.168.56.")), ips[0] if ips else "<this-pc-ip>")
        return f"http://{ip}:{self.v('port')}/?token={tok}" if tok else None

    def copy_lan_link(self):
        link = self.lan_link()
        if not link:
            messagebox.showinfo(APP, "No access token yet: press New access token.")
            return
        self.clipboard_clear()
        self.clipboard_append(link)
        messagebox.showinfo(APP, "Link with the token copied. Open it once on the other device; it remembers the token.")

    def save_key(self):
        key = self.key_var.get().strip()
        if not key:
            messagebox.showinfo(APP, "Paste the API key from OpenAlgo (its web page > API Key) first.")
            return
        if len(key) < 16 or any(c.isspace() for c in key):
            if not messagebox.askyesno(APP, "That does not look like an OpenAlgo API key (too short or has spaces). Save it anyway?"):
                return
        ws = self.v("workspace")
        try:
            write_key(ws, key)
        except OSError as e:
            messagebox.showerror(APP, f"Cannot write {env_file(ws)}: {e}")
            return
        self.key_var.set("")
        self.write(self.setup_log, f"API key saved in {env_file(ws)} (…{key[-4:]}). Restart the web app if it is running.")
        self.refresh_status()

    def remove_key(self):
        ws = self.v("workspace")
        if read_key(ws) and messagebox.askyesno(APP, f"Remove the API key from {env_file(ws)}?"):
            write_key(ws, None)
            self.refresh_status()

    def browse(self, k, kind):
        p = filedialog.askdirectory(initialdir=self.v(k) or "C:\\") if kind == "dir" else \
            filedialog.askopenfilename(filetypes=[("python.exe", "python.exe"), ("all", "*.*")])
        if p:
            self.vars[k].set(os.path.normpath(p))
            self.persist()
            self.refresh_status()

    def create_venv(self):
        venv, cmds = self.v("venv"), []
        if not os.path.exists(self.vpy()):
            py = find_python(self.cfg | {"python": self.v("python")})
            if not py:
                messagebox.showerror(APP, "No Python found to create the venv. Install Python 3.11+ (python.org) or set 'Base Python'.")
                return
            cmds.append([py, "-m", "venv", venv])
        else:
            self.write(self.setup_log, f"venv exists: {venv}")
        py = self.vpy() if os.path.exists(self.vpy()) else find_python(self.cfg | {"python": self.v("python")})
        first, fallback = self.install_cmds(py, [])
        if not first:
            messagebox.showerror(APP, f"No script_openalgo package file: put script_openalgo-<version>-py3-none-any.whl (or .tar.gz) next to the launcher ({app_dir()}), or set the source repo.")
            return
        cmds.append(first)
        self.write(self.setup_log, "installing " + ("OFFLINE from the wheelhouse next to the launcher" if "--no-index" in first else "dependencies from PyPI (internet)")
                   + f": {os.path.basename(self.wheel())}")

        def done(ok):
            if not ok and fallback:
                self.write(self.setup_log, "offline install failed: trying again online (PyPI)")
                self.run("install (online)", [fallback], self.setup_log)
        self.run("create venv + install", cmds, self.setup_log, done=done, cwd=os.path.dirname(self.vpy()) if os.path.exists(self.vpy()) else None)

    def update_pkg(self):
        w = self.wheel()
        if not os.path.exists(self.vpy()):
            messagebox.showinfo(APP, "The venv does not exist yet: use '1. Create venv'.")
            return
        if not w:
            messagebox.showerror(APP, f"No script_openalgo package file: put script_openalgo-<version>-py3-none-any.whl (or .tar.gz) next to the launcher ({app_dir()}), or set the source repo.")
            return
        if self.web and self.web.poll() is None:
            if not messagebox.askyesno(APP, "The web app is running from this venv. Stop it to update?"):
                return
            self.stop_web()
        self.write(self.setup_log, f"updating to {os.path.basename(w)} ({dict((x, y) for x, y in self.wheels()).get(w)})")
        self.run("update package", [[self.vpy(), "-m", "pip", "install", "--force-reinstall", "--no-deps", w]], self.setup_log)

    def init_ws(self):
        ws = self.v("workspace")
        if os.path.exists(os.path.join(ws, "script_openalgo.toml")):
            messagebox.showinfo(APP, f"The workspace already exists:\n{ws}")
            return
        self.run("create workspace", [[self.vpy(), "-m", "script_openalgo", "init", ws]], self.setup_log, cwd=os.path.dirname(ws) or None)

    def package_version(self):
        if not os.path.exists(self.vpy()):
            return None
        try:
            out = subprocess.run([self.vpy(), "-c", "import importlib.metadata as m; print(m.version('script_openalgo'))"],
                                 capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
            return out.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None

    # ---------------- Web app tab ----------------
    def tab_web(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="  Web app  ")
        bar = ttk.Frame(tab)
        bar.pack(fill="x")
        self.web_state = tk.StringVar(value="stopped")
        ttk.Button(bar, text="▶ Start web app", command=self.start_web).pack(side="left", padx=2)
        ttk.Button(bar, text="■ Stop", command=self.stop_web).pack(side="left", padx=2)
        ttk.Button(bar, text="Open in browser", command=lambda: webbrowser.open(f"http://127.0.0.1:{self.v('port')}/")).pack(side="left", padx=2)
        ttk.Label(bar, textvariable=self.web_state, font=("Segoe UI", 10, "bold")).pack(side="left", padx=12)
        self.auto_open = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="open the browser when it is up", variable=self.auto_open).pack(side="right")
        ttk.Label(tab, foreground="#555", text="Stop ends the web app only: jobs it started (tests, signals, auto-trade) keep running "
                  "and are picked up again on the next start. Stop auto-trade from its Trade tab first.").pack(fill="x", pady=4)
        fr, self.web_log = self.make_log(tab, height=24)
        fr.pack(fill="both", expand=True)

    def start_web(self):
        if self.web and self.web.poll() is None:
            webbrowser.open(f"http://127.0.0.1:{self.v('port')}/")
            return
        if port_open(self.v("port")):
            if messagebox.askyesno(APP, f"Something already answers on port {self.v('port')} (another Strategy Lab?). Open it in the browser?"):
                webbrowser.open(f"http://127.0.0.1:{self.v('port')}/")
            return
        if not os.path.exists(self.vpy()):
            messagebox.showinfo(APP, "The venv does not exist yet: Setup tab, '1. Create venv'.")
            return
        self.persist()
        lan = self.lan_var.get()
        env = self.venv_env(STRATEGY_LAB_PORT=self.v("port"), STRATEGY_LAB_BIND="0.0.0.0" if lan else "127.0.0.1")
        try:
            self.web = subprocess.Popen(self.cli("webapp"), cwd=self.v("workspace"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", env=env,
                                        creationflags=NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        except OSError as e:
            messagebox.showerror(APP, f"Cannot start the web app: {e}")
            return
        self.write(self.web_log, f"> {' '.join(self.cli('webapp'))}  (port {self.v('port')})")
        proc = self.web

        def reader():
            for line in proc.stdout:
                self.write(self.web_log, line.rstrip("\n"))
            self.write(self.web_log, f"[web app ended, exit code {proc.wait()}]")
        threading.Thread(target=reader, daemon=True).start()

        port, auto = self.v("port"), self.auto_open.get()       # read Tk values here, not in the thread

        def opener():
            for _ in range(60):
                if proc.poll() is not None:
                    return
                if port_open(port):
                    if auto:
                        webbrowser.open(f"http://127.0.0.1:{port}/")
                    self.later(self.refresh_status)
                    return
                time.sleep(0.5)
        threading.Thread(target=opener, daemon=True).start()

    def stop_web(self):
        if self.web and self.web.poll() is None:
            self.web.terminate()                          # the app only (not /T): its job processes keep running
            try:
                self.web.wait(10)
            except subprocess.TimeoutExpired:
                self.web.kill()
            self.write(self.web_log, "[stopped]")
        elif port_open(self.v("port")):
            messagebox.showinfo(APP, f"The web app on port {self.v('port')} was not started from this launcher; close it where it runs.")
        self.refresh_status()

    # ---------------- Data tab ----------------
    def tab_data(self, nb):
        tab = ttk.Frame(nb, padding=8)
        nb.add(tab, text="  Data  ")
        top = ttk.Frame(tab)
        top.pack(fill="x")
        ttk.Label(top, text="Instrument").pack(side="left")
        self.d_inst = ttk.Combobox(top, values=INSTRUMENTS + ["ALL"], width=12)
        self.d_inst.set("NIFTY")
        self.d_inst.pack(side="left", padx=4)
        ttk.Label(top, text="Date").pack(side="left")
        self.d_date = ttk.Entry(top, width=12)
        self.d_date.insert(0, "")
        self.d_date.pack(side="left", padx=4)
        ttk.Label(top, text="(empty = previous trading day)", foreground="#666").pack(side="left")
        ttk.Label(top, text="   Days").pack(side="left")
        self.d_days = ttk.Spinbox(top, from_=1, to=120, width=5)
        self.d_days.set("30")
        self.d_days.pack(side="left", padx=4)
        ttk.Button(top, text="Refresh overview", command=self.refresh_data).pack(side="right")
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=6)
        acts = [("Fetch day", self.fetch_day), ("Backfill contract life", self.backfill), ("Next-expiry options (days)", self.next_expiry),
                ("Option-chain snapshot", self.chain), ("Sync results", lambda: self.run("sync", [self.cli("sync")], self.data_log)),
                ("Rebuild journal", lambda: self.run("journal", [self.cli("journal", "build")], self.data_log)),
                ("Backup (export)", self.backup), ("Restore (import)…", self.restore), ("Open data folder", self.open_cache)]
        for text, fn in acts:
            ttk.Button(bar, text=text, command=fn).pack(side="left", padx=2)
        ttk.Button(bar, text="Stop", command=self.stop_task).pack(side="right", padx=2)
        cols = ("inst", "days", "complete", "first", "last", "nextexp", "size")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings", height=7)
        for c, t, w in zip(cols, ("Instrument", "Cached days", "Complete", "First day", "Last day", "Next-expiry days", "Size MB"),
                           (110, 90, 80, 100, 100, 110, 80)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.tree.selection() and self.d_inst.set(self.tree.item(self.tree.selection()[0])["values"][0]))
        ttk.Label(tab, foreground="#555", text="Expired option contracts cannot be fetched again: back up the data folder regularly "
                  "(Backup). Fetching needs OpenAlgo running and the broker logged in.").pack(fill="x", pady=4)
        fr, self.data_log = self.make_log(tab, height=12)
        fr.pack(fill="both", expand=True)
        self.after(800, self.refresh_data)

    def inst(self):
        return (self.d_inst.get() or "NIFTY").strip().upper()

    def fetch_day(self):
        args = ["fetch", "--instrument", self.inst()]
        if self.d_date.get().strip():
            try:
                date.fromisoformat(self.d_date.get().strip())
            except ValueError:
                messagebox.showerror(APP, "Date as YYYY-MM-DD (or empty for the previous trading day).")
                return
            args += ["--date", self.d_date.get().strip()]
        self.run(f"fetch {self.inst()}", [self.cli(*args)], self.data_log, done=lambda ok: self.refresh_data())

    def backfill(self):
        if messagebox.askyesno(APP, f"Backfill {self.inst()}: every day since the nearest contract was listed (60-100 API calls)?"):
            self.run(f"backfill {self.inst()}", [self.cli("backfill", "--instrument", self.inst())], self.data_log, done=lambda ok: self.refresh_data())

    def next_expiry(self):
        days = self.d_days.get() or "30"
        if messagebox.askyesno(APP, f"Fetch the NEXT expiry's options for every cached {self.inst()} day of the last {days} days?"):
            self.run(f"next expiry {self.inst()}", [self.cli("fetch", "--instrument", self.inst(), "--next-expiry", "--days", days)],
                     self.data_log, done=lambda ok: self.refresh_data())

    def chain(self):
        self.run(f"option chain {self.inst()}", [self.cli("fetch", "--instrument", self.inst(), "--chain")], self.data_log)

    def backup(self):
        self.run("backup", [self.cli("export")], self.data_log)

    def restore(self):
        p = filedialog.askopenfilename(initialdir=os.path.join(self.v("workspace"), "backups"), filetypes=[("backup zip", "*.zip")])
        if not p:
            return
        if self.web and self.web.poll() is None:
            messagebox.showinfo(APP, "Stop the web app first (Web app tab).")
            return
        if messagebox.askyesno(APP, f"Check {os.path.basename(p)} first without changing anything?"):
            self.run("import (check only)", [self.cli("import", p, "--dry-run")], self.data_log)
        elif messagebox.askyesno(APP, "Import it now? The workspace databases are replaced (a safety backup is made first)."):
            self.run("import", [self.cli("import", p)], self.data_log, done=lambda ok: self.refresh_data())

    def cache_dir(self):
        return os.path.join(self.v("workspace"), "data_cache")

    def open_cache(self):
        d = self.cache_dir()
        if os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showinfo(APP, f"No data folder yet: {d}")

    def refresh_data(self):
        d = self.cache_dir()
        self.tree.delete(*self.tree.get_children())
        if not os.path.isdir(d):
            return
        stats = {}
        for p in glob.glob(os.path.join(d, "manifest_*.json")):
            name = os.path.basename(p)[len("manifest_"):-len(".json")]
            inst, _, day = name.rpartition("_")
            if not inst:
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            s = stats.setdefault(inst, {"days": [], "complete": 0, "next": 0, "bytes": 0})
            s["days"].append(day)
            s["complete"] += 1 if m.get("complete") else 0
            s["next"] += 1 if os.path.exists(os.path.join(d, f"nextexp_{inst}_{day}.json")) else 0
        for f in os.scandir(d):
            if f.name.endswith(".parquet"):
                for inst, s in stats.items():
                    if f.name.startswith(inst) and f.name[len(inst):len(inst) + 1].isdigit() or f.name.startswith(inst + "_"):
                        s["bytes"] += f.stat().st_size
                        break
        for inst in sorted(stats):
            s = stats[inst]
            ds = sorted(s["days"])
            self.tree.insert("", "end", values=(inst, len(ds), s["complete"], ds[0], ds[-1], s["next"], f"{s['bytes'] / 1e6:.1f}"))
        known = sorted(set(INSTRUMENTS) | set(stats))
        self.d_inst.configure(values=known + ["ALL"])

    # ---------------- status ----------------
    def refresh_status(self):
        ver = self.package_version()
        venv_ok = os.path.exists(self.vpy())
        ws_ok = os.path.exists(os.path.join(self.v("workspace"), "script_openalgo.toml"))
        w, info = self.wheel(), bundle_info()
        src = dict(self.wheels()).get(w) if w else None
        self.setup_info.set(
            f"venv: {'OK' if venv_ok else 'MISSING (use 1. Create venv)'}  |  package: {ver or '-'}  |  "
            f"workspace: {'OK' if ws_ok else 'missing (Create workspace)'}  |  newest wheel: {os.path.basename(w) + ' (' + src + ')' if w else 'none'}"
            + f"\nPackage files are looked for next to the launcher ({app_dir()}) and in the repo dist; the newest is used."
            + (f" A wheelhouse with {info.get('wheels', '?')} wheels for Python {info.get('python')} allows an offline install." if info else ""))
        running = self.web and self.web.poll() is None
        up = port_open(self.v("port"))
        self.web_state.set(f"running on http://127.0.0.1:{self.v('port')}/" if running and up else
                           "starting…" if running else f"another web app answers on port {self.v('port')}" if up else "stopped")
        key = read_key(self.v("workspace"))
        envk = os.environ.get(KEY_NAME)
        tok = read_key(self.v("workspace"), "STRATEGY_LAB_TOKEN")
        self.lan_info.set(("ON: " + ", ".join(f"http://{ip}:{self.v('port')}" for ip in self.lan_hosts())
                           + ("" if tok else "  (NO TOKEN: press New access token)")) if self.lan_var.get()
                          else "off (this PC only)")
        self.key_info.set(f"saved in workspace .env (…{key[-4:]})" if key else
                          "set in the Windows environment (OPENALGO_API_KEY)" if envk else "NO KEY: paste it and press Save key")
        self.status.set(f"Workspace {self.v('workspace')}  ·  OpenAlgo {self.v('host')}  ·  settings {CFG_FILE}")

    def on_close(self):
        if self.web and self.web.poll() is None:
            ans = messagebox.askyesnocancel(APP, "The web app is running. Stop it before closing?\n\nYes = stop it, No = leave it running, Cancel = stay")
            if ans is None:
                return
            if ans:
                self.stop_web()
        self.persist()
        self.destroy()


def _status_tick(app):
    app.refresh_status()
    app.after(5000, lambda: _status_tick(app))


if __name__ == "__main__":
    app = Launcher()
    app.after(5000, lambda: _status_tick(app))
    app.mainloop()
