from __future__ import annotations

"""Rotinas de parsing de URIs de proxy para estruturas Xray/V2Ray."""

import json
import re
from typing import Any, Dict, List
from urllib.parse import parse_qs, unquote, urlparse


class ParsingMixin:
    """Responsável por interpretar diferentes esquemas de proxy."""

    def _parse_uri_to_outbound(self, uri: str):
        """Direciona o link para o parser adequado de acordo com o esquema."""
        uri = uri.strip()
        if not uri or uri.startswith(("#", "//")):
            raise ValueError("Linha vazia ou comentário.")

        match = re.match(r"^([a-z0-9]+)://", uri, re.I)
        if not match:
            raise ValueError(f"Esquema desconhecido na URI: {uri[:80]}")

        scheme = match.group(1).lower()
        parser = getattr(self, f"_parse_{scheme}", None)
        if parser is None:
            raise ValueError(f"Esquema não suportado: {scheme}")
        
        return parser(uri)

    def _parse_ss(self, uri: str):
        """Normaliza um link ``ss://`` para um outbound Shadowsocks."""
        parsed = urlparse(uri)
        tag = self._sanitize_tag(unquote(parsed.fragment) if parsed.fragment else None, "ss")

        # Formato: ss://<base64_encoded_part>#<tag>
        encoded_part = parsed.netloc + parsed.path
        if not encoded_part:
            raise ValueError("Link ss:// está vazio ou malformado.")

        try:
            decoded_text = self._decode_bytes(self._b64decode_padded(encoded_part))
        except Exception as exc:
            raise ValueError(f"Falha ao decodificar base64 do ss://: {exc}") from exc
        
        # Formato decodificado: method:password@hostname:port
        match = re.match(r"^(?P<method>.+?):(?P<password>.+?)@(?P<host>.+?):(?P<port>\d+)$", decoded_text)
        if not match:
            raise ValueError("Formato ss:// decodificado inválido.")
        
        data = match.groupdict()
        port = self._safe_int(data["port"])
        if port is None:
            raise ValueError(f"Porta inválida no ss://: {data['port']!r}")

        return self.Outbound(tag, {
            "tag": tag,
            "protocol": "shadowsocks",
            "settings": {
                "servers": [{
                    "address": data["host"],
                    "port": port,
                    "method": data["method"],
                    "password": data["password"],
                }]
            }
        })

    def _parse_vmess(self, uri: str):
        """Converte links ``vmess://`` baseados em JSON para um outbound Vmess."""
        payload = uri.strip()[8:]
        try:
            decoded = self._b64decode_padded(payload)
            data = json.loads(self._decode_bytes(decoded))
        except Exception as exc:
            raise ValueError(f"Payload vmess:// inválido: {exc}") from exc

        return self._vmess_outbound_from_dict(data)

    def _vmess_outbound_from_dict(self, data: Dict[str, Any], *, tag_fallback: str = "vmess"):
        """Constrói o outbound de vmess a partir do dicionário decodificado."""
        if not isinstance(data, dict):
            raise ValueError("Dados vmess devem ser um dicionário.")
        
        add = data.get("add")
        port_raw = data.get("port")
        uuid = data.get("id")
        if not all((add, port_raw, uuid)):
            raise ValueError("Dados vmess incompletos (add, port ou id ausente).")
        
        port = self._safe_int(port_raw)
        if port is None:
            raise ValueError(f"Porta vmess inválida: {port_raw!r}")

        tag = self._sanitize_tag(data.get("ps"), tag_fallback)
        params = {k: [str(v)] for k, v in data.items()}
        stream_settings = self._build_stream_settings(params, add)

        outbound = {
            "tag": tag,
            "protocol": "vmess",
            "settings": {
                "vnext": [{
                    "address": add,
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
        return self.Outbound(tag, outbound)

    def _parse_vless(self, uri: str):
        """Normaliza links ``vless://`` com suporte a RealITY para outbound VLESS."""
        parsed = urlparse(uri)
        uuid = parsed.username
        host = parsed.hostname
        port = parsed.port
        if not all((uuid, host, port)):
            raise ValueError("Link vless:// incompleto (usuário, host ou porta ausente).")
        
        params = parse_qs(parsed.query)
        tag = self._sanitize_tag(unquote(parsed.fragment) if parsed.fragment else None, "vless")
        stream_settings = self._build_stream_settings(params, host)

        outbound = {
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
        return self.Outbound(tag, outbound)

    def _parse_trojan(self, uri: str):
        """Converte links ``trojan://`` com suporte a WebSocket para outbound Trojan."""
        parsed = urlparse(uri)
        password = parsed.username
        host = parsed.hostname
        port = parsed.port
        if not all((password, host, port)):
            raise ValueError("Link trojan:// incompleto (senha, host ou porta ausente).")

        params = parse_qs(parsed.query)
        tag = self._sanitize_tag(unquote(parsed.fragment) if parsed.fragment else None, "trojan")
        stream_settings = self._build_stream_settings(params, host)

        outbound = {
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
        return self.Outbound(tag, outbound)
    
    def _build_stream_settings(
        self, params: Dict[str, List[str]], host: str
    ) -> Dict[str, Any]:
        """Cria a estrutura de streamSettings com base nos parâmetros da URI."""
        network = params.get("type", ["tcp"])[0]
        security = params.get("security", [""])[0]
        sni = params.get("sni", [host])[0] or host

        stream: Dict[str, Any] = {"network": network}

        # Configurações de transporte (ws, grpc, etc.)
        if network == "ws":
            ws_host = params.get("host", [sni])[0]
            stream["wsSettings"] = {
                "path": params.get("path", ["/"])[0],
                "headers": {"Host": ws_host or sni},
            }
        elif network == "grpc":
            stream["grpcSettings"] = {"serviceName": params.get("serviceName", [""])[0]}

        # Configurações de segurança (tls, reality)
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