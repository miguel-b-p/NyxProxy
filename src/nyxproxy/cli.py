#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ponto de entrada da aplicação de linha de comando (CLI) para o NyxProxy."""

import json
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel

from .manager import Proxy

# Cria a instância da aplicação Typer
app = typer.Typer(
    name="nyxproxy",
    help="Ferramenta para testar e criar pontes HTTP para proxies V2Ray/Xray.",
    add_completion=False,
    rich_markup_mode="markdown",
)

console = Console()


@app.callback()
def callback():
    """
    **NyxProxy**: Um gerenciador de proxies poderoso e flexível.
    """
    pass


@app.command(help="Testa proxies de fontes e exibe um relatório de status.")
def test(
    sources: List[str] = typer.Argument(
        ...,  # Obrigatório
        help="Uma ou mais fontes de proxies (arquivo local ou URL).",
        metavar="SOURCES...",
    ),
    country: Optional[str] = typer.Option(
        None,
        "--country",
        "-c",
        help="Filtra proxies por país (ex: 'BR', 'US', 'Germany').",
    ),
    threads: int = typer.Option(
        10,
        "--threads",
        "-t",
        min=1,
        help="Número de workers para testes paralelos.",
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        "-l",
        help="Limita o número máximo de proxies a serem carregados (0 = sem limite).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Ignora o cache e re-testa todos os proxies.",
    ),
    output_json: bool = typer.Option(
        False,
        "--output-json",
        "-j",
        help="Exibe os resultados do teste em formato JSON.",
    ),
    find_first: Optional[int] = typer.Option(
        None,
        "--find-first",
        "-ff",
        help="Para o teste após encontrar o número especificado de proxies funcionais.",
    ),
):
    """Executa o teste de proxies a partir das fontes fornecidas."""
    if not output_json:
        console.print(
            Panel(
                "[bold cyan]NyxProxy[/] - Testando Servidores",
                expand=False,
                border_style="purple",
            )
        )
    try:
        proxy_manager = Proxy(
            sources=sources,
            max_count=limit,
            use_console=not output_json,
            country=country,
        )

        if not proxy_manager.entries and not proxy_manager.parse_errors:
            console.print("[yellow]Aviso: Nenhuma proxy válida encontrada nas fontes.[/yellow]")
            raise typer.Exit()

        results = proxy_manager.test(
            threads=threads,
            country=country,
            force=force,
            verbose=not output_json,
            find_first=find_first,
        )

        if output_json:
            print(json.dumps(results, indent=2, ensure_ascii=False))

    except (FileNotFoundError, RuntimeError, ValueError) as e:
        console.print(f"[bold red]Erro: {e}[/bold red]")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[bold red]Ocorreu um erro inesperado: {e}[/bold red]")
        raise typer.Exit(code=1)


@app.command(help="Inicia pontes HTTP locais com os proxies aprovados.")
def start(
    sources: Optional[List[str]] = typer.Argument(
        None,
        help="Fontes de proxies (arquivo/URL). Se omitido, usa proxies do cache.",
        metavar="SOURCES...",
    ),
    country: Optional[str] = typer.Option(
        None,
        "--country",
        "-c",
        help="Inicia pontes apenas para proxies de um país específico.",
    ),
    threads: int = typer.Option(
        10,
        "--threads",
        "-t",
        min=1,
        help="Número de workers para os testes que precedem o início.",
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        "-l",
        help="Limita o número de proxies a serem carregados.",
    ),
    amounts: int = typer.Option(
        5,
        "--amounts",
        "-a",
        help="Número de pontes HTTP a serem iniciadas com os melhores proxies.",
    ),
):
    """Inicia pontes HTTP que ficam ativas até que o programa seja interrompido."""
    console.print(
        Panel(
            "[bold green]NyxProxy[/] - Iniciando Pontes HTTP",
            expand=False,
            border_style="green",
        )
    )
    try:
        proxy_manager = Proxy(
            sources=sources,
            max_count=limit,
            use_console=True,
            country=country,
        )
        proxy_manager.start(
            threads=threads,
            amounts=amounts,
            country=country,
            wait=True,  # Bloqueia a execução para manter as pontes ativas
            find_first=amounts, # Otimização: para de testar ao encontrar o necessário
        )

    except (FileNotFoundError, RuntimeError, ValueError) as e:
        console.print(f"[bold red]Erro ao iniciar: {e}[/bold red]")
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        # A lógica de 'stop' já é chamada pelo 'wait()'
        pass
    except Exception as e:
        console.print(f"[bold red]Ocorreu um erro inesperado: {e}[/bold red]")
        raise typer.Exit(code=1)

    console.print("\n[bold green]Todas as pontes foram encerradas. Até logo![/bold green]")


@app.command(
    help="Executa um comando através de pontes de proxy com proxychains.",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def chains(
    ctx: typer.Context,
    sources: Optional[List[str]] = typer.Option(
        None,
        "--source",
        "-s",
        help="Uma ou mais fontes de proxies (arquivo/URL). Se omitido, usa cache.",
    ),
    country: Optional[str] = typer.Option(
        None, "--country", "-c", help="Usa apenas proxies de um país específico."
    ),
    threads: int = typer.Option(
        10, "--threads", "-t", min=1, help="Threads para os testes pré-execução."
    ),
    amounts: int = typer.Option(5, "--amounts", "-a", help="Número de pontes a usar."),
    limit: int = typer.Option(0, "--limit", "-l", help="Limita proxies carregados."),
):
    """
    Inicia pontes e executa um comando através delas usando proxychains.

    O comando e seus argumentos devem ser passados após todas as opções do nyxproxy.
    Exemplo: `nyxproxy chains -a 3 -s my_proxies.txt -- curl -s ipinfo.io`
    """
    command_to_run = ctx.args
    if not command_to_run:
        console.print("[bold red]Erro:[/bold red] Especifique um comando para ser executado.")
        raise typer.Exit(code=1)

    console.print(
        Panel(
            "[bold magenta]NyxProxy[/] - Executando via ProxyChains",
            expand=False,
            border_style="magenta",
        )
    )
    try:
        proxy_manager = Proxy(
            sources=sources,
            max_count=limit,
            use_console=True,
            country=country,
        )
        exit_code = proxy_manager.run_with_chains(
            cmd_list=command_to_run,
            threads=threads,
            amounts=amounts,
            country=country,
        )
        raise typer.Exit(code=exit_code)

    except (FileNotFoundError, RuntimeError, ValueError) as e:
        console.print(f"[bold red]Erro: {e}[/bold red]")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[bold red]Ocorreu um erro inesperado: {e}[/bold red]")
        raise typer.Exit(code=1)


@app.command(help="Limpa o cache de proxies.")
def clear(
    age: Optional[str] = typer.Argument(
        None,
        help="Limpa proxies mais antigas que o tempo especificado. Ex: '5H', '1D', '2S,12H'. Se omitido, limpa todo o cache.",
        metavar="AGE",
    ),
):
    """Remove entradas do cache de testes de proxies."""
    console.print(Panel("[bold yellow]NyxProxy[/] - Limpando Cache", expand=False, border_style="yellow"))
    try:
        proxy_manager = Proxy(use_console=True, use_cache=True)
        proxy_manager.clear_cache(age)
    except Exception as e:
        console.print(f"[bold red]Erro ao limpar o cache: {e}[/bold red]")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()