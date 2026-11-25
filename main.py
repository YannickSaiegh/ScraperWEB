"""
Ponto de entrada CLI do Full-Site Web Scraper & Deep X-Ray Extractor.
Fornece interface de linha de comandos com Typer e monitorização em tempo real com Rich + Loguru.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from crawler import CrawlerConfig, CrawlStats, DEFAULT_OUTPUT_DIR, FullSiteCrawler
from extractor import MarkdownEngine
from utils import console, normalize_url, setup_logging

app = typer.Typer(
    name="full-site-scraper",
    help="Crawler recursivo assíncrono (Playwright) com Raio-X completo de conteúdo visível e oculto.",
    add_completion=False,
    rich_markup_mode="rich",
)


def _ensure_http_scheme(raw_url: str) -> str:
    cleaned = raw_url.strip()
    if not cleaned.startswith(("http://", "https://")):
        return f"https://{cleaned}"
    return cleaned


def _render_summary_table(summary: dict[str, object], deep_xray: bool) -> None:
    table = Table(
        title="Resumo da Extração & Raio-X",
        show_header=True,
        header_style="bold cyan",
        border_style="bright_black",
    )
    table.add_column("Métrica / Artefacto", style="bold")
    table.add_column("Valor", style="green")

    out_dir = Path(str(summary["output_directory"]))
    table.add_row("Domínio Alvo", str(summary["target_domain"]))
    table.add_row("URL Inicial", str(summary["start_url"]))
    table.add_row("Duração Total", f"{summary['duration_seconds']} s")
    table.add_row("Velocidade Média", f"{summary['pages_per_minute']} págs/min")
    table.add_row("URLs Descobertas (Links + Sitemap)", str(summary["total_discovered_urls"]))
    table.add_row("Páginas Extraídas (HTTP 2xx/3xx)", str(summary["pages_extracted_ok"]))
    table.add_row("Páginas com Erro HTTP (4xx/5xx)", str(summary["pages_http_error"]))
    table.add_row("Falhas de Rede / Timeout", str(summary["pages_failed"]))
    table.add_row("Ignoradas por robots.txt", str(summary["pages_skipped_robots"]))
    table.add_row("Itens Ocultos Extraídos (DOM/CSS)", str(summary["hidden_dom_items_extracted"]))
    table.add_row("APIs XHR / Fetch Intercetadas", str(summary["xhr_fetch_apis_intercepted"]))
    table.add_row("Assets Descarregados", str(summary["assets_downloaded"]))
    table.add_section()
    table.add_row("Pasta da Extração", str(out_dir))
    table.add_row("Dataset Completo (JSONL)", str(out_dir / "dataset.jsonl"))
    if deep_xray:
        table.add_row("Relatório Raio-X (Ocultos & APIs)", str(out_dir / "xray_hidden_report.json"))
    table.add_row("Páginas Markdown (+ Raio-X)", str(out_dir / "pages"))
    table.add_row("Assets Locais", str(out_dir / "assets"))
    table.add_row("Mapa do Site (Sitemap)", str(out_dir / "sitemap_extracted.json"))

    console.print()
    console.print(table)


@app.command()
def crawl(
    url: Annotated[
        str,
        typer.Argument(
            help="URL inicial do domínio alvo (ex: https://exemplo.pt).",
        ),
    ],
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir",
            "-o",
            help="Diretório base de saída (por predefinição `output/` dentro da pasta do projeto).",
        ),
    ] = DEFAULT_OUTPUT_DIR,
    isolate_run_folder: Annotated[
        bool,
        typer.Option(
            "--run-subfolder/--overwrite-domain-folder",
            help="Cria sempre uma nova subpasta exclusiva por execução dentro de `output/<dominio>/`.",
        ),
    ] = True,
    concurrency: Annotated[
        int,
        typer.Option(
            "--concurrency",
            "-c",
            min=1,
            max=20,
            help="Número máximo de abas simultâneas no Playwright (Semaphore).",
        ),
    ] = 4,
    min_delay: Annotated[
        float,
        typer.Option(
            "--min-delay",
            min=0.0,
            help="Delay mínimo aleatório (segundos) entre pedidos.",
        ),
    ] = 0.8,
    max_delay: Annotated[
        float,
        typer.Option(
            "--max-delay",
            min=0.0,
            help="Delay máximo aleatório (segundos) entre pedidos.",
        ),
    ] = 2.2,
    max_pages: Annotated[
        int,
        typer.Option(
            "--max-pages",
            "-m",
            min=0,
            help="Limite máximo de páginas a extrair (0 = todas as páginas do domínio).",
        ),
    ] = 0,
    max_depth: Annotated[
        int,
        typer.Option(
            "--max-depth",
            "-d",
            min=-1,
            help="Profundidade máxima de links a seguir (-1 = ilimitada, 0 = apenas URL inicial).",
        ),
    ] = -1,
    deep_xray: Annotated[
        bool,
        typer.Option(
            "--deep-xray/--no-deep-xray",
            help="Extrai TUDO o que está oculto: DOM/CSS escondido, comentários HTML, forms hidden, estados SPA JSON e APIs XHR/Fetch.",
        ),
    ] = True,
    discover_sitemap: Annotated[
        bool,
        typer.Option(
            "--discover-sitemap/--no-discover-sitemap",
            help="Descobre páginas órfãs/não listadas nos menus lendo `sitemap.xml` / `sitemap_index.xml`.",
        ),
    ] = True,
    append_xray_md: Annotated[
        bool,
        typer.Option(
            "--append-xray-md/--clean-md-only",
            help="Inclui no final de cada `.md` em `pages/` uma secção Raio-X com os dados ocultos e APIs da página.",
        ),
    ] = True,
    download_assets: Annotated[
        bool,
        typer.Option(
            "--download-assets/--no-download-assets",
            help="Descarrega localmente imagens (incluindo CSS backgrounds), vídeos, áudios e documentos em `assets/`.",
        ),
    ] = False,
    respect_robots: Annotated[
        bool,
        typer.Option(
            "--respect-robots/--ignore-robots",
            help="Respeita ou ignora as diretivas definidas no `robots.txt` do servidor.",
        ),
    ] = True,
    include_subdomains: Annotated[
        bool,
        typer.Option(
            "--include-subdomains/--same-host-only",
            help="Inclui também subdomínios (`*.dominio.pt`) durante o rastreio recursivo.",
        ),
    ] = False,
    include_raw_html: Annotated[
        bool,
        typer.Option(
            "--raw-html/--no-raw-html",
            help="Inclui o HTML bruto renderizado em cada registo de `dataset.jsonl`.",
        ),
    ] = True,
    markdown_engine: Annotated[
        MarkdownEngine,
        typer.Option(
            "--markdown-engine",
            help="Motor de limpeza e conversão para Markdown: `hybrid`, `trafilatura` ou `markdownify`.",
        ),
    ] = "hybrid",
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            min=5.0,
            help="Timeout de navegação por página em segundos.",
        ),
    ] = 30.0,
    max_retries: Annotated[
        int,
        typer.Option(
            "--max-retries",
            min=1,
            max=10,
            help="Número máximo de tentativas por URL com exponential backoff.",
        ),
    ] = 3,
    max_scroll_steps: Annotated[
        int,
        typer.Option(
            "--max-scrolls",
            min=0,
            max=100,
            help="Passos máximos de auto-scroll suave até ao fundo da página.",
        ),
    ] = 15,
    headless: Annotated[
        bool,
        typer.Option(
            "--headless/--headed",
            help="Executa o navegador Chromium em modo invisível (`--headless`) ou visível (`--headed`).",
        ),
    ] = True,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Ativa logs detalhados de nível DEBUG no terminal.",
        ),
    ] = False,
) -> None:
    """Inicia o rastreio recursivo e a extração completa (visível + oculta) do domínio alvo."""
    setup_logging(verbose=verbose)

    prepared_url = _ensure_http_scheme(url)
    normalized = normalize_url(prepared_url, strip_tracking=True)
    if not normalized:
        console.print(f"[bold red]Erro:[/bold red] A URL fornecida não é válida: {url!r}")
        raise typer.Exit(code=1)

    if max_delay < min_delay:
        max_delay = min_delay

    config = CrawlerConfig(
        start_url=normalized,
        output_dir=output_dir.resolve(),
        isolate_run_folder=isolate_run_folder,
        concurrency=concurrency,
        min_delay=min_delay,
        max_delay=max_delay,
        max_pages=max_pages,
        max_depth=max_depth,
        timeout_seconds=timeout,
        max_retries=max_retries,
        respect_robots=respect_robots,
        discover_sitemap=discover_sitemap,
        deep_xray=deep_xray,
        append_xray_to_markdown=append_xray_md,
        download_assets=download_assets,
        include_raw_html=include_raw_html,
        include_subdomains=include_subdomains,
        headless=headless,
        markdown_engine=markdown_engine,
        max_scroll_steps=max_scroll_steps,
    )

    console.print(
        Panel.fit(
            f"[bold cyan]Full-Site Crawler & Deep X-Ray Extractor[/bold cyan]\n"
            f"[white]Alvo:[/white] [underline]{normalized}[/underline]\n"
            f"[white]Concorrência:[/white] {concurrency} abas | "
            f"[white]Raio-X Ocultos/APIs:[/white] {'Ativo' if deep_xray else 'Inativo'} | "
            f"[white]Sitemap Órfãs:[/white] {'Ativo' if discover_sitemap else 'Inativo'} | "
            f"[white]robots.txt:[/white] {'Respeitar' if respect_robots else 'Ignorar'} | "
            f"[white]Assets:[/white] {'Ativo' if download_assets else 'Inativo'}",
            border_style="cyan",
        )
    )

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(bar_width=24),
        TextColumn("[green]OK:{task.fields[ok]}[/green]"),
        TextColumn("[yellow]Fila:{task.fields[queued]}[/yellow]"),
        TextColumn("[bright_magenta]Ocultos:{task.fields[hidden]}[/bright_magenta]"),
        TextColumn("[bright_cyan]APIs:{task.fields[apis]}[/bright_cyan]"),
        TextColumn("[red]Falhas:{task.fields[errors]}[/red]"),
        TextColumn("[bold white]{task.fields[speed]}[/bold white]"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )

    task_id = progress.add_task(
        "A extrair...",
        total=max_pages if max_pages > 0 else None,
        ok=0,
        queued=0,
        hidden=0,
        apis=0,
        errors=0,
        speed="0.0 págs/min",
    )

    def _update_progress(stats: CrawlStats) -> None:
        total_target = max_pages if max_pages > 0 else max(stats.discovered, 1)
        completed_count = min(stats.processed_total, total_target)
        progress.update(
            task_id,
            total=total_target,
            completed=completed_count,
            ok=stats.pages_ok,
            queued=stats.queued,
            hidden=stats.hidden_items_found,
            apis=stats.apis_intercepted,
            errors=stats.pages_failed + stats.pages_http_error,
            speed=f"{stats.pages_per_minute:.1f} págs/min",
        )

    crawler = FullSiteCrawler(config=config, on_progress=_update_progress)

    try:
        with progress:
            summary = asyncio.run(crawler.run())
        _render_summary_table(summary, deep_xray=deep_xray)
    except KeyboardInterrupt:
        logger.warning("Execução interrompida pelo utilizador (Ctrl+C). Os dados já recolhidos foram preservados.")
        raise typer.Exit(code=130)


if __name__ == "__main__":
    app()
