from __future__ import annotations

"""Funções utilitárias compartilhadas entre os mixins do gerenciador."""

import base64
import os
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Optional


class ProxyUtilityMixin:
    """Rotinas auxiliares que não dependem de estado complexo."""

    @staticmethod
    def _b64decode_padded(value: str) -> bytes:
        """Decodifica base64 tolerando strings sem padding."""
        value = value.strip()
        missing = (-len(value)) % 4
        if missing:
            value += "=" * missing
        return base64.urlsafe_b64decode(value)

    @staticmethod
    def _sanitize_tag(tag: Optional[str], fallback: str) -> str:
        """Normaliza tags para algo seguro de ser usado em arquivos ou logs."""
        if not tag:
            return fallback
        tag = re.sub(r"[^\w\-\.]+", "_", tag)
        return tag[:48] or fallback

    @staticmethod
    def _decode_bytes(data: bytes, *, encoding_hint: Optional[str] = None) -> str:
        """Converte bytes em texto testando codificações comuns."""
        if not isinstance(data, (bytes, bytearray)):
            return str(data)
        encodings = []
        if encoding_hint:
            encodings.append(encoding_hint)
        encodings.extend(["utf-8", "utf-8-sig", "latin-1"])
        tried = set()
        for enc in encodings:
            if not enc or enc in tried:
                continue
            tried.add(enc)
            try:
                return data.decode(enc)
            except UnicodeDecodeError:
                continue
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        """Converte valores em int retornando ``None`` em caso de falha."""
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        """Converte valores em float retornando ``None`` em caso de falha."""
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _read_source_text(self, source: str) -> str:
        """Obtém conteúdo bruto de um arquivo local ou URL contendo proxys."""
        if re.match(r"^https?://", source, re.I):
            if self.requests is None:
                raise RuntimeError("O pacote requests não está disponível para baixar URLs de proxy.")
            resp = self.requests.get(source, timeout=30)
            resp.raise_for_status()
            return self._decode_bytes(resp.content, encoding_hint=resp.encoding or None)
        path = Path(source)
        return self._decode_bytes(path.read_bytes())

    @staticmethod
    def _shutil_which(cmd: str) -> Optional[str]:
        """Localiza um executável equivalente ao comportamento de shutil.which."""
        if hasattr(shutil, "which") and callable(shutil.which):
            return shutil.which(cmd)

        paths = os.environ.get("PATH", "").split(os.pathsep)
        exts = [""]
        if os.name == "nt":
            exts = os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").lower().split(";")
        for directory in paths:
            candidate = Path(directory) / cmd
            if candidate.exists() and candidate.is_file() and os.access(str(candidate), os.X_OK):
                return str(candidate)
            if os.name == "nt":
                base = Path(directory) / cmd
                for ext in exts:
                    alt = base.with_suffix(ext)
                    if alt.exists() and alt.is_file() and os.access(str(alt), os.X_OK):
                        return str(alt)
        return None

    @classmethod
    def _which_xray(cls) -> str:
        """Descobre o binário do Xray/V2Ray respeitando variáveis de ambiente."""
        env_path = os.environ.get("XRAY_PATH")
        if env_path and Path(env_path).exists():
            return env_path
        for candidate in ("xray", "xray.exe", "v2ray", "v2ray.exe"):
            found = cls._shutil_which(candidate)
            if found:
                return found
        raise FileNotFoundError(
            "Não foi possível localizar o binário do Xray/V2Ray. Instale o xray-core ou configure XRAY_PATH."
        )

    @staticmethod
    def _format_destination(host: Optional[str], port: Optional[int]) -> str:
        """Monta representação amigável para host:porta exibida em tabelas."""
        if not host or host == "-":
            return "-"
        if port is None:
            return host
        return f"{host}:{port}"

    @staticmethod
    def _check_country_match(country_info: Dict[str, Any], desired: Optional[str]) -> bool:
        """Helper que verifica se um conjunto específico de campos de país corresponde ao país desejado."""
        if not desired:
            return True
        desired_norm = desired.strip().casefold()
        if not desired_norm:
            return True

        candidates = [
            str(country_info.get(k) or "").strip()
            for k in ("country", "country_code", "country_name")
            if country_info.get(k)
        ]
        candidates = [c for c in candidates if c and c != "-"]

        if not candidates:
            return False

        for c in candidates:
            if c.casefold() == desired_norm:
                return True
        for c in candidates:
            norm = c.casefold()
            if desired_norm in norm or norm in desired_norm:
                return True

        return False

    @classmethod
    def matches_country(cls, entry: Dict[str, Any], desired: Optional[str]) -> bool:
        """Valida se o registro atende ao filtro de país, exigindo que tanto o servidor quanto a saída correspondam."""
        if not desired:
            return True

        exit_country_info = {
            "country": entry.get("proxy_country"),
            "country_code": entry.get("proxy_country_code"),
        }
        server_country_info = {
            "country": entry.get("country"),
            "country_code": entry.get("country_code"),
            "country_name": entry.get("country_name"),
        }

        effective_exit_info = exit_country_info if exit_country_info.get("country") else server_country_info

        if not cls._check_country_match(effective_exit_info, desired):
            return False

        if entry.get("proxy_ip") and entry.get("proxy_ip") != entry.get("ip"):
            if not cls._check_country_match(server_country_info, desired):
                return False

        return True
