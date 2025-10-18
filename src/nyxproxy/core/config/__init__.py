"""
Configuration module for NyxProxy.

This module contains all configuration-related functionality including:
- Application settings and themes
- Custom exceptions
- Configuration file management
"""

from .exceptions import (
    NyxProxyError,
    InsufficientProxiesError,
    ProxyChainsError,
    ProxyParsingError,
    XrayError,
)
from .settings import (
    DEFAULT_CACHE_FILENAME,
    CACHE_VERSION,
    STATUS_STYLES,
    DEFAULT_RICH_THEME,
    DEFAULT_TEST_URL,
    DEFAULT_USER_AGENT,
    PROXYCHAINS_CONF_TEMPLATE,
)

__all__ = [
    # Exceptions
    "NyxProxyError",
    "InsufficientProxiesError",
    "ProxyChainsError",
    "ProxyParsingError",
    "XrayError",
    # Settings
    "DEFAULT_CACHE_FILENAME",
    "CACHE_VERSION",
    "STATUS_STYLES",
    "DEFAULT_RICH_THEME",
    "DEFAULT_TEST_URL",
    "DEFAULT_USER_AGENT",
    "PROXYCHAINS_CONF_TEMPLATE",
]
