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
        """Localiza o binário do proxychains4."""
        for candidate in ("proxychains4", "proxychains"):
            found = self._shutil_which(candidate)
            if found:
                return found
        raise FileNotFoundError(
            "Não foi possível localizar o binário do 'proxychains4' ou 'proxychains'. "
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

        # Inicia as pontes sem bloquear, para que possamos continuar
        # O 'start' já faz o teste se necessário.
        bridges = self.start(
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
            # Encontra o executável do proxychains
            proxychains_bin = self._which_proxychains()

            # Cria a lista de proxies para o arquivo de configuração
            proxy_lines = []
            for bridge in self._bridges:
                # Formato: http 127.0.0.1 54000
                proxy_lines.append(f"http 127.0.0.1 {bridge.port}")

            proxy_list_str = "\n".join(proxy_lines)
            config_content = PROXYCHAINS_CONF_TEMPLATE.format(
                proxy_list=proxy_list_str
            ).strip()
            print(config_content)

            # Cria um diretório e arquivo de configuração temporários
            tmpdir_path = Path(tempfile.mkdtemp(prefix="nyxproxy_chains_"))
            config_path = tmpdir_path / "proxychains.conf"
            config_path.write_text(config_content, encoding="utf-8")

            # Monta e executa o comando final
            full_command = [proxychains_bin, "-f", str(config_path), *cmd_list]

            if self.console:
                self.console.print("\n[bold magenta]Executando comando via ProxyChains...[/]")
                self.console.print(f"[dim]$ {' '.join(full_command)}[/dim]\n")

            # Usamos subprocess.run para aguardar a conclusão do processo
            result = subprocess.run(full_command)
            return result.returncode

        finally:
            # Limpeza crucial: parar as pontes e remover arquivos temporários
            if self.console:
                self.console.print("\n[bold yellow]Finalizando pontes e limpando recursos...[/]")
            self.stop()
            if tmpdir_path:
                shutil.rmtree(tmpdir_path, ignore_errors=True)