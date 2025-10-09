from __future__ import annotations

"""Rotinas para integração com o utilitário proxychains."""

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List

PROXYCHAINS_CONF_TEMPLATE = """
# proxychains.conf gerado por NyxProxy
random_chain
proxy_dns
remote_dns_subnet 224
tcp_read_time_out 15000
tcp_connect_time_out 8000
[ProxyList]
{proxy_list}
"""


class ChainsMixin:
    """Funcionalidades para executar comandos através do proxychains."""

    def _which_proxychains(self) -> str:
        """Localiza o binário do proxychains4 ou proxychains."""
        for candidate in ("proxychains4", "proxychains"):
            if found := self._shutil_which(candidate):
                return found
        raise FileNotFoundError(
            "Comando 'proxychains4' ou 'proxychains' não encontrado. "
            "Certifique-se de que ele está instalado e no seu PATH."
        )

    def run_with_chains(
        self,
        cmd_list: List[str],
        *,
        threads: int = 1,
        amounts: int = 1,
        country: str | None = None,
    ) -> int:
        """
        Inicia pontes, cria uma config para o proxychains e executa um comando.

        Retorna o código de saída do comando executado.
        """
        if not cmd_list:
            raise ValueError("O comando a ser executado não pode estar vazio.")

        # O 'start' já faz o teste se necessário.
        # wait=False para que o controle retorne para este método.
        self.start(
            threads=threads,
            amounts=amounts,
            country=country,
            wait=False,
            find_first=amounts,
        )

        if not self._bridges:
            raise RuntimeError("Nenhuma ponte de proxy pôde ser iniciada para o chain.")

        tmpdir_path: Path | None = None
        try:
            proxychains_bin = self._which_proxychains()

            proxy_lines = [f"http 127.0.0.1 {bridge.port}" for bridge in self._bridges]
            config_content = PROXYCHAINS_CONF_TEMPLATE.format(
                proxy_list="\n".join(proxy_lines)
            ).strip()

            tmpdir_path = Path(tempfile.mkdtemp(prefix="nyxproxy_chains_"))
            config_path = tmpdir_path / "proxychains.conf"
            config_path.write_text(config_content, encoding="utf-8")

            full_command = [proxychains_bin, "-f", str(config_path), *cmd_list]

            if self.console:
                self.console.print("\n[bold magenta]Executando comando via ProxyChains...[/]")
                # Usa aspas para comandos com espaços
                cmd_str = ' '.join(f"'{arg}'" if ' ' in arg else arg for arg in full_command)
                self.console.print(f"[dim]$ {cmd_str}[/dim]\n")
            
            # Executa o comando e aguarda sua conclusão
            result = subprocess.run(full_command)
            return result.returncode

        finally:
            # Limpeza crucial: parar as pontes e remover arquivos temporários
            if self.console:
                self.console.print("\n[bold yellow]Finalizando pontes e limpando recursos...[/]")
            self.stop()
            if tmpdir_path:
                shutil.rmtree(tmpdir_path, ignore_errors=True)