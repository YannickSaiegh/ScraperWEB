# Full-Site Web Scraper & Extractor

Web crawler recursivo (*Same-Domain*) e extrator completo de dados construído em **Python assíncrono** para sites estáticos e modernos (**SPAs em React, Next.js, Vue**, scroll infinito e *lazy-loading*).

Combina **Playwright (`async_playwright`)** para renderização JavaScript e interação dinâmica com **BeautifulSoup4**, **Trafilatura** e **Markdownify** para limpeza de *boilerplate* e conversão fiel do conteúdo principal para **Markdown**, preservando títulos, listas e tabelas.

---

## Arquitetura do Projeto

```text
full-site-scraper/
├── main.py          # CLI (Typer) + barra de progresso e painel em tempo real (Rich + Loguru)
├── crawler.py       # Motor assíncrono Playwright, Semaphore, rate-limiting, retries, auto-scroll e robots.txt
├── extractor.py     # Extração de SEO, Open Graph, JSON-LD, links, imagens, vídeos, documentos e Markdown
├── storage.py       # Escrita assíncrona em dataset.jsonl, árvore pages/*.md, assets/* e sitemap_extracted.json
├── utils.py         # Normalização de URLs, filtro de parâmetros utm_*, User-Agents e caminhos seguros
├── requirements.txt # Dependências do projeto
└── README.md        # Documentação e guia de utilização
```

### Fluxo de Execução

1. **Descoberta & Normalização (`utils.py`)**: Converte URLs relativas em absolutas, remove fragmentos (`#...`), limpa parâmetros de rastreamento (`utm_*`, `fbclid`, `gclid`, `msclkid`, etc.), ordena parâmetros legítimos e evita duplicados com conjuntos de URLs visitadas/agendadas.
2. **Navegação & Interação Dinâmica (`crawler.py`)**: Abre páginas concorrentes controladas por `asyncio.Semaphore`, aplica rotação de `User-Agent` e *viewport* de desktop, executa **auto-scroll suave até ao fundo da página** para acionar *lazy-loading* / *infinite scroll* e aguarda estabilização da rede (`networkidle`).
3. **Extração Estruturada (`extractor.py`)**:
   - **SEO & Semântica**: `title`, `meta description`, `canonical`, `robots`, `Open Graph`, `Twitter Cards`, `JSON-LD / Schema.org` e hierarquia de cabeçalhos (`H1`–`H3`).
   - **Conteúdo**: Texto principal limpo em **Markdown** (modo híbrido `trafilatura` + fallback `BeautifulSoup4`/`markdownify`) e **HTML bruto** renderizado.
   - **Grafo & Multimédia**: Links internos e externos, imagens (com `alt` e `title`), vídeos (`<video>`, `<source>` e *embeds*) e documentos descarregáveis (`.pdf`, `.csv`, `.xlsx`, `.docx`, `.zip`, etc.).
4. **Persistência (`storage.py`)**: Escrita incremental *crash-safe* em `dataset.jsonl`, ficheiros `.md` espelhados em `pages/`, download opcional de ficheiros mantendo a estrutura de pastas em `assets/` e geração final do `sitemap_extracted.json`.

---

## Instalação

### 1. Criar e ativar um ambiente virtual

**Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Linux / macOS:**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Instalar as dependências Python

```bash
pip install -r requirements.txt
```

### 3. Instalar o navegador Chromium do Playwright

```bash
playwright install chromium
```

> Em distribuições Linux de servidor (Ubuntu/Debian), caso faltem bibliotecas nativas do sistema, execute também:
> `playwright install-deps chromium`

---

## Exemplos de Execução via CLI

Ver todas as opções disponíveis:

```bash
python main.py --help
```

### 1. Extração completa padrão de um domínio (respeitando `robots.txt`)

```bash
python main.py https://quotes.toscrape.com/js/
```

### 2. Extração com download local de imagens, vídeos e documentos (`.pdf`, `.csv`, `.xlsx`, etc.)

```bash
python main.py https://books.toscrape.com --download-assets --max-pages 50 --concurrency 5
```

### 3. Controlo fino de concorrência, *rate limiting*, profundidade e `robots.txt`

```bash
python main.py https://exemplo.pt \
  --concurrency 3 \
  --min-delay 1.5 \
  --max-delay 3.5 \
  --max-depth 3 \
  --ignore-robots \
  --download-assets \
  --output-dir ./output
```

### 4. Modo de depuração visual (`--headed` + logs `DEBUG`)

```bash
python main.py https://exemplo.pt --max-pages 5 --headed --verbose
```

### Principais Argumentos CLI

| Flag | Predefinição | Descrição |
| :--- | :--- | :--- |
| `URL` | *(obrigatório)* | URL inicial do domínio alvo |
| `-o, --output-dir` | `./output` | Diretório base de saída |
| `-c, --concurrency` | `4` | Abas simultâneas no Playwright (`asyncio.Semaphore`) |
| `--min-delay` / `--max-delay` | `0.8` / `2.2` | Intervalo de pausa aleatória (segundos) entre pedidos |
| `-m, --max-pages` | `0` *(ilimitado)* | Número máximo de páginas a extrair |
| `-d, --max-depth` | `-1` *(ilimitado)* | Profundidade máxima de navegação recursiva |
| `--download-assets` | `--no-download-assets` | Faz download local de imagens, vídeos diretos e documentos em `assets/` |
| `--respect-robots` / `--ignore-robots` | `--respect-robots` | Respeita ou ignora regras de `robots.txt` |
| `--include-subdomains` | `--same-host-only` | Inclui subdomínios (`*.dominio.pt`) no rastreio |
| `--raw-html` / `--no-raw-html` | `--raw-html` | Inclui ou omite o HTML bruto em `dataset.jsonl` |
| `--markdown-engine` | `hybrid` | Motor de conversão Markdown (`hybrid`, `trafilatura` ou `markdownify`) |
| `--max-retries` | `3` | Tentativas máximas por URL com *exponential backoff* |
| `--timeout` | `30.0` | Timeout de carregamento por página (segundos) |
| `--max-scrolls` | `15` | Passos máximos de auto-scroll para páginas com *infinite scroll* |
| `--headless` / `--headed` | `--headless` | Executa o browser em background ou com janela visível |
| `-v, --verbose` | `False` | Ativa logs detalhados (`DEBUG`) |

---

## Estrutura de Saída (`output/<dominio>/<dominio>_<data_hora>/`)

Cada execução cria automaticamente uma subpasta exclusiva dentro da pasta do projeto (`output/<dominio>/<dominio>_<YYYY-MM-DD_HH-MM-SS>/`), garantindo que os resultados ficam sempre organizados dentro do diretório do scraper sem sobrescrever extrações anteriores:

```text
output/
└── exemplo.pt/
    └── exemplo.pt_2026-11-24_19-55-00/
        ├── dataset.jsonl             # 1 registo JSON completo por linha (visível + oculto)
        ├── xray_hidden_report.json   # Relatório consolidado: DOM/CSS oculto, APIs XHR/Fetch, comentários, forms e contactos
        ├── sitemap_extracted.json    # Resumo de métricas + mapa de URLs (incluindo órfãs via sitemap.xml), status HTTP e erros
        ├── pages/                    # Ficheiros Markdown espelhando as rotas (+ secção Raio-X no fim de cada página)
        │   ├── index.md
        │   ├── sobre.md
        │   └── blog/
        │       └── artigo-1.md
        └── assets/                   # Criado quando --download-assets está ativo (inclui CSS background-images)
            ├── docs/
            │   └── relatorio-2026.pdf
            ├── img/
            │   └── banner.webp
            └── _external/            # Assets servidos a partir de CDNs externos
                └── cdn.exemplo.pt/
                    └── logo.svg
```

### O que o modo Raio-X (`--deep-xray`, ativo por predefinição) revela

- **Elementos ocultos por DOM e CSS externo:** Textos escondidos com `display: none`, `visibility: hidden`, `opacity: 0` (detetados em tempo real com `window.getComputedStyle`), atributos `hidden`, `aria-hidden="true"`, classes `sr-only`/`visually-hidden`, tooltips `data-*`, acordeões `<details>` (automaticamente expandidos), `<template>` e `<noscript>`.
- **Comentários HTML (`<!-- ... -->`):** Todos os comentários deixados no código-fonte de cada página.
- **Estado interno / Hydration de SPAs:** Extração automática de `__NEXT_DATA__` (Next.js), `__NUXT__`, `__INITIAL_STATE__`, `__APOLLO_STATE__` e blocos `<script type="application/json">`.
- **APIs XHR / Fetch / GraphQL:** Interceção passiva de todos os pedidos assíncronos que a página faz em background enquanto carrega e faz scroll, incluindo amostra JSON da resposta.
- **Formulários & Inputs invisíveis:** Estrutura de todos os `<form>` e respetivos campos `<input type="hidden">`.
- **Páginas órfãs & Links escondidos:** Descoberta automática de URLs públicas via `sitemap.xml` / `sitemap_index.xml` (mesmo sem link nos menus), links em atributos `data-href`/`data-url`, imagens em `background-image` CSS (incluindo `::before`/`::after`), áudios, iframes e contactos `mailto:`/`tel:`.

### Formato de cada linha em `dataset.jsonl`

```json
{
  "url": "https://exemplo.pt/artigos/guia-dados",
  "final_url": "https://exemplo.pt/artigos/guia-dados",
  "status_code": 200,
  "content_type": "text/html; charset=utf-8",
  "crawled_at": "2026-11-24T19:30:00+00:00",
  "depth": 1,
  "response_time_ms": 1420,
  "seo": {
    "title": "Guia de Engenharia de Dados",
    "meta_description": "Boas práticas de recolha e estruturação de dados.",
    "canonical_url": "https://exemplo.pt/artigos/guia-dados",
    "language": "pt-PT",
    "robots": "index, follow",
    "author": "Equipa Dados",
    "keywords": ["dados", "scraping", "python"],
    "open_graph": { "og:title": "Guia de Engenharia de Dados", "og:type": "article" },
    "twitter_cards": {},
    "json_ld": [{ "@context": "https://schema.org", "@type": "Article" }],
    "headings": { "h1": ["Guia de Engenharia de Dados"], "h2": ["Arquitetura"] }
  },
  "markdown": "# Guia de Engenharia de Dados\n\nConteúdo limpo...",
  "markdown_engine_used": "trafilatura",
  "word_count": 842,
  "internal_links": [{ "url": "https://exemplo.pt/contacto", "anchor_text": "Contacto", "rel": [] }],
  "external_links": [{ "url": "https://python.org", "anchor_text": "Python", "rel": ["noopener"] }],
  "images": [{ "url": "https://exemplo.pt/img/capa.webp", "alt": "Diagrama", "title": "", "local_path": "assets/img/capa.webp" }],
  "videos": [],
  "documents": [{ "url": "https://exemplo.pt/docs/anexo.pdf", "filename": "anexo.pdf", "extension": ".pdf", "anchor_text": "Download PDF", "local_path": "assets/docs/anexo.pdf" }],
  "markdown_relpath": "pages/artigos/guia-dados.md",
  "raw_html": "<!DOCTYPE html><html>...</html>"
}
```
