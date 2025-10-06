#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ferramenta orientada a biblioteca para testar e criar pontes HTTP para proxys V2Ray/Xray.

O módulo expõe a classe :class:`Proxy`, que gerencia carregamento de links, testes
com filtragem opcional por país e criação de túneis HTTP locais utilizando Xray ou
V2Ray. Todo o comportamento é pensado para uso programático em outros módulos.
"""

from __future__ import annotations

import atexit
import base64
import json
import os
import ipaddress
import random
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import parse_qs, unquote, urlparse, urlsplit
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


__all__ = ["Proxy"]

try:
    import requests  # opcional: apenas se precisar de rede
except Exception:  # pragma: no cover - manter funcionalidade sem requests
    requests = None

try:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text
except Exception:  # pragma: no cover - uso opcional de rich
    Console = None
    Table = None
    Text = None


class Proxy:
    """Gerencia uma coleção de proxys, com suporte a testes e criação de pontes HTTP."""

    DEFAULT_CACHE_FILENAME: str = "proxy_cache.json"
    CACHE_VERSION: int = 1

    STATUS_STYLES: Dict[str, str] = {
        "AGUARDANDO": "dim",
        "TESTANDO": "yellow",
        "OK": "bold green",
        "ERRO": "bold red",
        "FILTRADO": "cyan",
    }

    @dataclass(frozen=True)
    class Outbound:
        """Representa um outbound configurado para o Xray/V2Ray."""

        tag: str
        config: Dict[str, Any]

    @dataclass
    class BridgeRuntime:
        """Representa uma ponte HTTP ativa e seus recursos associados."""

        tag: str
        port: int
        scheme: str
        uri: str
        process: Optional[subprocess.Popen]
        workdir: Optional[Path]

        @property
        def url(self) -> str:
            return f"http://127.0.0.1:{self.port}"

    def __init__(
        self,
        proxies: Optional[Iterable[str]] = None,
        sources: Optional[Iterable[str]] = None,
        *,
        country: Optional[str] = None,
        base_port: int = 54000,
        max_count: int = 0,
        use_console: bool = False,
        use_cache: bool = True,
        cache_path: Optional[Union[str, os.PathLike]] = None,
        command_output: bool = True,
        requests_session: Optional[Any] = None,
    ) -> None:
        """Inicializa o gerenciador carregando proxys, fontes e cache se necessário."""
        self.country_filter = country
        self.base_port = base_port
        self.max_count = max_count
        self.requests = requests_session or requests
        self.use_console = bool(use_console and Console)
        self.console = Console() if self.use_console and Console else None
        self._port_allocation_lock = threading.Lock()
        self._allocated_ports = set()
        self._cache_lock = threading.Lock()

        self._outbounds: List[Tuple[str, Proxy.Outbound]] = []
        self._entries: List[Dict[str, Any]] = []
        self._bridges: List[Proxy.BridgeRuntime] = []
        self._running = False
        self._atexit_registered = False
        self._parse_errors: List[str] = []

        self.use_cache = use_cache
        default_cache_path = Path(__file__).with_name(self.DEFAULT_CACHE_FILENAME)
        self.cache_path = Path(cache_path) if cache_path is not None else default_cache_path
        self._cache_entries: Dict[str, Dict[str, Any]] = {}
        self._stop_event = threading.Event()
        self._wait_thread: Optional[threading.Thread] = None
        self.command_output = bool(command_output)
        self._cache_available = False

        if self.use_cache:
            self._load_cache()

        if proxies:
            self.add_proxies(proxies)
        if sources:
            self.add_sources(sources)

        if self.use_cache and not self._entries and self._outbounds:
            self._prime_entries_from_cache()

    # ----------- utilidades básicas -----------

    def _make_base_entry(self, index: int, raw_uri: str, outbound: Proxy.Outbound) -> Dict[str, Any]:
        """Monta o dicionário padrão com as informações mínimas de um outbound."""
        return {
            "index": index,
            "tag": outbound.tag,
            "uri": raw_uri,
            "status": "AGUARDANDO",
            "host": "-",
            "port": None,
            "ip": "-",
            "country": "-",
            "country_code": None,
            "country_name": None,
            "ping": None,
            "error": None,
            "country_match": None,
            "tested_at": None,
            "tested_at_ts": None,
            "cached": False,
        }

    def _apply_cached_entry(self, entry: Dict[str, Any], cached: Dict[str, Any]) -> Dict[str, Any]:
        """Mescla dados recuperados do cache ao registro corrente da proxy."""
        if not cached:
            return entry
        merged = dict(entry)

        text_fields = (
            "status",
            "host",
            "ip",
            "country",
            "country_code",
            "country_name",
            "proxy_ip",
            "proxy_country",
            "proxy_country_code",
            "error",
            "tested_at",
        )

        for key in text_fields:
            if key not in cached:
                continue
            value = cached.get(key)
            if isinstance(value, str):
                normalized = value.strip()
                if not normalized and key not in {"status", "error"}:
                    continue
                merged[key] = normalized or merged.get(key)
            elif value is not None:
                merged[key] = value

        port_value = cached.get("port")
        parsed_port = self._safe_int(port_value) if port_value is not None else None
        if parsed_port is not None:
            merged["port"] = parsed_port

        ping_value = cached.get("ping", cached.get("ping_ms"))
        parsed_ping = self._safe_float(ping_value) if ping_value is not None else None
        if parsed_ping is not None:
            merged["ping"] = parsed_ping

        tested_at_ts = self._safe_float(cached.get("tested_at_ts"))
        if tested_at_ts is not None:
            merged["tested_at_ts"] = tested_at_ts

        merged["cached"] = True
        return merged

    def _register_new_outbound(self, raw_uri: str, outbound: Proxy.Outbound) -> None:
        """Atualiza as estruturas internas quando um novo outbound é aceito."""
        index = len(self._outbounds)
        entry = self._make_base_entry(index, raw_uri, outbound)
        if self.use_cache and self._cache_entries:
            cached = self._cache_entries.get(raw_uri)
            if cached:
                entry = self._apply_cached_entry(entry, cached)
                entry["country_match"] = self.matches_country(entry, self.country_filter)
        self._entries.append(entry)

    def _prime_entries_from_cache(self) -> None:
        """Reconstrói os registros a partir do cache sem repetir parsing."""
        if not self.use_cache or not self._cache_entries:
            return
        rebuilt: List[Dict[str, Any]] = []
        for idx, (raw_uri, outbound) in enumerate(self._outbounds):
            entry = self._make_base_entry(idx, raw_uri, outbound)
            cached = self._cache_entries.get(raw_uri)
            if cached:
                entry = self._apply_cached_entry(entry, cached)
                entry["country_match"] = self.matches_country(entry, self.country_filter)
            rebuilt.append(entry)
        self._entries = rebuilt

    def _format_timestamp(self, ts: float) -> str:
        """Retorna carimbo de data no formato ISO 8601 UTC sem microssegundos."""
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        iso = dt.replace(microsecond=0).isoformat()
        return iso.replace("+00:00", "Z")

    def _load_cache(self) -> None:
        """Carrega resultados persistidos anteriormente para acelerar novos testes."""
        if not self.use_cache:
            return
        self._cache_available = False
        try:
            raw_cache = self.cache_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            return

        try:
            data = json.loads(raw_cache)
        except json.JSONDecodeError:
            return

        if not isinstance(data, dict):
            return
        entries = data.get("entries")
        if not isinstance(entries, list):
            return

        cache_map: Dict[str, Dict[str, Any]] = {}
        for item in entries:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri")
            if not isinstance(uri, str) or not uri.strip():
                continue
            cache_map[uri] = item

        self._cache_entries = cache_map
        if cache_map:
            self._cache_available = True

    def _save_cache(self, entries: List[Dict[str, Any]]) -> None:
        """Persiste a última bateria de testes para acelerar execuções futuras (thread-safe)."""
        if not self.use_cache:
            return

        with self._cache_lock:
            cache_dir = self.cache_path.parent
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

            def prepare(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
                if not isinstance(entry, dict):
                    return None
                uri = entry.get("uri")
                if not isinstance(uri, str) or not uri.strip():
                    return None

                tested_ts = self._safe_float(entry.get("tested_at_ts"))
                if tested_ts is None:
                    tested_ts = time.time()
                    entry["tested_at_ts"] = tested_ts

                tested_at = entry.get("tested_at")
                if not isinstance(tested_at, str) or not tested_at.strip():
                    tested_at = self._format_timestamp(tested_ts)
                    entry["tested_at"] = tested_at

                return {
                    "uri": uri,
                    "tag": entry.get("tag"),
                    "status": entry.get("status"),
                    "host": entry.get("host"),
                    "port": entry.get("port"),
                    "ip": entry.get("ip"),
                    "country": entry.get("country"),
                    "country_code": entry.get("country_code"),
                    "country_name": entry.get("country_name"),
                    "proxy_ip": entry.get("proxy_ip"),
                    "proxy_country": entry.get("proxy_country"),
                    "proxy_country_code": entry.get("proxy_country_code"),
                    "ping": entry.get("ping"),
                    "error": entry.get("error"),
                    "tested_at": tested_at,
                    "tested_at_ts": tested_ts,
                }

            payload_entries = [prepared for entry in entries if (prepared := prepare(entry))]

            payload = {
                "version": self.CACHE_VERSION,
                "generated_at": self._format_timestamp(time.time()),
                "entries": payload_entries,
            }

            try:
                self.cache_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except OSError:
                pass
            else:
                self._cache_entries = {item["uri"]: item for item in payload_entries}
                self._cache_available = bool(payload_entries)

    @staticmethod
    def _b64decode_padded(value: str) -> bytes:
        """Decodifica base64 tolerando strings sem padding."""
        value = value.strip()
        missing = (-len(value)) % 4
        if missing:
            value += "=" * missing
        return base64.urlsafe_b64decode(value)

    @staticmethod
    def _sanitize_tag(tag: Optional[str], fallback: str) -> str:
        """Normaliza tags para algo seguro de ser usado em arquivos ou logs."""
        if not tag:
            return fallback
        tag = re.sub(r"[^\w\-\.]+", "_", tag)
        return tag[:48] or fallback

    @staticmethod
    def _decode_bytes(data: bytes, *, encoding_hint: Optional[str] = None) -> str:
        """Converte bytes em texto testando codificações comuns."""
        if not isinstance(data, (bytes, bytearray)):
            return str(data)
        encodings = []
        if encoding_hint:
            encodings.append(encoding_hint)
        encodings.extend(["utf-8", "utf-8-sig", "latin-1"])
        tried = set()
        for enc in encodings:
            if not enc or enc in tried:
                continue
            tried.add(enc)
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        """Converte valores em int retornando ``None`` em caso de falha."""
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        """Converte valores em float retornando ``None`` em caso de falha."""
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _read_source_text(self, source: str) -> str:
        """Obtém conteúdo bruto de um arquivo local ou URL contendo proxys."""
        if re.match(r"^https?://", source, re.I):
            if self.requests is None:
                raise RuntimeError("O pacote requests não está disponível para baixar URLs de proxy.")
            resp = self.requests.get(source, timeout=30)
            resp.raise_for_status()
            return self._decode_bytes(resp.content, encoding_hint=resp.encoding or None)
        path = Path(source)
        return self._decode_bytes(path.read_bytes())

    @staticmethod
    def _shutil_which(cmd: str) -> Optional[str]:
        """Localiza um executável equivalente ao comportamento de shutil.which."""
        if hasattr(shutil, 'which') and callable(shutil.which):
            return shutil.which(cmd)
            
        paths = os.environ.get("PATH", "").split(os.pathsep)
        exts = [""]
        if os.name == "nt":
            exts = os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").lower().split(";")
        for directory in paths:
            candidate = Path(directory) / cmd
            if candidate.exists() and candidate.is_file() and os.access(str(candidate), os.X_OK):
                return str(candidate)
            if os.name == "nt":
                base = Path(directory) / cmd
                for ext in exts:
                    alt = base.with_suffix(ext)
                    if alt.exists() and alt.is_file() and os.access(str(alt), os.X_OK):
                        return str(alt)
        return None

    @classmethod
    def _which_xray(cls) -> str:
        """Descobre o binário do Xray/V2Ray respeitando variáveis de ambiente."""
        env_path = os.environ.get("XRAY_PATH")
        if env_path and Path(env_path).exists():
            return env_path
        for candidate in ("xray", "xray.exe", "v2ray", "v2ray.exe"):
            found = cls._shutil_which(candidate)
            if found:
                return found
        raise FileNotFoundError(
            "Não foi possível localizar o binário do Xray/V2Ray. Instale o xray-core ou configure XRAY_PATH."
        )

    @staticmethod
    def _format_destination(host: Optional[str], port: Optional[int]) -> str:
        """Monta representação amigável para host:porta exibida em tabelas."""
        if not host or host == "-":
            return "-"
        if port is None:
            return host
        return f"{host}:{port}"

    @staticmethod
    def _check_country_match(country_info: Dict[str, Any], desired: Optional[str]) -> bool:
        """Helper que verifica se um conjunto específico de campos de país corresponde ao país desejado."""
        if not desired:
            return True
        desired_norm = desired.strip().casefold()
        if not desired_norm:
            return True

        candidates = [
            str(country_info.get(k) or "").strip()
            for k in ("country", "country_code", "country_name")
            if country_info.get(k)
        ]
        candidates = [c for c in candidates if c and c != "-"]

        if not candidates:
            return False

        for c in candidates:
            if c.casefold() == desired_norm:
                return True
        for c in candidates:
            norm = c.casefold()
            if desired_norm in norm or norm in desired_norm:
                return True

        return False

    @classmethod
    def matches_country(cls, entry: Dict[str, Any], desired: Optional[str]) -> bool:
        """Valida se o registro atende ao filtro de país, exigindo que tanto o servidor quanto a saída correspondam."""
        if not desired:
            return True

        # Define informações para os locais de saída e do servidor
        exit_country_info = {
            "country": entry.get("proxy_country"),
            "country_code": entry.get("proxy_country_code"),
        }
        server_country_info = {
            "country": entry.get("country"),
            "country_code": entry.get("country_code"),
            "country_name": entry.get("country_name"),
        }

        # A localização efetiva é a de saída se existir, senão, a do servidor
        effective_exit_info = exit_country_info if exit_country_info.get("country") else server_country_info
        
        # Regra 1: A localização de saída efetiva DEVE corresponder
        if not cls._check_country_match(effective_exit_info, desired):
            return False
            
        # Regra 2: Se o servidor e a saída são diferentes, o servidor TAMBÉM DEVE corresponder
        if entry.get("proxy_ip") and entry.get("proxy_ip") != entry.get("ip"):
            if not cls._check_country_match(server_country_info, desired):
                return False
                
        # Se passou em todas as verificações, é uma correspondência válida
        return True

    # ----------- carregamento de proxys -----------

    def add_proxies(self, proxies: Iterable[str]) -> int:
        """Adiciona proxys a partir de URIs completos (ss, vmess, vless, trojan)."""
        added = 0
        for raw in proxies:
            if raw is None:
                continue
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            try:
                outbound = self._parse_uri_to_outbound(line)
            except Exception as exc:
                self._parse_errors.append(f"Linha ignorada: {line[:80]} -> {exc}")
                continue
            
            self._outbounds.append((line, outbound))
            self._register_new_outbound(line, outbound)
            
            added += 1
            if self.max_count and len(self._outbounds) >= self.max_count:
                break
        return added

    def add_sources(self, sources: Iterable[str]) -> int:
        """Carrega proxys de arquivos locais ou URLs linha a linha."""
        added = 0
        for src in sources:
            text = self._read_source_text(src)
            lines = [ln.strip() for ln in text.splitlines()]
            added += self.add_proxies(lines)
        return added

    # ----------- parsing -----------

    def _parse_uri_to_outbound(self, uri: str) -> Proxy.Outbound:
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

    def _parse_ss(self, uri: str) -> Proxy.Outbound:
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

        at_split = stripped_payload.rsplit('@', 1)
        if len(at_split) != 2:
            raise ValueError("Formato ss:// inválido.")
        userinfo_b64, hostport = at_split
        userinfo = None
        try:
            userinfo = self._decode_bytes(self._b64decode_padded(userinfo_b64))
        except Exception as exc:
            raise ValueError(f"Falha no base64 do ss://: {exc}") from exc
        if ':' not in userinfo:
            raise ValueError("Formato userinfo ss:// inválido (esperado method:password).")
        method, password = userinfo.split(':', 1)
        if ':' not in hostport:
            raise ValueError("Host ou porta ausentes no link ss://.")
        host, port_raw = hostport.split(':', 1)
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise ValueError(f"Porta ss inválida: {port_raw!r}") from exc

        config = {
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
        }
        return self.Outbound(tag, config)

    def _parse_vmess(self, uri: str) -> Proxy.Outbound:
        """Converte links ``vmess://`` com conteúdo base64 para outbounds."""
        payload = uri.strip()[8:]
        try:
            decoded = self._decode_bytes(self._b64decode_padded(payload))
        except Exception as exc:
            raise ValueError(f"Erro ao decodificar vmess://: {exc}") from exc
        try:
            data = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON inválido em vmess://: {exc}") from exc
        return self._vmess_outbound_from_dict(data)

    def _vmess_outbound_from_dict(self, data: Dict[str, Any], *, tag_fallback: str = "vmess") -> Proxy.Outbound:
        """Adapta o dicionário decodificado de vmess para a estrutura do Xray."""
        tag = self._sanitize_tag(data.get("ps"), tag_fallback)

        host = data.get("add") or data.get("address")
        port_raw = data.get("port", 0)
        try:
            port = int(str(port_raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"Porta vmess inválida: {port_raw!r}")

        uuid = data.get("id")
        if not host or not port or not uuid:
            raise ValueError("vmess:// incompleto (host/port/id).")

        alter_id = data.get("aid", 0)
        try:
            alter_id = int(str(alter_id).strip() or "0")
        except (TypeError, ValueError):
            alter_id = 0

        net = str(data.get("net") or data.get("network") or "tcp").lower()
        tls_flag = str(data.get("tls") or data.get("security") or "").lower()
        tls = tls_flag == "tls"
        security = "tls" if tls else "none"

        sni = data.get("sni") or data.get("host")
        path = data.get("path") or "/"
        host_header = data.get("host")

        if net == "ws":
            transport = {
                "network": "ws",
                "wsSettings": {
                    "path": path or "/",
                    "headers": {"Host": host_header} if host_header else {}
                }
            }
        elif net == "grpc":
            service_name = data.get("serviceName") or (path or "/").lstrip("/")
            transport = {
                "network": "grpc",
                "grpcSettings": {"serviceName": service_name}
            }
        else:
            transport = {"network": "tcp"}

        scy = data.get("scy") or "auto"

        outbound_config = {
            "tag": tag,
            "protocol": "vmess",
            "settings": {
                "vnext": [{
                    "address": host,
                    "port": port,
                    "users": [{
                        "id": uuid,
                        "alterId": alter_id,
                        "security": scy
                    }]
                }]
            },
            "streamSettings": {
                "security": security,
                **transport
            }
        }

        if tls and sni:
            outbound_config["streamSettings"]["tlsSettings"] = {"serverName": sni}

        return self.Outbound(tag, outbound_config)

    def _parse_vless(self, uri: str) -> Proxy.Outbound:
        """Converte links ``vless://`` adicionando suporte a transportes modernos."""
        p = urlparse(uri)
        tag = self._sanitize_tag(unquote(p.fragment) if p.fragment else None, "vless")
        uuid = p.username
        host = p.hostname
        port = p.port
        q = parse_qs(p.query or "")
        flow = q.get("flow", [""])[0]
        security = q.get("security", ["none"])[0]
        sni = q.get("sni", [None])[0]
        alpn = q.get("alpn", [])
        net = q.get("type", ["tcp"])[0]
        path = q.get("path", ["/"])[0]
        host_header = q.get("host", [None])[0]
        service_name = q.get("serviceName", [""])[0]

        if not uuid or not host or not port:
            raise ValueError("vless:// incompleto (uuid/host/port).")

        transport: Dict[str, Any]
        if net == "ws":
            transport = {"network": "ws", "wsSettings": {
                "path": path,
                "headers": {"Host": host_header} if host_header else {}
            }}
        elif net == "grpc":
            transport = {"network": "grpc", "grpcSettings": {"serviceName": service_name}}
        else:
            transport = {"network": "tcp"}

        stream = {"security": "none", **transport}
        if security in ("tls", "reality"):
            stream["security"] = security
            tls_key = "tlsSettings" if security == "tls" else "realitySettings"
            tls_settings: Dict[str, Any] = {}
            if sni:
                tls_settings["serverName"] = sni
            if alpn:
                tls_settings["alpn"] = alpn
            stream[tls_key] = tls_settings

        outbound = {
            "tag": tag,
            "protocol": "vless",
            "settings": {
                "vnext": [{
                    "address": host,
                    "port": port,
                    "users": [{
                        "id": uuid,
                        "encryption": q.get("encryption", ["none"])[0],
                        "flow": flow,
                    }]
                }]
            },
            "streamSettings": stream
        }
        return self.Outbound(tag, outbound)

    def _parse_trojan(self, uri: str) -> Proxy.Outbound:
        """Converte links ``trojan://`` assegurando parâmetros TLS e transporte."""
        p = urlparse(uri)
        tag = self._sanitize_tag(unquote(p.fragment) if p.fragment else None, "trojan")
        password = unquote(p.username or "")
        host = p.hostname
        port = p.port
        q = parse_qs(p.query or "")
        security = (q.get("security", ["tls"])[0]).lower()
        sni = q.get("sni", [None])[0]
        alpn = q.get("alpn", [])
        net = (q.get("type", ["tcp"])[0]).lower()
        path = q.get("path", ["/"])[0]
        host_header = q.get("host", [None])[0]
        service_name = q.get("serviceName", [""])[0]

        if not password or not host or not port:
            raise ValueError("trojan:// incompleto (password/host/port).")

        if net == "ws":
            transport: Dict[str, Any] = {
                "network": "ws",
                "wsSettings": {
                    "path": path,
                    "headers": {"Host": host_header} if host_header else {}
                }
            }
        elif net == "grpc":
            transport = {"network": "grpc", "grpcSettings": {"serviceName": service_name}}
        else:
            transport = {"network": "tcp"}

        stream: Dict[str, Any] = {"security": "none", **transport}
        if security in ("tls", "reality"):
            stream["security"] = security
            tls_key = "tlsSettings" if security == "tls" else "realitySettings"
            tls_settings: Dict[str, Any] = {}
            if sni:
                tls_settings["serverName"] = sni
            if alpn:
                tls_settings["alpn"] = alpn
            stream[tls_key] = tls_settings

        outbound = {
            "tag": tag,
            "protocol": "trojan",
            "settings": {
                "servers": [{
                    "address": host,
                    "port": port,
                    "password": password,
                    "flow": ""
                }]
            },
            "streamSettings": stream
        }
        return self.Outbound(tag, outbound)

    # ----------- verificação e filtros -----------

    def _outbound_host_port(self, outbound: Proxy.Outbound) -> Tuple[str, int]:
        """Extrai host e porta reais do outbound conforme o protocolo."""
        proto = outbound.config.get("protocol")
        settings = outbound.config.get("settings", {})
        host = None
        port = None
        if proto == "shadowsocks":
            server = settings.get("servers", [{}])[0]
            host = server.get("address")
            port = server.get("port")
        elif proto in ("vmess", "vless"):
            vnext = settings.get("vnext", [{}])[0]
            host = vnext.get("address")
            port = vnext.get("port")
        elif proto == "trojan":
            server = settings.get("servers", [{}])[0]
            host = server.get("address")
            port = server.get("port")
        else:
            raise ValueError(f"Protocolo não suportado para teste: {proto}")

        if host is None or port is None:
            raise ValueError(f"Host/port ausentes no outbound {outbound.tag} ({proto}).")
        try:
            return host, int(str(port).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Porta inválida no outbound {outbound.tag}: {port!r}") from exc

    @staticmethod
    def _is_public_ip(ip: str) -> bool:
        """Retorna ``True`` se o IP for público e roteável pela Internet."""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return not (
            addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_multicast or addr.is_link_local
        )

    def _lookup_country(self, ip: Optional[str]) -> Optional[Dict[str, Optional[str]]]:
        """Consulta informações de localização do IP usando findip.net."""
        if not ip or self.requests is None or not self._is_public_ip(ip):
            return None
        try:
            token = "747e7c8d93c344d2973066cf6eeb7d93"
            
            resp = self.requests.get(
                f"https://api.findip.net/{ip}/?token={token}", 
                timeout=5
            )
            resp.raise_for_status()
            data = resp.json()
            
            country_info = data.get("country", {})
            
            country_code = country_info.get("iso_code")
            if isinstance(country_code, str):
                country_code = (country_code.strip() or None)
                if country_code:
                    country_code = country_code.upper()
            
            country_names = country_info.get("names", {})
            country_name = country_names.get("en")
            
            if isinstance(country_name, str):
                country_name = country_name.strip() or None
            
            label = country_name or country_code
            
            if not (label or country_code or country_name):
                return None
            
            return {
                "name": country_name,
                "code": country_code,
                "label": label,
            }
        except Exception:
            return None

    def _test_outbound(self, raw_uri: str, outbound: Proxy.Outbound, timeout: float = 10.0) -> Dict[str, Any]:
        """Executa medições para um outbound específico retornando métricas usando rota real."""
        result: Dict[str, Any] = {
            "tag": outbound.tag,
            "protocol": outbound.config.get("protocol"),
            "uri": raw_uri,
        }
        
        try:
            host, port = self._outbound_host_port(outbound)
        except Exception as exc:
            result["error"] = f"host/port não identificados: {exc}"
            return result

        result["host"] = host
        result["port"] = port

        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except Exception:
            infos = []
        ip = None
        ipv6 = None
        for info in infos:
            family, *_rest, sockaddr = info
            address = sockaddr[0]
            if family == socket.AF_INET:
                ip = address
                break
            if ipv6 is None and family == socket.AF_INET6:
                ipv6 = address
        result["ip"] = ip or ipv6

        if result.get("ip"):
            country_info = self._lookup_country(result["ip"])
            if country_info:
                if label := country_info.get("label"):
                    result["country"] = label
                if code := country_info.get("code"):
                    result["country_code"] = code
                if name := country_info.get("name"):
                    result["country_name"] = name

        func_result = self._test_proxy_functionality(
            raw_uri, outbound, timeout=timeout
        )
        
        if func_result.get("functional"):
            result["ping_ms"] = func_result.get("response_time")
            result["functional"] = True
            result["external_ip"] = func_result.get("external_ip")
            
            if func_result.get("external_ip") and func_result["external_ip"] != result.get("ip"):
                result["proxy_ip"] = func_result["external_ip"]
                proxy_country = self._lookup_country(func_result["external_ip"])
                if proxy_country:
                    result["proxy_country"] = proxy_country.get("label")
                    result["proxy_country_code"] = proxy_country.get("code")
        else:
            result["error"] = func_result.get("error", "Proxy não funcional")
            result["functional"] = False

        return result

    def _perform_health_checks(
        self,
        outbounds: List[Tuple[str, Proxy.Outbound]],
        *,
        country_filter: Optional[str] = None,
        emit_progress: Optional[Any] = None,
        force_refresh: bool = False,
        functional_timeout: float = 10.0,
        threads: int = 1,
        stop_on_success: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Percorre os outbounds testando conectividade real de forma concorrente."""
        all_results: List[Dict[str, Any]] = []
        reuse_cache = self.use_cache and not force_refresh
        success_count = 0

        to_test: List[Tuple[int, str, Proxy.Outbound]] = []
        
        # Carrega resultados OK do cache primeiro
        if reuse_cache:
            for idx, (raw, outbound) in enumerate(outbounds):
                if raw in self._cache_entries:
                    cached_data = self._cache_entries[raw]
                    entry = self._apply_cached_entry(self._make_base_entry(idx, raw, outbound), cached_data)
                    
                    if entry.get("status") == "OK" and self.matches_country(entry, country_filter):
                        entry["country_match"] = True
                        all_results.append(entry)
                        success_count += 1
                        if emit_progress:
                            self._emit_test_progress(entry, len(all_results), len(outbounds), emit_progress)
                    else:
                        to_test.append((idx, raw, outbound))
                else:
                    to_test.append((idx, raw, outbound))
        else:
            to_test = list(enumerate(outbounds))


        limit_reached = stop_on_success is not None and stop_on_success > 0
        if limit_reached and success_count >= stop_on_success:
            if self.console:
                self.console.print(f"\n[bold green]Encontradas {success_count} proxies válidas no cache, atingindo o limite de {stop_on_success}. Testes adicionais ignorados.[/]")
            # Preenche o resto com entradas não testadas
            tested_uris = {e["uri"] for e in all_results}
            for idx, (raw, outbound) in enumerate(outbounds):
                if raw not in tested_uris:
                     all_results.append(self._make_base_entry(idx, raw, outbound))
            all_results.sort(key=lambda x: x.get("index", float('inf')))
            return all_results
        
        if to_test:
            def worker(idx: int, raw: str, outbound: Proxy.Outbound) -> Dict[str, Any]:
                """Testa uma proxy e retorna seu resultado."""
                entry = self._make_base_entry(idx, raw, outbound)
                try:
                    preview_host, preview_port = self._outbound_host_port(outbound)
                    entry.update({"host": preview_host, "port": preview_port})
                except Exception:
                    pass
                entry["status"] = "TESTANDO"

                result = self._test_outbound(raw, outbound, timeout=functional_timeout)
                finished_at = time.time()

                entry.update({
                    "host": result.get("host") or entry["host"],
                    "port": result.get("port") if result.get("port") is not None else entry["port"],
                    "ip": result.get("ip") or entry["ip"],
                    "country": result.get("country") or entry["country"],
                    "country_code": result.get("country_code") or entry.get("country_code"),
                    "country_name": result.get("country_name") or entry.get("country_name"),
                    "ping": result.get("ping_ms"),
                    "tested_at_ts": finished_at,
                    "tested_at": self._format_timestamp(finished_at),
                    "functional": result.get("functional", False),
                    "external_ip": result.get("external_ip"),
                    "proxy_ip": result.get("proxy_ip"),
                    "proxy_country": result.get("proxy_country"),
                    "proxy_country_code": result.get("proxy_country_code"),
                })

                if entry["functional"]:
                    entry["status"] = "OK"
                    entry["error"] = None
                else:
                    entry["status"] = "ERRO"
                    entry["error"] = result.get("error", "Teste falhou")

                if country_filter and entry["status"] == "OK":
                    entry["country_match"] = self.matches_country(entry, country_filter)
                    if not entry["country_match"]:
                        entry["status"] = "FILTRADO"
                        exit_country = entry.get("proxy_country") or entry.get("country") or "-"
                        server_country = entry.get("country") or "-"
                        if exit_country != server_country:
                             entry["error"] = f"Filtro '{country_filter}': Servidor ({server_country}) ou Saída ({exit_country}) não correspondem"
                        else:
                             entry["error"] = f"Filtro '{country_filter}': País de saída é {exit_country}"
                
                return entry

            if self.console and emit_progress:
                self.console.print(f"\n[yellow]Iniciando teste de {len(to_test)} proxies com até {threads} workers...[/]")
            
            with ThreadPoolExecutor(max_workers=threads) as executor:
                futures = {executor.submit(worker, idx, raw, outbound) for idx, raw, outbound in to_test}

                for future in as_completed(futures):
                    try:
                        result_entry = future.result()
                        all_results.append(result_entry)

                        if self.use_cache:
                            # Atualiza o cache em memória e salva no disco em tempo real
                            self._cache_entries[result_entry["uri"]] = result_entry
                            self._save_cache(list(self._cache_entries.values()))
                        
                        if emit_progress:
                            self._emit_test_progress(result_entry, len(all_results), len(outbounds), emit_progress)
                        
                        if result_entry.get("status") == "OK":
                            success_count += 1
                        
                        if limit_reached and success_count >= stop_on_success:
                            if self.console and emit_progress:
                                self.console.print(f"\n[bold green]Limite de {stop_on_success} proxies encontradas. Finalizando testes.[/]")
                            # Cancela futuros restantes
                            for f in futures:
                                if not f.done():
                                    f.cancel()
                            break
                    except Exception as exc:
                        if self.console:
                            self.console.print(f"[bold red]Erro fatal em uma thread de teste: {exc}[/]")

        # Garante que todas as proxies originais tenham uma entrada no resultado final
        final_uris = {e["uri"] for e in all_results}
        for idx, (raw, outbound) in enumerate(outbounds):
            if raw not in final_uris:
                all_results.append(self._make_base_entry(idx, raw, outbound))

        all_results.sort(key=lambda x: x.get("index", float('inf')))
        return all_results

    # ----------- interface pública -----------

    @property
    def entries(self) -> List[Dict[str, Any]]:
        """Retorna os registros carregados ou decorrentes dos últimos testes."""
        return self._entries

    @property
    def parse_errors(self) -> List[str]:
        """Lista de linhas ignoradas ao interpretar os links informados."""
        return list(self._parse_errors)

    @contextmanager
    def _temporary_bridge(
        self,
        outbound: Proxy.Outbound,
        *,
        tag_prefix: str = "temp",
    ):
        """Cria uma ponte Xray temporária garantindo limpeza de recursos."""
        port: Optional[int] = None
        proc: Optional[subprocess.Popen] = None
        cfg_dir: Optional[Path] = None

        try:
            port = self._find_available_port()
            cfg = self._make_xray_config_http_inbound(port, outbound)
            xray_bin = self._which_xray()
            
            proc, cfg_path = self._launch_bridge_with_diagnostics(
                xray_bin, cfg, f"{tag_prefix}_{outbound.tag}"
            )
            cfg_dir = cfg_path.parent

            time.sleep(1.0)
            if proc.poll() is not None:
                error_output = ""
                if proc.stderr:
                    error_output = self._decode_bytes(proc.stderr.read()).strip()
                
                raise RuntimeError(
                    "Processo Xray temporário finalizou antes do teste. "
                    f"Erro: {error_output or 'Nenhuma saída de erro.'}"
                )

            yield port, proc
        finally:
            self._terminate_process(proc, wait_timeout=2)
            self._safe_remove_dir(cfg_dir)
            if port is not None:
                self._release_port(port)


    def _test_proxy_functionality(
        self, 
        raw_uri: str, 
        outbound: Proxy.Outbound,
        timeout: float = 10.0,
        test_url: str = "http://httpbin.org/ip"
    ) -> Dict[str, Any]:
        """Testa a funcionalidade real da proxy criando uma ponte temporária e fazendo uma requisição."""
        result = {
            "functional": False,
            "response_time": None,
            "external_ip": None,
            "error": None
        }
        
        if self.requests is None:
            result["error"] = "requests não disponível para teste funcional"
            return result
        
        exceptions_mod = getattr(self.requests, "exceptions", None)
        if exceptions_mod is None and requests is not None:
            exceptions_mod = getattr(requests, "exceptions", None)

        response = None
        duration_ms: Optional[float] = None

        try:
            with self._temporary_bridge(outbound, tag_prefix="test") as (test_port, _):
                proxy_url = f"http://127.0.0.1:{test_port}"
                proxies = {"http": proxy_url, "https": proxy_url}
                start_time = time.perf_counter()
                
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                }

                response = self.requests.get(
                    test_url,
                    proxies=proxies,
                    timeout=timeout,
                    verify=False,
                    headers=headers
                )
                response.raise_for_status()
                duration_ms = (time.perf_counter() - start_time) * 1000
        except RuntimeError as exc:
            result["error"] = str(exc)
            return result
        except Exception as exc:
            result["error"] = self._format_request_error(exc, timeout, exceptions_mod)
            return result

        result["functional"] = True
        result["response_time"] = duration_ms
        if response is not None:
            result["external_ip"] = self._extract_external_ip(response)
        return result

    @staticmethod
    def _matches_exception(exc: Exception, candidate: Any) -> bool:
        """Retorna True se ``exc`` for instância de ``candidate`` (classe ou tupla)."""
        if candidate is None:
            return False
        try:
            return isinstance(exc, candidate)
        except TypeError:
            return False

    def _format_request_error(self, exc: Exception, timeout: float, exceptions_mod: Any) -> str:
        """Normaliza mensagens de erro de requisições HTTP via proxy."""
        timeout_exc = getattr(exceptions_mod, "Timeout", None) if exceptions_mod else None
        proxy_exc = getattr(exceptions_mod, "ProxyError", None) if exceptions_mod else None
        conn_exc = getattr(exceptions_mod, "ConnectionError", None) if exceptions_mod else None
        http_exc = getattr(exceptions_mod, "HTTPError", None) if exceptions_mod else None

        if self._matches_exception(exc, timeout_exc):
            return f"Timeout após {timeout:.1f}s"
        if self._matches_exception(exc, proxy_exc):
            return f"Erro de proxy: {str(exc)[:100]}"
        if self._matches_exception(exc, conn_exc):
            return f"Erro de conexão: {str(exc)[:100]}"
        if self._matches_exception(exc, http_exc):
            response = getattr(exc, 'response', None)
            if response is not None:
                return f"Erro HTTP {response.status_code}: {response.reason}"
        
        return f"Erro na requisição: {str(exc)[:100]}"

    @staticmethod
    def _extract_external_ip(response: Any) -> Optional[str]:
        """Extrai IP externo da resposta JSON do httpbin.org/ip."""
        try:
            data = response.json()
        except Exception:
            return None

        origin = data.get("origin")
        if isinstance(origin, str) and origin.strip():
            return origin.split(",")[0].strip()

        return None

    def _find_available_port(self) -> int:
        """Encontra uma porta TCP disponível pedindo ao SO para alocar uma."""
        with self._port_allocation_lock:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
                
                if port in self._allocated_ports:
                    return self._find_available_port()
                
                self._allocated_ports.add(port)
                return port
            except OSError as e:
                raise RuntimeError("Não foi possível alocar uma porta TCP disponível pelo sistema operacional.") from e
            finally:
                sock.close()

    @staticmethod
    def _terminate_process(proc: Optional[subprocess.Popen], *, wait_timeout: float = 3.0) -> None:
        """Finaliza um processo de forma silenciosa, ignorando erros."""
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=wait_timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    @staticmethod
    def _safe_remove_dir(path: Optional[Path]) -> None:
        """Remove diretórios temporários sem propagar exceções."""
        if path is None:
            return
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    def _release_port(self, port: Optional[int]) -> None:
        """Libera uma porta registrada como em uso pelos testes temporários."""
        if port is None:
            return
        with self._port_allocation_lock:
            self._allocated_ports.discard(port)

    def _emit_test_progress(self, entry: Dict[str, Any], count: int, total: int, emit_progress: Any) -> None:
        """Emite informações de progresso do teste."""
        destino = self._format_destination(entry.get("host"), entry.get("port"))
        ping_preview = entry.get("ping")
        ping_fmt = f"{ping_preview:.1f} ms" if isinstance(ping_preview, (int, float)) else "-"
        
        status_fmt = {
            "OK": "[bold green]OK[/]",
            "ERRO": "[bold red]ERRO[/]",
            "TESTANDO": "[yellow]TESTANDO[/]",
            "AGUARDANDO": "[dim]AGUARDANDO[/]",
            "FILTRADO": "[cyan]FILTRADO[/]",
        }.get(entry["status"], entry["status"])
        
        cache_note = ""
        if entry.get("cached"):
            cache_note = " [dim](cache)[/]" if Console else " (cache)"
        
        display_country = entry.get("proxy_country") or entry.get("country") or "-"
        
        emit_progress.print(
            f"[{count}/{total}] {status_fmt}{cache_note} [bold]{entry['tag']}[/] -> "
            f"{destino} | IP: {entry.get('ip') or '-'} | "
            f"País: {display_country} | Ping: {ping_fmt}"
        )
        
        if entry.get("proxy_ip") and entry.get("proxy_ip") != entry.get("ip"):
            original_country = entry.get("country", "-")
            emit_progress.print(
                f"    [dim]País do Servidor: {original_country} -> "
                f"País de Saída: {entry.get('proxy_country', '-')}[/]"
            )
        
        if entry.get("error"):
            emit_progress.print(f"    [dim]Motivo: {entry['error']}[/]")

    def test(
        self,
        *,
        threads: int = 1,
        country: Optional[str] = None,
        verbose: Optional[bool] = None,
        timeout: float = 10.0,
        force: bool = False,
        find_first: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Testa as proxies carregadas usando rota real para medir ping."""
        if not self._outbounds:
            raise RuntimeError("Nenhuma proxy carregada para testar.")

        country_filter = country if country is not None else self.country_filter
        emit = self.console if (self.console is not None and (verbose is None or verbose)) else None

        results = self._perform_health_checks(
            self._outbounds,
            country_filter=country_filter,
            emit_progress=emit,
            force_refresh=force,
            functional_timeout=timeout,
            threads=threads,
            stop_on_success=find_first,
        )

        self._entries = results
        self.country_filter = country_filter
        
        if self.console is not None and (verbose is None or verbose):
            self._render_test_summary(results, country_filter)

        return results

    def _render_test_summary(self, entries: List[Dict[str, Any]], country_filter: Optional[str]) -> None:
        """Exibe relatório amigável via Rich quando disponível."""
        if not self.console or Table is None:
            return
        
        ok_entries = [e for e in entries if e.get("status") == "OK"]
        if country_filter:
            table_entries = [entry for entry in ok_entries if entry.get("country_match")]
        else:
            table_entries = ok_entries

        self.console.print()
        self.console.rule("Proxies Funcionais")
        if table_entries:
            self.console.print(self._render_test_table(table_entries))
        else:
            msg = "[yellow]Nenhuma proxy funcional encontrada.[/yellow]"
            if country_filter:
                 msg = f"[yellow]Nenhuma proxy funcional corresponde ao filtro de país '{country_filter}'.[/yellow]"
            self.console.print(msg)

        success = sum(1 for entry in entries if entry.get("status") == "OK")
        fail = sum(1 for entry in entries if entry.get("status") == "ERRO")
        filtered = sum(1 for entry in entries if entry.get("status") == "FILTRADO")

        self.console.print()
        self.console.rule("Resumo do Teste")
        summary_parts = [
            f"[bold cyan]Total:[/] {len(entries)}",
            f"[bold green]Sucesso:[/] {success}",
            f"[bold red]Falhas:[/] {fail}",
        ]
        if filtered:
            summary_parts.append(f"[cyan]Filtradas:[/] {filtered}")
        self.console.print("    ".join(summary_parts))

        failed_entries = [
            entry for entry in entries
            if entry.get("status") == "ERRO" and entry.get("error")
        ]
        if failed_entries:
            self.console.print()
            self.console.print("[bold red]Detalhes das falhas:[/]")
            for entry in failed_entries[:10]:
                self.console.print(f" - [bold]{entry.get('tag') or '-'}[/]: {entry['error']}")
            if len(failed_entries) > 10:
                self.console.print(f"  [dim]... e mais {len(failed_entries) - 10} outras falhas.[/dim]")


    @staticmethod
    def _render_test_table(entries: List[Dict[str, Any]]):
        """Gera uma tabela Rich com o resultado dos testes."""
        if Table is None:
            raise RuntimeError("render_test_table requer a biblioteca 'rich'.")
        
        entries.sort(key=lambda e: e.get("ping") or float('inf'))

        table = Table(show_header=True, header_style="bold cyan", expand=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Tag", no_wrap=True, max_width=30)
        table.add_column("Destino", overflow="fold")
        table.add_column("IP Real (Saída)", no_wrap=True)
        table.add_column("País (Saída)", no_wrap=True)
        table.add_column("Ping", justify="right", no_wrap=True)
        for entry in entries:
            status = entry.get("status", "-")
            style = Proxy.STATUS_STYLES.get(status, "white")
            status_cell = Text(status, style=style) if Text else status
            destino = Proxy._format_destination(entry.get("host"), entry.get("port"))
            ping = entry.get("ping")
            ping_str = f"{ping:.1f} ms" if isinstance(ping, (int, float)) else "-"
            
            display_ip = entry.get("proxy_ip") or entry.get("ip") or "-"
            display_country = entry.get("proxy_country") or entry.get("country") or "-"
            
            table.add_row(
                status_cell,
                (entry.get("tag") or "-"),
                destino,
                display_ip,
                display_country,
                ping_str,
            )
        return table

    def start(
        self,
        *,
        threads: int = 1,
        amounts: int = 1,
        country: Optional[str] = None,
        auto_test: bool = True,
        wait: bool = False,
        find_first: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Cria pontes HTTP locais para as proxys aprovadas, testando se necessário."""
        if self._running:
            raise RuntimeError("As pontes já estão em execução. Chame stop() antes de iniciar novamente.")
        if not self._outbounds:
            raise RuntimeError("Nenhuma proxy carregada para iniciar.")

        country_filter = country if country is not None else self.country_filter
        
        if auto_test:
            # Verifica primeiro proxies válidas no cache
            ok_from_cache = [
                e for e in self._entries 
                if e.get("status") == "OK" and self.matches_country(e, country_filter)
            ]

            needed_proxies = find_first or amounts
            if len(ok_from_cache) < needed_proxies:
                if self.console:
                    self.console.print(f"[yellow]Cache insuficiente. Procurando e testando até {needed_proxies} proxies válidas...[/yellow]")
                
                self.test(
                    threads=threads,
                    country=country_filter,
                    verbose=False,  # O start terá seu próprio resumo
                    find_first=needed_proxies,
                    force=False # Não re-testa o que já está OK no cache
                )
            elif self.console:
                self.console.print("[green]Proxies suficientes encontradas no cache. Iniciando imediatamente.[/green]")

        approved_entries = [
            entry for entry in self._entries
            if entry.get("status") == "OK" 
            and self.matches_country(entry, country_filter)
        ]
        
        def get_ping_for_sort(entry: Dict[str, Any]) -> float:
            ping = entry.get("ping")
            return float(ping) if isinstance(ping, (int, float)) else float('inf')
        
        approved_entries.sort(key=get_ping_for_sort)
        
        if not approved_entries:
            if country_filter:
                raise RuntimeError(
                    f"Nenhuma proxy aprovada para o país '{country_filter}'. "
                    "Execute o teste e verifique os resultados."
                )
            else:
                raise RuntimeError("Nenhuma proxy aprovada para iniciar. Execute test() e verifique os resultados.")

        if amounts > 0:
            if len(approved_entries) < amounts:
                 if self.console:
                    self.console.print(
                        f"[yellow]Aviso: Apenas {len(approved_entries)} proxies aprovadas encontradas (solicitado: {amounts}). "
                        "Iniciando as disponíveis.[/yellow]"
                    )
            approved_entries = approved_entries[:amounts]
        

        xray_bin = self._which_xray()

        self._stop_event.clear()
        bridges_runtime: List[Proxy.BridgeRuntime] = []
        bridges_display: List[Tuple[Proxy.BridgeRuntime, float]] = []

        if self.console and approved_entries:
            self.console.print()
            self.console.print(
                f"[green]Iniciando {len(approved_entries)} pontes ordenadas por ping[/]"
            )

        try:
            for entry in approved_entries:
                raw_uri, outbound = self._outbounds[entry["index"]]
                
                port = self._find_available_port()
                cfg = self._make_xray_config_http_inbound(port, outbound)
                scheme = raw_uri.split("://", 1)[0].lower()

                proc, cfg_path = self._launch_bridge_with_diagnostics(xray_bin, cfg, outbound.tag)
                bridge = Proxy.BridgeRuntime(
                    tag=outbound.tag,
                    port=port,
                    scheme=scheme,
                    uri=raw_uri,
                    process=proc,
                    workdir=cfg_path.parent,
                )
                bridges_runtime.append(bridge)
                bridges_display.append((bridge, get_ping_for_sort(entry)))
        except Exception:
            for bridge in bridges_runtime:
                self._terminate_process(bridge.process)
                self._safe_remove_dir(bridge.workdir)
                self._release_port(bridge.port)
            raise

        self._bridges = bridges_runtime
        self._running = True

        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True

        if self.console:
            self.console.print()
            self.console.rule(f"Pontes HTTP ativas{f' - País: {country_filter}' if country_filter else ''} - Ordenadas por Ping")
            for idx, (bridge, ping) in enumerate(bridges_display):
                ping_str = f"{ping:6.1f}ms" if ping != float('inf') else "      -      "
                self.console.print(
                    f"[bold cyan]ID {idx:<2}[/] http://127.0.0.1:{bridge.port}  ->  [{ping_str}]"
                )

            self.console.print()
            self.console.print("Pressione Ctrl+C para encerrar todas as pontes.")

        bridges_with_id = [
            {"id": idx, "url": bridge.url, "uri": bridge.uri}
            for idx, bridge in enumerate(self._bridges)
        ]

        if wait:
            self.wait()
        else:
            self._start_wait_thread()

        return bridges_with_id

    def _start_wait_thread(self) -> None:
        """Dispara thread em segundo plano para monitorar processos iniciados."""
        if self._wait_thread and self._wait_thread.is_alive():
            return
        thread = threading.Thread(target=self._wait_loop_wrapper, name="ProxyWaitThread", daemon=True)
        self._wait_thread = thread
        thread.start()

    def _wait_loop_wrapper(self) -> None:
        """Executa ``wait`` capturando exceções para um término limpo da thread."""
        try:
            self.wait()
        except RuntimeError:
            pass

    def wait(self) -> None:
        """Bloqueia até que todas as pontes terminem ou ``stop`` seja chamado."""
        if not self._running:
            raise RuntimeError("Nenhuma ponte ativa para aguardar.")
        try:
            while not self._stop_event.is_set():
                alive = any(
                    bridge.process and bridge.process.poll() is None
                    for bridge in self._bridges
                )
                if not alive:
                    if self.console:
                        self.console.print("\n[yellow]Todos os processos xray finalizaram.[/yellow]")
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            if self.console:
                self.console.print("\n[yellow]Interrupção recebida, encerrando pontes...[/yellow]")
        finally:
            self.stop()

    def stop(self) -> None:
        """Finaliza processos Xray ativos e limpa arquivos temporários."""
        if not self._running and not self._bridges:
            return

        self._stop_event.set()
        
        bridges_to_stop = list(self._bridges)
        if bridges_to_stop:
            for bridge in bridges_to_stop:
                self._terminate_process(bridge.process)
                self._safe_remove_dir(bridge.workdir)
                self._release_port(bridge.port)

        self._bridges = []
        self._running = False

        if self._wait_thread and self._wait_thread is not threading.current_thread():
            self._wait_thread.join(timeout=1.0)
        self._wait_thread = None

    def get_http_proxy(self) -> List[Dict[str, Any]]:
        """Retorna ID, URL local e URI de cada ponte em execução."""
        if not self._running:
            return []
        return [
            {"id": idx, "url": bridge.url, "uri": bridge.uri}
            for idx, bridge in enumerate(self._bridges)
        ]

    # ----------- geração e execução de config -----------

    def _make_xray_config_http_inbound(self, port: int, outbound: Proxy.Outbound) -> Dict[str, Any]:
        """Monta o arquivo de configuração do Xray para uma ponte HTTP local."""
        cfg = {
            "log": {"loglevel": "warning"},
            "inbounds": [{
                "tag": "http-in",
                "listen": "127.0.0.1",
                "port": port,
                "protocol": "http",
                "settings": {}
            }],
            "outbounds": [
                outbound.config,
                {"tag": "direct", "protocol": "freedom", "settings": {}},
                {"tag": "block", "protocol": "blackhole", "settings": {}}
            ],
            "routing": {
                "domainStrategy": "AsIs",
                "rules": [
                    {"type": "field", "outboundTag": outbound.tag, "network": "tcp,udp"}
                ]
            }
        }
        if "tag" not in cfg["outbounds"][0]:
            cfg["outbounds"][0]["tag"] = outbound.tag
        return cfg

    def _launch_bridge_with_diagnostics(
        self, xray_bin: str, cfg: Dict[str, Any], name: str
    ) -> Tuple[subprocess.Popen, Path]:
        """Inicializa o Xray com captura de stdout/stderr para melhor diagnóstico."""
        tmpdir = Path(tempfile.mkdtemp(prefix=f"xray_{name}_"))
        cfg_path = tmpdir / "config.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

        proc = subprocess.Popen(
            [xray_bin, "-config", str(cfg_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return proc, cfg_path
    
    def rotate_proxy(self, bridge_id: int) -> bool:
        """Troca a proxy de uma ponte em execução por outra proxy aleatória e funcional."""
        if not self._running or not (0 <= bridge_id < len(self._bridges)):
            if self.console:
                msg = f"ID de ponte inválido: {bridge_id}. IDs válidos: 0 a {len(self._bridges) - 1}."
                self.console.print(f"[red]Erro: {msg}[/red]")
            return False

        bridge = self._bridges[bridge_id]
        uri_to_replace = bridge.uri

        candidates = [
            entry for entry in self._entries
            if entry.get("status") == "OK"
            and self.matches_country(entry, self.country_filter)
            and entry.get("uri") != uri_to_replace
        ]

        if not candidates:
            if self.console:
                self.console.print(f"[yellow]Aviso: Nenhuma outra proxy disponível para rotacionar a ponte ID {bridge_id}.[/yellow]")
            return False

        new_entry = random.choice(candidates)
        new_raw_uri, new_outbound = self._outbounds[new_entry["index"]]
        new_scheme = new_raw_uri.split("://", 1)[0].lower()

        self._terminate_process(bridge.process, wait_timeout=2)
        self._safe_remove_dir(bridge.workdir)

        try:
            xray_bin = self._which_xray()
            cfg = self._make_xray_config_http_inbound(bridge.port, new_outbound)
            new_proc, new_cfg_path = self._launch_bridge_with_diagnostics(xray_bin, cfg, new_outbound.tag)
        except Exception as e:
            if self.console:
                self.console.print(f"[red]Falha ao reiniciar ponte {bridge_id} na porta {bridge.port}: {e}[/red]")
            bridge.process = None # Marca a ponte como inativa
            return False

        self._bridges[bridge_id] = Proxy.BridgeRuntime(
            tag=new_outbound.tag,
            port=bridge.port,
            scheme=new_scheme,
            uri=new_raw_uri,
            process=new_proc,
            workdir=new_cfg_path.parent,
        )

        if self.console:
            self.console.print(
                f"[green]Sucesso:[/green] Ponte [bold]ID {bridge_id}[/] (porta {bridge.port}) rotacionada para a proxy '[bold]{new_outbound.tag}[/]'"
            )

        return True