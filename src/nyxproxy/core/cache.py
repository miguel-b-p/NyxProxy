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

    def _make_base_entry(
        self, index: int, raw_uri: str, outbound: Outbound
    ) -> Dict[str, Any]:
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

    def _apply_cached_entry(
        self, entry: Dict[str, Any], cached: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Mescla dados recuperados do cache ao registro corrente da proxy."""
        if not cached:
            return entry

        merged = dict(entry)
        merged["cached"] = True

        # Aplica os campos do cache, garantindo tipos corretos
        merged["status"] = str(cached.get("status", merged["status"])).strip()
        merged["country"] = str(cached.get("country", merged["country"])).strip()
        merged["country_code"] = str(
            cached.get("country_code", merged["country_code"])
        ).strip()
        
        ping = self._safe_float(cached.get("ping"))
        if ping is not None:
            merged["ping"] = ping

        tested_at_ts = self._safe_float(cached.get("tested_at_ts"))
        if tested_at_ts is not None:
            merged["tested_at_ts"] = tested_at_ts
            merged["tested_at"] = self._format_timestamp(tested_at_ts)

        return merged

    def _register_new_outbound(self, raw_uri: str, outbound: Outbound) -> None:
        """Atualiza as estruturas internas quando um novo outbound é aceito."""
        index = len(self._outbounds)
        self._outbounds.append((raw_uri, outbound))
        
        entry = self._make_base_entry(index, raw_uri, outbound)
        if self.use_cache and raw_uri in self._cache_entries:
            cached = self._cache_entries[raw_uri]
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
            if cached := self._cache_entries.get(raw_uri):
                entry = self._apply_cached_entry(entry, cached)
                entry["country_match"] = self.matches_country(entry, self.country_filter)
            rebuilt.append(entry)
        self._entries = rebuilt

    def _format_timestamp(self, ts: float) -> str:
        """Retorna carimbo de data no formato ISO 8601 com fuso horário local."""
        try:
            dt_local = datetime.fromtimestamp(ts).astimezone()
            return dt_local.replace(microsecond=0).isoformat()
        except (OSError, ValueError): # Timestamps muito grandes/pequenos
            return "Data inválida"

    def _load_cache(self) -> None:
        """Carrega resultados persistidos anteriormente para acelerar novos testes."""
        if not self.use_cache:
            return
        
        self._cache_available = False
        if not self.cache_path.is_file():
            return

        try:
            raw_cache = self.cache_path.read_text(encoding="utf-8")
            data = json.loads(raw_cache)
            if not isinstance(data, dict):
                return
        except (OSError, json.JSONDecodeError):
            return

        entries = data.get("entries")
        if not isinstance(entries, list):
            return

        cache_map: Dict[str, Dict[str, Any]] = {}
        for item in entries:
            if isinstance(item, dict) and isinstance(item.get("uri"), str):
                cache_map[item["uri"]] = item
        
        self._cache_entries = cache_map
        if cache_map:
            self._cache_available = True

    def _save_cache(self, entries: List[Dict[str, Any]]) -> None:
        """Persiste a última bateria de testes de forma segura (thread-safe)."""
        if not self.use_cache:
            return

        with self._cache_lock:
            # Prepara a lista de entradas para salvar, mantendo apenas os campos essenciais
            payload_entries = []
            for entry in entries:
                if not (isinstance(entry, dict) and (uri := entry.get("uri"))):
                    continue
                
                # Garante que 'tested_at_ts' exista para o cache
                tested_at_ts = entry.get("tested_at_ts")
                if not isinstance(tested_at_ts, (int, float)):
                    # Se um item do cache antigo sem timestamp for carregado,
                    # ele não será salvo novamente a menos que seja re-testado.
                    continue

                payload_entries.append({
                    "uri": uri,
                    "status": entry.get("status"),
                    "country": entry.get("country"),
                    "country_code": entry.get("country_code"),
                    "ping": entry.get("ping"),
                    "tested_at_ts": tested_at_ts,
                })

            payload = {
                "version": self.CACHE_VERSION,
                "generated_at": self._format_timestamp(time.time()),
                "entries": payload_entries,
            }

            try:
                # Cria o diretório pai, se não existir
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                # Atualiza o cache em memória com os dados que foram salvos
                self._cache_entries = {item["uri"]: item for item in payload_entries}
                self._cache_available = bool(payload_entries)
            except OSError as e:
                if self.console:
                    self.console.print(f"[red]Erro ao salvar cache: {e}[/red]")

    def _parse_age_str(self, age_str: str) -> float:
        """Analisa uma string como '1D,5H' e retorna a duração em segundos."""
        if not age_str:
            raise ValueError("A string de idade não pode estar vazia.")

        units = {"H": 3600, "D": 86400, "S": 604800} # Horas, Dias, Semanas
        total_seconds = 0
        
        parts = [p.strip().upper() for p in age_str.split(',')]
        for part in parts:
            if not part:
                continue
            
            match = re.match(r"^(\d+)([HDS])$", part)
            if not match:
                raise ValueError(
                    f"Formato inválido: '{part}'. Use Nº seguido de H, D, ou S."
                )
            
            value, unit = int(match.group(1)), match.group(2)
            if value <= 0:
                raise ValueError(f"O valor do tempo deve ser positivo: '{part}'.")
            
            total_seconds += value * units[unit]
            
        if total_seconds == 0:
            raise ValueError("Nenhum critério de tempo válido fornecido.")
            
        return float(total_seconds)

    def _format_duration_display(self, total_seconds: float) -> str:
        """Formata uma duração em segundos para uma string legível."""
        if total_seconds <= 0:
            return "0 segundos"

        parts = []
        units = [("semana", 604800), ("dia", 86400), ("hora", 3600)]

        for name, duration in units:
            if total_seconds >= duration:
                count = int(total_seconds // duration)
                plural = "s" if count > 1 else ""
                parts.append(f"{count} {name}{plural}")
                total_seconds %= duration

        return ", ".join(parts) or f"{total_seconds:.0f} segundos"

    def clear_cache(self, age_str: Optional[str] = None) -> None:
        """Limpa o cache, opcionalmente removendo apenas entradas antigas."""
        # Garante que o cache esteja carregado do disco, se existir
        if not self._cache_entries and self.cache_path.exists():
            self._load_cache()

        initial_count = len(self._cache_entries)
        if initial_count == 0:
            if self.console:
                self.console.print("[green]Cache já está vazio.[/green]")
            return

        if age_str is None:
            # Limpeza completa
            self._save_cache([])
            if self.console:
                self.console.print(
                    f"[green]Sucesso![/green] Cache com {initial_count} proxies foi completamente limpo."
                )
            return

        try:
            duration_sec = self._parse_age_str(age_str)
            age_display = self._format_duration_display(duration_sec)
        except ValueError as e:
            if self.console:
                self.console.print(f"[bold red]Erro:[/bold red] {e}")
            return

        now_ts = time.time()
        threshold_ts = now_ts - duration_sec

        entries_to_keep = [
            entry for entry in self._cache_entries.values()
            if isinstance(entry.get("tested_at_ts"), (int, float)) 
            and entry["tested_at_ts"] > threshold_ts
        ]
        
        removed_count = initial_count - len(entries_to_keep)
        if removed_count == 0:
            if self.console:
                self.console.print(f"[green]Nenhuma proxy com mais de {age_display} foi encontrada.[/green]")
        else:
            self._save_cache(entries_to_keep)
            if self.console:
                self.console.print(
                    f"[green]Sucesso![/green] {removed_count} proxies antigas removidas "
                    f"({len(entries_to_keep)} restantes)."
                )