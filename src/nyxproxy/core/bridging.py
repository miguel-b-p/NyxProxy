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

            time.sleep(1.0)
            if proc.poll() is not None:
                error_output = ""
                if proc.stderr:
                    error_output = self._decode_bytes(proc.stderr.read()).strip()

                raise RuntimeError(
                    "Processo Xray temporário finalizou antes do teste. "
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
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]

                if port in self._allocated_ports:
                    return self._find_available_port()

                self._allocated_ports.add(port)
                return port
            except OSError as e:
                raise RuntimeError("Não foi possível alocar uma porta TCP disponível pelo sistema operacional.") from e
            finally:
                sock.close()


    @staticmethod
    def _terminate_process(proc: Optional[subprocess.Popen], *, wait_timeout: float = 3.0) -> None:
        """Finaliza um processo de forma silenciosa, ignorando erros."""
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=wait_timeout)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


    @staticmethod
    def _safe_remove_dir(path: Optional[Path]) -> None:
        """Remove diretórios temporários sem propagar exceções."""
        if path is None:
            return
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass


    def _release_port(self, port: Optional[int]) -> None:
        """Libera uma porta registrada como em uso pelos testes temporários."""
        if port is None:
            return
        with self._port_allocation_lock:
            self._allocated_ports.discard(port)


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
        if self._running:
            raise RuntimeError("As pontes já estão em execução. Chame stop() antes de iniciar novamente.")

        if not self._outbounds:
            if self.use_cache and self._cache_entries:
                if self.console:
                    self.console.print("[yellow]Nenhuma fonte fornecida. Usando proxies do cache...[/yellow]")
                
                cached_outbounds = []
                for uri in self._cache_entries.keys():
                    try:
                        outbound = self._parse_uri_to_outbound(uri)
                        cached_outbounds.append((uri, outbound))
                    except Exception:
                        continue # Ignora URIs do cache que não podem mais ser parseadas
                
                if not cached_outbounds:
                    raise RuntimeError("Nenhuma proxy válida pôde ser carregada do cache.")

                self._outbounds = cached_outbounds
                self._prime_entries_from_cache()
            else:
                raise RuntimeError("Nenhuma proxy carregada e o cache está vazio. Forneça uma fonte de proxies.")

        if not self._outbounds:
            raise RuntimeError("Nenhuma proxy carregada para iniciar.")

        country_filter = country if country is not None else self.country_filter

        if auto_test:
            # Verifica primeiro proxies válidas no cache
            ok_from_cache = [
                e for e in self._entries
                if e.get("status") == "OK" and self.matches_country(e, country_filter)
            ]

            needed_proxies = find_first or amounts
            if len(ok_from_cache) < needed_proxies:
                if self.console:
                    self.console.print(f"[yellow]Cache insuficiente. Procurando e testando até {needed_proxies} proxies válidas...[/yellow]")

                self.test(
                    threads=threads,
                    country=country_filter,
                    verbose=False,  # O start terá seu próprio resumo
                    find_first=needed_proxies,
                    force=False # Não re-testa o que já está OK no cache
                )
            elif self.console:
                self.console.print("[green]Proxies suficientes encontradas no cache. Iniciando imediatamente.[/green]")

        approved_entries = [
            entry for entry in self._entries
            if entry.get("status") == "OK"
            and self.matches_country(entry, country_filter)
        ]

        def get_ping_for_sort(entry: Dict[str, Any]) -> float:
            ping = entry.get("ping")
            return float(ping) if isinstance(ping, (int, float)) else float('inf')

        approved_entries.sort(key=get_ping_for_sort)

        if not approved_entries:
            if country_filter:
                raise RuntimeError(
                    f"Nenhuma proxy aprovada para o país '{country_filter}'. "
                    "Execute o teste e verifique os resultados."
                )
            else:
                raise RuntimeError("Nenhuma proxy aprovada para iniciar. Execute test() e verifique os resultados.")

        if amounts > 0:
            if len(approved_entries) < amounts:
                 if self.console:
                    self.console.print(
                        f"[yellow]Aviso: Apenas {len(approved_entries)} proxies aprovadas encontradas (solicitado: {amounts}). "
                        "Iniciando as disponíveis.[/yellow]"
                    )
            approved_entries = approved_entries[:amounts]


        xray_bin = self._which_xray()

        self._stop_event.clear()
        bridges_runtime: List[BridgeRuntime] = []
        bridges_display: List[Tuple[BridgeRuntime, float]] = []

        if self.console and approved_entries:
            self.console.print()
            self.console.print(
                f"[green]Iniciando {len(approved_entries)} pontes ordenadas por ping[/]"
            )

        try:
            for entry in approved_entries:
                raw_uri, outbound = self._outbounds[entry["index"]]

                port = self._find_available_port()
                cfg = self._make_xray_config_http_inbound(port, outbound)
                scheme = raw_uri.split("://", 1)[0].lower()

                proc, cfg_path = self._launch_bridge_with_diagnostics(xray_bin, cfg, outbound.tag)
                bridge = self.BridgeRuntime(
                    tag=outbound.tag,
                    port=port,
                    scheme=scheme,
                    uri=raw_uri,
                    process=proc,
                    workdir=cfg_path.parent,
                )
                bridges_runtime.append(bridge)
                bridges_display.append((bridge, get_ping_for_sort(entry)))
        except Exception:
            for bridge in bridges_runtime:
                self._terminate_process(bridge.process)
                self._safe_remove_dir(bridge.workdir)
                self._release_port(bridge.port)
            raise

        self._bridges = bridges_runtime
        self._running = True

        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True

        if self.console:
            self.console.print()
            self.console.rule(f"Pontes HTTP ativas{f' - País: {country_filter}' if country_filter else ''} - Ordenadas por Ping")
            for idx, (bridge, ping) in enumerate(bridges_display):
                ping_str = f"{ping:6.1f}ms" if ping != float('inf') else "      -      "
                self.console.print(
                    f"[bold cyan]ID {idx:<2}[/] http://127.0.0.1:{bridge.port}  ->  [{ping_str}]"
                )

            self.console.print()
            self.console.print("Pressione Ctrl+C para encerrar todas as pontes.")

        bridges_with_id = [
            {"id": idx, "url": bridge.url, "uri": bridge.uri}
            for idx, bridge in enumerate(self._bridges)
        ]

        if wait:
            self.wait()
        else:
            self._start_wait_thread()

        return bridges_with_id


    def _start_wait_thread(self) -> None:
        """Dispara thread em segundo plano para monitorar processos iniciados."""
        if self._wait_thread and self._wait_thread.is_alive():
            return
        thread = threading.Thread(target=self._wait_loop_wrapper, name="ProxyWaitThread", daemon=True)
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
                        self.console.print("\n[yellow]Todos os processos xray finalizaram.[/yellow]")
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:
            if self.console:
                self.console.print("\n[yellow]Interrupção recebida, encerrando pontes...[/yellow]")
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
            self._wait_thread.join(timeout=1.0)
        self._wait_thread = None


    def get_http_proxy(self) -> List[Dict[str, Any]]:
        """Retorna ID, URL local e URI de cada ponte em execução."""
        if not self._running:
            return []
        return [
            {"id": idx, "url": bridge.url, "uri": bridge.uri}
            for idx, bridge in enumerate(self._bridges)
        ]

    def _make_xray_config_http_inbound(self, port: int, outbound: Outbound) -> Dict[str, Any]:
        """Monta o arquivo de configuração do Xray para uma ponte HTTP local."""
        cfg = {
            "log": {"loglevel": "warning"},
            "inbounds": [{
                "tag": "http-in",
                "listen": "127.0.0.1",
                "port": port,
                "protocol": "http",
                "settings": {}
            }],
            "outbounds": [
                outbound.config,
                {"tag": "direct", "protocol": "freedom", "settings": {}},
                {"tag": "block", "protocol": "blackhole", "settings": {}}
            ],
            "routing": {
                "domainStrategy": "AsIs",
                "rules": [
                    {"type": "field", "outboundTag": outbound.tag, "network": "tcp,udp"}
                ]
            }
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
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

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
        uri_to_replace = bridge.uri

        candidates = [
            entry for entry in self._entries
            if entry.get("status") == "OK"
            and self.matches_country(entry, self.country_filter)
            and entry.get("uri") != uri_to_replace
        ]

        if not candidates:
            if self.console:
                self.console.print(f"[yellow]Aviso: Nenhuma outra proxy disponível para rotacionar a ponte ID {bridge_id}.[/yellow]")
            return False

        new_entry = random.choice(candidates)
        new_raw_uri, new_outbound = self._outbounds[new_entry["index"]]
        new_scheme = new_raw_uri.split("://", 1)[0].lower()

        self._terminate_process(bridge.process, wait_timeout=2)
        self._safe_remove_dir(bridge.workdir)

        try:
            xray_bin = self._which_xray()
            cfg = self._make_xray_config_http_inbound(bridge.port, new_outbound)
            new_proc, new_cfg_path = self._launch_bridge_with_diagnostics(xray_bin, cfg, new_outbound.tag)
        except Exception as e:
            if self.console:
                self.console.print(f"[red]Falha ao reiniciar ponte {bridge_id} na porta {bridge.port}: {e}[/red]")
            bridge.process = None # Marca a ponte como inativa
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
                f"[green]Sucesso:[/green] Ponte [bold]ID {bridge_id}[/] (porta {bridge.port}) rotacionada para a proxy '[bold]{new_outbound.tag}[/]'"
            )

        return True