"""Central configuration: which workspace folder to use, which OpenAlgo server to talk to, and the API key.

Nothing here starts or needs the OpenAlgo server software; the library only talks to it over HTTP, so the server can be on
another machine.

OpenAlgo host (first match wins):
    1. --host URL on the command line (set_host)            e.g.  script-openalgo --host http://192.168.1.20:5000 doctor
    2. environment variable OPENALGO_HOST
    3. `host` under [openalgo] in <workspace>/script_openalgo.toml
    4. default http://127.0.0.1:5000

Workspace folder (where candles, results, strategies and the databases live; first match wins):
    1. --home DIR on the command line (set_home)
    2. environment variable SCRIPT_OPENALGO_HOME
    3. the nearest folder, starting from the current one and going up, that contains script_openalgo.toml
    4. the current folder

API key (never printed, never stored in the workspace config): environment variable OPENALGO_API_KEY, else the line
OPENALGO_API_KEY=... in <workspace>/.env.
"""
import os
import tomllib

DEFAULT_HOST = "http://127.0.0.1:5000"
CONFIG_NAME = "script_openalgo.toml"
ENV_HOME, ENV_HOST, ENV_KEY = "SCRIPT_OPENALGO_HOME", "OPENALGO_HOST", "OPENALGO_API_KEY"


def normalize_host(url):
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "http://" + url
    return url


def set_host(url):
    """Use this OpenAlgo server for this process and every child process it starts."""
    url = normalize_host(url)
    if url:
        os.environ[ENV_HOST] = url
    return url


def set_home(path):
    p = os.path.abspath(os.path.expanduser(path))
    os.environ[ENV_HOME] = p
    return p


def home():
    h = os.environ.get(ENV_HOME)
    if h:
        return os.path.abspath(h)
    d = os.path.abspath(os.getcwd())
    while True:
        if os.path.exists(os.path.join(d, CONFIG_NAME)):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.abspath(os.getcwd())
        d = parent


def read_config(folder=None):
    p = os.path.join(folder or home(), CONFIG_NAME)
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                return tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            return {}
    return {}


def host():
    return normalize_host(os.environ.get(ENV_HOST) or read_config().get("openalgo", {}).get("host") or DEFAULT_HOST)


def load_dotenv(folder=None):
    """Values of <workspace>/.env (simple KEY=VALUE lines). Used for child processes only; never logged."""
    env = {}
    p = os.path.join(folder or home(), ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def api_key():
    return os.environ.get(ENV_KEY) or load_dotenv().get(ENV_KEY) or None


def child_env(extra=None):
    """Environment for child processes: same workspace, same server, API key available."""
    env = dict(os.environ)
    for k, v in load_dotenv().items():
        env.setdefault(k, v)
    env[ENV_HOME] = home()
    env[ENV_HOST] = host()
    env.update(extra or {})
    return env


# ---- folders inside the workspace ----
def _sub(*parts):
    return os.path.join(home(), *parts)


def strategies_dir():
    return _sub("strategies")


def results_dir():
    return _sub("results")


def data_cache_dir():
    return os.environ.get("BACKTEST_CACHE_DIR") or _sub("data_cache")


def strategy_db_dir():
    return _sub("strategy_db")


def claude_dir():
    return _sub(".claude")


def state_dir():
    return _sub(".claude", "state")


def knowledge_dir():
    return _sub(".claude", "skills", "strategy_builder", "knowledge")


def backups_dir():
    return _sub("backups")


def write_default_config(folder, host_url=None):
    """Create <folder>/script_openalgo.toml if it does not exist (API key is NOT written here)."""
    p = os.path.join(folder, CONFIG_NAME)
    if os.path.exists(p):
        return p
    with open(p, "w", encoding="utf-8") as f:
        f.write('# script_openalgo workspace settings. Put the API key in .env (OPENALGO_API_KEY=...) or the environment, not here.\n'
                '[openalgo]\n'
                f'host = "{normalize_host(host_url) or DEFAULT_HOST}"\n')
    return p
