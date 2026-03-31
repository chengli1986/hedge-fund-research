# GMIA — Global Market Insight Aggregator

Tracks and aggregates research insights, market commentary, and papers from top hedge funds.

## Sources

| Fund | Method | Frequency | Notable |
|------|--------|-----------|---------|
| **Man Group** | SSR (requests) | Weekly | Macro, quant, systematic trading |
| **Bridgewater Associates** | SSR (requests) | Monthly | Macro, risk parity, All Weather |
| **AQR Capital** | Playwright (CSR) | Monthly | Factor investing, quantitative research |
| **GMO LLC** | JSON API | Quarterly | Value contrarian, 7-Year forecasts |
| **Oaktree Capital** | Playwright (CSR) | Monthly | Howard Marks memos, credit/distressed |

## Usage

```bash
# Fetch all sources
python3 fetch_articles.py

# Fetch single source
python3 fetch_articles.py --source man-group

# Preview without saving
python3 fetch_articles.py --dry-run

# List configured sources
python3 fetch_articles.py --list
```

## Requirements

- Python 3.12+
- `requests`, `beautifulsoup4`, `playwright`
- Chromium browser (`playwright install chromium`)

## Data

Articles stored in `data/articles.jsonl` (one JSON object per line).

## Architecture

```
fetch_articles.py     — Fetcher: scrapes all sources, dedup, stores JSONL
config/sources.json   — Source configuration
data/articles.jsonl   — Article storage (gitignored)
```
