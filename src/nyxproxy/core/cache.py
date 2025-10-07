from __future__ import annotations

"""Funções de cache e preparação de entradas para o gerenciador de proxys."""

import json
import re
import time
from datetime import datetime, timezone, timedelta
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
            "tested_at", # <-- O campo de data principal agora é uma string
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

        # --- REMOVIDO ---
        # A lógica para 'tested_at_ts' foi removida pois o campo não será mais usado.

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
                
                # --- LÓGICA ALTERADA ---
                # Gera a string de data se não existir. Este é o único campo de data agora.
                tested_at = entry.get("tested_at")
                if not isinstance(tested_at, str) or not tested_at.strip():
                    tested_at = self._format_timestamp(time.time())

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
                    # O campo 'tested_at_ts' foi completamente removido.
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

    def _parse_age_str(self, age_str: str) -> float:
        """
        Analisa uma string de idade como '1D,5H,2S' e retorna a duração mínima em segundos.
        'H' para Horas, 'D' para Dias, 'S' para Semanas.
        """
        if not age_str:
            raise ValueError("A string de idade não pode estar vazia.")

        units = {
            'H': 60 * 60,           # Horas
            'D': 24 * 60 * 60,      # Dias
            'S': 7 * 24 * 60 * 60,  # Semanas
        }
        
        parts = [p.strip().upper() for p in age_str.split(',')]
        durations_in_seconds = []
        
        for part in parts:
            if not part:
                continue
            
            match = re.match(r'^(\d+)([HDS])$', part)
            if not match:
                raise ValueError(f"Formato de tempo inválido: '{part}'. Use números seguidos de 'H' (horas), 'D' (dias) ou 'S' (semanas).")
            
            value = int(match.group(1))
            unit = match.group(2)
            
            if value <= 0:
                raise ValueError(f"O valor do tempo deve ser positivo: '{part}'.")
            
            durations_in_seconds.append(value * units[unit])
            
        if not durations_in_seconds:
            raise ValueError("Nenhum critério de tempo válido foi fornecido.")
            
        return min(durations_in_seconds)

    def clear_cache(self, age_str: Optional[str] = None, console: Optional[Any] = None) -> None:
        """
        Limpa o cache de proxies. Se age_str for fornecido, remove apenas entradas mais antigas.
        """
        if not self.cache_path.exists() and not self._cache_entries:
            if console:
                console.print("[yellow]Cache não encontrado. Nada a fazer.[/yellow]")
            return
        
        if not self._cache_entries and self.cache_path.exists():
            self._load_cache()

        initial_count = len(self._cache_entries)

        if initial_count == 0:
            if console:
                console.print("[green]Cache já está vazio.[/green]")
            if self.cache_path.exists():
                try:
                    self.cache_path.unlink()
                except OSError:
                    pass
            return

        # Limpeza total
        if age_str is None:
            try:
                self._save_cache([]) 
                if console:
                    console.print(f"[green]Sucesso![/green] Cache com {initial_count} proxies foi completamente limpo.")
            except Exception as e:
                if console:
                    console.print(f"[red]Erro ao limpar o cache: {e}[/red]")
            return

        # Limpeza parcial
        try:
            min_duration_sec = self._parse_age_str(age_str)
            
            if min_duration_sec >= 7 * 24 * 60 * 60 and min_duration_sec % (7 * 24 * 60 * 60) == 0:
                num = int(min_duration_sec / (7 * 24 * 60 * 60))
                age_display = f"{num} semana(s)"
            elif min_duration_sec >= 24 * 60 * 60 and min_duration_sec % (24 * 60 * 60) == 0:
                num = int(min_duration_sec / (24 * 60 * 60))
                age_display = f"{num} dia(s)"
            else:
                num = int(min_duration_sec / (60 * 60))
                age_display = f"{num} hora(s)"

        except ValueError as e:
            if console:
                console.print(f"[bold red]Erro:[/bold red] {e}")
            return

        # --- LÓGICA DE COMPARAÇÃO TOTALMENTE ALTERADA ---
        # 1. Calcula o momento exato do "limite" no passado.
        now_dt = datetime.now(timezone.utc)
        threshold_dt = now_dt - timedelta(seconds=min_duration_sec)
        
        # 2. Formata esse limite como uma string ISO 8601, igual ao formato do cache.
        threshold_str = threshold_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

        # 3. Mantém apenas as entradas cuja string de data é MAIOR (mais recente) que a string de limite.
        #    A comparação de strings funciona para este formato.
        entries_to_keep = []
        for entry in self._cache_entries.values():
            entry_time_str = entry.get("tested_at")
            if entry_time_str and entry_time_str > threshold_str:
                entries_to_keep.append(entry)
        
        removed_count = initial_count - len(entries_to_keep)

        if removed_count == 0:
            if console:
                console.print(f"[green]Nenhuma proxy verificada há mais de {age_display} foi encontrada.[/green]")
        else:
            self._save_cache(entries_to_keep)
            if console:
                console.print(f"[green]Sucesso![/green] {removed_count} proxies antigas foram removidas. {len(entries_to_keep)} restantes no cache.")