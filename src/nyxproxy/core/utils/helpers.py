from __future__ import annotations

"""Utility functions shared among the manager's mixins."""

import base64
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

import aiofiles

from ..config.exceptions import XrayError
from ..models.proxy import TestResult


class ProxyUtilityMixin:
    """Auxiliary routines that do not depend on complex state."""

    @staticmethod
    def _b64decode_padded(value: str) -> bytes:
        """Decodes base64 (URL-safe) tolerating strings without padding."""
        value = value.strip().replace('-', '+').replace('_', '/')
        missing_padding = len(value) % 4
        if missing_padding:
            value += "=" * (4 - missing_padding)
        return base64.b64decode(value)

    @staticmethod
    def _sanitize_tag(tag: Optional[str], fallback: str) -> str:
        """Normalizes tags to something safe for use in files or logs."""
        if not tag or not tag.strip():
            return fallback
        safe_tag = re.sub(r"[^\w\-\. ]+", "", tag).strip()
        safe_tag = re.sub(r"\s+", "_", safe_tag)
        return safe_tag[:48] or fallback

    @staticmethod
    def _decode_bytes(data: bytes, *, encoding_hint: Optional[str] = None) -> str:
        """Robustly converts bytes to text by trying common encodings."""
        if not isinstance(data, (bytes, bytearray)):
            return str(data)

        encodings = ["utf-8", "latin-1"]
        if encoding_hint and encoding_hint.lower() not in encodings:
            encodings.insert(0, encoding_hint)

        for enc in encodings:
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        """Safely converts a value to int, returning None on failure."""
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        """Safely converts a value to float, returning None on failure."""
        if isinstance(value, float):
            return value
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    async def _read_source_text(self, source: str) -> str:
            """Gets content from a local file or URL, with error handling."""
            if re.match(r"^https?://", source, re.I):
                if self.requests is None:
                    raise RuntimeError(
                        "'requests' package is required to download from URLs."
                    )
                resp = await self.requests.get(
                    source, timeout=30, headers={'User-Agent': self.user_agent}
                )
                resp.raise_for_status()
                return self._decode_bytes(resp.content, encoding_hint=resp.encoding)

            path = Path(source)
            if not path.is_file():
                raise FileNotFoundError(f"Source file not found: {source}")
            async with aiofiles.open(path, "rb") as f:
                content = await f.read()
            return self._decode_bytes(content)

    @staticmethod
    def _shutil_which(cmd: str) -> Optional[str]:
        """Wrapper for shutil.which for compatibility and robustness."""
        return shutil.which(cmd)

    @classmethod
    def _which_xray(cls) -> str:
        """Finds the Xray binary, prioritizing the XRAY_PATH environment variable."""
        if env_path := os.environ.get("XRAY_PATH"):
            if Path(env_path).is_file():
                return env_path

        for candidate in ("xray", "v2ray"):
            if found := cls._shutil_which(candidate):
                return found

        raise XrayError(
            "Binary 'xray' or 'v2ray' not found. "
            "Install xray-core or set the XRAY_PATH environment variable."
        )

    @staticmethod
    def _format_destination(host: Optional[str], port: Optional[int]) -> str:
        """Formats 'host:port' for user-friendly display."""
        if not host or host == "-":
            return "-"
        return f"{host}:{port}" if port else host

    @staticmethod
    def _check_country_match(geo_info: Optional[Dict[str, Any]], desired: str) -> bool:
        """Checks if a dictionary of country fields matches the desired country."""
        if not geo_info:
            return False
        desired_norm = desired.strip().casefold()
        if not desired_norm:
            return True

        candidates = {
            str(geo_info.get(k) or "").strip().casefold()
            for k in ("label", "country_code", "country_name")
        }
        candidates.discard("")
        candidates.discard("-")

        return any(desired_norm == c for c in candidates)

    @classmethod
    def matches_country(cls, entry: TestResult, desired: Optional[str]) -> bool:
        """Validates if a proxy entry matches the country filter."""
        if not desired:
            return True

        effective_geo = entry.exit_geo or entry.server_geo
        if not effective_geo:
            return False

        return cls._check_country_match(effective_geo.__dict__, desired)