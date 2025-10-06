from __future__ import annotations

"""Modelos de dados compartilhados pelo gerenciador de proxys."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import subprocess


@dataclass(frozen=True)
class Outbound:
    """Representa um outbound configurado para o Xray/V2Ray."""

    tag: str
    config: Dict[str, Any]


@dataclass
class BridgeRuntime:
    """Representa uma ponte HTTP ativa e seus recursos associados."""

    tag: str
    port: int
    scheme: str
    uri: str
    process: Optional[subprocess.Popen]
    workdir: Optional[Path]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
