#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ferramenta orientada a biblioteca para testar e criar pontes HTTP para proxys V2Ray/Xray.

O módulo expõe a classe :class:`Proxy`, que gerencia carregamento de links, testes
com filtragem opcional por país e criação de túneis HTTP locais utilizando Xray ou
V2Ray. Todo o comportamento é pensado para uso programático em outros módulos.
"""

from __future__ import annotations

import os
import threading
import requests
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from rich.console import Console
from dotenv import load_dotenv

import urllib3

from .core import (
    BridgeMixin,
    CacheMixin,
    LoadingMixin,
    ParsingMixin,
    ProxyUtilityMixin,
    TestingMixin,
)
from .core import (
    BridgeRuntime as CoreBridgeRuntime,
)
from .core import (
    Outbound as CoreOutbound,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

__all__ = ["Proxy"]

class Proxy(
    ProxyUtilityMixin,
    CacheMixin,
    LoadingMixin,
    ParsingMixin,
    TestingMixin,
    BridgeMixin,
):
    """Gerencia uma coleção de proxys, com suporte a testes e criação de pontes HTTP."""

    DEFAULT_CACHE_FILENAME: str = "proxy_cache.json"
    CACHE_VERSION: int = 1

    STATUS_STYLES: Dict[str, str] = {
        "AGUARDANDO": "dim",
        "TESTANDO": "yellow",
        "OK": "bold green",
        "ERRO": "bold red",
        "FILTRADO": "cyan",
    }

    Outbound = CoreOutbound
    BridgeRuntime = CoreBridgeRuntime

    def __init__(
        self,
        proxies: Optional[Iterable[str]] = None,
        sources: Optional[Iterable[str]] = None,
        *,
        country: Optional[str] = None,
        base_port: int = 54000,
        max_count: int = 0,
        use_console: bool = False,
        use_cache: bool = True,
        cache_path: Optional[Union[str, os.PathLike]] = None,
        command_output: bool = True,
        requests_session: Optional[Any] = None,
    ) -> None:
        """Inicializa o gerenciador carregando proxys, fontes e cache se necessário."""
        self._findip_token = os.getenv("FINDIP_TOKEN")
        if not self._findip_token:
            raise ValueError(
                "O token da API findip.net não foi definido. "
                "Defina a variável de ambiente FINDIP_TOKEN em um arquivo .env ou exporte-a."
            )
        
        self.country_filter = country
        self.base_port = base_port
        self.max_count = max_count
        self.requests = requests_session or requests
        self.use_console = bool(use_console and Console)
        self.console = Console() if self.use_console and Console else None
        self._port_allocation_lock = threading.Lock()
        self._allocated_ports = set()
        self._cache_lock = threading.Lock()

        self._outbounds: List[Tuple[str, Proxy.Outbound]] = []
        self._entries: List[Dict[str, Any]] = []
        self._bridges: List[Proxy.BridgeRuntime] = []
        self._running = False
        self._atexit_registered = False
        self._parse_errors: List[str] = []

        self.use_cache = use_cache
        default_cache_path = Path(__file__).with_name(self.DEFAULT_CACHE_FILENAME)
        self.cache_path = Path(cache_path) if cache_path is not None else default_cache_path
        self._cache_entries: Dict[str, Dict[str, Any]] = {}
        self._stop_event = threading.Event()
        self._wait_thread: Optional[threading.Thread] = None
        self.command_output = bool(command_output)
        self._cache_available = False

        if self.use_cache:
            self._load_cache()

        if proxies:
            self.add_proxies(proxies)
        if sources:
            self.add_sources(sources)

        if self.use_cache and not self._entries and self._outbounds:
            self._prime_entries_from_cache()

    @property
    def entries(self) -> List[Dict[str, Any]]:
        """Retorna os registros carregados ou decorrentes dos últimos testes."""
        return self._entries


    @property
    def parse_errors(self) -> List[str]:
        """Lista de linhas ignoradas ao interpretar os links informados."""
        return list(self._parse_errors)

    def clear_cache(self, age_str: Optional[str] = None):
        """
        Limpa o cache de proxies, total ou parcialmente com base na idade.

        Args:
            age_str: String opcional para filtrar por idade (ex: '1D', '2S').
                     Se None, limpa todo o cache.
        """
        # A lógica real está no CacheMixin, que é herdado.
        # O método no mixin precisa do console, que é um atributo desta classe.
        super().clear_cache(age_str=age_str, console=self.console)