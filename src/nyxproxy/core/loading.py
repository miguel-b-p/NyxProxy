from __future__ import annotations

"""Carregamento de proxys a partir de fontes externas."""

from typing import Iterable

import requests


class LoadingMixin:
    """Operações responsáveis por adicionar proxys ao gerenciador."""

    def add_proxies(self, proxies: Iterable[str]) -> int:
        """Adiciona proxys a partir de URIs, retornando o número de adicionados."""
        added_count = 0
        for raw_uri in proxies:
            if not raw_uri:
                continue
            
            line = raw_uri.strip()
            if not line or line.startswith(("#", "//")):
                continue
            
            try:
                outbound = self._parse_uri_to_outbound(line)
                self._register_new_outbound(line, outbound)
                added_count += 1
                if self.max_count and len(self._outbounds) >= self.max_count:
                    if self.console:
                        self.console.print(f"[yellow]Limite de {self.max_count} proxies atingido.[/yellow]")
                    break
            except Exception as exc:
                self._parse_errors.append(f"Linha ignorada: {line[:80]} -> {exc}")

        return added_count

    def add_sources(self, sources: Iterable[str]) -> int:
        """Carrega proxys de arquivos locais ou URLs, retornando o total adicionado."""
        total_added = 0
        for src in sources:
            if not src:
                continue
            try:
                text = self._read_source_text(src)
                lines = text.splitlines()
                total_added += self.add_proxies(lines)
                if self.max_count and len(self._outbounds) >= self.max_count:
                    break
            except FileNotFoundError:
                if self.console:
                    self.console.print(f"[bold red]Erro:[/bold red] Arquivo não encontrado: '{src}'")
            except requests.exceptions.RequestException as e:
                if self.console:
                    error_reason = str(e).split('\n', 1)[0]
                    self.console.print(f"[bold red]Erro:[/bold red] Falha ao baixar de '{src}': {error_reason}")
            except Exception as e:
                if self.console:
                    self.console.print(f"[bold red]Erro:[/bold red] Falha ao processar fonte '{src}': {e}")
        
        return total_added