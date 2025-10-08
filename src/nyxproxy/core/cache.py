from __future__ import annotations

"""Funções de cache e preparação de entradas para o gerenciador de proxys."""

import json
import re
import time
from datetime import datetime
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

        # Aplica os campos do cache simplificado, se existirem
        status = cached.get("status")
        if isinstance(status, str):
            merged["status"] = status.strip() or merged["status"]

        country = cached.get("country")
        if isinstance(country, str) and country.strip():
            merged["country"] = country.strip()

        country_code = cached.get("country_code")
        if isinstance(country_code, str) and country_code.strip():
            merged["country_code"] = country_code.strip()

        ping = self._safe_float(cached.get("ping"))
        if ping is not None:
            merged["ping"] = ping

        tested_at_ts = self._safe_float(cached.get("tested_at_ts"))
        if tested_at_ts is not None:
            merged["tested_at_ts"] = tested_at_ts
            # Gera a string de data/hora a partir do timestamp para consistência
            merged["tested_at"] = self._format_timestamp(tested_at_ts)

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
        """Retorna carimbo de data no formato ISO 8601 com fuso horário local."""
        dt_local = datetime.fromtimestamp(ts).astimezone()
        iso = dt_local.replace(microsecond=0).isoformat()
        return iso

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
                
                tested_at_ts = entry.get("tested_at_ts")
                if not isinstance(tested_at_ts, (int, float)):
                    tested_at_ts = time.time()

                return {
                    "uri": uri,
                    "status": entry.get("status"),
                    "country": entry.get("country"),
                    "country_code": entry.get("country_code"),
                    "ping": entry.get("ping"),
                    "tested_at_ts": tested_at_ts,
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
        Analisa uma string de idade como '1D,5H' e retorna a duração TOTAL em segundos (soma de todas as partes).
        'H' para Horas, 'D' para Dias, 'S' para Semanas.
        """
        if not age_str:
            raise ValueError("A string de idade não pode estar vazia.")

        units = {
            'H': 3600,
            'D': 86400,
            'S': 604800,
        }
        
        parts = [p.strip().upper() for p in age_str.split(',')]
        total_seconds = 0
        
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
            
            total_seconds += value * units[unit]
            
        if total_seconds == 0:
            raise ValueError("Nenhum critério de tempo válido foi fornecido.")
            
        return float(total_seconds)

    def _format_duration_display(self, total_seconds: float) -> str:
        """Formata uma duração total em segundos para uma string legível (ex: '1 dia, 5 horas')."""
        if total_seconds <= 0:
            return "0 segundos"

        parts = []
        units = [
            ("semana", 604800),
            ("dia", 86400),
            ("hora", 3600),
        ]

        remaining_seconds = total_seconds

        for name, duration in units:
            if remaining_seconds >= duration:
                count = int(remaining_seconds // duration)
                plural = "s" if count > 1 else ""
                parts.append(f"{count} {name}{plural}")
                remaining_seconds %= duration

        if not parts and total_seconds > 0:
            num = total_seconds / 3600
            return f"{num:.1f} hora(s)"

        return ", ".join(parts)


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

        if age_str is None:
            self._save_cache([]) 
            if console:
                console.print(f"[green]Sucesso![/green] Cache com {initial_count} proxies foi completamente limpo.")
            return

        try:
            total_duration_sec = self._parse_age_str(age_str)
            age_display = self._format_duration_display(total_duration_sec)
        except ValueError as e:
            if console:
                console.print(f"[bold red]Erro:[/bold red] {e}")
            return

        now_ts = time.time()
        threshold_ts = now_ts - total_duration_sec

        entries_to_keep = [
            entry for entry in self._cache_entries.values()
            if isinstance(entry.get("tested_at_ts"), (int, float)) and entry["tested_at_ts"] > threshold_ts
        ]
        
        removed_count = initial_count - len(entries_to_keep)

        if removed_count == 0:
            if console:
                console.print(f"[green]Nenhuma proxy verificada há mais de {age_display} foi encontrada.[/green]")
        else:
            self._save_cache(entries_to_keep)
            if console:
                console.print(f"[green]Sucesso![/green] {removed_count} proxies antigas foram removidas ({len(entries_to_keep)} restantes no cache).")