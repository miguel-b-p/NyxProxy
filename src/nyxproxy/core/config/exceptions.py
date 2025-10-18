"""Custom exception types for NyxProxy for clearer error handling."""


class NyxProxyError(Exception):
    """Base exception for all custom errors in this application."""

    pass


class XrayError(NyxProxyError):
    """Raised when there's an issue with the Xray/V2Ray binary or process."""

    pass


class ProxyChainsError(NyxProxyError):
    """Raised for errors related to the proxychains utility."""

    pass


class ProxyParsingError(NyxProxyError):
    """Raised when a proxy URI cannot be parsed into a valid outbound configuration."""

    pass


class InsufficientProxiesError(NyxProxyError):
    """Raised when an operation cannot be completed due to a lack of functional proxies."""

    pass