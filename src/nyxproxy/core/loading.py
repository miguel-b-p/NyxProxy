from __future__ import annotations

"""Loading of proxies from external sources."""

from typing import Iterable

import httpx

from .exceptions import ProxyParsingError


class LoadingMixin:
    """Operations responsible for adding proxies to the manager."""

    def add_proxies(self, proxies: Iterable[str]) -> int:
        """Adds proxies from URIs, returning the number added."""
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
                        self.console.print(f"[yellow]Limit of {self.max_count} proxies reached.[/yellow]")
                    break
            except ProxyParsingError as exc:
                self._parse_errors.append(f"Line ignored: {line[:80]} -> {exc}")

        return added_count

    async def add_sources(self, sources: Iterable[str]) -> int:
        """Loads proxies from local files or URLs, returning the total added."""
        total_added = 0
        for src in sources:
            if not src:
                continue
            try:
                text = await self._read_source_text(src)
                lines = text.splitlines()
                total_added += self.add_proxies(lines)
                if self.max_count and len(self._outbounds) >= self.max_count:
                    break
            except FileNotFoundError:
                if self.console:
                    self.console.print(f"[bold red]Error:[/bold red] File not found: '{src}'")
            except httpx.RequestError as e:
                if self.console:
                    error_reason = str(e).split('\n', 1)[0]
                    self.console.print(f"[bold red]Error:[/bold red] Failed to download from '{src}': {error_reason}")
            except Exception as e:
                if self.console:
                    self.console.print(f"[bold red]Error:[/bold red] Failed to process source '{src}': {e}")

        return total_added