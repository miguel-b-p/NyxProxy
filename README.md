# NyxProxy

![Nyx-inspired illustration](./nyx.png)
*Artwork source: [GreekMythology.com - Nyx](https://www.greekmythology.com/Other_Gods/Nyx/nyx.html).*

**NyxProxy** is a Python CLI and library that loads, tests, and orchestrates V2Ray/Xray proxies
(`vmess://`, `vless://`, `trojan://`, and `ss://`). It automates large-scale validation, spins up
local HTTP bridges with Xray-core, and plugs into `proxychains` so any application can tunnel
through working proxies without extra configuration.

---

## Overview

- **Concurrent pipelines:** asynchronous checks for connectivity, latency, and geolocation.
- **Smart cache:** reuses previous results and lets you export only working proxies.
- **Local bridges:** launches Xray instances and exposes them as `http://127.0.0.1:<port>`.
- **Proxychains integration:** runs commands through rotating proxies with minimal setup.
- **Typer + Rich CLI:** interactive tables, country filters, and JSON output for automation.

NyxProxy targets developers, security researchers, and power users who need reproducible proxy
workflows for scraping, testing, or bypass scenarios.

---

## Requirements

1. **Python 3.8+** - check with `python3 --version`.
2. **Xray-core** - make the `xray` binary available on `PATH`. See
   [XTLS/Xray-install](https://github.com/XTLS/Xray-install) or use your package manager
   (`apt`, `pacman`, `brew`, ...).
3. **proxychains-ng (optional)** - needed only for the `chains` command.
4. **FindIP token** - create a free account at [findip.net](https://findip.net/) and store the
   token in `.env` to enable geolocation lookups.

---

## Quick install

```bash
git clone https://github.com/miguel-b-p/NyxProxy.git
cd NyxProxy
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env  # fill in FINDIP_TOKEN before running tests
```

The `proxy.txt` file ships with sample URIs for quick smoke tests. Do not store production proxy
lists or tokens inside the repository tree.

---

## CLI usage

All commands accept multiple files and URLs as sources. When `--no-geo` is set, FindIP lookups are
skipped, which speeds up runs that do not rely on country filters.

### Test proxies

```bash
nyxproxy test proxy.txt --threads 20 --find-first 10
nyxproxy test list.txt https://example.com/proxies.txt --country BR --output-json
```

Key options:

- `--threads, -t`: number of workers (default 20).
- `--country, -c`: filter by ISO code or English name (`BR`, `United States`, ...).
- `--limit, -l`: limit how many URIs to load.
- `--force`: ignore the existing cache.
- `--find-first, -ff`: stop after finding N working proxies.
- `--no-geo`: skip geolocation lookups.
- `--output-json, -j`: return results as JSON.

### Start HTTP bridges

```bash
nyxproxy start proxy.txt --amounts 3 --country BR
nyxproxy start --amounts 2 --no-geo  # reuse cached results
```

Starts up to `--amounts` persistent local bridges (`http://127.0.0.1:<port>`). The command keeps
Xray running until you press `Ctrl+C`. Tune `--threads` and `--limit` to control the warm-up tests.

### Run through proxychains

```bash
nyxproxy chains --amounts 3 --source proxy.txt -- curl -s https://ifconfig.me
nyxproxy chains -s proxy.txt -s another_list.txt --country US -- wget "https://example.com/file"
```

Everything after `--` is forwarded to `proxychains`. NyxProxy generates the temporary config and
shuts down the bridges once the command exits.

### Clear the cache

```bash
nyxproxy clear          # remove the entire cache
nyxproxy clear 1S,2D    # remove entries older than 1 week and 2 days
nyxproxy clear 12H      # remove entries older than 12 hours
```

Accepted suffixes: `S` (week), `D` (day), `H` (hour), `M` (minute). Combine values with commas.

### List approved proxies

```bash
nyxproxy list-proxies
nyxproxy list-proxies --country NL --output-json
```

Loads the cache and shows only proxies with status `OK`. Use `--output-json` to integrate with
other tools.

### Export working proxies

```bash
nyxproxy export working_proxies.txt proxy.txt --threads 30 --find-first 50
nyxproxy export active.txt --country JP --no-geo  # cache only
```

Runs a fresh test (or reuses the cache) and writes the approved URIs to the chosen file. Pass
`--force` to revalidate everything and ignore cached results.

---

## Environment variables

NyxProxy relies on `python-dotenv`. Fill in `.env` with:

```ini
FINDIP_TOKEN="your_findip_token"
```

Keep this file out of version control. When new keys are introduced, update `.env.example`.

---

## Development

- Create the virtual environment and install in editable mode (`pip install -e .`).
- Run `ruff check src` and `ruff format src` to follow the style enforced by `pyproject.toml`.
- Exercise the main flow with `nyxproxy test proxy.txt --threads 5` before opening pull requests.
- Add automated tests under `tests/` (pytest) whenever possible. Execute them with `python -m pytest`.

---

## Contributing

1. Fork the repository and create a branch (`git checkout -b fix/my-change`).
2. Follow Conventional Commits (for example, `fix(parsing): correct port parsing`).
3. Update relevant docs and examples; include CLI output when behavior changes.
4. Open a Pull Request explaining the motivation, executed tests, and any security considerations.

To report bugs or suggest features, open an issue with logs and reproducible steps.

---

## License

Distributed under **CC BY-NC-SA 4.0**. See [LICENSE](LICENSE) for details.

---

## Authors

- **Miguel Batista Pinotti** - [miguel-b-p](https://github.com/miguel-b-p)
- **Leoni Frazao** - [Gameriano1](https://github.com/Gameriano1)
