from __future__ import annotations

"""Carregamento de proxys a partir de fontes externas."""

from typing import Iterable

import requests


class LoadingMixin:
    """Operações responsáveis por adicionar proxys ao gerenciador."""

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
            try:
                text = self._read_source_text(src)
                lines = [ln.strip() for ln in text.splitlines()]
                added += self.add_proxies(lines)
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
        return added