"""Centralized configuration and constants for NyxProxy."""

from typing import Dict

# -- Cache Settings --
DEFAULT_CACHE_FILENAME: str = "proxy_cache.json"
CACHE_VERSION: int = 1

# -- Testing Settings --
DEFAULT_TEST_URL: str = "https://www.cloudflare.com/cdn-cgi/trace"
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
)

# -- Console UI Settings --
STATUS_STYLES: Dict[str, str] = {
    "AGUARDANDO": "dim",
    "TESTANDO": "yellow",
    "OK": "bold green",
    "ERRO": "bold red",
    "FILTRADO": "cyan",
}

# -- ProxyChains Settings --
PROXYCHAINS_CONF_TEMPLATE = """
# proxychains.conf gerado por NyxProxy
random_chain
proxy_dns
remote_dns_subnet 224
tcp_read_time_out 15000
tcp_connect_time_out 8000
[ProxyList]
{proxy_list}
"""