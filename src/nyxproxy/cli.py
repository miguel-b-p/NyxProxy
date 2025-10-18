#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entry point for the NyxProxy Command Line Interface (CLI)."""

import json
import asyncio
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel

from .core import NyxProxyError
from .core.config import DEFAULT_RICH_THEME
from .manager import Proxy

app = typer.Typer(
    name="nyxproxy",
    help="A tool to test and create HTTP bridges for V2Ray/Xray proxies.",
    add_completion=False,
    rich_markup_mode="markdown",
)

console = Console(theme=DEFAULT_RICH_THEME)


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
                "[accent]NyxProxy[/] - Testing proxies",
                expand=False,
                border_style="accent",
            )
        )

    async def main():
        try:
            proxy_manager = Proxy(
                max_count=limit,
                use_console=not output_json,
                country=country,
            )
            await proxy_manager.load_resources(sources=sources)

            if not proxy_manager.entries and not proxy_manager.parse_errors:
                console.print("[warning]No valid proxies found in the sources.[/warning]")
                raise typer.Exit()

            results = await proxy_manager.test(
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

        except typer.Exit:
            raise
        except NyxProxyError as e:
            console.print(f"[danger]Error: {e}[/danger]")
            raise typer.Exit(code=1)
        except Exception as e:
            import traceback
            console.print(f"[danger]An unexpected error occurred: {e}[/danger]")
            console.print(f"[dim]{traceback.format_exc()}[/dim]")
            raise typer.Exit(code=1)

    asyncio.run(main())


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
            "[accent]NyxProxy[/] - Starting HTTP bridges",
            expand=False,
            border_style="success",
        )
    )

    async def main():
        try:
            proxy_manager = Proxy(
                max_count=limit,
                use_console=True,
                country=country,
            )
            await proxy_manager.load_resources(sources=sources)
            await proxy_manager.start(
                threads=threads,
                amounts=amounts,
                country=country,
                find_first=amounts,
                skip_geo=no_geo,
            )
            await proxy_manager.wait()

        except typer.Exit:
            raise
        except NyxProxyError as e:
            console.print(f"[danger]Error on startup: {e}[/danger]")
            raise typer.Exit(code=1)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            import traceback
            console.print(f"[danger]An unexpected error occurred: {e}[/danger]")
            console.print(f"[dim]{traceback.format_exc()}[/dim]")
            raise typer.Exit(code=1)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Absorb Ctrl+C at the top level

    console.print("\n[success]All bridges have been terminated. Goodbye![/success]")


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
        console.print("[danger]Error: Specify a command to execute.[/danger]")
        raise typer.Exit(code=1)

    console.print(
        Panel(
            "[accent]NyxProxy[/] - Running via proxychains",
            expand=False,
            border_style="accent.secondary",
        )
    )

    async def main():
        try:
            proxy_manager = Proxy(
                max_count=limit,
                use_console=True,
                country=country,
            )
            await proxy_manager.load_resources(sources=sources)
            exit_code = await proxy_manager.run_with_chains(
                cmd_list=command_to_run,
                threads=threads,
                amounts=amounts,
                country=country,
            )
            raise typer.Exit(code=exit_code)

        except typer.Exit:
            raise
        except NyxProxyError as e:
            console.print(f"[danger]Error: {e}[/danger]")
            raise typer.Exit(code=1)
        except Exception as e:
            import traceback
            console.print(f"[danger]An unexpected error occurred: {e}[/danger]")
            console.print(f"[dim]{traceback.format_exc()}[/dim]")
            raise typer.Exit(code=1)

    asyncio.run(main())


@app.command(help="Clears the proxy cache.")
def clear(
    age: Optional[str] = typer.Argument(
        None,
        help="Clears proxies older than the specified time. Ex: '5H', '1D', '2W,12H'. If omitted, clears the entire cache.",
        metavar="AGE",
    ),
):
    """Removes entries from the proxy test cache."""
    console.print(
        Panel(
            "[accent]NyxProxy[/] - Clearing cache",
            expand=False,
            border_style="warning",
        )
    )

    async def main():
        try:
            proxy_manager = Proxy(use_console=True, use_cache=True)
            await proxy_manager.load_resources()
            await proxy_manager.clear_cache(age)
        except Exception as e:
            console.print(f"[danger]Error clearing cache: {e}[/danger]")
            raise typer.Exit(code=1)

    asyncio.run(main())


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

    async def main():
        try:
            proxy_manager = Proxy(
                use_console=not output_json,
                country=country,
            )
            await proxy_manager.load_resources()

            if proxy_manager.use_cache and proxy_manager._cache_available:
                proxy_manager._load_outbounds_from_cache()
                proxy_manager._prime_entries_from_cache()
            else:
                console.print("[warning]No cache available. Run 'test' first.[/warning]")
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
                            "[accent]Functional proxies in cache[/]",
                            expand=False,
                            border_style="accent",
                        )
                    )
                    console.print(proxy_manager._render_test_table(approved))
                else:
                    console.print("[warning]No functional proxies in cache.[/warning]")

        except Exception as e:
            console.print(f"[danger]Error: {e}[/danger]")
            raise typer.Exit(code=1)

    asyncio.run(main())


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
            "[accent]NyxProxy[/] - Exporting working proxies",
            expand=False,
            border_style="success",
        )
    )

    async def main():
        try:
            proxy_manager = Proxy(
                use_console=True,
                country=country,
            )
            await proxy_manager.load_resources(sources=sources)
            await proxy_manager.test(
                threads=threads,
                force=force,
                verbose=True,
                skip_geo=no_geo,
            )
            good_uris = [e.uri for e in proxy_manager.entries if e.status == "OK"]
            Path(output).write_text("\n".join(good_uris) + "\n")
            console.print(f"[success]Exported {len(good_uris)} functional proxies to '{output}'.[/success]")

        except typer.Exit:
            raise
        except NyxProxyError as e:
            console.print(f"[danger]Error: {e}[/danger]")
            raise typer.Exit(code=1)
        except Exception as e:
            import traceback
            console.print(f"[danger]An unexpected error occurred: {e}[/danger]")
            console.print(f"[dim]{traceback.format_exc()}[/dim]")
            raise typer.Exit(code=1)

    asyncio.run(main())


if __name__ == "__main__":
    app()
