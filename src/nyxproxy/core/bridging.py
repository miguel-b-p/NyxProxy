from __future__ import annotations

"""Rotinas responsáveis por iniciar, monitorar e encerrar pontes HTTP."""

import atexit
import json
import random
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import BridgeRuntime, Outbound


class BridgeMixin:
    """Funcionalidades ligadas ao ciclo de vida das pontes Xray."""

    @contextmanager
    def _temporary_bridge(
        self,
        outbound: Outbound,
        *,
        tag_prefix: str = "temp",
    ):
        """Cria uma ponte Xray temporária garantindo limpeza de recursos."""
        port: Optional[int] = None
        proc: Optional[subprocess.Popen] = None
        cfg_dir: Optional[Path] = None

        try:
            port = self._find_available_port()
            cfg = self._make_xray_config_http_inbound(port, outbound)
            xray_bin = self._which_xray()

            proc, cfg_path = self._launch_bridge_with_diagnostics(
                xray_bin, cfg, f"{tag_prefix}_{outbound.tag}"
            )
            cfg_dir = cfg_path.parent

            # Aguarda um curto período e verifica se o processo não encerrou
            time.sleep(0.5)
            if proc.poll() is not None:
                error_output = ""
                if proc.stderr:
                    error_output = self._decode_bytes(proc.stderr.read()).strip()
                raise RuntimeError(
                    "Processo Xray temporário finalizou prematuramente. "
                    f"Erro: {error_output or 'Nenhuma saída de erro.'}"
                )

            yield port, proc
        finally:
            self._terminate_process(proc, wait_timeout=2)
            self._safe_remove_dir(cfg_dir)
            if port is not None:
                self._release_port(port)

    def _find_available_port(self) -> int:
        """Encontra uma porta TCP disponível pedindo ao SO para alocar uma."""
        with self._port_allocation_lock:
            while True:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                    if port not in self._allocated_ports:
                        self._allocated_ports.add(port)
                        return port
                except OSError as e:
                    raise RuntimeError(
                        "Não foi possível alocar uma porta TCP disponível."
                    ) from e
                finally:
                    sock.close()
                # Se a porta já estava alocada (improvável), o loop tenta novamente.

    @staticmethod
    def _terminate_process(
        proc: Optional[subprocess.Popen], *, wait_timeout: float = 3.0
    ) -> None:
        """Finaliza um processo de forma silenciosa, ignorando erros."""
        if not proc:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=wait_timeout)
        except Exception:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

    @staticmethod
    def _safe_remove_dir(path: Optional[Path]) -> None:
        """Remove diretórios temporários sem propagar exceções."""
        if path and path.is_dir():
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass

    def _release_port(self, port: Optional[int]) -> None:
        """Libera uma porta registrada como em uso pelos testes temporários."""
        if port is not None:
            with self._port_allocation_lock:
                self._allocated_ports.discard(port)

    def _prepare_proxies_for_start(self):
        """Valida e carrega as proxies a serem iniciadas, usando cache se necessário."""
        if self._running:
            raise RuntimeError(
                "As pontes já estão em execução. Chame stop() antes."
            )

        if not self._outbounds:
            if self.use_cache and self._cache_entries:
                if self.console:
                    self.console.print(
                        "[yellow]Nenhuma fonte fornecida. Usando proxies do cache...[/yellow]"
                    )

                cached_outbounds = []
                for uri in self._cache_entries.keys():
                    try:
                        outbound = self._parse_uri_to_outbound(uri)
                        cached_outbounds.append((uri, outbound))
                    except Exception:
                        continue  # Ignora URIs do cache que não podem mais ser parseadas

                if not cached_outbounds:
                    raise RuntimeError(
                        "Nenhuma proxy válida pôde ser carregada do cache."
                    )
                self._outbounds = cached_outbounds
                self._prime_entries_from_cache()
            else:
                raise RuntimeError(
                    "Nenhuma proxy carregada e o cache está vazio."
                )

        if not self._outbounds:
            raise RuntimeError("Nenhuma proxy carregada para iniciar.")

    def _test_and_filter_proxies_for_start(
        self, threads: int, amounts: int, country_filter: Optional[str], find_first: Optional[int]
    ) -> List[Dict[str, Any]]:
        """Testa, filtra e ordena as proxies que serão usadas para criar as pontes."""
        ok_from_cache = [
            e
            for e in self._entries
            if e.get("status") == "OK" and self.matches_country(e, country_filter)
        ]

        needed_proxies = find_first or amounts
        if len(ok_from_cache) < needed_proxies:
            if self.console:
                self.console.print(
                    f"[yellow]Cache insuficiente. Testando até {needed_proxies} proxies válidas...[/yellow]"
                )
            self.test(
                threads=threads,
                country=country_filter,
                verbose=False,
                find_first=needed_proxies,
                force=False,
            )
        elif self.console:
            self.console.print(
                "[green]Proxies suficientes encontradas no cache. Iniciando...[/green]"
            )

        approved_entries = [
            entry
            for entry in self._entries
            if entry.get("status") == "OK"
            and self.matches_country(entry, country_filter)
        ]

        approved_entries.sort(key=lambda e: float(e.get("ping") or "inf"))

        if not approved_entries:
            msg = (
                f"Nenhuma proxy aprovada para o país '{country_filter}'."
                if country_filter
                else "Nenhuma proxy aprovada para iniciar."
            )
            raise RuntimeError(f"{msg} Execute o teste e verifique os resultados.")

        if amounts > 0:
            if len(approved_entries) < amounts:
                if self.console:
                    self.console.print(
                        f"[yellow]Aviso: Apenas {len(approved_entries)} proxies aprovadas "
                        f"(solicitado: {amounts}). Iniciando as disponíveis.[/yellow]"
                    )
            return approved_entries[:amounts]
        return approved_entries

    def _launch_and_monitor_bridges(self, entries: List[Dict[str, Any]]) -> List[BridgeRuntime]:
        """Inicia os processos Xray para as proxies aprovadas e retorna os runtimes."""
        xray_bin = self._which_xray()
        bridges_runtime: List[BridgeRuntime] = []

        if self.console and entries:
            self.console.print(
                f"\n[green]Iniciando {len(entries)} pontes ordenadas por ping[/]"
            )

        try:
            for entry in entries:
                raw_uri, outbound = self._outbounds[entry["index"]]
                port = self._find_available_port()
                cfg = self._make_xray_config_http_inbound(port, outbound)
                proc, cfg_path = self._launch_bridge_with_diagnostics(
                    xray_bin, cfg, outbound.tag
                )

                # Verifica se o processo Xray não falhou imediatamente
                time.sleep(0.2)
                if proc.poll() is not None:
                    error_output = ""
                    if proc.stderr:
                        error_output = self._decode_bytes(proc.stderr.read()).strip()
                    raise RuntimeError(
                        f"Processo Xray para '{outbound.tag}' finalizou inesperadamente. "
                        f"Erro: {error_output or 'Nenhuma saída de erro.'}"
                    )

                bridge = self.BridgeRuntime(
                    tag=outbound.tag,
                    port=port,
                    scheme=raw_uri.split("://", 1)[0].lower(),
                    uri=raw_uri,
                    process=proc,
                    workdir=cfg_path.parent,
                )
                bridges_runtime.append(bridge)
        except Exception:
            # Garante a limpeza em caso de falha durante o loop de inicialização
            for bridge in bridges_runtime:
                self._terminate_process(bridge.process)
                self._safe_remove_dir(bridge.workdir)
                self._release_port(bridge.port)
            raise
        return bridges_runtime

    def start(
        self,
        *,
        threads: int = 1,
        amounts: int = 1,
        country: Optional[str] = None,
        auto_test: bool = True,
        wait: bool = False,
        find_first: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Cria pontes HTTP locais para as proxys aprovadas, testando se necessário."""
        self._prepare_proxies_for_start()
        country_filter = country if country is not None else self.country_filter

        approved_entries = (
            self._test_and_filter_proxies_for_start(
                threads, amounts, country_filter, find_first
            )
            if auto_test
            else self._entries
        )

        bridges_runtime = self._launch_and_monitor_bridges(approved_entries)
        self._bridges = bridges_runtime
        self._running = True

        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True

        if self.console:
            self._display_active_bridges_summary(country_filter)

        bridges_with_id = [
            {"id": idx, "url": bridge.url, "uri": bridge.uri, "tag": bridge.tag}
            for idx, bridge in enumerate(self._bridges)
        ]

        if wait:
            self.wait()
        else:
            self._start_wait_thread()

        return bridges_with_id

    def _display_active_bridges_summary(self, country_filter: Optional[str]) -> None:
        """Exibe a tabela de pontes ativas no console."""
        if not self.console:
            return
        
        entry_map = {e["uri"]: e for e in self._entries}
        
        self.console.print()
        title = "Pontes HTTP ativas"
        if country_filter:
            title += f" - País: {country_filter}"
        self.console.rule(f"{title} - Ordenadas por Ping")

        for idx, bridge in enumerate(self._bridges):
            entry = entry_map.get(bridge.uri)
            ping = entry.get("ping") if entry else None
            ping_str = f"{ping:6.1f}ms" if isinstance(ping, (int, float)) else "   -   "
            self.console.print(
                f"[bold cyan]ID {idx:<2}[/] http://127.0.0.1:{bridge.port}  ->  [{ping_str}] ('{bridge.tag}')"
            )

        self.console.print()
        self.console.print("Pressione Ctrl+C para encerrar todas as pontes.")

    def _start_wait_thread(self) -> None:
        """Dispara thread em segundo plano para monitorar processos iniciados."""
        if self._wait_thread and self._wait_thread.is_alive():
            return
        self._stop_event.clear()
        thread = threading.Thread(
            target=self._wait_loop_wrapper, name="ProxyWaitThread", daemon=True
        )
        self._wait_thread = thread
        thread.start()

    def _wait_loop_wrapper(self) -> None:
        """Executa ``wait`` capturando exceções para um término limpo da thread."""
        try:
            self.wait()
        except RuntimeError:
            pass

    def wait(self) -> None:
        """Bloqueia até que todas as pontes terminem ou ``stop`` seja chamado."""
        if not self._running:
            raise RuntimeError("Nenhuma ponte ativa para aguardar.")
        try:
            while not self._stop_event.is_set():
                alive = any(
                    bridge.process and bridge.process.poll() is None
                    for bridge in self._bridges
                )
                if not alive:
                    if self.console:
                        self.console.print(
                            "\n[yellow]Todos os processos xray finalizaram.[/yellow]"
                        )
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            if self.console:
                self.console.print(
                    "\n[yellow]Interrupção recebida, encerrando pontes...[/yellow]"
                )
        finally:
            self.stop()

    def stop(self) -> None:
        """Finaliza processos Xray ativos e limpa arquivos temporários."""
        if not self._running and not self._bridges:
            return

        self._stop_event.set()

        bridges_to_stop = list(self._bridges)
        if bridges_to_stop:
            for bridge in bridges_to_stop:
                self._terminate_process(bridge.process)
                self._safe_remove_dir(bridge.workdir)
                self._release_port(bridge.port)

        self._bridges = []
        self._running = False

        if self._wait_thread and self._wait_thread is not threading.current_thread():
            self._wait_thread.join(timeout=2.0)
        self._wait_thread = None

    def get_http_proxy(self) -> List[Dict[str, Any]]:
        """Retorna ID, URL local e URI de cada ponte em execução."""
        if not self._running:
            return []
        return [
            {"id": idx, "url": bridge.url, "uri": bridge.uri, "tag": bridge.tag}
            for idx, bridge in enumerate(self._bridges)
        ]

    def _make_xray_config_http_inbound(
        self, port: int, outbound: Outbound
    ) -> Dict[str, Any]:
        """Monta o arquivo de configuração do Xray para uma ponte HTTP local."""
        cfg = {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {
                    "tag": "http-in",
                    "listen": "127.0.0.1",
                    "port": port,
                    "protocol": "http",
                    "settings": {},
                }
            ],
            "outbounds": [
                outbound.config,
                {"tag": "direct", "protocol": "freedom", "settings": {}},
                {"tag": "block", "protocol": "blackhole", "settings": {}},
            ],
            "routing": {
                "domainStrategy": "AsIs",
                "rules": [
                    {
                        "type": "field",
                        "outboundTag": outbound.tag,
                        "network": "tcp,udp",
                    }
                ],
            },
        }
        if "tag" not in cfg["outbounds"][0]:
            cfg["outbounds"][0]["tag"] = outbound.tag
        return cfg

    def _launch_bridge_with_diagnostics(
        self, xray_bin: str, cfg: Dict[str, Any], name: str
    ) -> Tuple[subprocess.Popen, Path]:
        """Inicializa o Xray com captura de stdout/stderr para melhor diagnóstico."""
        tmpdir = Path(tempfile.mkdtemp(prefix=f"xray_{name}_"))
        cfg_path = tmpdir / "config.json"
        cfg_path.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        proc = subprocess.Popen(
            [xray_bin, "-config", str(cfg_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return proc, cfg_path

    def rotate_proxy(self, bridge_id: int) -> bool:
        """Troca a proxy de uma ponte em execução por outra proxy aleatória e funcional."""
        if not self._running or not (0 <= bridge_id < len(self._bridges)):
            if self.console:
                msg = f"ID de ponte inválido: {bridge_id}. IDs válidos: 0 a {len(self._bridges) - 1}."
                self.console.print(f"[red]Erro: {msg}[/red]")
            return False

        bridge = self._bridges[bridge_id]
        used_uris = {b.uri for b in self._bridges}

        candidates = [
            entry
            for entry in self._entries
            if entry.get("status") == "OK"
            and self.matches_country(entry, self.country_filter)
            and entry.get("uri") not in used_uris
        ]

        if not candidates:
            if self.console:
                self.console.print(
                    f"[yellow]Aviso: Nenhuma outra proxy disponível para rotacionar a ponte ID {bridge_id}.[/yellow]"
                )
            return False

        new_entry = random.choice(candidates)
        new_raw_uri, new_outbound = self._outbounds[new_entry["index"]]
        new_scheme = new_raw_uri.split("://", 1)[0].lower()

        self._terminate_process(bridge.process, wait_timeout=2)
        self._safe_remove_dir(bridge.workdir)

        try:
            xray_bin = self._which_xray()
            cfg = self._make_xray_config_http_inbound(bridge.port, new_outbound)
            new_proc, new_cfg_path = self._launch_bridge_with_diagnostics(
                xray_bin, cfg, new_outbound.tag
            )
        except Exception as e:
            if self.console:
                self.console.print(
                    f"[red]Falha ao reiniciar ponte {bridge_id} na porta {bridge.port}: {e}[/red]"
                )
            bridge.process = None  # Marca a ponte como inativa
            return False

        self._bridges[bridge_id] = self.BridgeRuntime(
            tag=new_outbound.tag,
            port=bridge.port,
            scheme=new_scheme,
            uri=new_raw_uri,
            process=new_proc,
            workdir=new_cfg_path.parent,
        )

        if self.console:
            self.console.print(
                f"[green]Sucesso:[/green] Ponte [bold]ID {bridge_id}[/] (porta {bridge.port}) "
                f"rotacionada para a proxy '[bold]{new_outbound.tag}[/]'"
            )
            self._display_active_bridges_summary(self.country_filter)

        return True