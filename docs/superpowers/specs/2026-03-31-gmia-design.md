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

Each stage is an independent script. Stages run sequentially via a wrapper script. Any stage can be re-run individually.

### Stage 1: fetch_articles.py (existing)

- Scrapes article metadata (title, date, URL) from each source
- Deduplicates by URL hash
- Appends new articles to `data/articles.jsonl`
- Fields: `id, source_id, source_name, title, url, date, fetched_at, summarized`

### Stage 2: fetch_content.py (new)

Downloads full article content for unsummarized articles.

**Per-source strategies:**

- **GMO**: Fetch article HTML page (cookie: `GMO_region=NorthAmerica`), extract PDF href via regex `href="([^"]+\.pdf)"`, download to `content/{id}.pdf`
- **Oaktree**: Fetch article HTML page via Playwright, parse `openPDF('url')` JavaScript calls, download PDF to `content/{id}.pdf`
- **AQR**: Fetch article page via Playwright, extract article body from `p.article__summary` or full page content section, save to `content/{id}.txt`
- **Man Group**: Fetch article page via requests, extract `div.teaser__content` or main article body, save to `content/{id}.txt`
- **Bridgewater**: Skip (index only)

**PDF text extraction**: `pdfplumber` library for PDF → text conversion.

**Output**: Sets `content_path` field in articles.jsonl for each processed article.

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

**Incremental processing**: Only analyze articles where `summarized: false`. After successful analysis, set `summarized: true` and write summary fields back to articles.jsonl.

**Cost control**: Log model used + token count per article. Skip articles with no content (Bridgewater).

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

`run_pipeline.sh` — sequential wrapper:
```bash
#!/bin/bash
set -eo pipefail
cd ~/hedge-fund-research
python3 fetch_articles.py
python3 fetch_content.py
python3 analyze_articles.py
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

## Error Handling

- Each stage logs to `logs/` and returns non-zero on failure
- cron-wrapper captures exit code in ops-status.jsonl → appears in nightly ops report
- LLM failures: try next model in fallback chain; if all fail, leave article as `summarized: false` for next run
- Content fetch failures: log and skip, retry next day
- PDF parse failures: fall back to treating as "index only" for that article

## Data Retention

- `articles.jsonl`: Append-only, no cleanup (grows slowly, ~50 articles/month)
- `content/`: PDFs/text files retained indefinitely (small, ~500KB/article avg)
- Web page: Regenerated daily, shows all articles
