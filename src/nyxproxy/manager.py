#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ferramenta e biblioteca para testar e criar pontes HTTP para proxys V2Ray/Xray."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

import requests
import urllib3
from dotenv import load_dotenv
from rich.console import Console

from .core import (
    BridgeMixin,
    CacheMixin,
    ChainsMixin,
    LoadingMixin,
    ParsingMixin,
    ProxyUtilityMixin,
    TestingMixin,
    BridgeRuntime as CoreBridgeRuntime,
    Outbound as CoreOutbound,
)

# Suprime avisos de requisições HTTPS sem verificação de certificado
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# Carrega variáveis de ambiente de um arquivo .env
load_dotenv()

__all__ = ["Proxy"]

class Proxy(
    ProxyUtilityMixin,
    CacheMixin,
    LoadingMixin,
    ParsingMixin,
    TestingMixin,
    BridgeMixin,
    ChainsMixin,
):
    """Gerencia uma coleção de proxys, com suporte a testes e criação de pontes HTTP."""

    DEFAULT_CACHE_FILENAME: str = "proxy_cache.json"
    CACHE_VERSION: int = 1

    # Estilos para exibição de status no console Rich
    STATUS_STYLES: Dict[str, str] = {
        "AGUARDANDO": "dim",
        "TESTANDO": "yellow",
        "OK": "bold green",
        "ERRO": "bold red",
        "FILTRADO": "cyan",
    }

    # Modelos de dados para type hinting e clareza
    Outbound = CoreOutbound
    BridgeRuntime = CoreBridgeRuntime

    def __init__(
        self,
        proxies: Optional[Iterable[str]] = None,
        sources: Optional[Iterable[str]] = None,
        *,
        country: Optional[str] = None,
        max_count: int = 0,
        use_console: bool = False,
        use_cache: bool = True,
        cache_path: Optional[Union[str, os.PathLike]] = None,
        requests_session: Optional[Any] = None,
    ) -> None:
        """Inicializa o gerenciador carregando proxys, fontes e cache."""
        self._findip_token = os.getenv("FINDIP_TOKEN")
        if not self._findip_token:
            raise ValueError(
                "O token da API findip.net não foi definido. "
                "Defina a variável de ambiente FINDIP_TOKEN em um arquivo .env."
            )
        
        # Configurações
        self.country_filter = country
        self.max_count = max_count
        self.use_cache = use_cache
        self.requests = requests_session or requests
        self.console = Console() if use_console else None
        
        # URL e User-Agent para testes de funcionalidade
        self.test_url = "https://www.cloudflare.com/cdn-cgi/trace"
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
        )
        
        # Estado interno
        self._outbounds: List[Tuple[str, Proxy.Outbound]] = []
        self._entries: List[Dict[str, Any]] = []
        self._bridges: List[Proxy.BridgeRuntime] = []
        self._parse_errors: List[str] = []
        self._running = False
        self._atexit_registered = False

        # Gerenciamento de threads e concorrência
        self._port_allocation_lock = threading.Lock()
        self._allocated_ports: set[int] = set()
        self._cache_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wait_thread: Optional[threading.Thread] = None

        # Configuração do Cache
        default_cache_path = Path.home() / ".nyxproxy" / self.DEFAULT_CACHE_FILENAME
        self.cache_path = Path(cache_path) if cache_path is not None else default_cache_path
        self._cache_entries: Dict[str, Dict[str, Any]] = {}
        self._cache_available = False
        self._ip_lookup_cache: Dict[str, Optional[Dict[str, Optional[str]]]] = {}

        if self.use_cache:
            self._load_cache()

        if proxies:
            self.add_proxies(proxies)
        if sources:
            self.add_sources(sources)

        # Se fontes foram adicionadas, popula as entradas com dados do cache
        if self._outbounds and not self._entries:
            self._prime_entries_from_cache()

    @property
    def entries(self) -> List[Dict[str, Any]]:
        """Retorna os registros carregados ou resultantes dos últimos testes."""
        return self._entries

    @property
    def parse_errors(self) -> List[str]:
        """Lista de erros de parsing encontrados ao carregar proxies."""
        return self._parse_errors

    def clear_cache(self, age_str: Optional[str] = None):
        """
        Limpa o cache de proxies, total ou parcialmente com base na idade.

        Args:
            age_str: String opcional para filtrar por idade (ex: '5H', '1D').
                     Se None, limpa todo o cache.
        """
        # A lógica real está no CacheMixin, que herda e usa self.console.
        super().clear_cache(age_str=age_str)