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

**Security**: Content fetcher includes path traversal protection (filename sanitization) and gate detection (paywall/login pages flagged before LLM analysis).

```bash
# Discover new entrypoints (dry-run)
python3 discover_entrypoints.py --source bridgewater

# Write discovered entrypoints to config
python3 discover_entrypoints.py --source bridgewater --write

# Validate existing entrypoints
python3 validate_entrypoints.py
python3 validate_entrypoints.py --source gmo --fix
```

## Candidate Fund Discovery

Automated pipeline for finding and evaluating new hedge fund research sources:

1. **Site discovery** — crawls candidate fund homepages, extracts research links + RSS feeds
2. **Rule-based screening** — detects login walls, paywalls, index-only pages
3. **Entrypoint scoring** — reuses scorer engine with isolated candidate state
4. **LLM deep analysis** — Claude Code agent judges quality (HIGH/MEDIUM/LOW) and GMIA fit
5. **Email report** — HTML summary with color-coded quality/status after each run

```bash
# Manual run
bash scripts/wrapper-candidate-discovery.sh

# Seed pool: 5 funds (PIMCO, D.E. Shaw, Blackstone, Two Sigma, KKR)
# Cron: daily at 03:00 BJT (gmia-candidate-discovery)
# Skip logic: 7-day cooldown for analyzed, 30-day for rejected/watchlist
```

## Autoresearch

Scorer weight optimization program using automated experiment loop:

- **Program**: `autoresearch/program.md` — experiment definitions + results log
- **Wrapper**: `scripts/wrapper-autoresearch-gmia.sh` — runs daily at 20:15 BJT via cron
- **History sync**: after each run, `sync-ar-history.py` auto-updates the `autoresearch.html` experiment table on docs.sinostor.com.cn
- **Status**: 7 experiments logged (all at 0.9700 precision — weight tuning ongoing)

## Trial Manager

`gmia-trial-manager.py` — 7-day live trial window for candidate funds. Runs daily, requires ≥3 articles to pass, and performs Haiku quality sampling on day 1/4 (3 articles each, relevance/depth/extractable scores). Outcomes: APPROVE (add to sources), REJECT (remove from candidates).

## Tests

196 tests passing, 2 failing (wellington `screen_failed` status not in VALID_STATUSES — test code bug; neuberger-berman seed without candidate entry — data gap) — unit, functional, and integration tests (15 nightly/live tests deselected by default via pytest.ini).

```bash
python3 -m pytest tests/ -q

## Requirements

- Python 3.12+
- `requests`, `beautifulsoup4`, `playwright`, `feedparser`
- Chromium browser (`playwright install chromium`)

## Data

- `data/articles.jsonl` — 74 articles (metadata + summaries), gitignored
- `content/*.txt` — fetched article content files
- `config/sources.json` — source configuration
- `config/entrypoints.json` — verified entrypoint URLs per source
- `config/inspection_state.json` — fetch quality metrics for anomaly detection
