# NyxProxy

![Nyx-inspired illustration](./nyx.png)
*Artwork source: [GreekMythology.com - Nyx](https://www.greekmythology.com/Other_Gods/Nyx/nyx.html).*

**NyxProxy** is a Python CLI and library that loads, tests, and orchestrates V2Ray/Xray proxies
(`vmess://`, `vless://`, `trojan://`, and `ss://`). It automates large-scale validation, spins up
local HTTP bridges with Xray-core, features a built-in TCP load balancer for distributing connections
across multiple proxies, and plugs into `proxychains` so any application can tunnel through working
proxies without extra configuration.

---

## Overview

- **Concurrent pipelines:** asynchronous checks for connectivity, latency, and geolocation with 2-phase testing for optimal performance.
- **Smart cache:** reuses previous results and persistent geolocation cache to minimize API calls.
- **Local bridges:** launches Xray instances and exposes them as `http://127.0.0.1:<port>`.
- **Load balancer:** built-in TCP load balancer distributes connections across multiple proxies through a single port.
- **Interactive management:** real-time commands to rotate proxies, adjust amounts, manage sources, and control load balancer.
- **Country verification:** double-checks exit country to ensure proxies actually route through the specified country.
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

All commands accept multiple files and URLs as sources. Geolocation lookups are **disabled by default**
for faster performance. Use `--with-geo` to enable country detection when needed.

### Test proxies

```bash
nyxproxy test proxy.txt --threads 50 --find-first 10
nyxproxy test list.txt https://example.com/proxies.txt --country BR --with-geo --output-json
```

Key options:

- `--threads, -t`: number of workers (default 50).
- `--country, -c`: filter by ISO code or English name (`BR`, `United States`, ...). Verifies exit country matches filter.
- `--limit, -l`: limit how many URIs to load.
- `--force`: ignore the existing cache.
- `--find-first, -ff`: stop after finding N working proxies.
- `--with-geo`: enable geolocation lookups (disabled by default for speed).
- `--output-json, -j`: return results as JSON.

NyxProxy uses 2-phase testing: fast TCP socket screening followed by functional tests only on online proxies,
reducing resource usage by 60-80% and testing speed by 50-70%.

### Start HTTP bridges

```bash
nyxproxy start proxy.txt --amounts 3 --country BR
nyxproxy start --amounts 5  # reuse cached results (geo disabled by default)
nyxproxy start -a 10 --bridge 8080 proxy.txt  # start 10 bridges + load balancer on port 8080
```

Starts up to `--amounts` persistent local bridges (`http://127.0.0.1:<port>`). The command keeps
Xray running until you press `Ctrl+C`. Tune `--threads` and `--limit` to control the warm-up tests.

**Load Balancer:**
- `--bridge, -b <port>`: starts a TCP load balancer that distributes connections across all bridges.
- Supports multiple strategies: `random` (default), `round-robin`, `least-conn`.
- Access all proxies through a single port: `http://127.0.0.1:8080`.

**Interactive Commands** (available during `start` and `chains` modes):
- `proxy rotate <id|all>` - Rotate a specific proxy or all proxies
- `proxy amount <number>` - Dynamically adjust the number of active bridges
- `bridge on <port>` - Start load balancer on specified port
- `bridge off` - Stop the load balancer
- `bridge stats` - Show load balancer statistics
- `source add <url>` - Add a new proxy source
- `source rem <id>` - Remove a source by ID
- `source list` - List all configured sources
- `help` - Show all available commands

### Run through proxychains

```bash
nyxproxy chains --amounts 3 --source proxy.txt -- curl -s https://ifconfig.me
nyxproxy chains -s proxy.txt -s another_list.txt --country US --with-geo -- wget "https://example.com/file"
```

Everything after `--` is forwarded to `proxychains`. NyxProxy generates the temporary config and
shuts down the bridges once the command exits. Same interactive commands as `start` mode are available.

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
nyxproxy export working_proxies.txt proxy.txt --threads 50 --find-first 50
nyxproxy export active.txt --country JP  # cache only, geo disabled by default
nyxproxy export active.txt --country JP --with-geo --force  # fresh test with geo verification
```

Runs a fresh test (or reuses the cache) and writes the approved URIs to the chosen file. Pass
`--force` to revalidate everything and ignore cached results. When using `--country`, exit country
is verified to ensure proxies actually route through the specified country.

---

## Load Balancer

The built-in TCP load balancer distributes connections across multiple proxy bridges through a single port,
making it easy to rotate proxies automatically without client-side logic.

### Starting the load balancer

```bash
# Start 10 bridges with load balancer on port 8080
nyxproxy start -a 10 --bridge 8080 proxy.txt

# Or start it interactively after bridges are running
nyxproxy start -a 10 proxy.txt
# Then type: bridge on 8080
```

### Using the load balancer

```bash
# Every request automatically uses a different random proxy
curl --proxy http://127.0.0.1:8080 https://api.ipify.org  # IP: 104.26.15.85
curl --proxy http://127.0.0.1:8080 https://api.ipify.org  # IP: 172.66.46.236
curl --proxy http://127.0.0.1:8080 https://api.ipify.org  # IP: 185.236.232.94
```

### Load balancer strategies

- **random** (default): Each connection uses a random proxy for uniform distribution
- **round-robin**: Sequential rotation through all proxies (0, 1, 2, 0, 1, 2, ...)
- **least-conn**: Selects the proxy with the fewest active connections

### Interactive management

While `nyxproxy start` is running:

```
bridge on 8080        # Start load balancer on port 8080
bridge stats          # Show connection statistics
bridge off            # Stop load balancer
proxy amount 20       # Dynamically adjust to 20 bridges
```

### Statistics example

```
Load Balancer Stats:
  Port: 8080
  Strategy: random
  Total connections: 1,234
  Active connections: 45
```

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
