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
)

console = Console()


@app.callback()
def callback():
    """
    NyxProxy: Um gerenciador de proxies poderoso e flexível.
    """
    pass


@app.command(help="Testa proxies de fontes e exibe um relatório de status.")
def test(
    sources: List[str] = typer.Argument(
        ...,  # ... significa que é obrigatório
        help="Uma ou mais fontes de proxies (arquivo local ou URL).",
        metavar="SOURCES...",
    ),
    country: Optional[str] = typer.Option(
        None,
        "--country",
        "-c",
        help="Filtra proxies por país (ex: 'BR', 'US', 'Alemanha').",
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
    """
    Executa o teste de proxies.
    """
    console.print(
        Panel(
            "[bold cyan]NyxProxy[/] - Testando Servidores",
            expand=False,
            border_style="purple",
        )
    )

    try:
        # Instancia o gerenciador de proxies com as configurações da CLI
        proxy_manager = Proxy(
            sources=sources,
            max_count=limit,
            use_console=not output_json,  # Desativa o console interno se a saída for JSON
            country=country,
        )

        if not proxy_manager.entries and not proxy_manager.parse_errors:
            console.print("[yellow]Aviso: Nenhuma proxy encontrada nas fontes informadas.[/]")
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

    except FileNotFoundError:
        console.print("[bold red]Erro: O arquivo de proxy não foi encontrado.[/]")
        raise typer.Exit(code=1)
    except RuntimeError as e:
        console.print(f"[bold red]Erro durante a execução: {e}[/]")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[bold red]Ocorreu um erro inesperado: {e}[/]")
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
    """
    Inicia as pontes HTTP.
    """
    console.print(
        Panel(
            "[bold green]NyxProxy[/] - Iniciando Pontes HTTP",
            expand=False,
            border_style="green",
        )
    )

    try:
        # Instancia o gerenciador de proxies
        proxy_manager = Proxy(
            sources=sources,
            max_count=limit,
            use_console=True,  # Sempre usa o console para a saída de 'start'
            country=country,
        )

        # Inicia as pontes, o que aciona o teste automaticamente se necessário
        proxy_manager.start(
            threads=threads,
            amounts=amounts,
            country=country,
            wait=True,  # Bloqueia a execução para manter as pontes ativas
            find_first=amounts, # Otimização: para de testar ao encontrar o necessário
        )

    except FileNotFoundError as e:
        console.print(f"[bold red]Erro: O arquivo de proxy não foi encontrado: {e}[/]")
        raise typer.Exit(code=1)
    except RuntimeError as e:
        console.print(f"[bold red]Erro ao iniciar as pontes: {e}[/]")
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        console.print("\n[yellow]Encerrando as pontes...[/]")
    except Exception as e:
        console.print(f"[bold red]Ocorreu um erro inesperado: {e}[/]")
        raise typer.Exit(code=1)

    console.print("[bold green]Todas as pontes foram encerradas. Até logo![/]")


@app.command(
    help="Executa um comando através de pontes de proxy com proxychains.",
    # Mantemos estas configurações para que o COMANDO possa ter seus próprios argumentos
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def chains(
    ctx: typer.Context,
    # --- CORREÇÃO AQUI ---
    # Trocamos 'Argument' por 'Option' para evitar conflito.
    sources: Optional[List[str]] = typer.Option(
        None,
        "--source",
        "-s",
        help="Uma ou mais fontes de proxies (arquivo/URL). Se omitido, usa proxies do cache.",
    ),
    country: Optional[str] = typer.Option(
        None,
        "--country",
        "-c",
        help="Usa apenas proxies de um país específico para o chain.",
    ),
    threads: int = typer.Option(
        10,
        "--threads",
        "-t",
        min=1,
        help="Número de workers para os testes que precedem a execução.",
    ),
    amounts: int = typer.Option(
        5,
        "--amounts",
        "-a",
        help="Número de pontes a serem usadas no chain.",
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        "-l",
        help="Limita o número de proxies a serem carregados.",
    ),
):
    """
    Inicia pontes e executa um comando através delas usando proxychains.
    O comando e seus argumentos devem ser passados após todas as opções do nyxproxy.
    Use '--' para separar claramente as opções do comando, se necessário.
    """
    command_to_run = ctx.args
    if not command_to_run:
        console.print("[bold red]Erro:[/bold red] Você precisa especificar um comando para ser executado.")
        console.print("Exemplo: nyxproxy chains --amounts 3 -- wget -qO- https://httpbin.org/ip")
        raise typer.Exit(code=1)

    console.print(
        Panel(
            "[bold magenta]NyxProxy[/] - Executando via ProxyChains",
            expand=False,
            border_style="magenta",
        )
    )

    try:
        # --- CORREÇÃO AQUI ---
        # Passamos a nova variável 'sources' para o gerenciador
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
        # O processo já terminou, então saímos com seu código de retorno
        raise typer.Exit(code=exit_code)

    except FileNotFoundError as e:
        console.print(f"[bold red]Erro de dependência: {e}[/]")
        raise typer.Exit(code=1)
    except (RuntimeError, ValueError) as e:
        console.print(f"[bold red]Erro ao preparar o chain: {e}[/]")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[bold red]Ocorreu um erro inesperado: {e}[/]")
        raise typer.Exit(code=1)


@app.command(help="Limpa o cache de proxies.")
def clear(
    age: Optional[str] = typer.Argument(
        None,
        help="Limpa proxies mais antigas que o tempo especificado. Exemplos: '5H' (5 horas), '1D' (1 dia), '2S' (2 semanas), '1S,3D,5H'. Se omitido, limpa todo o cache.",
        metavar="AGE",
    ),
):
    """
    Remove entradas do cache de proxies.
    """
    console.print(
        Panel(
            "[bold yellow]NyxProxy[/] - Limpando Cache",
            expand=False,
            border_style="yellow",
        )
    )

    try:
        # Instancia o gerenciador sem fontes, apenas para acessar o cache.
        proxy_manager = Proxy(use_console=True)
        proxy_manager.clear_cache(age)

    except Exception as e:
        console.print(f"[bold red]Ocorreu um erro inesperado durante a limpeza do cache: {e}[/]")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()