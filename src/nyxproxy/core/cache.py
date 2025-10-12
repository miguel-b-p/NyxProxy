from __future__ import annotations

"""Cache functions and entry preparation for the proxy manager."""

import json
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import Outbound, TestResult


class CacheMixin:
    """Set of routines responsible for handling the proxy cache."""

    def _create_test_result_from_outbound(
        self, uri: str, outbound: Outbound
    ) -> TestResult:
        """Builds a default TestResult object from basic outbound info."""
        return TestResult(
            uri=uri,
            tag=outbound.tag,
            protocol=outbound.protocol,
            host=outbound.host,
            port=outbound.port,
        )

    def _apply_cached_data(self, result: TestResult, cached: Dict[str, Any]) -> None:
        """Merges data recovered from the cache into the current proxy record."""
        if not cached:
            return

        result.status = str(cached.get("status", result.status)).strip()
        ping = self._safe_float(cached.get("ping"))
        if ping is not None:
            result.ping = ping

        tested_at_ts = self._safe_float(cached.get("tested_at_ts"))
        if tested_at_ts is not None:
            result.tested_at_ts = tested_at_ts

        # Restore geolocation data from cache
        if "server_geo" in cached and isinstance(cached["server_geo"], dict):
            result.server_geo = self.GeoInfo(**cached["server_geo"])
        if "exit_geo" in cached and isinstance(cached["exit_geo"], dict):
            result.exit_geo = self.GeoInfo(**cached["exit_geo"])

    def _register_new_outbound(self, raw_uri: str, outbound: Outbound) -> None:
        """Updates internal structures when a new outbound is accepted."""
        self._outbounds[raw_uri] = outbound
        result = self._create_test_result_from_outbound(raw_uri, outbound)

        if self.use_cache and raw_uri in self._cache_entries:
            cached_data = self._cache_entries[raw_uri]
            self._apply_cached_data(result, cached_data)

        self._entries.append(result)

    def _prime_entries_from_cache(self) -> None:
        """Reconstructs records from the cache without re-parsing."""
        if not self.use_cache or not self._cache_entries:
            return

        rebuilt: List[TestResult] = []
        for raw_uri, outbound in self._outbounds.items():
            result = self._create_test_result_from_outbound(raw_uri, outbound)
            if cached := self._cache_entries.get(raw_uri):
                self._apply_cached_data(result, cached)
            rebuilt.append(result)
        self._entries = rebuilt

    def _load_outbounds_from_cache(self) -> None:
        """Loads outbounds directly from cache when no sources are given."""
        if not self.use_cache:
            return
        for uri, cached_data in self._cache_entries.items():
            try:
                outbound = self._parse_uri_to_outbound(uri)
                self._register_new_outbound(uri, outbound)
            except Exception:
                continue  # nosec B112 - Ignore URIs from cache that can no longer be parsed

    def _load_and_register_cache_entry(
        self, uri: str, cached_data: Dict[str, Any]
    ) -> None:
        """
        Parses a cached URI, creates an Outbound, and registers it.

        This helper function encapsulates the logic for loading a single valid
        proxy from the cache that was not present in the initial sources. It
        parses the URI, creates the corresponding `Outbound` configuration,
        and then uses `_register_new_outbound` to add it to the internal
        manager lists (`_outbounds` and `_entries`). The registration process
        will automatically re-apply the cached test data (`TestResult`).

        Args:
            uri: The proxy URI string from the cache.
            cached_data: The dictionary of cached data for this URI.
        """
        outbound = self._parse_uri_to_outbound(uri)
        self._register_new_outbound(uri, outbound)

    def _merge_ok_cache_entries(self) -> None:
        """
        Merges valid proxies from the cache into the current session.

        This method iterates through the cache entries and loads any proxy
        that has a status of 'OK' but was not loaded from the current set of
        sources. This ensures that previously tested, functional proxies are
        not lost when new sources are provided.
        """
        if not self.use_cache:
            return
        for uri, cached_data in self._cache_entries.items():
            try:
                # Condition 1: Proxy is not already loaded from a source
                # Condition 2: Proxy is marked as functional in the cache
                if uri not in self._outbounds and cached_data.get("status") == "OK":
                    self._load_and_register_cache_entry(uri, cached_data)
            except Exception:
                # Ignore URIs from the cache that can no longer be parsed
                # or cause other errors during registration.
                continue  # nosec B112

    @staticmethod
    def _format_timestamp(ts: float) -> str:
        """Returns a timestamp in ISO 8601 format with local timezone."""
        try:
            dt_local = datetime.fromtimestamp(ts).astimezone()
            return dt_local.replace(microsecond=0).isoformat()
        except (OSError, ValueError):  # Timestamps too large/small
            return "Invalid Date"

    def _load_cache(self) -> None:
        """Loads previously persisted results to speed up new tests."""
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

    def _save_cache(self) -> None:
        """Persists the latest batch of tests securely (thread-safe)."""
        if not self.use_cache:
            return

        with self._cache_lock:
            payload_entries = []
            for entry in self._entries:
                if entry.tested_at_ts is None:
                    continue  # Only save entries that have been tested

                payload_entry = {
                    "uri": entry.uri,
                    "status": entry.status,
                    "ping": entry.ping,
                    "tested_at_ts": entry.tested_at_ts,
                }
                if entry.server_geo:
                    payload_entry["server_geo"] = entry.server_geo.__dict__
                if entry.exit_geo:
                    payload_entry["exit_geo"] = entry.exit_geo.__dict__

                payload_entries.append(payload_entry)

            payload = {
                "version": self.CACHE_VERSION,
                "generated_at": self._format_timestamp(time.time()),
                "entries": payload_entries,
            }

            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self._cache_entries = {item["uri"]: item for item in payload_entries}
                self._cache_available = bool(payload_entries)
            except OSError as e:
                if self.console:
                    self.console.print(f"[red]Error saving cache: {e}[/red]")

    def _parse_age_str(self, age_str: str) -> float:
        """Analyzes a string like '1D,5H' and returns the duration in seconds."""
        if not age_str:
            raise ValueError("Age string cannot be empty.")

        units = {"H": 3600, "D": 86400, "W": 604800}  # Hours, Days, Weeks
        total_seconds = 0

        parts = [p.strip().upper() for p in age_str.split(',')]
        for part in parts:
            if not part:
                continue

            match = re.match(r"^(\d+)([HDW])$", part)
            if not match:
                raise ValueError(
                    f"Invalid format: '{part}'. Use number followed by H, D, or W."
                )

            value, unit = int(match.group(1)), match.group(2)
            if value <= 0:
                raise ValueError(f"Time value must be positive: '{part}'.")

            total_seconds += value * units[unit]

        if total_seconds == 0:
            raise ValueError("No valid time criteria provided.")

        return float(total_seconds)

    def _format_duration_display(self, total_seconds: float) -> str:
        """Formats a duration in seconds into a readable string."""
        if total_seconds <= 0:
            return "0 seconds"

        parts = []
        units = [("week", 604800), ("day", 86400), ("hour", 3600)]

        for name, duration in units:
            if total_seconds >= duration:
                count = int(total_seconds // duration)
                plural = "s" if count > 1 else ""
                parts.append(f"{count} {name}{plural}")
                total_seconds %= duration

        return ", ".join(parts) or f"{total_seconds:.0f} seconds"

    def clear_cache(self, age_str: Optional[str] = None) -> None:
        """Clears the cache, optionally removing only old entries."""
        if not self._cache_entries and self.cache_path.exists():
            self._load_cache()

        initial_count = len(self._cache_entries)
        if initial_count == 0:
            if self.console:
                self.console.print("[green]Cache is already empty.[/green]")
            return

        if age_str is None:
            # Full cleanup
            self._save_cache_from_list([])
            if self.console:
                self.console.print(
                    f"[green]Success![/green] Cache with {initial_count} proxies has been completely cleared."
                )
            return

        try:
            duration_sec = self._parse_age_str(age_str)
            age_display = self._format_duration_display(duration_sec)
        except ValueError as e:
            if self.console:
                self.console.print(f"[bold red]Error:[/bold red] {e}")
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
                self.console.print(f"[green]No proxies older than {age_display} were found.[/green]")
        else:
            self._save_cache_from_list(entries_to_keep)
            if self.console:
                self.console.print(
                    f"[green]Success![/green] {removed_count} old proxies removed "
                    f"({len(entries_to_keep)} remaining)."
                )

    def _save_cache_from_list(self, entries: List[Dict[str, Any]]) -> None:
        """Helper to save a list of dicts to the cache file."""
        payload = {
            "version": self.CACHE_VERSION,
            "generated_at": self._format_timestamp(time.time()),
            "entries": entries,
        }
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self._cache_entries = {item["uri"]: item for item in entries}
            self._cache_available = bool(entries)
        except OSError as e:
            if self.console:
                self.console.print(f"[red]Error saving cache: {e}[/red]")