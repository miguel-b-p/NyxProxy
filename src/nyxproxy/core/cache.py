from __future__ import annotations

"""Funções de cache e preparação de entradas para o gerenciador de proxys."""

import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .models import Outbound


class CacheMixin:
    """Conjunto de rotinas responsáveis por lidar com cache de proxys."""

    def _make_base_entry(self, index: int, raw_uri: str, outbound: Outbound) -> Dict[str, Any]:
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

    def _register_new_outbound(self, raw_uri: str, outbound: Outbound) -> None:
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
