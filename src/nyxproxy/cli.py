#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry point for the NyxProxy Command Line Interface (CLI)."""

import json
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel

from core import NyxProxyError
from manager import Proxy

app = typer.Typer(
    name="nyxproxy",
    help="A tool to test and create HTTP bridges for V2Ray/Xray proxies.",
    add_completion=False,
    rich_markup_mode="markdown",
)

console = Console()


@app.callback()
def callback():
    """
    **NyxProxy**: A powerful and flexible proxy manager.
    """
    pass


@app.command(help="Tests proxies from sources and displays a status report.")
def test(
    sources: List[str] = typer.Argument(
        ...,
        help="One or more proxy sources (local file or URL).",
        metavar="SOURCES...",
    ),
    country: Optional[str] = typer.Option(
        None,
        "--country",
        "-c",
        help="Filter proxies by country (e.g., 'BR', 'US', 'Germany').",
    ),
    threads: int = typer.Option(
        20,
        "--threads",
        "-t",
        min=1,
        help="Number of workers for parallel testing.",
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        "-l",
        help="Limit the maximum number of proxies to load (0 = no limit).",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Ignore cache and re-test all proxies.",
    ),
    no_geo: bool = typer.Option(
        False,
        "--no-geo",
        help="Skip geolocation lookups for faster testing.",
    ),
    output_json: bool = typer.Option(
        False,
        "--output-json",
        "-j",
        help="Display test results in JSON format.",
    ),
    find_first: Optional[int] = typer.Option(
        None,
        "--find-first",
        "-ff",
        help="Stop the test after finding the specified number of functional proxies.",
    ),
):
    """Executes the proxy test from the provided sources."""
    if not output_json:
        console.print(
            Panel(
                "[bold cyan]NyxProxy[/] - Testing Servers",
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
            console.print("[yellow]Warning: No valid proxies found in the sources.[/yellow]")
            raise typer.Exit()

        results = proxy_manager.test(
            threads=threads,
            country=country,
            force=force,
            verbose=not output_json,
            find_first=find_first,
            skip_geo=no_geo,
        )

        if output_json:
            # Convert dataclasses to dicts for JSON serialization
            json_results = [res.__dict__ for res in results]
            print(json.dumps(json_results, indent=2, ensure_ascii=False, default=str))

    except NyxProxyError as e:
        console.print(f"[bold red]Error: {e}[/bold red]")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[bold red]An unexpected error occurred: {e}[/bold red]")
        raise typer.Exit(code=1)


@app.command(help="Starts local HTTP bridges with the approved proxies.")
def start(
    sources: Optional[List[str]] = typer.Argument(
        None,
        help="Proxy sources (file/URL). If omitted, uses proxies from cache.",
        metavar="SOURCES...",
    ),
    country: Optional[str] = typer.Option(
        None,
        "--country",
        "-c",
        help="Start bridges only for proxies from a specific country.",
    ),
    threads: int = typer.Option(
        20,
        "--threads",
        "-t",
        min=1,
        help="Number of workers for pre-start tests.",
    ),
    limit: int = typer.Option(
        0,
        "--limit",
        "-l",
        help="Limit the number of proxies to load.",
    ),
    no_geo: bool = typer.Option(
        False,
        "--no-geo",
        help="Skip geolocation lookups for faster testing.",
    ),
    amounts: int = typer.Option(
        5,
        "--amounts",
        "-a",
        help="Number of HTTP bridges to start with the best proxies.",
    ),
):
    """Starts HTTP bridges that remain active until the program is interrupted."""
    console.print(
        Panel(
            "[bold green]NyxProxy[/] - Starting HTTP Bridges",
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
            wait=True,
            find_first=amounts,
            skip_geo=no_geo,
        )

    except NyxProxyError as e:
        console.print(f"[bold red]Error on startup: {e}[/bold red]")
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        console.print(f"[bold red]An unexpected error occurred: {e}[/bold red]")
        raise typer.Exit(code=1)

    console.print("\n[bold green]All bridges have been terminated. Goodbye![/bold green]")


@app.command(
    help="Executes a command through proxy bridges with proxychains.",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def chains(
    ctx: typer.Context,
    sources: Optional[List[str]] = typer.Option(
        None,
        "--source",
        "-s",
        help="One or more proxy sources (file/URL). If omitted, uses cache.",
    ),
    country: Optional[str] = typer.Option(
        None, "--country", "-c", help="Use only proxies from a specific country."
    ),
    threads: int = typer.Option(
        20, "--threads", "-t", min=1, help="Threads for pre-execution tests."
    ),
    amounts: int = typer.Option(5, "--amounts", "-a", help="Number of bridges to use."),
    limit: int = typer.Option(0, "--limit", "-l", help="Limit loaded proxies."),
):
    """
    Starts bridges and executes a command through them using proxychains.

    The command and its arguments must be passed after all nyxproxy options.
    Example: `nyxproxy chains -a 3 -s my_proxies.txt -- curl -s ipinfo.io`
    """
    command_to_run = ctx.args
    if not command_to_run:
        console.print("[bold red]Error:[/bold red] Specify a command to execute.")
        raise typer.Exit(code=1)

    console.print(
        Panel(
            "[bold magenta]NyxProxy[/] - Executing via ProxyChains",
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

    except NyxProxyError as e:
        console.print(f"[bold red]Error: {e}[/bold red]")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[bold red]An unexpected error occurred: {e}[/bold red]")
        raise typer.Exit(code=1)


@app.command(help="Clears the proxy cache.")
def clear(
    age: Optional[str] = typer.Argument(
        None,
        help="Clears proxies older than the specified time. Ex: '5H', '1D', '2W,12H'. If omitted, clears the entire cache.",
        metavar="AGE",
    ),
):
    """Removes entries from the proxy test cache."""
    console.print(Panel("[bold yellow]NyxProxy[/] - Clearing Cache", expand=False, border_style="yellow"))
    try:
        proxy_manager = Proxy(use_console=True, use_cache=True)
        proxy_manager.clear_cache(age)
    except Exception as e:
        console.print(f"[bold red]Error clearing cache: {e}[/bold red]")
        raise typer.Exit(code=1)


@app.command(help="Lists functional proxies from the cache.")
def list_proxies(
    country: Optional[str] = typer.Option(
        None,
        "--country",
        "-c",
        help="Filter by country.",
    ),
    output_json: bool = typer.Option(
        False,
        "--output-json",
        "-j",
        help="Output in JSON format.",
    ),
):
    """Displays a table or JSON of functional proxies loaded from cache."""
    try:
        proxy_manager = Proxy(
            use_console=not output_json,
            country=country,
        )
        if proxy_manager.use_cache and proxy_manager._cache_available:
            proxy_manager._load_outbounds_from_cache()
            proxy_manager._prime_entries_from_cache()
        else:
            console.print("[yellow]No cache available. Run 'test' first.[/yellow]")
            raise typer.Exit()

        approved = [
            e for e in proxy_manager.entries
            if e.status == "OK" and proxy_manager.matches_country(e, country)
        ]

        if output_json:
            json_results = [res.__dict__ for res in approved]
            print(json.dumps(json_results, indent=2, ensure_ascii=False, default=str))
        else:
            if approved:
                console.print(
                    Panel(
                        "[bold cyan]Functional Proxies from Cache[/]",
                        expand=False,
                        border_style="cyan",
                    )
                )
                console.print(proxy_manager._render_test_table(approved))
            else:
                console.print("[yellow]No functional proxies in cache.[/yellow]")

    except Exception as e:
        console.print(f"[bold red]Error: {e}[/bold red]")
        raise typer.Exit(code=1)


@app.command(help="Exports functional proxy URIs to a file.")
def export(
    output: str = typer.Argument(
        ...,
        help="Output file to save functional proxy URIs.",
    ),
    sources: List[str] = typer.Argument(
        None,
        help="Proxy sources (file/URL). If omitted, uses cache.",
        metavar="SOURCES...",
    ),
    country: Optional[str] = typer.Option(
        None,
        "--country",
        "-c",
        help="Filter by country.",
    ),
    threads: int = typer.Option(
        20,
        "--threads",
        "-t",
        min=1,
        help="Threads for testing.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Force re-test.",
    ),
    no_geo: bool = typer.Option(
        False,
        "--no-geo",
        help="Skip geolocation for faster export.",
    ),
):
    """Tests proxies (or uses cache) and exports functional URIs to a file."""
    console.print(
        Panel(
            "[bold green]NyxProxy[/] - Exporting Functional Proxies",
            expand=False,
            border_style="green",
        )
    )
    try:
        proxy_manager = Proxy(
            sources=sources,
            use_console=True,
            country=country,
        )
        proxy_manager.test(
            threads=threads,
            force=force,
            verbose=True,
            skip_geo=no_geo,
        )
        good_uris = [e.uri for e in proxy_manager.entries if e.status == "OK"]
        Path(output).write_text("\n".join(good_uris) + "\n")
        console.print(f"[green]Exported {len(good_uris)} functional proxies to '{output}'.[/green]")

    except NyxProxyError as e:
        console.print(f"[bold red]Error: {e}[/bold red]")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[bold red]An unexpected error occurred: {e}[/bold red]")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()