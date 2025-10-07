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
            "tested_at_ts": None, # <-- NOVO CAMPO
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

        # --- NOVA LÓGICA ---
        # Prioriza o timestamp numérico para consistência.
        tested_at_ts_value = cached.get("tested_at_ts")
        parsed_ts = self._safe_float(tested_at_ts_value)
        if parsed_ts is not None:
            merged["tested_at_ts"] = parsed_ts

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
        # --- LÓGICA ALTERADA PARA USAR O FUSO HORÁRIO LOCAL ---
        # Cria um datetime a partir do timestamp e o torna ciente do fuso horário do sistema
        dt_local = datetime.fromtimestamp(ts).astimezone()
        # Formata para ISO 8601, que incluirá o offset do fuso (ex: -03:00)
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
                
                # --- LÓGICA DE TEMPO ALTERADA E ROBUSTA ---
                # O timestamp numérico é a fonte da verdade.
                tested_at_ts = entry.get("tested_at_ts")
                if not isinstance(tested_at_ts, (int, float)):
                    tested_at_ts = time.time()  # Gera um novo se for inválido ou ausente

                # A string de data é gerada a partir do timestamp para consistência.
                tested_at_str = self._format_timestamp(tested_at_ts)

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
                    "tested_at": tested_at_str,     # Mantido para leitura humana
                    "tested_at_ts": tested_at_ts,   # Usado para lógica de comparação
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
            
        # --- LÓGICA CORRIGIDA: SOMA EM VEZ DE MÍNIMO ---
        return sum(durations_in_seconds)

    def _format_duration_display(self, total_seconds: float) -> str:
        """Formata uma duração total em segundos para uma string legível (ex: '1 dia, 5 horas')."""
        if total_seconds <= 0:
            return "0 segundos"

        parts = []
        units = [
            ("semana", 7 * 24 * 3600),
            ("dia", 24 * 3600),
            ("hora", 3600),
        ]

        remaining_seconds = total_seconds

        for name, duration in units:
            if remaining_seconds >= duration:
                count = int(remaining_seconds // duration)
                plural = "s" if count > 1 else ""
                parts.append(f"{count} {name}{plural}")
                remaining_seconds %= duration

        if not parts:
             # Se for menos de 1 hora, exibe como horas (pode ser decimal)
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
            total_duration_sec = self._parse_age_str(age_str)
            age_display = self._format_duration_display(total_duration_sec)

        except ValueError as e:
            if console:
                console.print(f"[bold red]Erro:[/bold red] {e}")
            return

        # --- LÓGICA DE COMPARAÇÃO ROBUSTA COM TIMESTAMPS (CORREÇÃO PRINCIPAL) ---
        # 1. Calcula o timestamp limite no passado.
        now_ts = time.time()
        threshold_ts = now_ts - total_duration_sec

        # 2. Mantém apenas as entradas cujo timestamp numérico é MAIS RECENTE que o limite.
        #    Esta comparação numérica é inequívoca e corrige o bug.
        entries_to_keep = []
        for entry in self._cache_entries.values():
            entry_ts = entry.get("tested_at_ts")
            # Se não houver timestamp numérico na entrada, ela é tratada como antiga e removida.
            if isinstance(entry_ts, (int, float)) and entry_ts > threshold_ts:
                entries_to_keep.append(entry)
        
        removed_count = initial_count - len(entries_to_keep)

        if removed_count == 0:
            if console:
                console.print(f"[green]Nenhuma proxy verificada há mais de {age_display} foi encontrada.[/green]")
        else:
            self._save_cache(entries_to_keep)
            if console:
                console.print(f"[green]Sucesso![/green] {removed_count} proxies antigas foram removidas ({len(entries_to_keep)} restantes no cache).")