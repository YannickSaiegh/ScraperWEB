"""
Orquestrador Assíncrono do Web Crawler & Scraper (`FullSiteCrawler`) com Inspeção Profunda ("Raio-X"):
- Renderização JavaScript completa com `async_playwright` (Chromium) para SPAs (React, Next.js, Vue)
- Interceção passiva de pedidos de rede `XHR` / `Fetch` / `GraphQL` disparados pela página
- Inspeção ao vivo no browser (`getComputedStyle`) para textos escondidos por CSS externo,
  imagens em `background-image` (incluindo `::before`/`::after`), expansão de `<details>` e globais de SPAs
- Descoberta automática de páginas órfãs através de `sitemap.xml` / `sitemap_index.xml` (incluindo `.gz`)
- Concorrência controlada com `asyncio.Semaphore`, rate limiting aleatório, rotação de `User-Agent` e `robots.txt`
"""

from __future__ import annotations

import asyncio
import gzip
import json
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from loguru import logger
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Response,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from extractor import MarkdownEngine, NetworkAPIRecord, PageExtractor
from storage import SitemapURLRecord, StorageManager
from utils import (
    extract_domain,
    get_random_user_agent,
    get_random_viewport,
    is_crawlable_page_url,
    is_same_domain,
    normalize_url,
)

PROJECT_ROOT: Path = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR: Path = PROJECT_ROOT / "output"
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class CrawlerConfig:
    start_url: str
    output_dir: Path = field(default_factory=lambda: DEFAULT_OUTPUT_DIR)
    isolate_run_folder: bool = True
    concurrency: int = 4
    min_delay: float = 0.8
    max_delay: float = 2.2
    max_pages: int = 0  # 0 = sem limite
    max_depth: int = -1  # -1 = sem limite
    timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_base: float = 1.5
    respect_robots: bool = True
    discover_sitemap: bool = True
    deep_xray: bool = True
    append_xray_to_markdown: bool = True
    download_assets: bool = False
    include_raw_html: bool = True
    include_subdomains: bool = False
    headless: bool = True
    markdown_engine: MarkdownEngine = "hybrid"
    max_scroll_steps: int = 15
    scroll_pause_ms: int = 300


@dataclass(slots=True)
class CrawlStats:
    discovered: int = 0
    queued: int = 0
    in_progress: int = 0
    pages_ok: int = 0
    pages_http_error: int = 0
    pages_failed: int = 0
    skipped_robots: int = 0
    assets_downloaded: int = 0
    hidden_items_found: int = 0
    apis_intercepted: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)

    @property
    def processed_total(self) -> int:
        return self.pages_ok + self.pages_http_error + self.pages_failed + self.skipped_robots

    @property
    def elapsed_seconds(self) -> float:
        return max(0.001, time.monotonic() - self.started_monotonic)

    @property
    def pages_per_minute(self) -> float:
        completed = self.pages_ok + self.pages_http_error
        return (completed / self.elapsed_seconds) * 60.0


class RobotsTxtGuard:
    """Carrega e avalia regras `robots.txt` e extrai URLs de `Sitemap:` declaradas."""

    def __init__(self, start_url: str, enabled: bool = True) -> None:
        self.enabled = enabled
        parts = urlsplit(start_url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.robots_url = f"{self.origin}/robots.txt"
        self._parser = RobotFileParser()
        self._loaded = False
        self.crawl_delay: float | None = None
        self.declared_sitemaps: list[str] = []

    async def load(self) -> None:
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                resp = await client.get(
                    self.robots_url,
                    headers={"User-Agent": get_random_user_agent()},
                )
                if resp.status_code == 200 and resp.text:
                    lines = resp.text.splitlines()
                    # Extrai sempre diretivas Sitemap: públicas mesmo que --ignore-robots esteja ativo
                    for line in lines:
                        stripped = line.strip()
                        if stripped.lower().startswith("sitemap:"):
                            sm_url = stripped.split(":", 1)[1].strip()
                            if sm_url and sm_url not in self.declared_sitemaps:
                                self.declared_sitemaps.append(sm_url)

                    if self.enabled:
                        self._parser.parse(lines)
                        self._loaded = True
                        delay = self._parser.crawl_delay("*")
                        if delay is not None:
                            try:
                                self.crawl_delay = float(delay)
                            except (TypeError, ValueError):
                                self.crawl_delay = None
                        logger.info(
                            f"robots.txt carregado ({self.robots_url})"
                            + (f" | Crawl-delay={self.crawl_delay}s" if self.crawl_delay else "")
                            + (f" | {len(self.declared_sitemaps)} Sitemap(s)" if self.declared_sitemaps else "")
                        )
                    else:
                        logger.warning(
                            f"Regras de bloqueio robots.txt ignoradas (--ignore-robots); "
                            f"{len(self.declared_sitemaps)} Sitemap(s) detetado(s)."
                        )
                else:
                    if self.enabled:
                        logger.info(
                            f"robots.txt indisponível ou vazio (HTTP {resp.status_code}); rastreio permitido."
                        )
        except Exception as exc:
            logger.warning(f"Não foi possível obter {self.robots_url} ({exc}); a prosseguir.")

    def is_allowed(self, url: str, user_agent: str = "*") -> bool:
        if not self.enabled or not self._loaded:
            return True
        try:
            return self._parser.can_fetch(user_agent, url) and self._parser.can_fetch("*", url)
        except Exception:
            return True


class SitemapXMLDiscoverer:
    """Descobre páginas públicas não ligadas nos menus ("páginas órfãs") via `sitemap.xml`."""

    def __init__(
        self,
        *,
        origin: str,
        target_domain: str,
        include_subdomains: bool = False,
        max_sitemaps: int = 12,
    ) -> None:
        self.origin = origin.rstrip("/")
        self.target_domain = target_domain
        self.include_subdomains = include_subdomains
        self.max_sitemaps = max_sitemaps

    async def discover_urls(self, declared_sitemaps: list[str]) -> list[str]:
        candidates = list(declared_sitemaps)
        default_sm = f"{self.origin}/sitemap.xml"
        if default_sm not in candidates:
            candidates.append(default_sm)

        visited_sitemaps: set[str] = set()
        discovered_pages: list[str] = []
        seen_pages: set[str] = set()

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=15.0,
            headers={"User-Agent": get_random_user_agent()},
        ) as client:
            queue = list(candidates)
            while queue and len(visited_sitemaps) < self.max_sitemaps:
                sm_url = queue.pop(0)
                if sm_url in visited_sitemaps:
                    continue
                visited_sitemaps.add(sm_url)

                try:
                    resp = await client.get(sm_url)
                    if resp.status_code != 200 or not resp.content:
                        continue

                    raw_bytes = resp.content
                    if sm_url.lower().endswith(".gz") or raw_bytes[:2] == b"\x1f\x8b":
                        try:
                            raw_bytes = gzip.decompress(raw_bytes)
                        except OSError:
                            continue

                    xml_text = raw_bytes.decode("utf-8", errors="replace").lstrip()
                    if not xml_text.startswith("<"):
                        continue

                    child_sitemaps, page_locs = self._parse_sitemap_xml(xml_text)
                    for child in child_sitemaps:
                        if child not in visited_sitemaps:
                            queue.append(child)

                    for loc in page_locs:
                        norm = normalize_url(loc, strip_tracking=True)
                        if (
                            norm
                            and norm not in seen_pages
                            and is_same_domain(norm, self.target_domain, include_subdomains=self.include_subdomains)
                            and is_crawlable_page_url(norm)
                        ):
                            seen_pages.add(norm)
                            discovered_pages.append(norm)
                except Exception as exc:
                    logger.debug(f"Sitemap ignorado ({sm_url}): {exc}")

        if discovered_pages:
            logger.info(
                f"[SITEMAP] Descobertas {len(discovered_pages)} URLs públicas em {len(visited_sitemaps)} ficheiro(s) sitemap."
            )
        return discovered_pages

    @staticmethod
    def _parse_sitemap_xml(xml_text: str) -> tuple[list[str], list[str]]:
        child_sitemaps: list[str] = []
        page_locs: list[str] = []
        try:
            # Remove namespaces XML para simplificar a pesquisa de tags <sitemap><loc> e <url><loc>
            cleaned_xml = re.sub(r'\sxmlns(?::\w+)?="[^"]+"', "", xml_text, count=5)
            root = ET.fromstring(cleaned_xml)
            root_tag = root.tag.split("}")[-1].lower()

            if root_tag == "sitemapindex":
                for sm in root.findall(".//sitemap/loc"):
                    if sm.text and sm.text.strip():
                        child_sitemaps.append(sm.text.strip())
            else:
                for loc in root.findall(".//url/loc"):
                    if loc.text and loc.text.strip():
                        page_locs.append(loc.text.strip())
        except ET.ParseError:
            pass
        return child_sitemaps, page_locs


class FullSiteCrawler:
    """Crawler recursivo assíncrono Same-Domain com extração completa visível + oculta."""

    def __init__(
        self,
        config: CrawlerConfig,
        on_progress: Callable[[CrawlStats], None] | None = None,
    ) -> None:
        normalized_start = normalize_url(config.start_url, strip_tracking=True)
        if not normalized_start:
            raise ValueError(f"URL inicial inválida: {config.start_url!r}")

        self.config = config
        self.start_url = normalized_start
        self.target_domain = extract_domain(normalized_start, strip_www=True)
        self.on_progress = on_progress
        self.stats = CrawlStats()

        self._semaphore = asyncio.Semaphore(max(1, config.concurrency))
        self._state_lock = asyncio.Lock()
        self._queue: asyncio.Queue[tuple[str, int, str | None]] = asyncio.Queue()

        self._seen_urls: set[str] = set()
        self._visited_urls: set[str] = set()
        self._claimed_slots: int = 0
        self._sitemap_records: dict[str, SitemapURLRecord] = {}

        self.robots = RobotsTxtGuard(self.start_url, enabled=config.respect_robots)
        self.extractor = PageExtractor(
            target_domain=self.target_domain,
            include_subdomains=config.include_subdomains,
            markdown_engine=config.markdown_engine,
            deep_xray=config.deep_xray,
            append_xray_to_markdown=config.append_xray_to_markdown,
        )
        self.storage = StorageManager(
            base_output_dir=config.output_dir,
            target_domain=self.target_domain,
            isolate_run_folder=config.isolate_run_folder,
            download_assets=config.download_assets,
            include_raw_html=config.include_raw_html,
            asset_concurrency=max(4, config.concurrency * 2),
            max_retries=config.max_retries,
            on_asset_downloaded=self._handle_asset_downloaded,
        )

    def _handle_asset_downloaded(self) -> None:
        self.stats.assets_downloaded = self.storage.downloaded_assets_count
        self._notify_progress()

    def _notify_progress(self) -> None:
        self.stats.queued = self._queue.qsize()
        if self.on_progress:
            self.on_progress(self.stats)

    async def run(self) -> dict[str, Any]:
        """Executa o ciclo completo de descoberta, renderização, inspeção profunda e gravação."""
        started_iso = datetime.now(timezone.utc).isoformat()
        self.stats = CrawlStats(started_monotonic=time.monotonic())

        await self.storage.initialize(reset_dataset=True)
        await self.robots.load()

        # Enfileira a URL inicial primeiro (prioridade máxima, depth=0)
        await self._enqueue_url(self.start_url, depth=0, discovered_from=None)

        # Descobre páginas órfãs/ocultas listadas nos ficheiros sitemap.xml públicos
        if self.config.discover_sitemap and self.config.max_depth != 0:
            discoverer = SitemapXMLDiscoverer(
                origin=self.robots.origin,
                target_domain=self.target_domain,
                include_subdomains=self.config.include_subdomains,
            )
            sitemap_urls = await discoverer.discover_urls(self.robots.declared_sitemaps)
            for sm_page_url in sitemap_urls:
                await self._enqueue_url(sm_page_url, depth=1, discovered_from="sitemap.xml")

        logger.info(
            f"A iniciar crawl em {self.start_url} | domínio={self.target_domain} | "
            f"concorrência={self.config.concurrency} | Raio-X={'ON' if self.config.deep_xray else 'OFF'} | "
            f"assets={'ON' if self.config.download_assets else 'OFF'}"
        )

        workers: list[asyncio.Task[None]] = []
        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(
                    headless=self.config.headless,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-default-browser-check",
                        "--disable-dev-shm-usage",
                    ],
                )
                try:
                    worker_count = max(1, self.config.concurrency)
                    workers = [
                        asyncio.create_task(self._worker_loop(worker_id=i + 1, browser=browser))
                        for i in range(worker_count)
                    ]
                    await self._queue.join()
                finally:
                    for w in workers:
                        w.cancel()
                    if workers:
                        await asyncio.gather(*workers, return_exceptions=True)
                    await browser.close()
        finally:
            finished_iso = datetime.now(timezone.utc).isoformat()
            summary = {
                "target_domain": self.target_domain,
                "start_url": self.start_url,
                "started_at": started_iso,
                "finished_at": finished_iso,
                "duration_seconds": round(self.stats.elapsed_seconds, 2),
                "pages_per_minute": round(self.stats.pages_per_minute, 2),
                "total_discovered_urls": self.stats.discovered,
                "pages_extracted_ok": self.stats.pages_ok,
                "pages_http_error": self.stats.pages_http_error,
                "pages_failed": self.stats.pages_failed,
                "pages_skipped_robots": self.stats.skipped_robots,
                "assets_downloaded": self.storage.downloaded_assets_count,
                "hidden_dom_items_extracted": self.stats.hidden_items_found,
                "xhr_fetch_apis_intercepted": self.stats.apis_intercepted,
                "output_directory": str(self.storage.domain_dir.resolve()),
            }
            await self.storage.write_sitemap(summary=summary, records=self._sitemap_records)
            if self.config.deep_xray:
                await self.storage.write_xray_report(summary=summary)
            await self.storage.close()
            logger.success(
                f"Extração concluída! Dados guardados na pasta: {self.storage.domain_dir.resolve()}"
            )

        return summary

    async def _enqueue_url(
        self,
        url: str,
        *,
        depth: int,
        discovered_from: str | None,
    ) -> bool:
        if self.config.max_depth >= 0 and depth > self.config.max_depth:
            return False

        async with self._state_lock:
            if url in self._seen_urls:
                return False
            if self.config.max_pages > 0 and self._claimed_slots >= self.config.max_pages:
                return False

            self._seen_urls.add(url)
            self._sitemap_records[url] = SitemapURLRecord(
                url=url,
                state="queued",
                depth=depth,
                discovered_from=discovered_from,
            )
            self.stats.discovered = len(self._seen_urls)
            self._queue.put_nowait((url, depth, discovered_from))

        self._notify_progress()
        return True

    async def _claim_page_slot(self, url: str) -> bool:
        async with self._state_lock:
            if url in self._visited_urls:
                return False
            if self.config.max_pages > 0 and self._claimed_slots >= self.config.max_pages:
                return False
            self._visited_urls.add(url)
            self._claimed_slots += 1
            return True

    async def _release_page_slot(self) -> None:
        async with self._state_lock:
            if self._claimed_slots > 0:
                self._claimed_slots -= 1

    async def _worker_loop(self, worker_id: int, browser: Browser) -> None:
        while True:
            url, depth, parent_url = await self._queue.get()
            try:
                await self._process_url(browser=browser, url=url, depth=depth, parent_url=parent_url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[Worker-{worker_id}] Erro inesperado em {url}: {exc}")
            finally:
                self._queue.task_done()
                self._notify_progress()

    async def _process_url(
        self,
        *,
        browser: Browser,
        url: str,
        depth: int,
        parent_url: str | None,
    ) -> None:
        record = self._sitemap_records.get(url) or SitemapURLRecord(
            url=url,
            state="queued",
            depth=depth,
            discovered_from=parent_url,
        )
        self._sitemap_records[url] = record

        if not self.robots.is_allowed(url):
            record.state = "skipped_robots"
            self.stats.skipped_robots += 1
            logger.info(f"[ROBOTS] Ignorado por diretiva robots.txt: {url}")
            return

        if not await self._claim_page_slot(url):
            if record.state == "queued":
                record.state = "skipped_limit_or_duplicate"
            return

        async with self._semaphore:
            self.stats.in_progress += 1
            self._notify_progress()
            try:
                await self._apply_rate_limit()
                await self._fetch_and_extract_with_retries(browser=browser, url=url, depth=depth, record=record)
            finally:
                self.stats.in_progress = max(0, self.stats.in_progress - 1)
                self._notify_progress()

    async def _apply_rate_limit(self) -> None:
        low = max(0.0, self.config.min_delay)
        high = max(low, self.config.max_delay)
        delay = random.uniform(low, high)
        if self.robots.crawl_delay is not None:
            delay = max(delay, self.robots.crawl_delay)
        if delay > 0:
            await asyncio.sleep(delay)

    async def _fetch_and_extract_with_retries(
        self,
        *,
        browser: Browser,
        url: str,
        depth: int,
        record: SitemapURLRecord,
    ) -> None:
        timeout_ms = int(self.config.timeout_seconds * 1000)
        last_error: str | None = None

        for attempt in range(1, self.config.max_retries + 1):
            record.retries = attempt - 1
            user_agent = get_random_user_agent()
            viewport = get_random_viewport()

            context: BrowserContext | None = None
            api_tasks: list[asyncio.Task[None]] = []
            captured_apis: list[NetworkAPIRecord] = []
            seen_api_keys: set[str] = set()
            t0 = time.perf_counter()

            try:
                context = await browser.new_context(
                    user_agent=user_agent,
                    viewport=viewport,
                    locale="pt-PT",
                    extra_http_headers={
                        "Accept-Language": "pt-PT,pt;q=0.9,en-US;q=0.8,en;q=0.7",
                    },
                    ignore_https_errors=True,
                )
                await context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )

                page = await context.new_page()

                # Interceção passiva de pedidos XHR / Fetch / GraphQL disparados pela própria página
                if self.config.deep_xray:
                    def _on_response(resp: Response) -> None:
                        req = resp.request
                        if req.resource_type not in ("xhr", "fetch"):
                            return
                        dedup = f"{req.method}:{resp.url}"
                        if dedup in seen_api_keys or len(captured_apis) >= 50:
                            return
                        seen_api_keys.add(dedup)
                        api_tasks.append(
                            asyncio.create_task(self._record_xhr_response(resp, captured_apis))
                        )

                    page.on("response", _on_response)

                response: Response | None = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                )

                status_code = response.status if response else 200
                headers = response.headers if response else {}
                content_type = headers.get("content-type", "text/html")

                if status_code in RETRYABLE_STATUS_CODES and attempt < self.config.max_retries:
                    retry_after = self._parse_retry_after(headers.get("retry-after"))
                    backoff = retry_after or self._compute_backoff(attempt)
                    logger.warning(
                        f"[HTTP {status_code}] Tentativa {attempt}/{self.config.max_retries} em {url} "
                        f"— novo pedido em {backoff:.1f}s"
                    )
                    await context.close()
                    context = None
                    await asyncio.sleep(backoff)
                    continue

                final_norm = normalize_url(page.url, strip_tracking=True) or url
                if not is_same_domain(
                    final_norm,
                    self.target_domain,
                    include_subdomains=self.config.include_subdomains,
                ):
                    record.state = "redirected_external"
                    record.final_url = final_norm
                    record.http_status = status_code
                    record.content_type = content_type
                    await self._release_page_slot()
                    logger.debug(f"Redirecionado para domínio externo ({final_norm}); ignorado.")
                    return

                async with self._state_lock:
                    self._visited_urls.add(final_norm)

                mime_main = content_type.split(";")[0].strip().lower()
                if mime_main and mime_main not in ("text/html", "application/xhtml+xml"):
                    record.state = "non_html"
                    record.final_url = final_norm
                    record.http_status = status_code
                    record.content_type = content_type
                    await self._release_page_slot()
                    logger.debug(f"Conteúdo não-HTML ({mime_main}) ignorado no browser: {url}")
                    return

                # 1. Auto-scroll suave até ao fundo + settle de rede
                await self._interact_and_scroll(page, timeout_ms=timeout_ms)

                # 2. Inspeção Raio-X ao vivo no browser (CSS display:none, background-images, globais SPA e abrir <details>)
                browser_hidden_snapshots: list[dict[str, str]] = []
                browser_bg_images: list[str] = []
                browser_spa_globals: dict[str, Any] = {}
                if self.config.deep_xray:
                    browser_hidden_snapshots, browser_bg_images, browser_spa_globals = (
                        await self._inspect_hidden_browser_state(page)
                    )

                if api_tasks:
                    await asyncio.gather(*api_tasks, return_exceptions=True)

                raw_html = await page.content()
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                crawled_iso = datetime.now(timezone.utc).isoformat()

                await context.close()
                context = None

                extracted = self.extractor.extract(
                    requested_url=url,
                    final_url=final_norm,
                    raw_html=raw_html,
                    status_code=status_code,
                    content_type=content_type,
                    crawled_at=crawled_iso,
                    depth=depth,
                    response_time_ms=elapsed_ms,
                    response_headers=headers,
                    browser_hidden_snapshots=browser_hidden_snapshots,
                    browser_bg_images=browser_bg_images,
                    browser_spa_globals=browser_spa_globals,
                    network_apis=captured_apis,
                )

                md_relpath = await self.storage.persist_page(extracted)

                self.stats.hidden_items_found += len(extracted.xray.hidden_dom_items)
                self.stats.apis_intercepted += len(extracted.xray.network_apis)

                record.final_url = final_norm
                record.http_status = status_code
                record.content_type = content_type
                record.title = extracted.seo.title
                record.markdown_file = md_relpath
                record.internal_links_count = len(extracted.internal_links)
                record.external_links_count = len(extracted.external_links)
                record.images_count = len(extracted.images)
                record.documents_count = len(extracted.documents)
                record.hidden_items_count = len(extracted.xray.hidden_dom_items)
                record.network_apis_count = len(extracted.xray.network_apis)
                record.response_time_ms = elapsed_ms
                record.crawled_at = crawled_iso

                if status_code >= 400:
                    record.state = "http_error"
                    record.error = f"HTTP {status_code}"
                    self.stats.pages_http_error += 1
                    logger.warning(
                        f"[{status_code}] {url} ({elapsed_ms} ms) — página de erro guardada em {md_relpath}"
                    )
                else:
                    record.state = "ok"
                    self.stats.pages_ok += 1
                    logger.info(
                        f"[{status_code}] {url} -> {md_relpath} | "
                        f"{extracted.word_count} palavras | "
                        f"{len(extracted.xray.hidden_dom_items)} ocultos | "
                        f"{len(extracted.xray.network_apis)} APIs | "
                        f"{len(extracted.images)} imgs | {elapsed_ms} ms"
                    )

                if status_code < 400:
                    for next_url in extracted.crawlable_internal_urls:
                        await self._enqueue_url(
                            next_url,
                            depth=depth + 1,
                            discovered_from=final_norm,
                        )

                return

            except (PlaywrightTimeoutError, PlaywrightError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.config.max_retries:
                    backoff = self._compute_backoff(attempt)
                    logger.warning(
                        f"[RETRY {attempt}/{self.config.max_retries}] Falha em {url} ({type(exc).__name__}) "
                        f"— nova tentativa em {backoff:.1f}s"
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(f"[FALHA] Esgotadas {self.config.max_retries} tentativas em {url}: {exc}")
            finally:
                if api_tasks:
                    await asyncio.gather(*api_tasks, return_exceptions=True)
                if context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass

        record.state = "failed"
        record.error = last_error or "Erro desconhecido"
        record.crawled_at = datetime.now(timezone.utc).isoformat()
        self.stats.pages_failed += 1

    @staticmethod
    async def _record_xhr_response(resp: Response, sink: list[NetworkAPIRecord]) -> None:
        """Lê de forma segura respostas XHR/Fetch (com amostra estruturada se for JSON)."""
        try:
            ctype = resp.headers.get("content-type", "")
            preview: Any = None
            if "json" in ctype.lower() and resp.status < 500:
                body_bytes = await resp.body()
                if body_bytes and len(body_bytes) <= 262_144:  # até 256 KB
                    parsed = json.loads(body_bytes.decode("utf-8", errors="replace"))
                    preview = PageExtractor._compact_json_preview(parsed, max_chars=1800)
            sink.append(
                NetworkAPIRecord(
                    url=resp.url,
                    method=resp.request.method,
                    status_code=resp.status,
                    resource_type=resp.request.resource_type,
                    content_type=ctype,
                    json_preview=preview,
                )
            )
        except Exception:
            pass

    async def _inspect_hidden_browser_state(
        self,
        page: Page,
    ) -> tuple[list[dict[str, str]], list[str], dict[str, Any]]:
        """
        Executa inspeção profunda dentro do motor Chromium:
        1. Deteta elementos escondidos por folhas CSS externas (`display:none`, `visibility:hidden`, `opacity:0`).
        2. Extrai todas as imagens em `background-image` computadas (incluindo `::before` e `::after`).
        3. Captura variáveis globais de estado de SPAs (`__NEXT_DATA__`, `__NUXT__`, `__INITIAL_STATE__`, etc.).
        4. Abre acordeões `<details>` fechados para revelar conteúdo preguiçoso no HTML final.
        """
        try:
            result = await page.evaluate(
                """
                async () => {
                    const hiddenSnapshots = [];
                    const bgUrls = new Set();
                    const urlRegex = /url\\(\\s*['"]?([^'")]+)['"]?\\s*\\)/gi;

                    const extractBg = (bgValue) => {
                        if (!bgValue || bgValue === 'none') return;
                        let match;
                        while ((match = urlRegex.exec(bgValue)) !== null) {
                            if (match[1] && !match[1].startsWith('data:')) {
                                bgUrls.add(match[1]);
                            }
                        }
                    };

                    const selectorHint = (el) => {
                        const tag = el.tagName.toLowerCase();
                        if (el.id) return `${tag}#${el.id}`;
                        if (el.className && typeof el.className === 'string') {
                            const cls = el.className.trim().split(/\\s+/).slice(0, 2).join('.');
                            if (cls) return `${tag}.${cls}`;
                        }
                        return tag;
                    };

                    const allElements = Array.from(document.querySelectorAll('body *'));
                    for (const el of allElements) {
                        const tag = el.tagName.toLowerCase();
                        if (['script', 'style', 'noscript', 'svg', 'path', 'meta', 'link'].includes(tag)) {
                            continue;
                        }

                        const cs = window.getComputedStyle(el);
                        extractBg(cs.backgroundImage);
                        extractBg(window.getComputedStyle(el, '::before').backgroundImage);
                        extractBg(window.getComputedStyle(el, '::after').backgroundImage);

                        // Captura apenas raízes de blocos ocultos (evita duplicar cada <span> filho do mesmo bloco oculto)
                        if (hiddenSnapshots.length < 120) {
                            const parentCs = el.parentElement ? window.getComputedStyle(el.parentElement) : null;
                            const isHiddenDisplay = cs.display === 'none' && (!parentCs || parentCs.display !== 'none');
                            const isHiddenVis = cs.visibility === 'hidden' && (!parentCs || parentCs.visibility !== 'hidden');
                            const isZeroOpacity = cs.opacity === '0' && (!parentCs || parentCs.opacity !== '0');

                            if (isHiddenDisplay || isHiddenVis || isZeroOpacity) {
                                const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                                if (text.length >= 5) {
                                    hiddenSnapshots.push({
                                        reason: isHiddenDisplay
                                            ? 'css-computed(display:none)'
                                            : isHiddenVis
                                            ? 'css-computed(visibility:hidden)'
                                            : 'css-computed(opacity:0)',
                                        tag,
                                        selector: selectorHint(el),
                                        text: text.slice(0, 1500),
                                    });
                                }
                            }
                        }
                    }

                    // Abre todos os <details> colapsados para garantir que o DOM final expõe o conteúdo interno
                    document.querySelectorAll('details:not([open])').forEach((d) => {
                        d.setAttribute('open', '');
                    });

                    // Captura estado público de hidratação de frameworks SPA
                    const spaGlobals = {};
                    const candidateGlobals = [
                        '__NEXT_DATA__',
                        '__NUXT__',
                        '__INITIAL_STATE__',
                        '__PRELOADED_STATE__',
                        '__APOLLO_STATE__',
                        '__REMIX_CONTEXT',
                    ];
                    for (const key of candidateGlobals) {
                        if (Object.prototype.hasOwnProperty.call(window, key) && window[key] != null) {
                            try {
                                spaGlobals[key] = JSON.parse(JSON.stringify(window[key]));
                            } catch (_) {
                                spaGlobals[key] = '[Objeto presente mas não serializável diretamente]';
                            }
                        }
                    }

                    return {
                        hiddenSnapshots,
                        bgImages: Array.from(bgUrls),
                        spaGlobals,
                    };
                }
                """
            )
            return (
                result.get("hiddenSnapshots", []),
                result.get("bgImages", []),
                result.get("spaGlobals", {}),
            )
        except PlaywrightError as exc:
            logger.debug(f"Inspeção profunda JS falhou em {page.url}: {exc}")
            return [], [], {}

    async def _interact_and_scroll(self, page: Page, timeout_ms: int) -> None:
        idle_timeout = min(max(3000, timeout_ms // 3), 10000)

        try:
            await page.wait_for_load_state("networkidle", timeout=idle_timeout)
        except PlaywrightTimeoutError:
            logger.debug(f"networkidle inicial excedeu {idle_timeout}ms em {page.url}; a avançar para scroll.")

        try:
            await page.evaluate(
                """
                async ({ maxSteps, pauseMs }) => {
                    const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                    let steps = 0;

                    while (steps < maxSteps) {
                        const scrollHeight = Math.max(
                            document.body ? document.body.scrollHeight : 0,
                            document.documentElement ? document.documentElement.scrollHeight : 0
                        );
                        const viewportHeight = window.innerHeight || 900;
                        const currentBottom = window.scrollY + viewportHeight;

                        if (currentBottom >= scrollHeight - 8) {
                            await delay(pauseMs);
                            const updatedHeight = Math.max(
                                document.body ? document.body.scrollHeight : 0,
                                document.documentElement ? document.documentElement.scrollHeight : 0
                            );
                            if (window.scrollY + viewportHeight >= updatedHeight - 8) {
                                break;
                            }
                        }

                        const distance = Math.max(400, Math.floor(viewportHeight * 0.8));
                        window.scrollBy({ top: distance, left: 0, behavior: "smooth" });
                        steps += 1;
                        await delay(pauseMs);
                    }

                    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "smooth" });
                    await delay(Math.min(pauseMs, 250));
                }
                """,
                {"maxSteps": self.config.max_scroll_steps, "pauseMs": self.config.scroll_pause_ms},
            )
        except PlaywrightError as exc:
            logger.debug(f"Auto-scroll interrompido em {page.url}: {exc}")

        try:
            await page.wait_for_load_state("networkidle", timeout=idle_timeout)
        except PlaywrightTimeoutError:
            logger.debug(f"networkidle pós-scroll excedeu {idle_timeout}ms em {page.url}; a extrair DOM atual.")

    def _compute_backoff(self, attempt: int) -> float:
        exp = self.config.backoff_base * (2 ** (attempt - 1))
        return min(30.0, exp + random.uniform(0.25, 1.1))

    @staticmethod
    def _parse_retry_after(header_val: str | None) -> float | None:
        if not header_val:
            return None
        try:
            seconds = float(header_val.strip())
            if 0 < seconds <= 60:
                return seconds
        except ValueError:
            pass
        return None
