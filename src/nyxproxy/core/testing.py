from __future__ import annotations

"""Implementações de testes e relatórios de status de proxys."""

import ipaddress
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

import requests
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .models import Outbound


class TestingMixin:
    """Conjunto de rotinas para validar proxys e exibir resultados."""

    def _outbound_host_port(self, outbound: Outbound) -> Tuple[str, int]:
        """Extrai host e porta reais do outbound de forma genérica."""
        proto = outbound.config.get("protocol")
        settings = outbound.config.get("settings", {})
        
        server_list_key = "vnext" if proto in ("vmess", "vless") else "servers"
        server_list = settings.get(server_list_key, [])

        if not server_list or not isinstance(server_list, list):
            raise ValueError(f"Lista de servidores '{server_list_key}' ausente no outbound.")

        server_config = server_list[0]
        host = server_config.get("address")
        port_raw = server_config.get("port")

        if not host or port_raw is None:
            raise ValueError("Host/port ausentes na configuração do servidor.")
        
        port = self._safe_int(port_raw)
        if port is None:
            raise ValueError(f"Porta inválida: {port_raw!r}")
            
        return str(host), port

    @staticmethod
    def _is_public_ip(ip: str) -> bool:
        """Retorna ``True`` se o IP for público e roteável pela Internet."""
        try:
            addr = ipaddress.ip_address(ip)
            return addr.is_global and not addr.is_private
        except ValueError:
            return False

    def _lookup_country(self, ip: Optional[str]) -> Optional[Dict[str, Optional[str]]]:
        """Consulta informações de localização do IP, usando cache em memória."""
        if not ip or not self._is_public_ip(ip):
            return None
        
        if ip in self._ip_lookup_cache:
            return self._ip_lookup_cache[ip]

        if not self.requests or not self._findip_token:
            return None

        result = None
        try:
            resp = self.requests.get(
                f"https://api.findip.net/{ip}/?token={self._findip_token}",
                timeout=5
            )
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                raise ValueError(data["error"])

            country_info = data.get("country", {})
            code = (country_info.get("iso_code") or "").strip().upper() or None
            name = (country_info.get("names", {}).get("en") or "").strip() or None
            
            if code or name:
                result = {"name": name, "code": code, "label": name or code}
        
        except (requests.exceptions.RequestException, ValueError, KeyError):
            result = None

        self._ip_lookup_cache[ip] = result
        return result

    def _test_outbound(self, raw_uri: str, outbound: Outbound, timeout: float) -> Dict[str, Any]:
        """Executa medições para um outbound, retornando um dicionário de resultados."""
        result: Dict[str, Any] = {
            "uri": raw_uri,
            "tag": outbound.tag,
            "protocol": outbound.config.get("protocol"),
            "functional": False
        }
        
        try:
            host, port = self._outbound_host_port(outbound)
            result.update({"host": host, "port": port})
            
            # Tenta resolver o IP do host
            try:
                ip_info = socket.getaddrinfo(host, None, socket.AF_INET)
                result["ip"] = ip_info[0][4][0] if ip_info else None
            except socket.gaierror:
                result["ip"] = None # Falha na resolução de DNS
            
            if result.get("ip"):
                if country_info := self._lookup_country(result["ip"]):
                    result.update({
                        "country": country_info.get("label"),
                        "country_code": country_info.get("code"),
                        "country_name": country_info.get("name"),
                    })

            # Teste funcional
            func_result = self._test_proxy_functionality(outbound, timeout=timeout)
            if func_result.get("functional"):
                result.update({
                    "functional": True,
                    "ping": func_result.get("response_time"),
                })
                # Atualiza país com base no IP de saída, se disponível
                if exit_ip := func_result.get("external_ip"):
                    if exit_country := self._lookup_country(exit_ip):
                        result.update({
                            "proxy_ip": exit_ip,
                            "proxy_country": exit_country.get("label"),
                            "proxy_country_code": exit_country.get("code"),
                        })
            else:
                result["error"] = func_result.get("error", "Proxy não funcional")

        except Exception as exc:
            result["error"] = f"Erro na preparação do teste: {exc}"

        return result

    def _test_proxy_functionality(
        self, outbound: Outbound, timeout: float = 10.0,
    ) -> Dict[str, Any]:
        """Testa a funcionalidade real da proxy criando uma ponte temporária."""
        if not self.requests:
            return {"functional": False, "error": "Módulo requests não disponível"}

        try:
            with self._temporary_bridge(outbound, tag_prefix="test") as (port, _):
                proxy_url = f"http://127.0.0.1:{port}"
                proxies = {"http": proxy_url, "https": proxy_url}
                
                start_time = time.perf_counter()
                response = self.requests.get(
                    self.test_url,
                    proxies=proxies,
                    timeout=timeout,
                    verify=False, # Ignora erros de certificado SSL
                    headers={"User-Agent": self.user_agent}
                )
                response.raise_for_status()
                duration_ms = (time.perf_counter() - start_time) * 1000

                return {
                    "functional": True,
                    "response_time": duration_ms,
                    "external_ip": self._extract_external_ip(response),
                }
        except Exception as exc:
            return {"functional": False, "error": self._format_request_error(exc, timeout)}

    def _format_request_error(self, exc: Exception, timeout: float) -> str:
        """Normaliza mensagens de erro de requisições HTTP via proxy."""
        if isinstance(exc, requests.exceptions.Timeout):
            return f"Timeout após {timeout:.1f}s"
        if isinstance(exc, requests.exceptions.ProxyError):
            return f"Erro de proxy: {str(exc.__cause__)[:100]}"
        if isinstance(exc, requests.exceptions.ConnectionError):
            return f"Erro de conexão: {str(exc.__cause__)[:100]}"
        if isinstance(exc, requests.exceptions.HTTPError):
            return f"Erro HTTP {exc.response.status_code}: {exc.response.reason}"
        return f"{type(exc).__name__}: {str(exc)[:100]}"

    @staticmethod
    def _extract_external_ip(response: requests.Response) -> Optional[str]:
        """Extrai IP externo da resposta JSON do serviço de teste."""
        try:
            data = response.json()
            if origin := data.get("origin"):
                return str(origin).split(",")[0].strip()
            # Adicione outros parsers de IP aqui se o test_url mudar
        except (ValueError, KeyError):
            return None
        return None

    def _perform_health_checks(
        self,
        outbounds: List[Tuple[str, Outbound]],
        *,
        country_filter: Optional[str] = None,
        emit_progress: Optional[Any] = None,
        force_refresh: bool = False,
        timeout: float = 10.0,
        threads: int = 1,
        stop_on_success: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Executa os testes de forma concorrente e gerencia o cache."""
        self._ip_lookup_cache.clear() # Limpa o cache de IP a cada nova bateria de testes
        all_results: List[Dict[str, Any]] = []
        to_test: List[Tuple[int, str, Outbound]] = []
        success_count = 0

        # Separa o que precisa ser testado do que pode ser pego do cache
        for idx, (raw, outbound) in enumerate(outbounds):
            use_cache = self.use_cache and not force_refresh and raw in self._cache_entries
            if use_cache:
                cached_data = self._cache_entries[raw]
                entry = self._apply_cached_entry(self._make_base_entry(idx, raw, outbound), cached_data)
                is_ok = entry.get("status") == "OK" and self.matches_country(entry, country_filter)
                
                if is_ok:
                    entry["country_match"] = True
                    all_results.append(entry)
                    success_count += 1
                    if emit_progress:
                        self._emit_test_progress(entry, len(all_results), len(outbounds), emit_progress)
                else:
                    to_test.append((idx, raw, outbound))
            else:
                to_test.append((idx, raw, outbound))

        limit_reached = stop_on_success and success_count >= stop_on_success
        if limit_reached:
            # Preenche o resto com entradas não testadas e retorna
            tested_uris = {e["uri"] for e in all_results}
            for idx, (raw, outbound) in enumerate(outbounds):
                if raw not in tested_uris:
                    all_results.append(self._make_base_entry(idx, raw, outbound))
            all_results.sort(key=lambda x: x.get("index", float('inf')))
            return all_results

        # Executa os testes em threads
        if to_test:
            def worker(idx: int, raw: str, outbound: Outbound) -> Dict[str, Any]:
                base_entry = self._make_base_entry(idx, raw, outbound)
                test_result = self._test_outbound(raw, outbound, timeout)
                base_entry.update(test_result)
                
                base_entry["status"] = "OK" if base_entry["functional"] else "ERRO"
                base_entry["tested_at_ts"] = time.time()
                
                if country_filter and base_entry["status"] == "OK":
                    match = self.matches_country(base_entry, country_filter)
                    base_entry["country_match"] = match
                    if not match:
                        base_entry["status"] = "FILTRADO"
                        base_entry["error"] = f"Não corresponde ao filtro '{country_filter}'"
                
                return base_entry

            with ThreadPoolExecutor(max_workers=threads) as executor:
                futures = {executor.submit(worker, *args) for args in to_test}
                for future in as_completed(futures):
                    result_entry = future.result()
                    all_results.append(result_entry)
                    
                    if self.use_cache:
                        self._save_cache(all_results)

                    if emit_progress:
                        self._emit_test_progress(result_entry, len(all_results), len(outbounds), emit_progress)
                    
                    if result_entry.get("status") == "OK":
                        success_count += 1
                        if stop_on_success and success_count >= stop_on_success:
                            for f in futures: f.cancel() # Cancela testes restantes
                            break
        
        # Garante que todas as proxies originais tenham uma entrada
        final_uris = {e["uri"] for e in all_results}
        for idx, (raw, outbound) in enumerate(outbounds):
            if raw not in final_uris:
                all_results.append(self._make_base_entry(idx, raw, outbound))

        all_results.sort(key=lambda x: x.get("index", float('inf')))
        return all_results

    def _emit_test_progress(self, entry: Dict[str, Any], count: int, total: int, progress_emitter: Any) -> None:
        """Formata e exibe uma linha de progresso do teste."""
        status = entry.get("status", "AGUARDANDO")
        style = self.STATUS_STYLES.get(status, "white")
        status_fmt = f"[{style}]{status}[/{style}]"
        
        ping = entry.get("ping")
        ping_fmt = f"{ping:.1f} ms" if isinstance(ping, (int, float)) else "-"
        
        country = entry.get("proxy_country") or entry.get("country") or "-"
        cache_note = " [dim](cache)[/]" if entry.get("cached") else ""

        progress_emitter.print(
            f"[{count}/{total}] {status_fmt}{cache_note} [bold]{entry['tag']}[/] -> "
            f"País: {country} | Ping: {ping_fmt}"
        )

        if error := entry.get("error"):
            progress_emitter.print(f"  [dim]Motivo: {error}[/]")

    def test(
        self,
        *,
        threads: int = 1,
        country: Optional[str] = None,
        verbose: Optional[bool] = None,
        timeout: float = 10.0,
        force: bool = False,
        find_first: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Testa as proxies carregadas, exibindo resultados e atualizando o estado interno."""
        if not self._outbounds:
            raise RuntimeError("Nenhuma proxy carregada para testar.")

        country_filter = country if country is not None else self.country_filter
        show_progress = self.console and (verbose is None or verbose)
        
        results = self._perform_health_checks(
            self._outbounds,
            country_filter=country_filter,
            emit_progress=self.console if show_progress else None,
            force_refresh=force,
            timeout=timeout,
            threads=threads,
            stop_on_success=find_first,
        )

        self._entries = results
        self.country_filter = country_filter

        if show_progress:
            self._render_test_summary(results, country_filter)

        return results

    def _render_test_summary(self, entries: List[Dict[str, Any]], country_filter: Optional[str]) -> None:
        """Exibe o relatório final dos testes no console."""
        if not self.console:
            return

        ok_entries = [e for e in entries if e.get("status") == "OK"]
        
        self.console.print()
        self.console.rule("Proxies Funcionais")
        if ok_entries:
            self.console.print(self._render_test_table(ok_entries))
        else:
            msg = f"Nenhuma proxy funcional encontrada para o filtro '{country_filter}'." if country_filter else "Nenhuma proxy funcional encontrada."
            self.console.print(f"[yellow]{msg}[/yellow]")
        
        counts = {
            "Total": len(entries),
            "Sucesso": sum(1 for e in entries if e.get("status") == "OK"),
            "Falhas": sum(1 for e in entries if e.get("status") == "ERRO"),
            "Filtradas": sum(1 for e in entries if e.get("status") == "FILTRADO"),
        }
        summary = "    ".join([f"[bold]{k}:[/] {v}" for k, v in counts.items()])

        self.console.print()
        self.console.rule("Resumo do Teste")
        self.console.print(summary)

    @classmethod
    def _render_test_table(cls, entries: List[Dict[str, Any]]) -> Table:
        """Gera uma tabela Rich com o resultado dos testes."""
        entries.sort(key=lambda e: e.get("ping") or float('inf'))
        table = Table(show_header=True, header_style="bold cyan", expand=True)
        table.add_column("Tag", no_wrap=True, max_width=30)
        table.add_column("Destino", overflow="fold")
        table.add_column("País (Saída)", no_wrap=True)
        table.add_column("Ping", justify="right", no_wrap=True)

        for entry in entries:
            destino = cls._format_destination(entry.get("host"), entry.get("port"))
            ping = entry.get("ping")
            ping_str = f"{ping:.1f} ms" if isinstance(ping, (int, float)) else "-"
            country = entry.get("proxy_country") or entry.get("country") or "-"
            
            table.add_row(entry.get("tag") or "-", destino, country, ping_str)
        return table