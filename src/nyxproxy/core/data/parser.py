from __future__ import annotations

"""Routines for parsing proxy URIs into Xray/V2Ray structures."""

import json
import re
from typing import Any, Dict, List
from urllib.parse import parse_qs, unquote, urlparse

from ..config.exceptions import ProxyParsingError
from ..models.proxy import Outbound


class ParsingMixin:
    """Responsible for interpreting different proxy schemes."""

    def _parse_uri_to_outbound(self, uri: str) -> Outbound:
        """Directs the link to the appropriate parser according to the scheme."""
        uri = uri.strip()
        if not uri or uri.startswith(("#", "//")):
            raise ProxyParsingError("Empty line or comment.")

        match = re.match(r"^([a-z0-9]+)://", uri, re.I)
        if not match:
            raise ProxyParsingError(f"Unknown scheme in URI: {uri[:80]}")

        scheme = match.group(1).lower()
        parser = getattr(self, f"_parse_{scheme}", None)
        if parser is None:
            raise ProxyParsingError(f"Unsupported scheme: {scheme}")

        return parser(uri)

    def _parse_ss(self, uri: str) -> Outbound:
        """Normalizes an `ss://` link to a Shadowsocks outbound."""
        parsed = urlparse(uri)
        tag = self._sanitize_tag(unquote(parsed.fragment) if parsed.fragment else None, "ss")

        encoded_part = parsed.netloc + parsed.path
        if not encoded_part:
            raise ProxyParsingError("ss:// link is empty or malformed.")

        try:
            decoded_text = self._decode_bytes(self._b64decode_padded(encoded_part))
        except Exception as exc:
            raise ProxyParsingError(f"Failed to decode base64 from ss://: {exc}") from exc

        match = re.match(r"^(?P<method>.+?):(?P<password>.+?)@(?P<host>.+?):(?P<port>\d+)$", decoded_text)
        if not match:
            raise ProxyParsingError("Invalid decoded ss:// format.")

        data = match.groupdict()
        port = self._safe_int(data["port"])
        if port is None:
            raise ProxyParsingError(f"Invalid port in ss://: {data['port']!r}")

        host = data["host"]
        config = {
            "tag": tag,
            "protocol": "shadowsocks",
            "settings": {
                "servers": [{
                    "address": host,
                    "port": port,
                    "method": data["method"],
                    "password": data["password"],
                }]
            }
        }
        return Outbound(tag=tag, config=config, protocol="shadowsocks", host=host, port=port)

    def _parse_vmess(self, uri: str) -> Outbound:
        """Converts JSON-based `vmess://` links to a Vmess outbound."""
        payload = uri.strip()[8:]
        try:
            decoded = self._b64decode_padded(payload)
            data = json.loads(self._decode_bytes(decoded))
        except Exception as exc:
            raise ProxyParsingError(f"Invalid vmess:// payload: {exc}") from exc

        return self._vmess_outbound_from_dict(data)

    def _vmess_outbound_from_dict(self, data: Dict[str, Any], *, tag_fallback: str = "vmess") -> Outbound:
        """Builds the vmess outbound from the decoded dictionary."""
        if not isinstance(data, dict):
            raise ProxyParsingError("Vmess data must be a dictionary.")

        host = data.get("add")
        port_raw = data.get("port")
        uuid = data.get("id")
        if not all((host, port_raw, uuid)):
            raise ProxyParsingError("Incomplete vmess data (add, port, or id missing).")

        port = self._safe_int(port_raw)
        if port is None:
            raise ProxyParsingError(f"Invalid vmess port: {port_raw!r}")

        tag = self._sanitize_tag(data.get("ps"), tag_fallback)
        params = {k: [str(v)] for k, v in data.items()}
        stream_settings = self._build_stream_settings(params, host)

        config = {
            "tag": tag,
            "protocol": "vmess",
            "settings": {
                "vnext": [{
                    "address": host,
                    "port": port,
                    "users": [{
                        "id": uuid,
                        "alterId": int(data.get("aid", 0)),
                        "security": data.get("scy", "auto"),
                        "level": int(data.get("level", 0)),
                    }]
                }]
            },
            "streamSettings": stream_settings,
        }
        return Outbound(tag=tag, config=config, protocol="vmess", host=host, port=port)

    def _parse_vless(self, uri: str) -> Outbound:
        """Normalizes `vless://` links with RealITY support to a VLESS outbound."""
        parsed = urlparse(uri)
        uuid = parsed.username
        host = parsed.hostname
        port = None
        try:
            port = parsed.port
        except ValueError as e:
            if 'Port could not be cast' in str(e):
                # Likely unbracketed IPv6 address
                authority = parsed.netloc
                if '@' in authority:
                    user, authority = authority.rsplit('@', 1)
                    uuid = unquote(user)  # In case
                if ':' in authority:
                    host, port_str = authority.rsplit(':', 1)
                    try:
                        port = int(port_str)
                    except ValueError:
                        raise ProxyParsingError(f"Invalid port '{port_str}' after manual parse.") from e
                else:
                    raise ProxyParsingError("No port found in vless URI.") from e
            else:
                raise ProxyParsingError("Error parsing vless port.") from e

        if not all((uuid, host, port)):
            raise ProxyParsingError("Incomplete vless:// link (user, host, or port missing).")

        params = parse_qs(parsed.query)
        tag = self._sanitize_tag(unquote(parsed.fragment) if parsed.fragment else None, "vless")
        stream_settings = self._build_stream_settings(params, host)

        config = {
            "tag": tag,
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": host,
                    "port": port,
                    "users": [{
                        "id": uuid,
                        "encryption": params.get("encryption", ["none"])[0],
                        "flow": params.get("flow", [""])[0],
                    }]
                }]
            },
            "streamSettings": stream_settings,
        }
        return Outbound(tag=tag, config=config, protocol="vless", host=host, port=port)

    def _parse_trojan(self, uri: str) -> Outbound:
        """Converts `trojan://` links with WebSocket support to a Trojan outbound."""
        parsed = urlparse(uri)
        password = parsed.username
        host = parsed.hostname
        port = None
        try:
            port = parsed.port
        except ValueError as e:
            if 'Port could not be cast' in str(e):
                # Likely unbracketed IPv6 address
                authority = parsed.netloc
                if '@' in authority:
                    user, authority = authority.rsplit('@', 1)
                    password = unquote(user)
                if ':' in authority:
                    host, port_str = authority.rsplit(':', 1)
                    try:
                        port = int(port_str)
                    except ValueError:
                        raise ProxyParsingError(f"Invalid port '{port_str}' after manual parse.") from e
                else:
                    raise ProxyParsingError("No port found in trojan URI.") from e
            else:
                raise ProxyParsingError("Error parsing trojan port.") from e

        if not all((password, host, port)):
            raise ProxyParsingError("Incomplete trojan:// link (password, host, or port missing).")

        params = parse_qs(parsed.query)
        tag = self._sanitize_tag(unquote(parsed.fragment) if parsed.fragment else None, "trojan")
        stream_settings = self._build_stream_settings(params, host)

        config = {
            "tag": tag,
            "protocol": "trojan",
            "settings": {
                "servers": [{
                    "address": host,
                    "port": port,
                    "password": password,
                    "flow": params.get("flow", [""])[0],
                }]
            },
            "streamSettings": stream_settings
        }
        return Outbound(tag=tag, config=config, protocol="trojan", host=host, port=port)

    def _build_stream_settings(
        self, params: Dict[str, List[str]], host: str
    ) -> Dict[str, Any]:
        """Creates the streamSettings structure based on URI parameters."""
        network = params.get("type", ["tcp"])[0]
        if network == "none":
            network = "tcp"  # Map 'none' to valid 'tcp' for plain connections
        security = params.get("security", [""])[0]
        sni = params.get("sni", [host])[0] or host

        stream: Dict[str, Any] = {"network": network}

        if network == "ws":
            ws_host = params.get("host", [sni])[0]
            stream["wsSettings"] = {
                "path": params.get("path", ["/"])[0],
                "headers": {"Host": ws_host or sni},
            }
        elif network == "grpc":
            stream["grpcSettings"] = {"serviceName": params.get("serviceName", [""])[0]}

        if security in ("tls", "reality"):
            stream["security"] = security
            settings_key = f"{security}Settings"
            sec_settings: Dict[str, Any] = {"serverName": sni}

            if alpn_list := params.get("alpn"):
                sec_settings["alpn"] = alpn_list
            if fp := params.get("fp", [""])[0]:
                sec_settings["fingerprint"] = fp
            if params.get("allowInsecure", ["0"])[0] == "1":
                sec_settings["allowInsecure"] = True

            if security == "reality":
                sec_settings.update({
                    "publicKey": params.get("pbk", [""])[0],
                    "shortId": params.get("sid", [""])[0],
                    "spiderX": params.get("spx", ["/"])[0],
                })
            stream[settings_key] = sec_settings

        return stream