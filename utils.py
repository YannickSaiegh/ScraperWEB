"""
Utilitários transversais do Web Scraper & Crawler:
- Normalização de URLs e remoção de parâmetros de rastreamento (utm_*, fbclid, etc.)
- Validação de domínio (Same-Domain) e classificação de extensões de ficheiros
- Conversão segura de URLs para caminhos locais (compatível com Windows e Linux/macOS)
- Pool de User-Agents realistas e viewports de desktop
- Configuração integrada de logs coloridos com Loguru + Rich
"""

from __future__ import annotations

import hashlib
import posixpath
import random
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from loguru import logger
from rich.console import Console

# Consola partilhada entre o Loguru e as barras de progresso Rich
console = Console(stderr=True)

# ---------------------------------------------------------------------------
# User-Agents realistas (Desktop: Windows, macOS, Linux) e Viewports padrão
# ---------------------------------------------------------------------------
USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.7; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
]

DESKTOP_VIEWPORTS: list[dict[str, int]] = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]

# ---------------------------------------------------------------------------
# Parâmetros de rastreamento / analytics a descartar na normalização
# ---------------------------------------------------------------------------
TRACKING_PARAM_PREFIXES: tuple[str, ...] = (
    "utm_",
    "pk_",
    "mtm_",
    "matomo_",
    "hsa_",
    "vero_",
    "gad_",
)

TRACKING_PARAM_NAMES: frozenset[str] = frozenset(
    {
        "fbclid",
        "gclid",
        "dclid",
        "gbraid",
        "wbraid",
        "msclkid",
        "yclid",
        "twclid",
        "ttclid",
        "li_fat_id",
        "mc_cid",
        "mc_eid",
        "_ga",
        "_gl",
        "_hsenc",
        "_hsmi",
        "mkt_tok",
        "igshid",
        "si",
        "spm",
        "ref",
        "ref_src",
        "ref_url",
        "source",
    }
)

# ---------------------------------------------------------------------------
# Classificação de extensões de ficheiros e assets
# ---------------------------------------------------------------------------
DOCUMENT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf",
        ".csv",
        ".xlsx",
        ".xls",
        ".doc",
        ".docx",
        ".ppt",
        ".pptx",
        ".odt",
        ".ods",
        ".odp",
        ".rtf",
        ".txt",
        ".zip",
        ".rar",
        ".7z",
        ".tar",
        ".gz",
        ".json",
        ".xml",
        ".epub",
    }
)

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".svg",
        ".avif",
        ".bmp",
        ".ico",
        ".tif",
        ".tiff",
    }
)

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".webm",
        ".ogg",
        ".ogv",
        ".mov",
        ".avi",
        ".mkv",
        ".m4v",
        ".m3u8",
    }
)

NON_HTML_ONLY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp3",
        ".wav",
        ".flac",
        ".aac",
        ".m4a",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".css",
        ".js",
        ".mjs",
        ".map",
        ".wasm",
        ".dmg",
        ".exe",
        ".msi",
        ".deb",
        ".rpm",
        ".apk",
        ".iso",
    }
)

# Extensões que não devem ser abertas como páginas HTML no Playwright
SKIP_CRAWL_EXTENSIONS: frozenset[str] = (
    DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | NON_HTML_ONLY_EXTENSIONS
)

IGNORED_SCHEMES: tuple[str, ...] = (
    "mailto:",
    "tel:",
    "sms:",
    "javascript:",
    "data:",
    "blob:",
    "whatsapp:",
    "skype:",
    "about:",
    "ftp:",
)

WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)

INVALID_FS_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def setup_logging(verbose: bool = False) -> None:
    """Configura o Loguru para escrever logs coloridos em harmonia com o Rich."""
    logger.remove()
    level = "DEBUG" if verbose else "INFO"

    def _rich_sink(message: str) -> None:
        console.print(message.rstrip("\n"), highlight=False, soft_wrap=True)

    logger.add(
        _rich_sink,
        level=level,
        colorize=True,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{module}</cyan>:<cyan>{function}</cyan> - "
            "<level>{message}</level>"
        ),
    )


def get_random_user_agent() -> str:
    """Retorna um User-Agent realista de Desktop escolhido aleatoriamente."""
    return random.choice(USER_AGENTS)


def get_random_viewport() -> dict[str, int]:
    """Retorna uma resolução de viewport padrão de Desktop."""
    return random.choice(DESKTOP_VIEWPORTS).copy()


def is_tracking_param(param_name: str) -> bool:
    """Verifica se um parâmetro de query string pertence a rastreamento/analytics."""
    lowered = param_name.strip().lower()
    if lowered in TRACKING_PARAM_NAMES:
        return True
    return lowered.startswith(TRACKING_PARAM_PREFIXES)


def normalize_url(
    url: str,
    base_url: str | None = None,
    strip_tracking: bool = True,
) -> str | None:
    """
    Normaliza uma URL:
    1. Resolve URLs relativas face a `base_url` (se fornecida).
    2. Ignora esquemas não-HTTP (`mailto:`, `javascript:`, `tel:`, etc.).
    3. Converte esquema e hostname para minúsculas e remove portas padrão (:80, :443).
    4. Remove fragmentos (`#section`), normaliza o path e remove barra final redundante.
    5. Remove parâmetros `utm_*` / tracking e ordena os restantes parâmetros alfabeticamente.
    """
    if not url:
        return None

    raw = url.strip()
    if not raw or raw.startswith("#"):
        return None

    lowered_raw = raw.lower()
    if lowered_raw.startswith(IGNORED_SCHEMES):
        return None

    resolved = urljoin(base_url, raw) if base_url else raw

    try:
        parts = urlsplit(resolved)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None

    if not parts.hostname:
        return None

    hostname = parts.hostname.lower().rstrip(".")
    try:
        port = parts.port
    except ValueError:
        return None

    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{hostname}:{port}"
    else:
        netloc = hostname

    # Normalização segura do path POSIX
    raw_path = parts.path or "/"
    normalized_path = posixpath.normpath(raw_path)
    if not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path
    # Evita duplicados entre /pagina e /pagina/ (mantendo "/" na raiz)
    if normalized_path != "/" and normalized_path.endswith("/"):
        normalized_path = normalized_path.rstrip("/")

    # Filtragem e ordenação determinística da query string
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    if strip_tracking:
        query_pairs = [(k, v) for k, v in query_pairs if not is_tracking_param(k)]
    query_pairs.sort(key=lambda pair: (pair[0], pair[1]))
    clean_query = urlencode(query_pairs, doseq=True)

    # Fragmento descartado intencionalmente ("")
    return urlunsplit((scheme, netloc, normalized_path, clean_query, ""))


def extract_domain(url: str, strip_www: bool = True) -> str:
    """Extrai o domínio (hostname) limpo a partir de uma URL."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if strip_www and host.startswith("www."):
        return host[4:]
    return host


def is_same_domain(
    candidate_url: str,
    target_domain: str,
    include_subdomains: bool = False,
) -> bool:
    """
    Verifica se `candidate_url` pertence ao mesmo domínio base de `target_domain`.
    Por omissão aceita `<dominio>` e `www.<dominio>`.
    Se `include_subdomains=True`, aceita também `*.<dominio>`.
    """
    base_target = target_domain.lower().removeprefix("www.")
    candidate_host = extract_domain(candidate_url, strip_www=False)
    if not candidate_host or not base_target:
        return False

    if candidate_host == base_target or candidate_host == f"www.{base_target}":
        return True

    if include_subdomains and candidate_host.endswith(f".{base_target}"):
        return True

    return False


def get_url_extension(url: str) -> str:
    """Obtém a extensão em minúsculas do caminho da URL (ex: '.pdf', '.jpg' ou '')."""
    path = unquote(urlsplit(url).path or "")
    _, ext = posixpath.splitext(path)
    return ext.lower()


def is_crawlable_page_url(url: str) -> bool:
    """Retorna True se a URL aparentar ser uma página navegável (e não um ficheiro binário/asset)."""
    ext = get_url_extension(url)
    return ext not in SKIP_CRAWL_EXTENSIONS


def sanitize_path_segment(segment: str, max_len: int = 100) -> str:
    """Limpa um segmento de caminho para ser seguro em Windows, macOS e Linux."""
    decoded = unquote(segment).strip()
    cleaned = INVALID_FS_CHARS_RE.sub("_", decoded)
    cleaned = cleaned.strip(" .")
    if not cleaned:
        cleaned = "_"

    # Evita nomes reservados do Windows (CON, NUL, AUX, COM1, etc.)
    stem = cleaned.split(".")[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"

    if len(cleaned) > max_len:
        name_part, dot, ext_part = cleaned.rpartition(".")
        if dot and len(ext_part) <= 10:
            short_hash = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:8]
            cleaned = f"{name_part[: max_len - len(ext_part) - 10]}_{short_hash}.{ext_part}"
        else:
            short_hash = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:8]
            cleaned = f"{cleaned[: max_len - 9]}_{short_hash}"

    return cleaned


def url_to_markdown_relpath(url: str) -> Path:
    """
    Converte uma URL numa rota relativa `.md` espelhando a estrutura do site.
    Exemplos:
      - https://exemplo.pt/                  -> index.md
      - https://exemplo.pt/sobre             -> sobre.md
      - https://exemplo.pt/blog/post-1       -> blog/post-1.md
      - https://exemplo.pt/produtos?page=2   -> produtos__q_a1b2c3d4.md
    """
    parts = urlsplit(url)
    raw_segments = [seg for seg in (parts.path or "/").split("/") if seg and seg not in (".", "..")]

    if not raw_segments:
        segments = ["index"]
    else:
        segments = [sanitize_path_segment(seg) for seg in raw_segments]
        # Remove extensão .html/.htm/.php redundante no último segmento antes de acrescentar .md
        last_lower = segments[-1].lower()
        for html_ext in (".html", ".htm", ".php", ".asp", ".aspx", ".jsp"):
            if last_lower.endswith(html_ext) and len(segments[-1]) > len(html_ext):
                segments[-1] = sanitize_path_segment(segments[-1][: -len(html_ext)])
                break

    if parts.query:
        q_hash = hashlib.sha1(parts.query.encode("utf-8")).hexdigest()[:8]
        segments[-1] = f"{segments[-1]}__q_{q_hash}"

    if not segments[-1].lower().endswith(".md"):
        segments[-1] = f"{segments[-1]}.md"

    return Path(*segments)


def url_to_asset_relpath(
    asset_url: str,
    target_domain: str,
    default_ext: str = ".bin",
) -> Path:
    """
    Converte a URL de um asset (imagem, vídeo ou documento) num caminho local
    dentro de `assets/`, preservando a estrutura de pastas do site.
    Assets alojados em CDNs/domínios externos ficam organizados em `assets/_external/<host>/...`.
    """
    parts = urlsplit(asset_url)
    raw_segments = [seg for seg in (parts.path or "/").split("/") if seg and seg not in (".", "..")]

    if not raw_segments:
        url_hash = hashlib.sha1(asset_url.encode("utf-8")).hexdigest()[:10]
        raw_segments = [f"asset_{url_hash}{default_ext}"]

    safe_segments = [sanitize_path_segment(seg) for seg in raw_segments]

    # Garante extensão caso a URL termine sem extensão explícita
    _, ext = posixpath.splitext(safe_segments[-1])
    if not ext and default_ext:
        ext_clean = default_ext if default_ext.startswith(".") else f".{default_ext}"
        safe_segments[-1] = f"{safe_segments[-1]}{ext_clean}"

    # Se tiver query string (ex: variantes de tamanho/formato em CDNs), diferencia por hash curto
    if parts.query:
        stem, dot, suffix = safe_segments[-1].rpartition(".")
        q_hash = hashlib.sha1(parts.query.encode("utf-8")).hexdigest()[:6]
        if dot:
            safe_segments[-1] = f"{stem}_{q_hash}.{suffix}"
        else:
            safe_segments[-1] = f"{safe_segments[-1]}_{q_hash}"

    if is_same_domain(asset_url, target_domain, include_subdomains=False):
        return Path(*safe_segments)

    host_segment = sanitize_path_segment(extract_domain(asset_url, strip_www=False) or "external")
    return Path("_external", host_segment, *safe_segments)


def deduplicate_preserving_order(items: Iterable[str]) -> list[str]:
    """Remove duplicados de uma sequência de strings mantendo a ordem de descoberta."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
