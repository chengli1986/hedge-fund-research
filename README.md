# GMIA — Global Market Insight Aggregator

Tracks and aggregates research insights, market commentary, and papers from top hedge funds. Summarizes via LLM and publishes a bilingual (CN/EN) dashboard.

## Sources (6)

| Fund | Method | Frequency | Notable |
|------|--------|-----------|---------|
| **Man Group** | SSR (requests) | Weekly | Macro, quant, systematic trading |
| **Bridgewater Associates** | SSR (requests) | Monthly | Macro, risk parity, All Weather — full content + LLM analysis |
| **AQR Capital** | Playwright (CSR) | Monthly | Factor investing, quantitative research |
| **GMO LLC** | JSON API | Quarterly | Value contrarian, 7-Year forecasts |
| **Oaktree Capital** | Playwright (CSR) | Monthly | Howard Marks memos, credit/distressed |
| **ARK Invest** | RSS feed | Weekly | Analyst Research, Market Commentary |

## Pipeline

```
run_pipeline.sh         — Orchestrator (runs all 4 stages)
  fetch_articles.py     — Stage 1: scrape metadata from all sources, dedup, store JSONL
  fetch_content.py      — Stage 2: download + normalize full article text
  analyze_articles.py   — Stage 3: LLM summarization (CN + EN summaries)
  publish.py            — Stage 4: generate bilingual HTML dashboard
```

## Usage

```bash
# Run full pipeline
bash run_pipeline.sh

# Fetch metadata only
python3 fetch_articles.py

# Fetch single source
python3 fetch_articles.py --source man-group

# Preview without saving
python3 fetch_articles.py --dry-run

# List configured sources
python3 fetch_articles.py --list
```

## Entrypoint Management

Three-layer architecture for resilient research URL management:

1. **Fixed entrypoints** (`config/entrypoints.json`) — verified URLs used for daily fetching
2. **Inspection** — quality metrics in `config/inspection_state.json`, warns on anomalies (consecutive zeros, high gate ratio, domain drift)
3. **Discovery** — `discover_entrypoints.py` scans homepages and scores candidate URLs (domain/path/structure/gate)

```bash
# Discover new entrypoints (dry-run)
python3 discover_entrypoints.py --source bridgewater

# Write discovered entrypoints to config
python3 discover_entrypoints.py --source bridgewater --write

# Validate existing entrypoints
python3 validate_entrypoints.py
python3 validate_entrypoints.py --source gmo --fix
```

## Tests

151 tests passing — unit, functional, and integration tests.

```bash
python3 -m pytest tests/ -q

## Requirements

- Python 3.12+
- `requests`, `beautifulsoup4`, `playwright`, `feedparser`
- Chromium browser (`playwright install chromium`)

## Data

- `data/articles.jsonl` — 61 articles (metadata + summaries), gitignored
- `content/*.txt` — fetched article content files
- `config/sources.json` — source configuration
- `config/entrypoints.json` — verified entrypoint URLs per source
- `config/inspection_state.json` — fetch quality metrics for anomaly detection
