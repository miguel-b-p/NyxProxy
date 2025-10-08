from __future__ import annotations

"""Implementações de testes e relatórios de status de proxys."""

import ipaddress
import os
import socket
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple
from rich.console import Console
from rich.table import Table
from rich.text import Text
from .models import Outbound

class TestingMixin:
    """Conjunto de rotinas para validar proxys e exibir resultados."""

    def _outbound_host_port(self, outbound: Outbound) -> Tuple[str, int]:
        """Extrai host e porta reais do outbound conforme o protocolo."""
        proto = outbound.config.get("protocol")
        settings = outbound.config.get("settings", {})
        host = None
        port = None
        if proto == "shadowsocks":
            server = settings.get("servers", [{}])[0]
            host = server.get("address")
            port = server.get("port")
        elif proto in ("vmess", "vless"):
            vnext = settings.get("vnext", [{}])[0]
            host = vnext.get("address")
            port = vnext.get("port")
        elif proto == "trojan":
            server = settings.get("servers", [{}])[0]
            host = server.get("address")
            port = server.get("port")
        else:
            raise ValueError(f"Protocolo não suportado para teste: {proto}")

        if host is None or port is None:
            raise ValueError(f"Host/port ausentes no outbound {outbound.tag} ({proto}).")
        try:
            return host, int(str(port).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Porta inválida no outbound {outbound.tag}: {port!r}") from exc


    @staticmethod
    def _is_public_ip(ip: str) -> bool:
        """Retorna ``True`` se o IP for público e roteável pela Internet."""
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return not (
            addr.is_private or addr.is_loopback or addr.is_reserved or addr.is_multicast or addr.is_link_local
        )


    def _lookup_country(self, ip: Optional[str]) -> Optional[Dict[str, Optional[str]]]:
        """Consulta informações de localização do IP usando findip.net."""
        if not ip or self.requests is None or not self._is_public_ip(ip):
            return None

        try:
            # O token é validado na inicialização do Proxy Manager
            resp = self.requests.get(
                f"https://api.findip.net/{ip}/?token={self._findip_token}",
                timeout=5
            )
            resp.raise_for_status()  # Lança exceção para códigos de erro HTTP (4xx ou 5xx)
            data = resp.json()

            # A API findip.net retorna um campo 'error' em caso de falha (ex: token inválido)
            if "error" in data:
                return None

            country_info = data.get("country", {})

            country_code = country_info.get("iso_code")
            if isinstance(country_code, str):
                country_code = (country_code.strip() or None)
                if country_code:
                    country_code = country_code.upper()

            country_names = country_info.get("names", {})
            country_name = country_names.get("en")

            if isinstance(country_name, str):
                country_name = country_name.strip() or None

            label = country_name or country_code

            if not (label or country_code or country_name):
                return None

            return {
                "name": country_name,
                "code": country_code,
                "label": label,
            }
        except requests.exceptions.RequestException:
            # Captura erros de conexão, timeout, HTTP 4xx/5xx, etc.
            return None
        except ValueError:  # requests.json() pode levantar ValueError (ou JSONDecodeError)
            # A resposta não foi um JSON válido
            return None


    def _test_outbound(self, raw_uri: str, outbound: Outbound, timeout: float = 10.0) -> Dict[str, Any]:
        """Executa medições para um outbound específico retornando métricas usando rota real."""
        result: Dict[str, Any] = {
            "tag": outbound.tag,
            "protocol": outbound.config.get("protocol"),
            "uri": raw_uri,
        }

        try:
            host, port = self._outbound_host_port(outbound)
        except Exception as exc:
            result["error"] = f"host/port não identificados: {exc}"
            return result

        result["host"] = host
        result["port"] = port

        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except Exception:
            infos = []
        ip = None
        ipv6 = None
        for info in infos:
            family, *_rest, sockaddr = info
            address = sockaddr[0]
            if family == socket.AF_INET:
                ip = address
                break
            if ipv6 is None and family == socket.AF_INET6:
                ipv6 = address
        result["ip"] = ip or ipv6

        if result.get("ip"):
            country_info = self._lookup_country(result["ip"])
            if country_info:
                if label := country_info.get("label"):
                    result["country"] = label
                if code := country_info.get("code"):
                    result["country_code"] = code
                if name := country_info.get("name"):
                    result["country_name"] = name

        func_result = self._test_proxy_functionality(
            raw_uri, outbound, timeout=timeout
        )

        if func_result.get("functional"):
            result["ping_ms"] = func_result.get("response_time")
            result["functional"] = True
            result["external_ip"] = func_result.get("external_ip")

            if func_result.get("external_ip") and func_result["external_ip"] != result.get("ip"):
                result["proxy_ip"] = func_result["external_ip"]
                proxy_country = self._lookup_country(func_result["external_ip"])
                if proxy_country:
                    result["proxy_country"] = proxy_country.get("label")
                    result["proxy_country_code"] = proxy_country.get("code")
        else:
            result["error"] = func_result.get("error", "Proxy não funcional")
            result["functional"] = False

        return result


    def _test_proxy_functionality(
        self,
        raw_uri: str,
        outbound: Outbound,
        timeout: float = 10.0,
        test_url: str = "https://reqbin.com/echo"
    ) -> Dict[str, Any]:
        """Testa a funcionalidade real da proxy criando uma ponte temporária e fazendo uma requisição."""
        result = {
            "functional": False,
            "response_time": None,
            "external_ip": None,
            "error": None
        }

        if self.requests is None:
            result["error"] = "requests não disponível para teste funcional"
            return result

        exceptions_mod = getattr(self.requests, "exceptions", None)
        if exceptions_mod is None and requests is not None:
            exceptions_mod = getattr(requests, "exceptions", None)

        response = None
        duration_ms: Optional[float] = None

        try:
            with self._temporary_bridge(outbound, tag_prefix="test") as (test_port, _):
                proxy_url = f"http://127.0.0.1:{test_port}"
                proxies = {"http": proxy_url, "https": proxy_url}
                start_time = time.perf_counter()

                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                }

                response = self.requests.get(
                    test_url,
                    proxies=proxies,
                    timeout=timeout,
                    verify=False,
                    headers=headers
                )
                response.raise_for_status()
                duration_ms = (time.perf_counter() - start_time) * 1000
        except RuntimeError as exc:
            result["error"] = str(exc)
            return result
        except Exception as exc:
            result["error"] = self._format_request_error(exc, timeout, exceptions_mod)
            return result

        result["functional"] = True
        result["response_time"] = duration_ms
        if response is not None:
            result["external_ip"] = self._extract_external_ip(response)
        return result


    @staticmethod
    def _matches_exception(exc: Exception, candidate: Any) -> bool:
        """Retorna True se ``exc`` for instância de ``candidate`` (classe ou tupla)."""
        if candidate is None:
            return False
        try:
            return isinstance(exc, candidate)
        except TypeError:
            return False


    def _format_request_error(self, exc: Exception, timeout: float, exceptions_mod: Any) -> str:
        """Normaliza mensagens de erro de requisições HTTP via proxy."""
        timeout_exc = getattr(exceptions_mod, "Timeout", None) if exceptions_mod else None
        proxy_exc = getattr(exceptions_mod, "ProxyError", None) if exceptions_mod else None
        conn_exc = getattr(exceptions_mod, "ConnectionError", None) if exceptions_mod else None
        http_exc = getattr(exceptions_mod, "HTTPError", None) if exceptions_mod else None

        if self._matches_exception(exc, timeout_exc):
            return f"Timeout após {timeout:.1f}s"
        if self._matches_exception(exc, proxy_exc):
            return f"Erro de proxy: {str(exc)[:100]}"
        if self._matches_exception(exc, conn_exc):
            return f"Erro de conexão: {str(exc)[:100]}"
        if self._matches_exception(exc, http_exc):
            response = getattr(exc, 'response', None)
            if response is not None:
                return f"Erro HTTP {response.status_code}: {response.reason}"

        return f"Erro na requisição: {str(exc)[:100]}"


    @staticmethod
    def _extract_external_ip(response: Any) -> Optional[str]:
        """Extrai IP externo da resposta JSON do httpbin.org/ip."""
        try:
            data = response.json()
        except Exception:
            return None

        origin = data.get("origin")
        if isinstance(origin, str) and origin.strip():
            return origin.split(",")[0].strip()

        return None


    def _perform_health_checks(
        self,
        outbounds: List[Tuple[str, Outbound]],
        *,
        country_filter: Optional[str] = None,
        emit_progress: Optional[Any] = None,
        force_refresh: bool = False,
        functional_timeout: float = 10.0,
        threads: int = 1,
        stop_on_success: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Percorre os outbounds testando conectividade real de forma concorrente."""
        all_results: List[Dict[str, Any]] = []
        reuse_cache = self.use_cache and not force_refresh
        success_count = 0

        to_test: List[Tuple[int, str, Outbound]] = []

        # Carrega resultados OK do cache primeiro
        if reuse_cache:
            for idx, (raw, outbound) in enumerate(outbounds):
                if raw in self._cache_entries:
                    cached_data = self._cache_entries[raw]
                    entry = self._apply_cached_entry(self._make_base_entry(idx, raw, outbound), cached_data)

                    if entry.get("status") == "OK" and self.matches_country(entry, country_filter):
                        entry["country_match"] = True
                        all_results.append(entry)
                        success_count += 1
                        if emit_progress:
                            self._emit_test_progress(entry, len(all_results), len(outbounds), emit_progress)
                    else:
                        to_test.append((idx, raw, outbound))
                else:
                    to_test.append((idx, raw, outbound))
        else:
            to_test = list(enumerate(outbounds))


        limit_reached = stop_on_success is not None and stop_on_success > 0
        if limit_reached and success_count >= stop_on_success:
            if self.console:
                self.console.print(f"\n[bold green]Encontradas {success_count} proxies válidas no cache, atingindo o limite de {stop_on_success}. Testes adicionais ignorados.[/]")
            # Preenche o resto com entradas não testadas
            tested_uris = {e["uri"] for e in all_results}
            for idx, (raw, outbound) in enumerate(outbounds):
                if raw not in tested_uris:
                            all_results.append(self._make_base_entry(idx, raw, outbound))
            all_results.sort(key=lambda x: x.get("index", float('inf')))
            return all_results

        if to_test:
            def worker(idx: int, raw: str, outbound: Outbound) -> Dict[str, Any]:
                """Testa uma proxy e retorna seu resultado."""
                entry = self._make_base_entry(idx, raw, outbound)
                try:
                    preview_host, preview_port = self._outbound_host_port(outbound)
                    entry.update({"host": preview_host, "port": preview_port})
                except Exception:
                    pass
                entry["status"] = "TESTANDO"

                result = self._test_outbound(raw, outbound, timeout=functional_timeout)
                finished_at = time.time()

                entry.update({
                    "host": result.get("host") or entry["host"],
                    "port": result.get("port") if result.get("port") is not None else entry["port"],
                    "ip": result.get("ip") or entry["ip"],
                    "country": result.get("country") or entry["country"],
                    "country_code": result.get("country_code") or entry.get("country_code"),
                    "country_name": result.get("country_name") or entry.get("country_name"),
                    "ping": result.get("ping_ms"),
                    "tested_at_ts": finished_at, # <-- PONTO CRÍTICO: ADICIONA O TIMESTAMP NUMÉRICO
                    "tested_at": self._format_timestamp(finished_at),
                    "functional": result.get("functional", False),
                    "external_ip": result.get("external_ip"),
                    "proxy_ip": result.get("proxy_ip"),
                    "proxy_country": result.get("proxy_country"),
                    "proxy_country_code": result.get("proxy_country_code"),
                })

                if entry["functional"]:
                    entry["status"] = "OK"
                    entry["error"] = None
                else:
                    entry["status"] = "ERRO"
                    entry["error"] = result.get("error", "Teste falhou")

                if country_filter and entry["status"] == "OK":
                    entry["country_match"] = self.matches_country(entry, country_filter)
                    if not entry["country_match"]:
                        entry["status"] = "FILTRADO"
                        exit_country = entry.get("proxy_country") or entry.get("country") or "-"
                        server_country = entry.get("country") or "-"
                        if exit_country != server_country:
                            entry["error"] = f"Filtro '{country_filter}': Servidor ({server_country}) ou Saída ({exit_country}) não correspondem"
                        else:
                            entry["error"] = f"Filtro '{country_filter}': País de saída é {exit_country}"

                return entry

            if self.console and emit_progress:
                self.console.print(f"\n[yellow]Iniciando teste de {len(to_test)} proxies com até {threads} workers...[/]")

            with ThreadPoolExecutor(max_workers=threads) as executor:
                futures = {executor.submit(worker, idx, raw, outbound) for idx, raw, outbound in to_test}

                for future in as_completed(futures):
                    try:
                        result_entry = future.result()
                        all_results.append(result_entry)

                        if self.use_cache:
                            # Atualiza o cache em memória e salva no disco em tempo real
                            self._cache_entries[result_entry["uri"]] = result_entry
                            self._save_cache(list(self._cache_entries.values()))

                        if emit_progress:
                            self._emit_test_progress(result_entry, len(all_results), len(outbounds), emit_progress)

                        if result_entry.get("status") == "OK":
                            success_count += 1

                        if limit_reached and success_count >= stop_on_success:
                            if self.console and emit_progress:
                                self.console.print(f"\n[bold green]Limite de {stop_on_success} proxies encontradas. Finalizando testes.[/]")
                            # Cancela futuros restantes
                            for f in futures:
                                if not f.done():
                                    f.cancel()
                            break
                    except Exception as exc:
                        if self.console:
                            self.console.print(f"[bold red]Erro fatal em uma thread de teste: {exc}[/]")

        # Garante que todas as proxies originais tenham uma entrada no resultado final
        final_uris = {e["uri"] for e in all_results}
        for idx, (raw, outbound) in enumerate(outbounds):
            if raw not in final_uris:
                all_results.append(self._make_base_entry(idx, raw, outbound))

        all_results.sort(key=lambda x: x.get("index", float('inf')))
        return all_results

    def _emit_test_progress(self, entry: Dict[str, Any], count: int, total: int, emit_progress: Any) -> None:
        """Emite informações de progresso do teste."""
        destino = self._format_destination(entry.get("host"), entry.get("port"))
        ping_preview = entry.get("ping")
        ping_fmt = f"{ping_preview:.1f} ms" if isinstance(ping_preview, (int, float)) else "-"

        status_fmt = {
            "OK": "[bold green]OK[/]",
            "ERRO": "[bold red]ERRO[/]",
            "TESTANDO": "[yellow]TESTANDO[/]",
            "AGUARDANDO": "[dim]AGUARDANDO[/]",
            "FILTRADO": "[cyan]FILTRADO[/]",
        }.get(entry["status"], entry["status"])

        cache_note = ""
        if entry.get("cached"):
            cache_note = " [dim](cache)[/]" if Console else " (cache)"

        display_country = entry.get("proxy_country") or entry.get("country") or "-"

        emit_progress.print(
            f"[{count}/{total}] {status_fmt}{cache_note} [bold]{entry['tag']}[/] -> "
            f"{destino} | IP: {entry.get('ip') or '-'} | "
            f"País: {display_country} | Ping: {ping_fmt}"
        )

        if entry.get("proxy_ip") and entry.get("proxy_ip") != entry.get("ip"):
            original_country = entry.get("country", "-")
            emit_progress.print(
                f"    [dim]País do Servidor: {original_country} -> "
                f"País de Saída: {entry.get('proxy_country', '-')}[/]"
            )

        if entry.get("error"):
            emit_progress.print(f"    [dim]Motivo: {entry['error']}[/]")


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
        """Testa as proxies carregadas usando rota real para medir ping."""
        if not self._outbounds:
            raise RuntimeError("Nenhuma proxy carregada para testar.")

        country_filter = country if country is not None else self.country_filter
        emit = self.console if (self.console is not None and (verbose is None or verbose)) else None

        results = self._perform_health_checks(
            self._outbounds,
            country_filter=country_filter,
            emit_progress=emit,
            force_refresh=force,
            functional_timeout=timeout,
            threads=threads,
            stop_on_success=find_first,
        )

        self._entries = results
        self.country_filter = country_filter

        if self.console is not None and (verbose is None or verbose):
            self._render_test_summary(results, country_filter)

        return results


    def _render_test_summary(self, entries: List[Dict[str, Any]], country_filter: Optional[str]) -> None:
        """Exibe relatório amigável via Rich quando disponível."""
        if not self.console or Table is None:
            return

        ok_entries = [e for e in entries if e.get("status") == "OK"]
        if country_filter:
            table_entries = [entry for entry in ok_entries if entry.get("country_match")]
        else:
            table_entries = ok_entries

        self.console.print()
        self.console.rule("Proxies Funcionais")
        if table_entries:
            self.console.print(self._render_test_table(table_entries))
        else:
            msg = "[yellow]Nenhuma proxy funcional encontrada.[/yellow]"
            if country_filter:
                msg = f"[yellow]Nenhuma proxy funcional corresponde ao filtro de país '{country_filter}'.[/yellow]"
            self.console.print(msg)

        success = sum(1 for entry in entries if entry.get("status") == "OK")
        fail = sum(1 for entry in entries if entry.get("status") == "ERRO")
        filtered = sum(1 for entry in entries if entry.get("status") == "FILTRADO")

        self.console.print()
        self.console.rule("Resumo do Teste")
        summary_parts = [
            f"[bold cyan]Total:[/] {len(entries)}",
            f"[bold green]Sucesso:[/] {success}",
            f"[bold red]Falhas:[/] {fail}",
        ]
        if filtered:
            summary_parts.append(f"[cyan]Filtradas:[/] {filtered}")
        self.console.print("    ".join(summary_parts))

        failed_entries = [
            entry for entry in entries
            if entry.get("status") == "ERRO" and entry.get("error")
        ]
        if failed_entries:
            self.console.print()
            self.console.print("[bold red]Detalhes das falhas:[/]")
            for entry in failed_entries[:10]:
                self.console.print(f" - [bold]{entry.get('tag') or '-'}[/]: {entry['error']}")
            if len(failed_entries) > 10:
                self.console.print(f"  [dim]... e mais {len(failed_entries) - 10} outras falhas.[/dim]")



    @classmethod
    def _render_test_table(cls, entries: List[Dict[str, Any]]):
        """Gera uma tabela Rich com o resultado dos testes."""
        if Table is None:
            raise RuntimeError("render_test_table requer a biblioteca 'rich'.")

        entries.sort(key=lambda e: e.get("ping") or float('inf'))

        table = Table(show_header=True, header_style="bold cyan", expand=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Tag", no_wrap=True, max_width=30)
        table.add_column("Destino", overflow="fold")
        table.add_column("IP Real (Saída)", no_wrap=True)
        table.add_column("País (Saída)", no_wrap=True)
        table.add_column("Ping", justify="right", no_wrap=True)
        for entry in entries:
            status = entry.get("status", "-")
            style = cls.STATUS_STYLES.get(status, "white")
            status_cell = Text(status, style=style) if Text else status
            destino = cls._format_destination(entry.get("host"), entry.get("port"))
            ping = entry.get("ping")
            ping_str = f"{ping:.1f} ms" if isinstance(ping, (int, float)) else "-"

            display_ip = entry.get("proxy_ip") or entry.get("ip") or "-"
            display_country = entry.get("proxy_country") or entry.get("country") or "-"

            table.add_row(
                status_cell,
                (entry.get("tag") or "-"),
                destino,
                display_ip,
                display_country,
                ping_str,
            )
        return table