"""
Motor de Extração Completa e Inspeção Profunda ("Raio-X") por Página:
- Parsing estrutural com BeautifulSoup4 (lxml / html.parser)
- Metadados SEO completos (Title, Description, Canonical, Robots, Open Graph, Twitter Cards, JSON-LD, TODAS as meta tags)
- Conversão híbrida de HTML para Markdown limpo (Trafilatura + BeautifulSoup4/Markdownify)
  + Apêndice opcional "Raio-X" com todo o conteúdo oculto descoberto na página
- Extração de Conteúdo Oculto:
  * Elementos escondidos no DOM/CSS (`hidden`, `aria-hidden`, `display:none`, `visibility:hidden`, `<details>`, `<template>`, `<noscript>`, classes `sr-only`)
  * Comentários HTML (`<!-- ... -->`) deixados no código-fonte
  * Estado JSON interno de SPAs (`__NEXT_DATA__`, `__NUXT__`, `<script type="application/json">`, variáveis globais)
  * Formulários e campos invisíveis (`<input type="hidden">`)
  * Links escondidos em atributos `data-href` / `data-url`
  * Imagens escondidas em CSS `background-image: url(...)`, `<audio>`, todos os `<iframe>` e `<script src>`
  * Contactos (`mailto:`, `tel:`, `whatsapp:`) e feeds RSS/Atom
  * APIs XHR / Fetch / GraphQL capturadas passivamente durante a renderização no Playwright
"""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, Tag
from loguru import logger
from markdownify import markdownify as md_convert

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None  # type: ignore[assignment]

from utils import (
    DOCUMENT_EXTENSIONS,
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    deduplicate_preserving_order,
    get_url_extension,
    is_crawlable_page_url,
    is_same_domain,
    normalize_url,
)

MarkdownEngine = Literal["hybrid", "trafilatura", "markdownify"]

AUDIO_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".wav", ".ogg", ".flac", ".aac", ".m4a"})

VIDEO_EMBED_PATTERNS: tuple[str, ...] = (
    "youtube.com/embed/",
    "youtube-nocookie.com/embed/",
    "player.vimeo.com/video/",
    "fast.wistia.net/embed/",
    "loom.com/embed/",
    "dailymotion.com/embed/",
)

NOISE_SELECTORS: tuple[str, ...] = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "form[role='search']",
    ".cookie-banner",
    "#cookie-banner",
    "#onetrust-consent-sdk",
    ".cc-window",
)

HIDDEN_CLASS_KEYWORDS: tuple[str, ...] = (
    "hidden",
    "d-none",
    "sr-only",
    "visually-hidden",
    "screen-reader",
    "collapse",
    "offcanvas",
    "modal",
    "drawer",
    "tooltip",
    "tab-pane",
    "accordion-collapse",
)

CSS_URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.IGNORECASE)
INLINE_STYLE_HIDDEN_RE = re.compile(
    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\D|$))",
    re.IGNORECASE,
)


@dataclass(slots=True)
class SEOMetadata:
    title: str = ""
    meta_description: str = ""
    canonical_url: str | None = None
    language: str | None = None
    robots: str | None = None
    author: str | None = None
    generator: str | None = None
    keywords: list[str] = field(default_factory=list)
    open_graph: dict[str, str] = field(default_factory=dict)
    twitter_cards: dict[str, str] = field(default_factory=dict)
    all_meta_tags: list[dict[str, str]] = field(default_factory=list)
    alternate_links: list[dict[str, str]] = field(default_factory=list)
    json_ld: list[dict[str, Any] | list[Any]] = field(default_factory=list)
    headings: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class LinkRecord:
    url: str
    anchor_text: str = ""
    rel: list[str] = field(default_factory=list)
    source: str = "a[href]"


@dataclass(slots=True)
class ImageRecord:
    url: str
    alt: str = ""
    title: str = ""
    source: str = "img"
    local_path: str | None = None


@dataclass(slots=True)
class VideoRecord:
    url: str
    kind: Literal["direct", "embed"] = "direct"
    mime_type: str = ""
    local_path: str | None = None


@dataclass(slots=True)
class AudioRecord:
    url: str
    mime_type: str = ""
    local_path: str | None = None


@dataclass(slots=True)
class DocumentRecord:
    url: str
    filename: str
    extension: str
    anchor_text: str = ""
    local_path: str | None = None


@dataclass(slots=True)
class HiddenDOMItem:
    reason: str  # ex: "display:none", "hidden-attr", "aria-hidden", "details-collapsed", "noscript", "template", "css-computed", "data-attribute"
    tag: str
    selector_hint: str
    text: str


@dataclass(slots=True)
class FormFieldInfo:
    name: str
    field_type: str
    value: str = ""
    hidden: bool = False


@dataclass(slots=True)
class FormRecord:
    action: str
    method: str
    form_id: str = ""
    fields: list[FormFieldInfo] = field(default_factory=list)


@dataclass(slots=True)
class EmbeddedStateRecord:
    source: str  # ex: "__NEXT_DATA__", "script[application/json]", "window.__INITIAL_STATE__"
    script_id: str = ""
    preview: Any = None
    raw_length: int = 0


@dataclass(slots=True)
class NetworkAPIRecord:
    url: str
    method: str
    status_code: int
    resource_type: str  # "xhr" | "fetch"
    content_type: str = ""
    json_preview: Any = None


@dataclass(slots=True)
class ContactsRecord:
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DeepXRayData:
    html_comments: list[str] = field(default_factory=list)
    hidden_dom_items: list[HiddenDOMItem] = field(default_factory=list)
    forms: list[FormRecord] = field(default_factory=list)
    embedded_json_states: list[EmbeddedStateRecord] = field(default_factory=list)
    network_apis: list[NetworkAPIRecord] = field(default_factory=list)
    iframes: list[str] = field(default_factory=list)
    external_scripts: list[str] = field(default_factory=list)
    stylesheets: list[str] = field(default_factory=list)
    contacts: ContactsRecord = field(default_factory=ContactsRecord)
    response_headers: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractedPageData:
    url: str
    final_url: str
    status_code: int
    content_type: str
    crawled_at: str
    depth: int
    response_time_ms: int
    seo: SEOMetadata
    markdown: str
    markdown_engine_used: str
    word_count: int
    internal_links: list[LinkRecord] = field(default_factory=list)
    external_links: list[LinkRecord] = field(default_factory=list)
    images: list[ImageRecord] = field(default_factory=list)
    videos: list[VideoRecord] = field(default_factory=list)
    audios: list[AudioRecord] = field(default_factory=list)
    documents: list[DocumentRecord] = field(default_factory=list)
    xray: DeepXRayData = field(default_factory=DeepXRayData)
    crawlable_internal_urls: list[str] = field(default_factory=list)
    markdown_relpath: str | None = None
    raw_html: str | None = None

    def to_dataset_dict(self, include_raw_html: bool = True) -> dict[str, Any]:
        """Serializa o registo estruturado completo para escrita em `dataset.jsonl`."""
        payload = asdict(self)
        payload.pop("crawlable_internal_urls", None)
        if not include_raw_html:
            payload.pop("raw_html", None)
        return payload


class PageExtractor:
    """Extrator modular de metadados SEO, Markdown limpo, links, assets e Raio-X de dados ocultos."""

    def __init__(
        self,
        target_domain: str,
        include_subdomains: bool = False,
        markdown_engine: MarkdownEngine = "hybrid",
        deep_xray: bool = True,
        append_xray_to_markdown: bool = True,
    ) -> None:
        self.target_domain = target_domain
        self.include_subdomains = include_subdomains
        self.markdown_engine: MarkdownEngine = markdown_engine
        self.deep_xray = deep_xray
        self.append_xray_to_markdown = append_xray_to_markdown

    def extract(
        self,
        *,
        requested_url: str,
        final_url: str,
        raw_html: str,
        status_code: int,
        content_type: str,
        crawled_at: str,
        depth: int,
        response_time_ms: int,
        response_headers: dict[str, str] | None = None,
        browser_hidden_snapshots: list[dict[str, str]] | None = None,
        browser_bg_images: list[str] | None = None,
        browser_spa_globals: dict[str, Any] | None = None,
        network_apis: list[NetworkAPIRecord] | None = None,
    ) -> ExtractedPageData:
        """Executa o pipeline completo de extração visível e oculta sobre o HTML renderizado."""
        soup = self._make_soup(raw_html)
        effective_base = self._resolve_base_href(soup, final_url)

        seo = self._extract_seo(soup, effective_base)
        (
            internal_links,
            external_links,
            crawlable_urls,
            linked_docs,
            linked_videos,
            linked_audios,
            contacts,
        ) = self._extract_links_and_linked_assets(soup, effective_base)

        images = self._extract_images(
            soup,
            effective_base,
            extra_bg_urls=browser_bg_images or [],
        )
        videos = self._extract_videos(soup, effective_base, extra_direct_videos=linked_videos)
        audios = self._extract_audios(soup, effective_base, extra_audios=linked_audios)
        documents = self._extract_embedded_documents(soup, effective_base, initial_docs=linked_docs)

        xray = DeepXRayData(
            contacts=contacts,
            network_apis=list(network_apis or []),
            response_headers=self._sanitize_headers(response_headers or {}),
        )

        if self.deep_xray:
            xray.html_comments = self._extract_html_comments(soup)
            xray.hidden_dom_items = self._extract_hidden_dom_items(
                soup,
                browser_snapshots=browser_hidden_snapshots or [],
            )
            xray.forms = self._extract_forms(soup, effective_base)
            xray.embedded_json_states = self._extract_embedded_states(
                soup,
                browser_spa_globals=browser_spa_globals or {},
            )
            xray.iframes = self._extract_all_iframes(soup, effective_base)
            xray.external_scripts, xray.stylesheets = self._extract_scripts_and_styles(soup, effective_base)

        markdown, engine_used = self._extract_clean_markdown(
            raw_html=raw_html,
            soup=soup,
            base_url=effective_base,
            fallback_title=seo.title,
        )

        if self.deep_xray and self.append_xray_to_markdown:
            xray_md = self._build_xray_markdown_appendix(seo=seo, xray=xray, documents=documents)
            if xray_md:
                markdown = f"{markdown}\n\n---\n\n{xray_md}".strip()

        word_count = len(re.findall(r"\w+", markdown, flags=re.UNICODE))

        return ExtractedPageData(
            url=requested_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            crawled_at=crawled_at,
            depth=depth,
            response_time_ms=response_time_ms,
            seo=seo,
            markdown=markdown,
            markdown_engine_used=engine_used,
            word_count=word_count,
            internal_links=internal_links,
            external_links=external_links,
            images=images,
            videos=videos,
            audios=audios,
            documents=documents,
            xray=xray,
            crawlable_internal_urls=crawlable_urls,
            raw_html=raw_html,
        )

    @staticmethod
    def _make_soup(raw_html: str) -> BeautifulSoup:
        """Instancia BeautifulSoup com parser `lxml` (rápido) e fallback para `html.parser`."""
        try:
            return BeautifulSoup(raw_html, "lxml")
        except Exception:
            return BeautifulSoup(raw_html, "html.parser")

    @staticmethod
    def _resolve_base_href(soup: BeautifulSoup, page_url: str) -> str:
        """Respeita `<base href="...">` caso a página o defina."""
        base_tag = soup.find("base", href=True)
        if base_tag and isinstance(base_tag, Tag):
            href = str(base_tag.get("href", "")).strip()
            if href:
                return urljoin(page_url, href)
        return page_url

    @staticmethod
    def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
        """Guarda cabeçalhos HTTP técnicos públicos omitindo cookies/credenciais de sessão."""
        excluded = {"set-cookie", "cookie", "authorization", "proxy-authorization"}
        return {
            k.lower(): v
            for k, v in headers.items()
            if k.lower() not in excluded
        }

    def _extract_seo(self, soup: BeautifulSoup, base_url: str) -> SEOMetadata:
        """Extrai Title, Description, Canonical, Open Graph, Twitter Cards, JSON-LD, Alternates e TODAS as meta tags."""
        seo = SEOMetadata()

        html_tag = soup.find("html")
        if isinstance(html_tag, Tag) and html_tag.get("lang"):
            seo.language = str(html_tag.get("lang")).strip()

        if soup.title and soup.title.string:
            seo.title = re.sub(r"\s+", " ", soup.title.string).strip()

        # Links canónicos, feeds RSS/Atom, hreflang e manifests
        for link_tag in soup.find_all("link", href=True):
            if not isinstance(link_tag, Tag):
                continue
            rels = [str(r).lower() for r in (link_tag.get("rel") or [])]
            resolved_href = normalize_url(str(link_tag.get("href", "")), base_url=base_url, strip_tracking=False)
            if not resolved_href:
                continue

            if "canonical" in rels and not seo.canonical_url:
                seo.canonical_url = normalize_url(resolved_href, strip_tracking=True)
            elif any(r in rels for r in ("alternate", "manifest", "amphtml", "feed")):
                seo.alternate_links.append(
                    {
                        "rel": " ".join(rels),
                        "type": str(link_tag.get("type") or ""),
                        "hreflang": str(link_tag.get("hreflang") or ""),
                        "title": str(link_tag.get("title") or ""),
                        "href": resolved_href,
                    }
                )

        # Todas as Meta tags da página (incluindo tags ocultas de CMS/verificação/artigos)
        for meta in soup.find_all("meta"):
            if not isinstance(meta, Tag):
                continue

            raw_attrs = {
                str(k).lower(): str(v).strip()
                for k, v in meta.attrs.items()
                if v is not None and str(v).strip()
            }
            if raw_attrs:
                seo.all_meta_tags.append(raw_attrs)

            content = raw_attrs.get("content", "")
            if not content:
                continue

            name_attr = raw_attrs.get("name", "").lower()
            prop_attr = raw_attrs.get("property", "").lower()

            if prop_attr.startswith("og:") or name_attr.startswith("og:"):
                seo.open_graph[prop_attr or name_attr] = content
            elif name_attr.startswith("twitter:") or prop_attr.startswith("twitter:"):
                seo.twitter_cards[name_attr or prop_attr] = content

            if name_attr == "description" and not seo.meta_description:
                seo.meta_description = content
            elif name_attr == "robots" and not seo.robots:
                seo.robots = content
            elif name_attr == "author" and not seo.author:
                seo.author = content
            elif prop_attr == "article:author" and not seo.author:
                seo.author = content
            elif name_attr == "generator" and not seo.generator:
                seo.generator = content
            elif name_attr == "keywords" and not seo.keywords:
                seo.keywords = [kw.strip() for kw in content.split(",") if kw.strip()]

        if not seo.title:
            seo.title = seo.open_graph.get("og:title", "")
            if not seo.title:
                first_h1 = soup.find("h1")
                if first_h1:
                    seo.title = re.sub(r"\s+", " ", first_h1.get_text(" ", strip=True))

        if not seo.meta_description:
            seo.meta_description = seo.open_graph.get("og:description", "")

        # JSON-LD / Schema.org
        for script in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
            raw_json = (script.string or script.get_text() or "").strip()
            if not raw_json:
                continue
            parsed = self._safe_parse_json(raw_json)
            if parsed is not None:
                seo.json_ld.append(parsed)

        # Hierarquia completa de cabeçalhos (H1-H6)
        headings: dict[str, list[str]] = {}
        for level in ("h1", "h2", "h3", "h4", "h5", "h6"):
            items = [
                re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
                for tag in soup.find_all(level)
                if tag.get_text(strip=True)
            ]
            if items:
                headings[level] = items
        seo.headings = headings

        return seo

    @staticmethod
    def _safe_parse_json(raw_json: str) -> dict[str, Any] | list[Any] | None:
        """Faz parsing tolerante de blocos JSON / JSON-LD."""
        try:
            data = json.loads(raw_json)
            if isinstance(data, (dict, list)):
                return data
        except json.JSONDecodeError:
            cleaned = re.sub(r"^\s*<!--|-->\s*$", "", raw_json).strip()
            try:
                data = json.loads(cleaned, strict=False)
                if isinstance(data, (dict, list)):
                    return data
            except json.JSONDecodeError as exc:
                logger.debug(f"Bloco JSON malformado ignorado: {exc}")
        return None

    def _extract_links_and_linked_assets(
        self,
        soup: BeautifulSoup,
        base_url: str,
    ) -> tuple[
        list[LinkRecord],
        list[LinkRecord],
        list[str],
        list[DocumentRecord],
        list[VideoRecord],
        list[AudioRecord],
        ContactsRecord,
    ]:
        """
        Extrai `<a href>` E também links escondidos em atributos `data-href` / `data-url`,
        bem como contactos (`mailto:`, `tel:`, `whatsapp:`), documentos, vídeos e áudios.
        """
        internal_links: list[LinkRecord] = []
        external_links: list[LinkRecord] = []
        crawlable_urls: list[str] = []
        documents: list[DocumentRecord] = []
        direct_videos: list[VideoRecord] = []
        direct_audios: list[AudioRecord] = []
        emails: list[str] = []
        phones: list[str] = []

        seen_internal: set[str] = set()
        seen_external: set[str] = set()
        seen_crawlable: set[str] = set()
        seen_docs: set[str] = set()
        seen_videos: set[str] = set()
        seen_audios: set[str] = set()

        candidates: list[tuple[Tag, str, str]] = []
        for a_tag in soup.find_all("a", href=True):
            if isinstance(a_tag, Tag):
                candidates.append((a_tag, str(a_tag.get("href") or "").strip(), "a[href]"))

        # Descobre também rotas escondidas em atributos JS (`data-href`, `data-url`, `data-link`)
        if self.deep_xray:
            for attr_name in ("data-href", "data-url", "data-link"):
                for el in soup.find_all(attrs={attr_name: True}):
                    if isinstance(el, Tag) and el.name != "img":
                        val = str(el.get(attr_name) or "").strip()
                        if val:
                            candidates.append((el, val, f"{el.name}[{attr_name}]"))

            # E links dentro de blocos <noscript> (que o parser pode guardar como texto HTML cru)
            for ns in soup.find_all("noscript"):
                ns_html = ns.decode_contents()
                if "<a" in ns_html.lower():
                    ns_soup = self._make_soup(ns_html)
                    for ns_a in ns_soup.find_all("a", href=True):
                        if isinstance(ns_a, Tag):
                            candidates.append((ns_a, str(ns_a.get("href") or "").strip(), "noscript>a[href]"))

        for tag, raw_href, source_label in candidates:
            if not raw_href:
                continue

            lower_href = raw_href.lower()
            if lower_href.startswith("mailto:"):
                addr = unquote(raw_href[7:].split("?")[0]).strip()
                if addr:
                    emails.append(addr)
                continue
            if lower_href.startswith("tel:"):
                num = unquote(raw_href[4:].split("?")[0]).strip()
                if num:
                    phones.append(num)
                continue
            if "wa.me/" in lower_href or lower_href.startswith("whatsapp:"):
                phones.append(raw_href)

            norm_url = normalize_url(raw_href, base_url=base_url, strip_tracking=True)
            if not norm_url:
                continue

            anchor_text = re.sub(r"\s+", " ", tag.get_text(" ", strip=True))
            if not anchor_text and tag.get("title"):
                anchor_text = str(tag.get("title")).strip()
            if not anchor_text and tag.get("aria-label"):
                anchor_text = str(tag.get("aria-label")).strip()

            rel_raw = tag.get("rel") or []
            rel_list = [str(r).lower() for r in rel_raw] if isinstance(rel_raw, list) else [str(rel_raw).lower()]

            ext = get_url_extension(norm_url)

            if ext in DOCUMENT_EXTENSIONS and norm_url not in seen_docs:
                seen_docs.add(norm_url)
                filename = posixpath.basename(unquote(urlsplit(norm_url).path)) or f"document{ext}"
                documents.append(
                    DocumentRecord(
                        url=norm_url,
                        filename=filename,
                        extension=ext,
                        anchor_text=anchor_text,
                    )
                )

            if ext in VIDEO_EXTENSIONS and norm_url not in seen_videos:
                seen_videos.add(norm_url)
                direct_videos.append(VideoRecord(url=norm_url, kind="direct"))

            if ext in AUDIO_EXTENSIONS and norm_url not in seen_audios:
                seen_audios.add(norm_url)
                direct_audios.append(AudioRecord(url=norm_url))

            same_dom = is_same_domain(
                norm_url,
                self.target_domain,
                include_subdomains=self.include_subdomains,
            )

            record = LinkRecord(url=norm_url, anchor_text=anchor_text, rel=rel_list, source=source_label)
            if same_dom:
                if norm_url not in seen_internal:
                    seen_internal.add(norm_url)
                    internal_links.append(record)

                if is_crawlable_page_url(norm_url) and norm_url not in seen_crawlable:
                    # Mesmo links com rel="nofollow" internos são seguidos em modo deep_xray para não esconder páginas internas
                    if self.deep_xray or "nofollow" not in rel_list:
                        seen_crawlable.add(norm_url)
                        crawlable_urls.append(norm_url)
            else:
                if norm_url not in seen_external:
                    seen_external.add(norm_url)
                    external_links.append(record)

        contacts = ContactsRecord(
            emails=deduplicate_preserving_order(emails),
            phones=deduplicate_preserving_order(phones),
        )
        return internal_links, external_links, crawlable_urls, documents, direct_videos, direct_audios, contacts

    def _extract_images(
        self,
        soup: BeautifulSoup,
        base_url: str,
        extra_bg_urls: list[str] | None = None,
    ) -> list[ImageRecord]:
        """Extrai `<img>`, `<picture><source>` e imagens ocultas em CSS `background-image`."""
        images: list[ImageRecord] = []
        seen_urls: set[str] = set()

        for img in soup.find_all("img"):
            if not isinstance(img, Tag):
                continue
            candidate = self._pick_best_image_url(img)
            if not candidate or candidate.lower().startswith("data:"):
                continue
            norm_img_url = normalize_url(candidate, base_url=base_url, strip_tracking=True)
            if not norm_img_url or norm_img_url in seen_urls:
                continue
            seen_urls.add(norm_img_url)
            alt_text = re.sub(r"\s+", " ", str(img.get("alt") or "")).strip()
            title_text = re.sub(r"\s+", " ", str(img.get("title") or "")).strip()
            images.append(ImageRecord(url=norm_img_url, alt=alt_text, title=title_text, source="img"))

        # Variantes em <picture><source srcset="...">
        for source in soup.select("picture source[srcset]"):
            if not isinstance(source, Tag):
                continue
            best = self._parse_highest_srcset(str(source.get("srcset") or ""))
            if best and not best.lower().startswith("data:"):
                norm_src = normalize_url(best, base_url=base_url, strip_tracking=True)
                if norm_src and norm_src not in seen_urls:
                    seen_urls.add(norm_src)
                    images.append(ImageRecord(url=norm_src, source="picture>source"))

        # Imagens escondidas em atributos inline `style="background-image: url(...)"`
        if self.deep_xray:
            for styled_el in soup.find_all(style=True):
                if not isinstance(styled_el, Tag):
                    continue
                style_str = str(styled_el.get("style") or "")
                for match in CSS_URL_RE.findall(style_str):
                    if match.lower().startswith("data:"):
                        continue
                    norm_bg = normalize_url(match, base_url=base_url, strip_tracking=True)
                    if norm_bg and norm_bg not in seen_urls:
                        ext = get_url_extension(norm_bg)
                        if not ext or ext in IMAGE_EXTENSIONS:
                            seen_urls.add(norm_bg)
                            images.append(
                                ImageRecord(
                                    url=norm_bg,
                                    alt=str(styled_el.get("aria-label") or "").strip(),
                                    source="css-inline-background",
                                )
                            )

            # Imagens computadas pelo browser (vindas de folhas de estilo CSS externas)
            for bg_url in extra_bg_urls or []:
                if not bg_url or bg_url.lower().startswith("data:"):
                    continue
                norm_bg = normalize_url(bg_url, base_url=base_url, strip_tracking=True)
                if norm_bg and norm_bg not in seen_urls:
                    seen_urls.add(norm_bg)
                    images.append(ImageRecord(url=norm_bg, source="css-computed-background"))

        return images

    @staticmethod
    def _pick_best_image_url(img: Tag) -> str | None:
        for attr in ("src", "data-src", "data-lazy-src", "data-original", "data-url", "data-hi-res-src"):
            val = str(img.get(attr) or "").strip()
            if val and not val.lower().startswith("data:"):
                return val

        for srcset_attr in ("srcset", "data-srcset"):
            srcset = str(img.get(srcset_attr) or "").strip()
            if srcset:
                best = PageExtractor._parse_highest_srcset(srcset)
                if best:
                    return best
        return None

    @staticmethod
    def _parse_highest_srcset(srcset: str) -> str | None:
        best_url: str | None = None
        best_score = -1.0

        for part in srcset.split(","):
            tokens = part.strip().split()
            if not tokens:
                continue
            url_candidate = tokens[0].strip()
            if url_candidate.lower().startswith("data:"):
                continue
            score = 1.0
            if len(tokens) > 1:
                descriptor = tokens[1].lower()
                match = re.match(r"^(\d+(?:\.\d+)?)(w|x)$", descriptor)
                if match:
                    score = float(match.group(1))
            if score >= best_score:
                best_score = score
                best_url = url_candidate

        return best_url

    def _extract_videos(
        self,
        soup: BeautifulSoup,
        base_url: str,
        extra_direct_videos: list[VideoRecord] | None = None,
    ) -> list[VideoRecord]:
        videos: list[VideoRecord] = []
        seen: set[str] = set()

        for vid in extra_direct_videos or []:
            if vid.url not in seen:
                seen.add(vid.url)
                videos.append(vid)

        for video_tag in soup.find_all("video"):
            if not isinstance(video_tag, Tag):
                continue
            src = str(video_tag.get("src") or "").strip()
            if src:
                norm = normalize_url(src, base_url=base_url, strip_tracking=True)
                if norm and norm not in seen:
                    seen.add(norm)
                    videos.append(VideoRecord(url=norm, kind="direct"))

            for source_tag in video_tag.find_all("source", src=True):
                s_src = str(source_tag.get("src") or "").strip()
                mime = str(source_tag.get("type") or "").strip()
                norm = normalize_url(s_src, base_url=base_url, strip_tracking=True)
                if norm and norm not in seen:
                    seen.add(norm)
                    videos.append(VideoRecord(url=norm, kind="direct", mime_type=mime))

        for iframe in soup.find_all("iframe", src=True):
            if not isinstance(iframe, Tag):
                continue
            i_src = str(iframe.get("src") or "").strip()
            norm = normalize_url(i_src, base_url=base_url, strip_tracking=True)
            if not norm or norm in seen:
                continue
            lowered = norm.lower()
            if any(pattern in lowered for pattern in VIDEO_EMBED_PATTERNS):
                seen.add(norm)
                videos.append(VideoRecord(url=norm, kind="embed"))

        return videos

    @staticmethod
    def _extract_audios(
        soup: BeautifulSoup,
        base_url: str,
        extra_audios: list[AudioRecord] | None = None,
    ) -> list[AudioRecord]:
        audios: list[AudioRecord] = list(extra_audios or [])
        seen: set[str] = {a.url for a in audios}

        for audio_tag in soup.find_all("audio"):
            if not isinstance(audio_tag, Tag):
                continue
            src = str(audio_tag.get("src") or "").strip()
            if src:
                norm = normalize_url(src, base_url=base_url, strip_tracking=True)
                if norm and norm not in seen:
                    seen.add(norm)
                    audios.append(AudioRecord(url=norm))
            for source_tag in audio_tag.find_all("source", src=True):
                s_src = str(source_tag.get("src") or "").strip()
                mime = str(source_tag.get("type") or "").strip()
                norm = normalize_url(s_src, base_url=base_url, strip_tracking=True)
                if norm and norm not in seen:
                    seen.add(norm)
                    audios.append(AudioRecord(url=norm, mime_type=mime))

        return audios

    @staticmethod
    def _extract_embedded_documents(
        soup: BeautifulSoup,
        base_url: str,
        initial_docs: list[DocumentRecord] | None = None,
    ) -> list[DocumentRecord]:
        documents: list[DocumentRecord] = list(initial_docs or [])
        seen: set[str] = {doc.url for doc in documents}

        for tag_name, attr_name in (("embed", "src"), ("object", "data"), ("iframe", "src")):
            for tag in soup.find_all(tag_name):
                if not isinstance(tag, Tag):
                    continue
                raw = str(tag.get(attr_name) or "").strip()
                if not raw:
                    continue
                norm = normalize_url(raw, base_url=base_url, strip_tracking=True)
                if not norm or norm in seen:
                    continue
                ext = get_url_extension(norm)
                if ext in DOCUMENT_EXTENSIONS:
                    seen.add(norm)
                    filename = posixpath.basename(unquote(urlsplit(norm).path)) or f"document{ext}"
                    documents.append(
                        DocumentRecord(
                            url=norm,
                            filename=filename,
                            extension=ext,
                            anchor_text=str(tag.get("title") or ""),
                        )
                    )

        return documents

    # -----------------------------------------------------------------------
    # Extração Profunda ("Raio-X"): Comentários, DOM Oculto, Forms, SPA JSON
    # -----------------------------------------------------------------------
    @staticmethod
    def _extract_html_comments(soup: BeautifulSoup, max_len: int = 800) -> list[str]:
        """Extrai comentários `<!-- ... -->` presentes no código HTML."""
        comments: list[str] = []
        seen: set[str] = set()
        for node in soup.find_all(string=lambda text: isinstance(text, Comment)):
            cleaned = re.sub(r"\s+", " ", str(node)).strip()
            if not cleaned or len(cleaned) < 3:
                continue
            if len(cleaned) > max_len:
                cleaned = cleaned[:max_len] + " … [truncado]"
            if cleaned not in seen:
                seen.add(cleaned)
                comments.append(cleaned)
        return comments

    def _extract_hidden_dom_items(
        self,
        soup: BeautifulSoup,
        browser_snapshots: list[dict[str, str]],
    ) -> list[HiddenDOMItem]:
        """
        Captura textos e conteúdos escondidos por CSS, atributos `hidden`/`aria-hidden`,
        acordeões `<details>`, `<noscript>`, `<template>` e atributos `data-*` informativos.
        """
        items: list[HiddenDOMItem] = []
        seen_texts: set[str] = set()

        def _add_item(reason: str, tag_name: str, hint: str, raw_text: str) -> None:
            cleaned = re.sub(r"\s+", " ", raw_text).strip()
            if len(cleaned) < 4:
                return
            if len(cleaned) > 1500:
                cleaned = cleaned[:1500] + " …"
            dedup_key = cleaned.lower()
            if dedup_key in seen_texts:
                return
            seen_texts.add(dedup_key)
            items.append(HiddenDOMItem(reason=reason, tag=tag_name, selector_hint=hint, text=cleaned))

        # 1. Snapshots capturados ao vivo no browser via `getComputedStyle` (inclui `display:none` de CSS externo!)
        for snap in browser_snapshots:
            _add_item(
                reason=snap.get("reason", "css-computed-hidden"),
                tag_name=snap.get("tag", "div"),
                hint=snap.get("selector", ""),
                raw_text=snap.get("text", ""),
            )

        # 2. Blocos <details> (acordeões e FAQs colapsáveis)
        for details in soup.find_all("details"):
            if not isinstance(details, Tag):
                continue
            _add_item("details-accordion", "details", self._tag_hint(details), details.get_text(" ", strip=True))

        # 3. Elementos com atributo `hidden`, `aria-hidden="true"`, style `display:none` ou classes ocultas
        for el in soup.find_all(True):
            if not isinstance(el, Tag) or el.name in ("script", "style", "svg", "path", "head", "meta", "link"):
                continue

            reason: str | None = None
            if el.has_attr("hidden"):
                reason = "hidden-attribute"
            elif str(el.get("aria-hidden") or "").lower() == "true":
                reason = "aria-hidden"
            elif el.has_attr("style") and INLINE_STYLE_HIDDEN_RE.search(str(el.get("style") or "")):
                reason = "inline-style-hidden"
            else:
                classes = " ".join(el.get("class") or []).lower()
                if classes and any(kw in classes for kw in HIDDEN_CLASS_KEYWORDS):
                    reason = "hidden-css-class"

            if reason:
                txt = el.get_text(" ", strip=True)
                if txt:
                    _add_item(reason, el.name, self._tag_hint(el), txt)

            # Tooltips e textos escondidos em atributos `data-*`
            for data_attr in ("data-tooltip", "data-tippy-content", "data-original-title", "data-content", "data-description"):
                val = str(el.get(data_attr) or "").strip()
                if val:
                    _add_item(f"attr:{data_attr}", el.name, self._tag_hint(el), val)

        # 4. Conteúdo dentro de <template>
        for tpl in soup.find_all("template"):
            if not isinstance(tpl, Tag):
                continue
            tpl_soup = self._make_soup(tpl.decode_contents())
            txt = tpl_soup.get_text(" ", strip=True)
            if txt:
                _add_item("html-template", "template", self._tag_hint(tpl), txt)

        # 5. Conteúdo alternativo dentro de <noscript>
        for ns in soup.find_all("noscript"):
            if not isinstance(ns, Tag):
                continue
            ns_soup = self._make_soup(ns.decode_contents())
            txt = ns_soup.get_text(" ", strip=True)
            if txt:
                _add_item("noscript-fallback", "noscript", self._tag_hint(ns), txt)

        return items

    @staticmethod
    def _tag_hint(tag: Tag) -> str:
        ident = str(tag.get("id") or "").strip()
        if ident:
            return f"{tag.name}#{ident}"
        classes = [str(c).strip() for c in (tag.get("class") or []) if str(c).strip()]
        if classes:
            return f"{tag.name}.{'.'.join(classes[:3])}"
        return tag.name

    @staticmethod
    def _extract_forms(soup: BeautifulSoup, base_url: str) -> list[FormRecord]:
        """Extrai todos os formulários da página, destacando `<input type="hidden">`."""
        forms: list[FormRecord] = []
        for form in soup.find_all("form"):
            if not isinstance(form, Tag):
                continue
            raw_action = str(form.get("action") or "").strip()
            action_url = normalize_url(raw_action, base_url=base_url, strip_tracking=False) or raw_action or base_url
            method = str(form.get("method") or "GET").upper().strip()
            form_id = str(form.get("id") or form.get("name") or "").strip()

            fields: list[FormFieldInfo] = []
            for inp in form.find_all(["input", "select", "textarea"]):
                if not isinstance(inp, Tag):
                    continue
                f_type = str(inp.get("type") or inp.name).lower().strip()
                f_name = str(inp.get("name") or inp.get("id") or "").strip()
                is_hidden = f_type == "hidden" or inp.has_attr("hidden")
                raw_val = "" if f_type == "password" else str(inp.get("value") or "").strip()
                if len(raw_val) > 300:
                    raw_val = raw_val[:300] + "…"
                if f_name or is_hidden:
                    fields.append(FormFieldInfo(name=f_name, field_type=f_type, value=raw_val, hidden=is_hidden))

            forms.append(FormRecord(action=action_url, method=method, form_id=form_id, fields=fields))
        return forms

    def _extract_embedded_states(
        self,
        soup: BeautifulSoup,
        browser_spa_globals: dict[str, Any],
    ) -> list[EmbeddedStateRecord]:
        """
        Extrai dados JSON internos de SPAs (ex: `__NEXT_DATA__`, `__NUXT__`,
        blocos `<script type="application/json">` e variáveis globais capturadas no browser).
        """
        states: list[EmbeddedStateRecord] = []

        for script in soup.find_all("script"):
            if not isinstance(script, Tag):
                continue
            s_id = str(script.get("id") or "").strip()
            s_type = str(script.get("type") or "").lower().strip()

            # Ignora JSON-LD já extraído em `seo.json_ld`
            if "ld+json" in s_type:
                continue

            is_json_script = (
                s_id in ("__NEXT_DATA__", "__NUXT_DATA__")
                or s_type in ("application/json", "application/manifest+json", "importmap")
            )
            if not is_json_script:
                continue

            raw_text = (script.string or script.get_text() or "").strip()
            if not raw_text:
                continue

            parsed = self._safe_parse_json(raw_text)
            preview = self._compact_json_preview(parsed if parsed is not None else raw_text[:600])
            states.append(
                EmbeddedStateRecord(
                    source=s_id or f"script[{s_type}]",
                    script_id=s_id,
                    preview=preview,
                    raw_length=len(raw_text),
                )
            )

        for global_name, global_val in browser_spa_globals.items():
            if global_val is not None:
                raw_serialized = json.dumps(global_val, ensure_ascii=False, default=str)
                states.append(
                    EmbeddedStateRecord(
                        source=f"window.{global_name}",
                        script_id=global_name,
                        preview=self._compact_json_preview(global_val),
                        raw_length=len(raw_serialized),
                    )
                )

        return states

    @staticmethod
    def _compact_json_preview(data: Any, max_chars: int = 4000) -> Any:
        """Mantém estrutura JSON legível truncando apenas se for excessivamente grande."""
        try:
            dumped = json.dumps(data, ensure_ascii=False, default=str)
            if len(dumped) <= max_chars:
                return data
            if isinstance(data, dict):
                summary: dict[str, Any] = {"_truncated": True, "_top_keys": list(data.keys())[:30]}
                for k in list(data.keys())[:8]:
                    val_str = json.dumps(data[k], ensure_ascii=False, default=str)
                    summary[k] = data[k] if len(val_str) <= 500 else f"{val_str[:500]}…"
                return summary
            if isinstance(data, list):
                return {"_truncated": True, "_total_items": len(data), "_sample": data[:3]}
            return dumped[:max_chars] + "…"
        except Exception:
            return str(data)[:max_chars]

    @staticmethod
    def _extract_all_iframes(soup: BeautifulSoup, base_url: str) -> list[str]:
        urls: list[str] = []
        for iframe in soup.find_all("iframe", src=True):
            if isinstance(iframe, Tag):
                norm = normalize_url(str(iframe.get("src") or ""), base_url=base_url, strip_tracking=False)
                if norm:
                    urls.append(norm)
        return deduplicate_preserving_order(urls)

    @staticmethod
    def _extract_scripts_and_styles(soup: BeautifulSoup, base_url: str) -> tuple[list[str], list[str]]:
        scripts: list[str] = []
        styles: list[str] = []

        for s in soup.find_all("script", src=True):
            if isinstance(s, Tag):
                norm = normalize_url(str(s.get("src") or ""), base_url=base_url, strip_tracking=False)
                if norm:
                    scripts.append(norm)

        for link in soup.find_all("link", href=True):
            if isinstance(link, Tag):
                rels = [str(r).lower() for r in (link.get("rel") or [])]
                if "stylesheet" in rels:
                    norm = normalize_url(str(link.get("href") or ""), base_url=base_url, strip_tracking=False)
                    if norm:
                        styles.append(norm)

        return deduplicate_preserving_order(scripts), deduplicate_preserving_order(styles)

    # -----------------------------------------------------------------------
    # Geração de Markdown Limpo + Secção Raio-X
    # -----------------------------------------------------------------------
    def _extract_clean_markdown(
        self,
        *,
        raw_html: str,
        soup: BeautifulSoup,
        base_url: str,
        fallback_title: str,
    ) -> tuple[str, str]:
        if self.markdown_engine in ("hybrid", "trafilatura") and trafilatura is not None:
            try:
                traf_md = trafilatura.extract(
                    raw_html,
                    url=base_url,
                    output_format="markdown",
                    include_formatting=True,
                    include_links=True,
                    include_images=True,
                    include_tables=True,
                    favor_recall=True,
                )
                if traf_md:
                    cleaned_traf = self._normalize_markdown_whitespace(traf_md)
                    if self.markdown_engine == "trafilatura" or len(cleaned_traf) >= 160:
                        return self._ensure_top_heading(cleaned_traf, fallback_title), "trafilatura"
            except Exception as exc:
                logger.debug(f"Trafilatura falhou em {base_url}, a usar fallback BS4+Markdownify: {exc}")

        bs4_md = self._extract_with_bs4_markdownify(soup=soup, base_url=base_url)
        cleaned_bs4 = self._normalize_markdown_whitespace(bs4_md)
        return self._ensure_top_heading(cleaned_bs4, fallback_title), "markdownify"

    @staticmethod
    def _extract_with_bs4_markdownify(soup: BeautifulSoup, base_url: str) -> str:
        working_soup = PageExtractor._make_soup(str(soup))

        for comment in working_soup.find_all(string=lambda text: isinstance(text, Comment)):
            comment.extract()

        for selector in NOISE_SELECTORS:
            for node in working_soup.select(selector):
                node.decompose()

        main_container = (
            working_soup.find("main")
            or working_soup.find(attrs={"role": "main"})
            or working_soup.find("article")
        )

        if isinstance(main_container, Tag) and len(main_container.get_text(strip=True)) >= 120:
            target_root: Tag = main_container
        else:
            body = working_soup.body or working_soup
            for peripheral in body.find_all(["nav", "footer", "aside"]):
                peripheral.decompose()
            target_root = body

        for a_tag in target_root.find_all("a", href=True):
            norm = normalize_url(str(a_tag.get("href", "")), base_url=base_url, strip_tracking=True)
            if norm:
                a_tag["href"] = norm

        for img_tag in target_root.find_all("img"):
            best = PageExtractor._pick_best_image_url(img_tag)
            if best:
                norm_img = normalize_url(best, base_url=base_url, strip_tracking=True)
                if norm_img:
                    img_tag["src"] = norm_img

        return md_convert(
            str(target_root),
            heading_style="ATX",
            bullets="-",
            strip=["script", "style"],
        )

    @staticmethod
    def _build_xray_markdown_appendix(
        *,
        seo: SEOMetadata,
        xray: DeepXRayData,
        documents: list[DocumentRecord],
    ) -> str:
        """Constrói um apêndice Markdown estruturado com todos os dados ocultos/técnicos encontrados."""
        sections: list[str] = []

        if xray.contacts.emails or xray.contacts.phones:
            lines = ["### Contactos Detetados"]
            for em in xray.contacts.emails:
                lines.append(f"- **Email:** `{em}`")
            for ph in xray.contacts.phones:
                lines.append(f"- **Telefone / WhatsApp:** `{ph}`")
            sections.append("\n".join(lines))

        if xray.hidden_dom_items:
            lines = ["### Elementos Ocultos no DOM / CSS (`hidden`, `display:none`, Acordeões, `<template>`, `<noscript>`)"]
            for item in xray.hidden_dom_items[:40]:
                lines.append(f"- **[{item.reason}]** `{item.selector_hint}`: {item.text}")
            if len(xray.hidden_dom_items) > 40:
                lines.append(f"- *(+{len(xray.hidden_dom_items) - 40} elementos ocultos adicionais em `dataset.jsonl`)*")
            sections.append("\n".join(lines))

        if xray.html_comments:
            lines = ["### Comentários HTML (`<!-- ... -->`)"]
            for cmt in xray.html_comments[:25]:
                lines.append(f"- `{cmt}`")
            sections.append("\n".join(lines))

        hidden_fields_summary: list[str] = []
        for form in xray.forms:
            hidden_inputs = [f for f in form.fields if f.hidden]
            if hidden_inputs:
                fields_str = ", ".join(f"`{inp.name}={inp.value}`" for inp in hidden_inputs[:15])
                hidden_fields_summary.append(
                    f"- **Form `{form.method}`** ({form.action}) — Campos `hidden`: {fields_str}"
                )
        if hidden_fields_summary:
            sections.append("### Formulários & Campos Invisíveis (`input[type=hidden]`)\n" + "\n".join(hidden_fields_summary))

        if xray.embedded_json_states:
            lines = ["### Estado JSON Interno / Hydration de SPA"]
            for st in xray.embedded_json_states:
                preview_json = json.dumps(st.preview, ensure_ascii=False, indent=2)
                if len(preview_json) > 1200:
                    preview_json = preview_json[:1200] + "\n  // ... (completo em dataset.jsonl)"
                lines.append(f"#### `{st.source}` ({st.raw_length} bytes)\n```json\n{preview_json}\n```")
            sections.append("\n".join(lines))

        if xray.network_apis:
            lines = ["### APIs XHR / Fetch / GraphQL Intercetadas no Carregamento"]
            for api in xray.network_apis[:30]:
                lines.append(f"- **`{api.method}` [{api.status_code}]** `{api.url}` (`{api.content_type}`)")
            sections.append("\n".join(lines))

        if documents:
            lines = ["### Documentos Descarregáveis Encontrados"]
            for doc in documents:
                lines.append(f"- [{doc.filename}]({doc.url}) ({doc.anchor_text or doc.extension})")
            sections.append("\n".join(lines))

        if seo.alternate_links or xray.iframes:
            lines = ["### Feeds, Alternates & Iframes Embutidos"]
            for alt in seo.alternate_links:
                lines.append(f"- **Link `{alt['rel']}`** ({alt['type'] or alt['hreflang']}): {alt['href']}")
            for ifr in xray.iframes:
                lines.append(f"- **Iframe:** {ifr}")
            sections.append("\n".join(lines))

        if not sections:
            return ""

        return "## Raio-X: Conteúdo Oculto, Estado Interno & APIs\n\n" + "\n\n".join(sections)

    @staticmethod
    def _normalize_markdown_whitespace(markdown_text: str) -> str:
        lines = [line.rstrip() for line in markdown_text.splitlines()]
        joined = "\n".join(lines)
        joined = re.sub(r"\n{3,}", "\n\n", joined)
        return joined.strip()

    @staticmethod
    def _ensure_top_heading(markdown_text: str, fallback_title: str) -> str:
        if not fallback_title:
            return markdown_text
        if re.search(r"^#\s+\S+", markdown_text, flags=re.MULTILINE):
            return markdown_text
        if not markdown_text:
            return f"# {fallback_title}"
        return f"# {fallback_title}\n\n{markdown_text}"
