# script_openalgo

Research, backtest and manage option strategies against **any OpenAlgo server**. The OpenAlgo server software is **not** part of
this package: it only talks to a server over HTTP, so the server can run on another machine.

## Install (on any machine with Python 3.11+)

```
pip install script_openalgo-0.1.1-py3-none-any.whl          # or the .tar.gz
```

## First use

```
script-openalgo init my_workspace           # creates the workspace (config, folders, agents, skill, example strategies)
cd my_workspace
copy .env.example .env                      # then put OPENALGO_API_KEY=... in .env   (macOS/Linux: cp)
script-openalgo --host http://<openalgo-host>:5000 doctor
script-openalgo --host http://<openalgo-host>:5000 fetch --instrument ALL
script-openalgo --host http://<openalgo-host>:5000 backfill --instrument ALL
script-openalgo multiday                    # every strategy on every usable day
script-openalgo webapp                      # http://127.0.0.1:5055
```

## Where the OpenAlgo server address comes from (first match wins)

1. `--host URL` on the command line
2. environment variable `OPENALGO_HOST`
3. `host` in `script_openalgo.toml` in the workspace
4. default `http://127.0.0.1:5000`

The API key comes from `OPENALGO_API_KEY` (environment) or the workspace `.env` file. It is never written to the config.

## The workspace

Everything you create lives in one folder: `strategies/`, `results/`, `data_cache/` (candles), `strategy_db/` (databases),
`.claude/` (agents, skill, knowledge). Move machines with `script-openalgo export` / `script-openalgo import`, or the **Backup** tab
of the web app: one zip with every file, verified by SHA-256, API key excluded.

## AI steps (optional)

The researcher, strategy-writer and reviewer agents are Claude Code agents (shipped in the package, copied to the workspace by
`init`). They run through the **Claude Code CLI**, which is not part of this package: install it and log in to use them. Data
fetching, backtests, verification and the web app work without it.
