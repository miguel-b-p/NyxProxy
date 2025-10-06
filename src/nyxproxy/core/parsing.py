from __future__ import annotations

"""Rotinas de parsing de URIs de proxy para estruturas Xray/V2Ray."""

import json
import re
from typing import Any, Dict
from urllib.parse import parse_qs, unquote, urlparse, urlsplit


class ParsingMixin:
    """Responsável por interpretar diferentes esquemas de proxy."""

    def _parse_uri_to_outbound(self, uri: str):
        """Direciona o link para o parser adequado de acordo com o esquema."""
        uri = uri.strip()
        if not uri or uri.startswith("#") or uri.startswith("//"):
            raise ValueError("Linha vazia ou comentário.")
        match = re.match(r"^([a-z0-9]+)://", uri, re.I)
        if not match:
            raise ValueError(f"Esquema desconhecido na linha: {uri[:80]}")
        scheme = match.group(1).lower()
        parser = {
            "ss": self._parse_ss,
            "vmess": self._parse_vmess,
            "vless": self._parse_vless,
            "trojan": self._parse_trojan,
        }.get(scheme)
        if parser is None:
            raise ValueError(f"Esquema não suportado: {scheme}")
        return parser(uri)

    def _parse_ss(self, uri: str):
        """Normaliza um link ``ss://`` incluindo casos em JSON inline."""
        frag = urlsplit(uri).fragment
        tag = self._sanitize_tag(unquote(frag) if frag else None, "ss")

        payload = uri.strip()[5:]
        stripped_payload = payload.split('#')[0]

        try:
            decoded_preview = self._decode_bytes(self._b64decode_padded(stripped_payload))
        except Exception:
            decoded_preview = None

        if decoded_preview:
            text_preview = decoded_preview.strip()
            if text_preview.startswith('{') and text_preview.endswith('}'):
                try:
                    data_json = json.loads(text_preview)
                except json.JSONDecodeError:
                    pass
                else:
                    if {
                        "server", "method"
                    }.issubset(data_json.keys()) or {
                        "address", "method"
                    }.issubset(data_json.keys()) or {
                        "server", "password"
                    }.issubset(data_json.keys()):
                        ss_host = data_json.get("server") or data_json.get("address")
                        ss_port_raw = data_json.get("server_port") or data_json.get("port")
                        ss_method = data_json.get("method") or data_json.get("cipher")
                        ss_password = data_json.get("password") or data_json.get("passwd") or ""
                        if not ss_host or not ss_port_raw or not ss_method:
                            raise ValueError("Link ss:// incompleto (server/port/method ausentes no JSON).")
                        try:
                            ss_port = int(str(ss_port_raw).strip())
                        except (TypeError, ValueError):
                            raise ValueError(f"Porta ss inválida: {ss_port_raw!r}")
                        return self.Outbound(tag, {
                            "tag": tag,
                            "protocol": "shadowsocks",
                            "settings": {
                                "servers": [{
                                    "address": ss_host,
                                    "port": ss_port,
                                    "method": ss_method,
                                    "password": ss_password,
                                }]
                            }
                        })

        try:
            decoded = self._b64decode_padded(payload)
        except Exception as exc:
            raise ValueError(f"Falha ao decodificar base64 do ss://: {exc}") from exc
        text = self._decode_bytes(decoded)
        if '@' not in text:
            raise ValueError("Formato inválido para ss://, faltando '@'.")
        method_password, host_port = text.split('@', 1)
        if ':' not in method_password or ':' not in host_port:
            raise ValueError("Formato inválido para ss://, faltando separadores.")
        method, password = method_password.split(':', 1)
        host, port_text = host_port.split(':', 1)
        port = self._safe_int(port_text)
        if port is None:
            raise ValueError(f"Porta inválida no ss://: {port_text!r}")
        return self.Outbound(tag, {
            "tag": tag,
            "protocol": "shadowsocks",
            "settings": {
                "servers": [{
                    "address": host,
                    "port": port,
                    "method": method,
                    "password": password,
                }]
            }
        })

    def _parse_vmess(self, uri: str):
        """Converte links ``vmess://`` baseados em JSON interno."""
        payload = uri.strip()[8:]
        try:
            decoded = self._b64decode_padded(payload)
        except Exception as exc:
            raise ValueError(f"Falha ao decodificar base64 do vmess://: {exc}") from exc
        try:
            data = json.loads(self._decode_bytes(decoded))
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON inválido no vmess://: {exc}") from exc
        return self._vmess_outbound_from_dict(data)

    def _vmess_outbound_from_dict(self, data: Dict[str, Any], *, tag_fallback: str = "vmess"):
        """Constrói o outbound de vmess a partir do dicionário decodificado."""
        if not isinstance(data, dict):
            raise ValueError("Dados vmess devem ser um dicionário.")
        add = data.get("add") or data.get("address")
        port = data.get("port") or data.get("serv_port")
        uuid = data.get("id") or data.get("uuid")
        if not add or not port or not uuid:
            raise ValueError("Dados vmess incompletos: address, port ou id ausente.")
        try:
            port = int(str(port).strip())
        except (TypeError, ValueError):
            raise ValueError(f"Porta vmess inválida: {port!r}")
        tag = self._sanitize_tag(data.get("ps"), tag_fallback)
        security = data.get("scy") or data.get("security") or "auto"
        network = data.get("net") or data.get("network") or "tcp"
        host = data.get("host") or data.get("sni") or data.get("servername")
        path = data.get("path") or data.get("path_ws")
        tls = data.get("tls") or data.get("tls_mode") or data.get("security_tls")
        alpn = data.get("alpn")
        mux = data.get("mux")
        skip_cert = data.get("skip_cert_verify") or data.get("allowInsecure")
        flow = data.get("flow")
        header_type = data.get("type") or data.get("headerType")

        stream_settings: Dict[str, Any] = {"network": network}
        if host:
            stream_settings["security"] = "tls"
            stream_settings.setdefault("tlsSettings", {})["serverName"] = host
        if tls and str(tls).lower() != "none":
            stream_settings["security"] = "tls"
        if alpn:
            stream_settings.setdefault("tlsSettings", {})["alpn"] = alpn
        if skip_cert:
            stream_settings.setdefault("tlsSettings", {})["allowInsecure"] = True
        if network == "ws":
            stream_settings.setdefault("wsSettings", {})
            if host:
                stream_settings["wsSettings"]["headers"] = {"Host": host}
            if path:
                stream_settings["wsSettings"]["path"] = path
        elif network == "grpc":
            stream_settings.setdefault("grpcSettings", {})
            if service_name := data.get("serviceName"):
                stream_settings["grpcSettings"]["serviceName"] = service_name
        elif network == "tcp" and header_type:
            stream_settings.setdefault("tcpSettings", {})
            stream_settings["tcpSettings"]["header"] = {"type": header_type}

        outbound = {
            "tag": tag,
            "protocol": "vmess",
            "settings": {
                "vnext": [{
                    "address": add,
                    "port": port,
                    "users": [{
                        "id": uuid,
                        "alterId": int(str(data.get("aid") or 0)),
                        "security": security,
                        "level": int(str(data.get("level") or 0)),
                        "flow": flow or "",
                    }]
                }]
            },
            "streamSettings": stream_settings,
            "mux": {"enabled": bool(mux)},
        }
        return self.Outbound(tag, outbound)

    def _parse_vless(self, uri: str):
        """Normaliza links ``vless://`` com suporte a RealITY e Xtls-Fast-Open."""
        parsed = urlparse(uri)
        user_info = parsed.username
        host = parsed.hostname
        port = parsed.port
        if not user_info or not host or not port:
            raise ValueError("Link vless:// incompleto: usuário, host ou porta ausente.")
        params = parse_qs(parsed.query)
        tag = self._sanitize_tag(unquote(parsed.fragment) if parsed.fragment else None, "vless")
        flow = params.get("flow", [""])[0]
        security = params.get("security", [""])[0]
        sni = params.get("sni", params.get("host", [""]))[0]
        alpn = params.get("alpn", [])
        reality = security.lower() == "reality"
        fp = params.get("fp", [""])[0]
        pbk = params.get("pbk", [""])[0]
        sid = params.get("sid", [""])[0]
        spx = params.get("spx", [""])[0]
        type_param = params.get("type", ["grpc"])[0]
        service_name = params.get("serviceName", params.get("serviceName", [""]))[0]
        
        network = params.get("type", params.get("network", ["tcp"]))[0]

        stream = {"network": network}
        if network == "ws":
            stream["wsSettings"] = {
                "path": params.get("path", [""])[0],
                "headers": {"Host": sni or host}
            }
        elif network == "grpc":
            stream["grpcSettings"] = {
                "serviceName": service_name or params.get("serviceName", [""])[0]
            }
        else:
            stream["tcpSettings"] = {
                "header": {"type": params.get("headerType", [""])[0] or "none"}
            }

        if security:
            stream["security"] = security
            if security.lower() == "tls":
                stream.setdefault("tlsSettings", {})
                if sni:
                    stream["tlsSettings"]["serverName"] = sni
                if alpn:
                    stream["tlsSettings"]["alpn"] = alpn
                if fp:
                    stream["tlsSettings"]["fingerprint"] = fp
            elif reality:
                stream.setdefault("realitySettings", {})
                stream["security"] = "reality"
                stream["realitySettings"].update({
                    "publicKey": pbk,
                    "shortId": sid,
                    "spiderX": spx or "/",
                    "serverName": sni or host,
                })
                if fp:
                    stream["realitySettings"]["fingerprint"] = fp

        outbound = {
            "tag": tag,
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": host,
                    "port": port,
                    "users": [{
                        "id": user_info,
                        "encryption": params.get("encryption", ["none"])[0],
                        "flow": flow or "",
                    }]
                }]
            },
            "streamSettings": stream,
        }

        return self.Outbound(tag, outbound)

    def _parse_trojan(self, uri: str):
        """Converte links ``trojan://`` com suporte a WebSocket e TLS."""
        parsed = urlparse(uri)
        password = parsed.username
        host = parsed.hostname
        port = parsed.port
        if not password or not host or not port:
            raise ValueError("Link trojan:// incompleto: credenciais ou destino ausentes.")
        params = parse_qs(parsed.query)
        tag = self._sanitize_tag(unquote(parsed.fragment) if parsed.fragment else None, "trojan")
        network = params.get("type", ["tcp"])[0]
        security = params.get("security", [""])[0]
        sni = params.get("sni", params.get("host", [""]))[0]
        host_header = params.get("host", [""])[0]
        path = params.get("path", ["/"])[0]
        alpn = params.get("alpn", [])
        fp = params.get("fp", [""])[0]
        allow_insecure = params.get("allowInsecure", ["0"])[0] == "1"
        flow = params.get("flow", [""])[0]

        stream: Dict[str, Any] = {"network": network}
        if network == "ws":
            stream["wsSettings"] = {
                "path": path,
                "headers": {"Host": host_header or sni or host}
            }
        elif network == "grpc":
            stream["grpcSettings"] = {
                "serviceName": params.get("serviceName", [""])[0]
            }
        else:
            stream["tcpSettings"] = {
                "header": {"type": params.get("headerType", ["none"])[0]}
            }

        if security:
            stream["security"] = security
            tls_key = "tlsSettings" if security == "tls" else "realitySettings"
            tls_settings: Dict[str, Any] = {}
            if sni:
                tls_settings["serverName"] = sni
            if alpn:
                tls_settings["alpn"] = alpn
            if fp:
                tls_settings["fingerprint"] = fp
            if allow_insecure:
                tls_settings["allowInsecure"] = True
            stream[tls_key] = tls_settings

        if flow:
            stream.setdefault("tcpSettings", {})
            stream["tcpSettings"].setdefault("header", {})
            stream["tcpSettings"]["header"]["type"] = flow

        outbound = {
            "tag": tag,
            "protocol": "trojan",
            "settings": {
                "servers": [{
                    "address": host,
                    "port": port,
                    "password": password,
                    "flow": flow or ""
                }]
            },
            "streamSettings": stream
        }
        return self.Outbound(tag, outbound)
