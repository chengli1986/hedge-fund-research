# GMIA Design Spec — Global Market Insight Aggregator

## Overview

Track research articles from 5 top hedge funds, fetch full content (PDF/HTML), generate bilingual LLM summaries with theme tags, and publish a dashboard page on docs.sinostor.com.cn.

## Sources

| Fund | Fetch Method | Content Method | Content Type |
|------|-------------|---------------|-------------|
| Man Group | SSR (requests+BS4) | Scrape HTML article body | Text |
| Bridgewater | SSR (requests+BS4) | **Index only** (gated) | None |
| AQR | Playwright (CSR) | Scrape HTML article body | Text |
| GMO | JSON API | Download PDF (direct link) | PDF |
| Oaktree | Playwright (CSR) | Download PDF (openPDF() JS) | PDF |

## Architecture — 4-Stage Pipeline

```
cron-wrapper.sh (daily, ~05:00 BJT)
  ├─ Stage 1: fetch_articles.py     → data/articles.jsonl (metadata)
  ├─ Stage 2: fetch_content.py      → content/{id}.pdf or content/{id}.txt
  ├─ Stage 3: analyze_articles.py   → updates articles.jsonl (summaries + themes)
  └─ Stage 4: publish.py            → /var/www/overview/hedge-fund-research.html
```

Each stage is an independent script. Stages run sequentially via a wrapper script. Any stage can be re-run individually. **Each stage boundary has a validation gate** — invalid data never flows downstream.

### Stage 1: fetch_articles.py (existing)

- Scrapes article metadata (title, date, URL) from each source
- Deduplicates by URL hash
- Appends new articles to `data/articles.jsonl`
- Fields: `id, source_id, source_name, title, url, date, fetched_at, summarized`

**Source identity validation** (exit gate):
- Every fetched article URL must match the source's expected hostname (e.g., AQR articles must come from `aqr.com`, not `oaktreecapital.com`)
- Validation rules defined per source in `sources.json` via `expected_hostname` field
- On mismatch: reject the article, log a `SOURCE_MISMATCH` warning, do not write to JSONL
- This prevents cross-source contamination from redirects, site changes, or scraper bugs

### Stage 2: fetch_content.py (new)

Downloads full article content for unsummarized articles.

**Per-source strategies:**

- **GMO**: Fetch article HTML page (cookie: `GMO_region=NorthAmerica`), extract PDF href via regex `href="([^"]+\.pdf)"`, download to `content/{id}.pdf`
- **Oaktree**: Fetch article HTML page via Playwright, parse `openPDF('url')` JavaScript calls, download PDF to `content/{id}.pdf`
- **AQR**: Fetch article page via Playwright, extract article body from `p.article__summary` or full page content section, save to `content/{id}.txt`
- **Man Group**: Fetch article page via requests, extract `div.teaser__content` or main article body, save to `content/{id}.txt`
- **Bridgewater**: Skip (index only)

**PDF text extraction**: `pdfplumber` library for PDF → text conversion.

**Hard validation gates** (per fetch):
- HTTP status must be 2xx; non-2xx → skip article, log `FETCH_FAILED`
- PDF downloads: verify `content-type` contains `application/pdf` and file size > 1KB
- GMO JSON API: validate response is valid JSON with expected `listing` array schema; HTML error pages (e.g., "Something Went Wrong") must be detected and rejected
- HTML content: verify response contains expected source-specific content markers (not a generic error page or CAPTCHA)
- On any validation failure: do not write content file, set `content_status: "failed"` in JSONL, prevent Stage 3 from processing this article

**Content normalization** (before saving):
- HTML sources (AQR, Man Group): extract only the article body using source-specific CSS selectors, strip navigation, footer, legal text, cookie banners, modals, and pagination controls
- Save clean text only — no site chrome, no HTML tags in `.txt` output
- PDF sources (GMO, Oaktree): extract text via pdfplumber, strip headers/footers/page numbers
- This ensures Stage 3 (LLM) receives clean, structured input regardless of source, reducing token waste and improving analysis consistency across model fallbacks

**Output**: Sets `content_path` and `content_status` fields in articles.jsonl for each processed article.

### Stage 3: analyze_articles.py (new)

LLM deep analysis of article content, generating bilingual summaries.

**Model priority** (multi-model fallback):
1. Gemini 2.5 Pro (free tier)
2. GPT-4.1 Mini
3. Claude Sonnet

**Per-article prompt produces:**
- `summary_en`: English summary (2-3 sentences, key thesis + implications)
- `summary_zh`: Chinese summary (2-3 sentences, same content)
- `themes`: 1-3 theme tags from predefined list (see below)
- `key_takeaway_en`: One-line English takeaway
- `key_takeaway_zh`: One-line Chinese takeaway

**Predefined theme tags** (~15):
`AI/Tech`, `Macro/Rates`, `Oil/Energy`, `Credit/Fixed Income`, `Equities/Value`, `China/EM`, `Risk/Volatility`, `Geopolitics`, `ESG/Climate`, `Quant/Factor`, `Asset Allocation`, `Crypto/Digital`, `Real Estate`, `Private Markets`, `Behavioral/Sentiment`

**Incremental processing**: Only analyze articles where `summarized: false` AND `content_status: "ok"`. After successful analysis, set `summarized: true` and write summary fields back to articles.jsonl.

**Input validation**: Refuse to analyze articles with `content_status: "failed"` or missing content files. These are treated as index-only until the next fetch cycle retries them.

**Cost control**: Log model used + token count per article. Skip articles with no content (Bridgewater) or failed content fetch.

### Stage 4: publish.py (new)

Generates a static HTML page at `/var/www/overview/hedge-fund-research.html`.

**Page structure:**

#### Header Bar
- Title: "Hedge Fund Research Insights"
- Stats: total articles | new this week | 5 funds tracked
- Last updated timestamp
- CN/EN toggle button (same pattern as existing docs pages)

#### Section 1: Latest Research (Timeline)
- All articles sorted by date descending
- Each entry: `[Fund badge] Date | English title | Chinese summary (expandable)`
- Badge colors: Man=blue, Bridgewater=orange, AQR=green, GMO=purple, Oaktree=red
- Bridgewater entries show "Index only" tag, no summary
- Default: 20 items visible, "Load more" button for rest
- Click title → opens original article URL

#### Section 2: By Fund
- 5 cards in a responsive grid
- Card header: Fund name + one-line description + notable authors
- Card body: Latest 5 articles (title + date + summary preview)
- Click article → original URL

#### Section 3: Theme Tracker
- Group articles by theme tags
- Each theme section: theme name + article count + list of articles from different funds
- Enables cross-fund comparison on same topic (e.g., "3 funds published on AI this month")

**Template**: Dark GitHub-style theme matching existing docs.sinostor.com.cn pages. Use `gstack.html` or `index.html` as CSS reference.

**Post-generation**: Run `gzip -k -f` on the output file. Update sidebar nav in `index.html` if not already present.

## Cron Schedule

```
# GMIA daily fetch + analyze + publish (05:00 BJT = 21:00 UTC)
0 21 * * * ~/cron-wrapper.sh --name gmia-daily --timeout 600 --lock -- bash ~/hedge-fund-research/run_pipeline.sh >> ~/logs/gmia.log 2>&1
```

`run_pipeline.sh` — sequential wrapper with gate enforcement:
```bash
#!/bin/bash
set -eo pipefail
cd ~/hedge-fund-research

# Stage 1: fetch metadata (source identity validated internally)
python3 fetch_articles.py

# Stage 2: fetch + validate + normalize content
# (hard gates on HTTP status, content-type, schema, content length)
python3 fetch_content.py

# Stage 3: LLM analysis (only processes content_status="ok" articles)
python3 analyze_articles.py

# Stage 4: publish (always runs — shows whatever data is available)
python3 publish.py
```

05:00 BJT chosen to avoid collision with existing crons and ensure fresh data for morning reading.

## File Structure

```
hedge-fund-research/
├── config/
│   └── sources.json              # Source configuration (existing)
├── content/                       # Downloaded PDFs and text (gitignored)
│   ├── {article_id}.pdf
│   └── {article_id}.txt
├── data/
│   └── articles.jsonl            # Article metadata + summaries (gitignored)
├── templates/
│   └── page_template.html        # HTML template for publish.py
├── logs/                          # Fetch/analysis logs (gitignored)
├── fetch_articles.py             # Stage 1: metadata scraper (existing)
├── fetch_content.py              # Stage 2: PDF/HTML content downloader
├── analyze_articles.py           # Stage 3: LLM analysis
├── publish.py                    # Stage 4: HTML page generator
├── run_pipeline.sh               # Sequential pipeline wrapper
├── requirements.txt              # Python dependencies
└── README.md
```

## Dependencies

Existing: `requests`, `beautifulsoup4`, `playwright`

New: `pdfplumber` (PDF text extraction), `openai` (GPT-4.1 Mini fallback), `google-genai` or `google-generativeai` (Gemini 2.5 Pro), `anthropic` (Claude Sonnet fallback)

## Error Handling & Validation Strategy

Informed by Codex adversarial review (2026-03-31). Core principle: **fail closed — invalid data never flows downstream**.

### Per-Stage Validation Gates

| Stage | Gate | On Failure |
|-------|------|------------|
| Stage 1 (fetch metadata) | URL hostname must match `expected_hostname` per source | Reject article, log `SOURCE_MISMATCH` |
| Stage 2 (fetch content) | HTTP 2xx + correct content-type + schema validation | Set `content_status: "failed"`, skip content save |
| Stage 2 (normalize) | Cleaned text must be >100 chars (not empty/error page) | Set `content_status: "failed"` |
| Stage 3 (LLM analysis) | Only process `content_status: "ok"` articles | Skip, leave `summarized: false` |
| Stage 4 (publish) | Only display articles with valid metadata | Show "Index only" for unsummarized |

### General Error Handling

- Each stage logs to `logs/` and returns non-zero on failure
- cron-wrapper captures exit code in ops-status.jsonl → appears in nightly ops report
- LLM failures: try next model in fallback chain; if all fail, leave article as `summarized: false` for next run
- Content fetch failures: set `content_status: "failed"`, retry next day automatically (incremental processing picks up unfetched articles)
- PDF parse failures: fall back to treating as "index only" for that article

## Data Retention

- `articles.jsonl`: Append-only, no cleanup (grows slowly, ~50 articles/month)
- `content/`: PDFs/text files retained indefinitely (small, ~500KB/article avg)
- Web page: Regenerated daily, shows all articles

## Appendix: Codex Adversarial Review (2026-03-31)

Review run via `/codex:adversarial-review` against the initial design. Verdict: **needs-attention**. All three findings have been addressed in this spec revision:

| # | Severity | Finding | Resolution |
|---|----------|---------|------------|
| 1 | Critical | Source identity not enforced — cross-source contamination possible | Added `expected_hostname` validation in Stage 1 exit gate |
| 2 | High | Upstream fetch failures treated as usable payloads | Added hard validation gates in Stage 2 (HTTP status, content-type, schema, min content length) |
| 3 | Medium | Raw page chrome/junk passed to LLM — token waste, inconsistent analysis | Added content normalization step in Stage 2 (source-specific CSS extraction, strip non-content HTML) |
