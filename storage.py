"""
Camada de Persistência e Download Assíncrono de Assets (`output/<dominio>/<dominio>_<data_hora>/`):
- `dataset.jsonl`: Registo JSON por linha com todos os dados visíveis e ocultos por página
- `pages/`: Ficheiros `.md` individuais espelhando as rotas do site (com secção Raio-X de dados ocultos)
- `assets/`: Subpastas mantendo a estrutura do site para imagens (incluindo CSS backgrounds), vídeos, áudios e documentos
- `sitemap_extracted.json`: Mapa completo de todas as URLs descobertas (incluindo órfãs via sitemap.xml), status HTTP e erros
- `xray_hidden_report.json`: Relatório consolidado de elementos ocultos, comentários HTML, APIs XHR/Fetch, forms e contactos
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import mimetypes
import random
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import aiofiles
import httpx
from loguru import logger

from extractor import ExtractedPageData
from utils import (
    deduplicate_preserving_order,
    get_random_user_agent,
    sanitize_path_segment,
    url_to_asset_relpath,
    url_to_markdown_relpath,
)

COMMON_MIME_EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/x-icon": ".ico",
    "application/pdf": ".pdf",
    "text/csv": ".csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
    "application/zip": ".zip",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/xml": ".xml",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/ogg": ".ogg",
}


@dataclass(slots=True)
class SitemapURLRecord:
    url: str
    state: str
    depth: int
    discovered_from: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    title: str | None = None
    markdown_file: str | None = None
    internal_links_count: int = 0
    external_links_count: int = 0
    images_count: int = 0
    documents_count: int = 0
    hidden_items_count: int = 0
    network_apis_count: int = 0
    retries: int = 0
    response_time_ms: int | None = None
    crawled_at: str | None = None
    error: str | None = None


class StorageManager:
    """Gere toda a estrutura de saída `<pasta_projeto>/output/<dominio>/<execucao>/` de forma assíncrona."""

    def __init__(
        self,
        *,
        base_output_dir: Path,
        target_domain: str,
        isolate_run_folder: bool = True,
        download_assets: bool = False,
        include_raw_html: bool = True,
        asset_concurrency: int = 6,
        max_asset_size_mb: int = 150,
        max_retries: int = 3,
        on_asset_downloaded: Callable[[], None] | None = None,
    ) -> None:
        safe_domain = sanitize_path_segment(target_domain)
        self.target_domain = target_domain
        domain_root = base_output_dir / safe_domain

        if isolate_run_folder:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            candidate = domain_root / f"{safe_domain}_{timestamp}"
            suffix = 2
            while candidate.exists():
                candidate = domain_root / f"{safe_domain}_{timestamp}_{suffix}"
                suffix += 1
            self.domain_dir = candidate
        else:
            self.domain_dir = domain_root

        self.pages_dir = self.domain_dir / "pages"
        self.assets_dir = self.domain_dir / "assets"
        self.dataset_path = self.domain_dir / "dataset.jsonl"
        self.sitemap_path = self.domain_dir / "sitemap_extracted.json"
        self.xray_report_path = self.domain_dir / "xray_hidden_report.json"

        self.download_assets = download_assets
        self.include_raw_html = include_raw_html
        self.max_asset_bytes = max_asset_size_mb * 1024 * 1024
        self.max_retries = max_retries
        self.on_asset_downloaded = on_asset_downloaded

        self._jsonl_lock = asyncio.Lock()
        self._xray_lock = asyncio.Lock()
        self._fs_lock = asyncio.Lock()
        self._asset_sem = asyncio.Semaphore(max(1, asset_concurrency))
        self._asset_inflight: dict[str, asyncio.Task[str | None]] = {}
        self._http_client: httpx.AsyncClient | None = None
        self.downloaded_assets_count: int = 0

        # Acumuladores para o relatório global `xray_hidden_report.json`
        self._all_emails: list[str] = []
        self._all_phones: list[str] = []
        self._all_external_scripts: list[str] = []
        self._all_iframes: list[str] = []
        self._discovered_apis: dict[str, dict[str, Any]] = {}
        self._comments_by_page: dict[str, list[str]] = {}
        self._hidden_dom_by_page: dict[str, list[dict[str, str]]] = {}
        self._forms_by_page: dict[str, list[dict[str, Any]]] = {}
        self._spa_states_by_page: dict[str, list[dict[str, Any]]] = {}

    async def initialize(self, reset_dataset: bool = True) -> None:
        self.domain_dir.mkdir(parents=True, exist_ok=True)
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        if self.download_assets:
            self.assets_dir.mkdir(parents=True, exist_ok=True)
            self._http_client = httpx.AsyncClient(
                follow_redirects=True,
                timeout=httpx.Timeout(30.0, connect=10.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                headers={"Accept": "*/*"},
            )

        if reset_dataset and self.dataset_path.exists():
            self.dataset_path.unlink()

    async def close(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def persist_page(self, page_data: ExtractedPageData) -> str:
        if self.download_assets:
            await self._download_page_assets(page_data)

        md_relpath = await self._write_markdown_page(page_data)
        page_data.markdown_relpath = md_relpath

        await self._append_dataset_record(page_data)
        await self._accumulate_xray_findings(page_data)
        return md_relpath

    async def _accumulate_xray_findings(self, page_data: ExtractedPageData) -> None:
        """Agrega dados ocultos, APIs, comentários, forms e contactos para o relatório consolidado."""
        page_key = page_data.final_url or page_data.url
        xray = page_data.xray

        async with self._xray_lock:
            self._all_emails.extend(xray.contacts.emails)
            self._all_phones.extend(xray.contacts.phones)
            self._all_external_scripts.extend(xray.external_scripts)
            self._all_iframes.extend(xray.iframes)

            if xray.html_comments:
                self._comments_by_page[page_key] = xray.html_comments

            if xray.hidden_dom_items:
                self._hidden_dom_by_page[page_key] = [asdict(item) for item in xray.hidden_dom_items]

            if xray.forms:
                self._forms_by_page[page_key] = [asdict(f) for f in xray.forms]

            if xray.embedded_json_states:
                self._spa_states_by_page[page_key] = [
                    {
                        "source": st.source,
                        "script_id": st.script_id,
                        "raw_length": st.raw_length,
                        "preview": st.preview,
                    }
                    for st in xray.embedded_json_states
                ]

            for api in xray.network_apis:
                sig = f"{api.method} {api.url}"
                if sig not in self._discovered_apis:
                    self._discovered_apis[sig] = {
                        "method": api.method,
                        "url": api.url,
                        "status_code": api.status_code,
                        "resource_type": api.resource_type,
                        "content_type": api.content_type,
                        "found_on_page": page_key,
                        "json_preview": api.json_preview,
                    }

    async def _write_markdown_page(self, page_data: ExtractedPageData) -> str:
        rel_path = url_to_markdown_relpath(page_data.final_url or page_data.url)
        target_path = await self._resolve_collision_safe_path(self.pages_dir, rel_path)

        frontmatter_lines = [
            "---",
            f"url: {json.dumps(page_data.url, ensure_ascii=False)}",
            f"final_url: {json.dumps(page_data.final_url, ensure_ascii=False)}",
            f"canonical_url: {json.dumps(page_data.seo.canonical_url, ensure_ascii=False)}",
            f"title: {json.dumps(page_data.seo.title, ensure_ascii=False)}",
            f"description: {json.dumps(page_data.seo.meta_description, ensure_ascii=False)}",
            f"generator: {json.dumps(page_data.seo.generator, ensure_ascii=False)}",
            f"status_code: {page_data.status_code}",
            f"crawled_at: {json.dumps(page_data.crawled_at, ensure_ascii=False)}",
            f"word_count: {page_data.word_count}",
            f"hidden_dom_items: {len(page_data.xray.hidden_dom_items)}",
            f"html_comments: {len(page_data.xray.html_comments)}",
            f"network_apis: {len(page_data.xray.network_apis)}",
            f"markdown_engine: {json.dumps(page_data.markdown_engine_used, ensure_ascii=False)}",
            "---",
            "",
        ]
        full_markdown = "\n".join(frontmatter_lines) + (page_data.markdown or "") + "\n"

        async with aiofiles.open(target_path, mode="w", encoding="utf-8") as fh:
            await fh.write(full_markdown)

        return target_path.relative_to(self.domain_dir).as_posix()

    async def _resolve_collision_safe_path(self, root_dir: Path, rel_path: Path) -> Path:
        async with self._fs_lock:
            target = root_dir / rel_path
            current = root_dir
            for part in rel_path.parts[:-1]:
                current = current / part
                if current.exists() and current.is_file():
                    renamed = current.with_name(f"{current.name}__file")
                    current.rename(renamed)

            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.is_dir():
                target = target / "index.md"
            return target

    async def _append_dataset_record(self, page_data: ExtractedPageData) -> None:
        record_dict = page_data.to_dataset_dict(include_raw_html=self.include_raw_html)
        serialized = json.dumps(record_dict, ensure_ascii=False) + "\n"

        async with self._jsonl_lock:
            async with aiofiles.open(self.dataset_path, mode="a", encoding="utf-8") as fh:
                await fh.write(serialized)

    async def _download_page_assets(self, page_data: ExtractedPageData) -> None:
        if self._http_client is None:
            return

        tasks: list[asyncio.Task[None]] = []

        async def _assign_image(idx: int, url: str) -> None:
            local = await self._get_or_download_asset(url, default_ext=".img")
            page_data.images[idx].local_path = local

        async def _assign_doc(idx: int, url: str, ext: str) -> None:
            local = await self._get_or_download_asset(url, default_ext=ext or ".bin")
            page_data.documents[idx].local_path = local

        async def _assign_video(idx: int, url: str) -> None:
            local = await self._get_or_download_asset(url, default_ext=".mp4")
            page_data.videos[idx].local_path = local

        async def _assign_audio(idx: int, url: str) -> None:
            local = await self._get_or_download_asset(url, default_ext=".mp3")
            page_data.audios[idx].local_path = local

        for i, img in enumerate(page_data.images):
            tasks.append(asyncio.create_task(_assign_image(i, img.url)))

        for i, doc in enumerate(page_data.documents):
            tasks.append(asyncio.create_task(_assign_doc(i, doc.url, doc.extension)))

        for i, vid in enumerate(page_data.videos):
            if vid.kind == "direct" and not vid.url.lower().endswith(".m3u8"):
                tasks.append(asyncio.create_task(_assign_video(i, vid.url)))

        for i, aud in enumerate(page_data.audios):
            tasks.append(asyncio.create_task(_assign_audio(i, aud.url)))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _get_or_download_asset(self, asset_url: str, default_ext: str = ".bin") -> str | None:
        existing_task = self._asset_inflight.get(asset_url)
        if existing_task is not None:
            return await existing_task

        task = asyncio.create_task(self._download_single_asset(asset_url, default_ext=default_ext))
        self._asset_inflight[asset_url] = task
        return await task

    async def _download_single_asset(self, asset_url: str, default_ext: str = ".bin") -> str | None:
        if self._http_client is None:
            return None

        async with self._asset_sem:
            for attempt in range(1, self.max_retries + 1):
                try:
                    headers = {"User-Agent": get_random_user_agent()}
                    async with self._http_client.stream("GET", asset_url, headers=headers) as resp:
                        if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                            backoff = (2 ** (attempt - 1)) + random.uniform(0.2, 0.9)
                            await asyncio.sleep(backoff)
                            continue

                        if resp.status_code >= 400:
                            logger.debug(f"Asset HTTP {resp.status_code} ao descarregar {asset_url}")
                            return None

                        content_length = resp.headers.get("content-length")
                        if content_length and content_length.isdigit():
                            if int(content_length) > self.max_asset_bytes:
                                logger.warning(
                                    f"Asset ignorado por exceder limite ({content_length} bytes): {asset_url}"
                                )
                                return None

                        inferred_ext = self._infer_extension(resp.headers.get("content-type", ""), default_ext)
                        rel_asset_path = url_to_asset_relpath(
                            asset_url,
                            self.target_domain,
                            default_ext=inferred_ext,
                        )
                        dest_path = await self._resolve_collision_safe_path(self.assets_dir, rel_asset_path)

                        written = 0
                        async with aiofiles.open(dest_path, mode="wb") as fh:
                            async for chunk in resp.aiter_bytes(chunk_size=65536):
                                written += len(chunk)
                                if written > self.max_asset_bytes:
                                    logger.warning(f"Download abortado (limite excedido): {asset_url}")
                                    break
                                await fh.write(chunk)

                        if written > self.max_asset_bytes:
                            dest_path.unlink(missing_ok=True)
                            return None

                        self.downloaded_assets_count += 1
                        if self.on_asset_downloaded:
                            self.on_asset_downloaded()

                        return dest_path.relative_to(self.domain_dir).as_posix()

                except (httpx.HTTPError, OSError) as exc:
                    if attempt < self.max_retries:
                        backoff = (2 ** (attempt - 1)) + random.uniform(0.2, 0.9)
                        await asyncio.sleep(backoff)
                    else:
                        logger.debug(f"Falha ao descarregar asset {asset_url}: {exc}")
                        return None

        return None

    @staticmethod
    def _infer_extension(content_type_header: str, fallback_ext: str) -> str:
        mime = content_type_header.split(";")[0].strip().lower()
        if not mime:
            return fallback_ext
        if mime in COMMON_MIME_EXTENSIONS:
            return COMMON_MIME_EXTENSIONS[mime]
        guessed = mimetypes.guess_extension(mime)
        return guessed or fallback_ext

    async def write_sitemap(
        self,
        *,
        summary: dict[str, Any],
        records: dict[str, SitemapURLRecord],
    ) -> Path:
        ordered_urls = [asdict(records[url]) for url in sorted(records.keys())]
        payload = {
            "summary": summary,
            "urls": ordered_urls,
        }
        serialized = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        async with aiofiles.open(self.sitemap_path, mode="w", encoding="utf-8") as fh:
            await fh.write(serialized)
        return self.sitemap_path

    async def write_xray_report(self, *, summary: dict[str, Any]) -> Path:
        """Gera `xray_hidden_report.json` com todos os elementos ocultos, APIs, comentários e contactos."""
        unique_emails = deduplicate_preserving_order(self._all_emails)
        unique_phones = deduplicate_preserving_order(self._all_phones)
        unique_scripts = deduplicate_preserving_order(self._all_external_scripts)
        unique_iframes = deduplicate_preserving_order(self._all_iframes)

        report_payload = {
            "domain": self.target_domain,
            "generated_at": summary.get("finished_at"),
            "totals": {
                "unique_emails": len(unique_emails),
                "unique_phones": len(unique_phones),
                "unique_xhr_fetch_apis": len(self._discovered_apis),
                "pages_with_hidden_dom": len(self._hidden_dom_by_page),
                "pages_with_html_comments": len(self._comments_by_page),
                "pages_with_forms": len(self._forms_by_page),
                "pages_with_spa_state": len(self._spa_states_by_page),
                "unique_external_scripts": len(unique_scripts),
                "unique_iframes": len(unique_iframes),
            },
            "contacts": {
                "emails": unique_emails,
                "phones": unique_phones,
            },
            "discovered_xhr_fetch_apis": list(self._discovered_apis.values()),
            "spa_hydration_states_by_page": self._spa_states_by_page,
            "forms_and_hidden_inputs_by_page": self._forms_by_page,
            "html_comments_by_page": self._comments_by_page,
            "hidden_dom_by_page": self._hidden_dom_by_page,
            "external_scripts": unique_scripts,
            "iframes": unique_iframes,
        }
        serialized = json.dumps(report_payload, indent=2, ensure_ascii=False) + "\n"
        async with aiofiles.open(self.xray_report_path, mode="w", encoding="utf-8") as fh:
            await fh.write(serialized)
        return self.xray_report_path
