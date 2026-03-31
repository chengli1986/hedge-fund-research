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

**Atomic file writes**: All content files are written to a temporary file first (`content/{id}.tmp`), then renamed to the final path (`content/{id}.pdf` or `.txt`) via `os.replace()`. This prevents partial writes from being read by Stage 3 if the pipeline crashes mid-download.

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
# GMIA daily fetch + analyze + publish (03:45 BJT = 19:45 UTC)
45 19 * * * ~/cron-wrapper.sh --name gmia-daily --timeout 600 --lock -- bash ~/hedge-fund-research/run_pipeline.sh >> ~/logs/gmia.log 2>&1
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

03:45 BJT chosen to avoid collision with existing crons (sap-nightly finishes by 19:15 UTC, us-close starts at 20:00 UTC on weekdays) and ensure fresh data for morning reading.

**Concurrency safety**: `--lock` flag in cron-wrapper prevents overlapping runs. Combined with atomic file writes in Stage 2 and append-only JSONL in Stage 1/3, no concurrent or retried run can corrupt another run's artifacts.

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
├── tests/                         # Test suite (4 tiers)
│   ├── conftest.py
│   ├── fixtures/                  # Saved HTML/JSON/PDF snapshots
│   ├── test_unit_*.py             # Unit tests (fast, no network)
│   ├── test_functional_*.py       # Functional tests (fixture data)
│   ├── test_sanity.py             # Live smoke tests (@pytest.mark.live)
│   └── test_regression.py         # Nightly regression (@pytest.mark.nightly)
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

New: `pdfplumber` (PDF text extraction), `openai` (GPT-4.1 Mini fallback), `google-genai` or `google-generativeai` (Gemini 2.5 Pro), `anthropic` (Claude Sonnet fallback), `pytest` (testing)

## Testing Strategy

Tests live in `tests/` directory, run via `pytest`. Four tiers:

### Unit Tests (`tests/test_unit_*.py`)

Fast, no network, no LLM calls. Mock all external dependencies.

**Stage 1 — fetch_articles.py:**
- `test_article_id_deterministic` — same source+URL always produces same ID
- `test_article_id_unique` — different URLs produce different IDs
- `test_parse_date_formats` — all supported date formats parse correctly (March 18, 2026 / 18 March 2026 / 2026-03-18 / etc.)
- `test_parse_date_invalid` — garbage input returns None
- `test_dedup_by_url` — duplicate URLs filtered, first occurrence kept
- `test_hostname_validation_pass` — article URL matching `expected_hostname` accepted
- `test_hostname_validation_reject` — mismatched hostname (e.g., oaktree.com URL in AQR source) rejected with SOURCE_MISMATCH
- `test_hostname_validation_redirect` — URL that redirected to different host rejected
- `test_load_existing_ids` — existing JSONL correctly parsed for dedup

**Stage 2 — fetch_content.py:**
- `test_pdf_content_type_validation` — non-PDF content-type rejected
- `test_json_api_html_error_rejected` — HTML error page in JSON slot detected and rejected
- `test_json_api_valid_response` — valid GMO JSON parsed correctly
- `test_html_normalization_strips_chrome` — nav, footer, legal, cookie banners removed from HTML
- `test_html_normalization_preserves_body` — article body text preserved after stripping
- `test_min_content_length_gate` — content <100 chars rejected as failed
- `test_atomic_write` — content written to .tmp first, then renamed (verify no partial files)
- `test_content_status_failed_on_error` — fetch failure sets `content_status: "failed"` in JSONL

**Stage 3 — analyze_articles.py:**
- `test_skip_failed_content` — articles with `content_status: "failed"` skipped
- `test_skip_already_summarized` — articles with `summarized: true` skipped
- `test_skip_bridgewater` — Bridgewater articles (no content) skipped
- `test_model_fallback_order` — Gemini failure triggers GPT-4.1 Mini, then Claude Sonnet
- `test_all_models_fail` — article left as `summarized: false` when all models fail
- `test_summary_output_schema` — LLM output parsed into correct fields (summary_en, summary_zh, themes, key_takeaway_en, key_takeaway_zh)
- `test_theme_tags_from_predefined_list` — only tags from the predefined list accepted

**Stage 4 — publish.py:**
- `test_html_output_valid` — generated HTML passes basic structure checks (has <html>, <body>, required sections)
- `test_bilingual_toggle` — both CN and EN content present in output
- `test_timeline_sorted_by_date` — articles appear in reverse chronological order
- `test_badge_colors_per_fund` — each fund gets its assigned badge color
- `test_bridgewater_index_only_tag` — Bridgewater entries show "Index only" marker
- `test_theme_grouping` — articles grouped correctly by theme tags
- `test_empty_data_graceful` — publish still generates valid page with no articles

### Functional Tests (`tests/test_functional_*.py`)

Test each stage end-to-end with fixture data (saved HTML/JSON snapshots), no live network.

- `test_man_group_parse_fixture` — parse saved Man Group HTML, verify correct titles/dates/summaries extracted
- `test_bridgewater_parse_fixture` — parse saved Bridgewater HTML, verify titles/dates extracted
- `test_aqr_parse_fixture` — parse saved AQR rendered HTML, verify titles/dates/categories
- `test_gmo_api_parse_fixture` — parse saved GMO JSON API response, verify all fields
- `test_oaktree_parse_fixture` — parse saved Oaktree rendered HTML, verify dedup of audio/text versions
- `test_gmo_pdf_extraction_fixture` — extract text from a saved sample PDF, verify content
- `test_full_pipeline_fixtures` — run all 4 stages with fixture data, verify final HTML output has correct articles

**Fixture directory**: `tests/fixtures/` containing saved HTML pages, JSON responses, and sample PDFs per source.

### Sanity Tests (`tests/test_sanity.py`)

Quick smoke tests that hit live sites (run on-demand or nightly, not on every commit). Verify sources are still accessible and parsers produce non-empty results.

- `test_man_group_live_reachable` — fetch Man Group, get ≥1 article with title and date
- `test_bridgewater_live_reachable` — fetch Bridgewater, get ≥1 article with title
- `test_aqr_live_reachable` — fetch AQR via Playwright, get ≥1 article with date
- `test_gmo_api_live_reachable` — fetch GMO JSON API, get valid response with ≥1 article
- `test_oaktree_live_reachable` — fetch Oaktree via Playwright, get ≥1 article with title and date
- `test_config_valid` — sources.json loads, all required fields present, all source IDs have matching fetcher

Mark with `@pytest.mark.live` — excluded from default `pytest` runs, included via `pytest -m live`.

### Nightly Regression Tests (`tests/test_regression.py`)

Run via cron (nightly at ~04:00 BJT, before the pipeline). Catch site changes that break parsers.

- `test_man_group_article_count` — Man Group returns 3-10 articles (not 0, not 100+)
- `test_bridgewater_article_count` — Bridgewater returns 5-20 articles
- `test_aqr_article_count` — AQR returns 5-15 articles
- `test_gmo_article_count` — GMO returns 5-15 articles
- `test_oaktree_article_count` — Oaktree returns 5-20 articles
- `test_all_articles_have_titles` — every article from every source has a non-empty title
- `test_date_parsing_coverage` — ≥80% of articles have successfully parsed dates (not None)
- `test_no_cross_source_contamination` — every article's URL hostname matches its source's `expected_hostname`
- `test_gmo_api_returns_json` — GMO API returns valid JSON (not HTML error page)
- `test_pipeline_dry_run` — full pipeline with `--dry-run` completes without error

Mark with `@pytest.mark.nightly`. On failure, send alert email (same pattern as flight-search-cn nightly tests).

### Cron Entry for Nightly Tests

```
# GMIA nightly regression (03:30 BJT = 19:30 UTC, 15min before pipeline)
30 19 * * * ~/cron-wrapper.sh --name gmia-nightly-test --timeout 300 --lock -- python3 -m pytest ~/hedge-fund-research/tests/ -m nightly --tb=short -q >> ~/logs/gmia-nightly.log 2>&1
```

### File Structure Update

```
tests/
├── conftest.py                    # Shared fixtures, pytest config
├── fixtures/                      # Saved HTML/JSON/PDF snapshots
│   ├── man-group-insights.html
│   ├── bridgewater-research.html
│   ├── aqr-research-rendered.html
│   ├── gmo-api-response.json
│   ├── oaktree-insights-rendered.html
│   └── sample-research.pdf
├── test_unit_fetch_articles.py
├── test_unit_fetch_content.py
├── test_unit_analyze.py
├── test_unit_publish.py
├── test_functional_parsers.py
├── test_functional_pipeline.py
├── test_sanity.py
└── test_regression.py
```

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

### Round 2 (2026-03-31)

Verdict: **needs-attention**. Findings #1 and #2 were based on Codex's own debug artifacts (not pipeline output) and were already addressed in round 1. Finding #3 was a genuine new issue:

| # | Severity | Finding | Resolution |
|---|----------|---------|------------|
| 1 | High | JSON slot accepts HTML error pages | Already resolved in round 1 (Stage 2 hard gates) |
| 2 | High | Source provenance cross-contamination | Already resolved in round 1 (`expected_hostname`) |
| 3 | Medium | Concurrent/retried runs can overwrite files — stale artifact reuse | Added: cron-wrapper `--lock` prevents concurrent runs, Stage 2 uses atomic temp-file + `os.replace()` writes |
