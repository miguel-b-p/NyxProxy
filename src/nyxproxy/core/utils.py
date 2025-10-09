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
        """Decodifica base64 (URL-safe) tolerando strings sem padding."""
        value = value.strip().replace('-', '+').replace('_', '/')
        missing_padding = len(value) % 4
        if missing_padding:
            value += "=" * (4 - missing_padding)
        return base64.b64decode(value)

    @staticmethod
    def _sanitize_tag(tag: Optional[str], fallback: str) -> str:
        """Normaliza tags para algo seguro de ser usado em arquivos ou logs."""
        if not tag or not tag.strip():
            return fallback
        # Remove caracteres problemáticos, substitui espaços e limita o comprimento
        safe_tag = re.sub(r"[^\w\-\. ]+", "", tag).strip()
        safe_tag = re.sub(r"\s+", "_", safe_tag)
        return safe_tag[:48] or fallback

    @staticmethod
    def _decode_bytes(data: bytes, *, encoding_hint: Optional[str] = None) -> str:
        """Converte bytes em texto testando codificações comuns de forma robusta."""
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
        # Fallback final com substituição de caracteres problemáticos
        return data.decode("utf-8", errors="replace")

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        """Converte um valor em int de forma segura, retornando None em caso de falha."""
        if isinstance(value, int):
            return value
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        """Converte um valor em float de forma segura, retornando None em caso de falha."""
        if isinstance(value, float):
            return value
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return None

    def _read_source_text(self, source: str) -> str:
        """Obtém conteúdo de um arquivo local ou URL, com tratamento de erro."""
        if re.match(r"^https?://", source, re.I):
            if self.requests is None:
                raise RuntimeError("O pacote 'requests' é necessário para baixar de URLs.")
            resp = self.requests.get(source, timeout=30, headers={'User-Agent': self.user_agent})
            resp.raise_for_status()
            return self._decode_bytes(resp.content, encoding_hint=resp.encoding)
        
        path = Path(source)
        if not path.is_file():
            raise FileNotFoundError(f"O arquivo de origem não foi encontrado: {source}")
        return self._decode_bytes(path.read_bytes())

    @staticmethod
    def _shutil_which(cmd: str) -> Optional[str]:
        """Wrapper para shutil.which para compatibilidade e robustez."""
        return shutil.which(cmd)

    @classmethod
    def _which_xray(cls) -> str:
        """Descobre o binário do Xray, priorizando a variável de ambiente XRAY_PATH."""
        if env_path := os.environ.get("XRAY_PATH"):
            if Path(env_path).is_file():
                return env_path
        
        for candidate in ("xray", "v2ray"):
            if found := cls._shutil_which(candidate):
                return found
        
        raise FileNotFoundError(
            "Binário do 'xray' ou 'v2ray' não encontrado. "
            "Instale o xray-core ou defina a variável de ambiente XRAY_PATH."
        )

    @staticmethod
    def _format_destination(host: Optional[str], port: Optional[int]) -> str:
        """Formata 'host:port' para exibição amigável."""
        if not host or host == "-":
            return "-"
        return f"{host}:{port}" if port else host

    @staticmethod
    def _check_country_match(entry_data: Dict[str, Any], desired: str) -> bool:
        """Verifica se os campos de país de um dicionário correspondem ao desejado."""
        desired_norm = desired.strip().casefold()
        if not desired_norm:
            return True

        # Campos a serem verificados (código, nome, etc.)
        candidates = {
            str(entry_data.get(k) or "").strip().casefold()
            for k in ("country", "country_code", "country_name")
        }
        # Remove valores vazios ou placeholders
        candidates.discard("")
        candidates.discard("-")

        return any(desired_norm == c for c in candidates)

    @classmethod
    def matches_country(cls, entry: Dict[str, Any], desired: Optional[str]) -> bool:
        """Valida se uma entrada de proxy corresponde ao filtro de país."""
        if not desired:
            return True

        # Determina o país de saída (pode ser diferente do país do servidor)
        exit_country_info = {
            "country": entry.get("proxy_country"),
            "country_code": entry.get("proxy_country_code"),
        }
        
        server_country_info = {
            "country": entry.get("country"),
            "country_code": entry.get("country_code"),
            "country_name": entry.get("country_name"),
        }

        # O país de saída efetivo é o do proxy, se existir, senão o do servidor.
        effective_exit_info = exit_country_info if exit_country_info.get("country") else server_country_info
        
        return cls._check_country_match(effective_exit_info, desired)