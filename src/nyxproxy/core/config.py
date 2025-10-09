"""
Centralized configuration for NyxProxy.

Manages a `config.json` file in `~/.nyxproxy/`.
- If the file doesn't exist, it's created with default values.
- If the file exists, it's loaded.
- If the file is missing keys, they are added with default values and the file is updated.
"""

import json
from pathlib import Path
from typing import Any, Dict

from .exceptions import NyxProxyError

# --- Default Values Definition ---

_DEFAULT_CONFIG_VALUES = {
    "DEFAULT_TEST_URL": "https://www.cloudflare.com/cdn-cgi/trace",
    "DEFAULT_USER_AGENT": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
    ),
}

_DEFAULT_PROXYCHAINS_TEMPLATE = (
    "# proxychains.conf gerado por NyxProxy\n"
    "random_chain\n"
    "proxy_dns\n"
    "remote_dns_subnet 224\n"
    "tcp_read_time_out 15000\n"
    "tcp_connect_time_out 8000\n"
    "[ProxyList]\n"
    "{proxy_list}"
)


# --- Configuration Loading and Initialization ---


def _initialize_config() -> Dict[str, Any]:
    """
    Loads configuration from the JSON file, creating or updating it as needed.
    Also ensures the user-level chains.txt template exists.
    """
    config_dir = Path.home() / ".nyxproxy"
    config_file_path = config_dir / "config.json"
    chains_template_path = config_dir / "chains.txt"

    config_dir.mkdir(parents=True, exist_ok=True)

    # Create chains.txt with default content if it doesn't exist
    if not chains_template_path.is_file():
        chains_template_path.write_text(_DEFAULT_PROXYCHAINS_TEMPLATE, encoding="utf-8")

    if not config_file_path.is_file():
        # Config file doesn't exist, create it with defaults
        with config_file_path.open("w", encoding="utf-8") as f:
            json.dump(_DEFAULT_CONFIG_VALUES, f, ensure_ascii=False, indent=4)
        return _DEFAULT_CONFIG_VALUES

    # Config file exists, load and validate it
    try:
        with config_file_path.open("r", encoding="utf-8") as f:
            loaded_config = json.load(f)
    except json.JSONDecodeError as e:
        raise NyxProxyError(
            f"Configuration file at '{config_file_path}' is corrupted. "
            "Please fix or delete it. Error: " + str(e)
        ) from e

    # Check for missing keys and update the file if necessary (for migrations)
    updated = False
    for key, value in _DEFAULT_CONFIG_VALUES.items():
        if key not in loaded_config:
            loaded_config[key] = value
            updated = True

    if updated:
        with config_file_path.open("w", encoding="utf-8") as f:
            json.dump(loaded_config, f, ensure_ascii=False, indent=4)

    return loaded_config


def _load_proxychains_template() -> str:
    """Loads the proxychains template from the user's config directory."""
    chains_template_path = Path.home() / ".nyxproxy" / "chains.txt"
    try:
        return chains_template_path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise NyxProxyError(
            f"Proxychains template '{chains_template_path}' not found. "
            "Try running the application to generate it or restore it."
        ) from e


# Initialize config on module import
_config = _initialize_config()


# --- Public Configuration Variables ---

# Static settings that are not user-configurable via JSON
DEFAULT_CACHE_FILENAME: str = "proxy_cache.json"
CACHE_VERSION: int = 1
STATUS_STYLES: Dict[str, str] = {
    "AGUARDANDO": "dim",
    "TESTANDO": "yellow",
    "OK": "bold green",
    "ERRO": "bold red",
    "FILTRADO": "cyan",
}

# Settings loaded from config.json
DEFAULT_TEST_URL: str = _config["DEFAULT_TEST_URL"]
DEFAULT_USER_AGENT: str = _config["DEFAULT_USER_AGENT"]

# Settings loaded from user config files
PROXYCHAINS_CONF_TEMPLATE: str = _load_proxychains_template()