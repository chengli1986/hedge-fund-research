# GMIA — Global Market Insight Aggregator

Tracks and aggregates research insights, market commentary, and papers from top hedge funds. Summarizes via LLM and publishes a bilingual (CN/EN) dashboard.

## Sources (21)

| Fund | Method | Frequency | Notable |
|------|--------|-----------|---------|
| **Man Group** | SSR (requests) | Weekly | Macro, quant, systematic trading |
| **Bridgewater Associates** | SSR (requests) | Monthly | Macro, risk parity, All Weather — full content + LLM analysis |
| **AQR Capital** | Playwright (CSR) | Monthly | Factor investing, quantitative research |
| **GMO LLC** | JSON API | Quarterly | Value contrarian, 7-Year forecasts |
| **Oaktree Capital** | Playwright (CSR) | Monthly | Howard Marks memos, credit/distressed |
| **ARK Invest** | RSS feed | Weekly | Analyst Research, Market Commentary |
| **Cambridge Associates** | SSR (requests) | Monthly | Private equity, venture capital, private credit |
| **Wellington Management** | Playwright (AEM) | Weekly | Equity, macro, fixed income, multi-asset, ESG |
| **Amundi Research Center** | RSS | Weekly | Macro, ESG, emerging markets, fixed income |
| **T. Rowe Price** | Playwright (AEM) | Weekly | Equity, fixed income, active management |
| **PIMCO** | Playwright (Coveo) | Weekly | World's largest fixed-income manager; macro, credit, secular outlook |
| **Aberdeen Investments** | Playwright (Next.js) | Weekly | EM debt, multi-asset, sustainable investing |
| **PGIM** | SSR (AEM) | Weekly | Prudential's IM arm; fixed income, private credit, real estate, alternatives |
| **Brookfield Asset Management** | SSR (Drupal) | Monthly | Real assets, infrastructure, renewable power, private equity |
| **J.P. Morgan Asset Management** | AEM JSON API | Weekly | Multi-asset, fixed income, market insights, Long-Term Capital Market Assumptions |
| **Verdad Capital** | SSR (requests) | Monthly | Quant value/factor, emerging markets, private equity replication; Boston, ~$500M |
| **MSCI Research** | Playwright (Next.js) | Weekly | Index/factor research, ESG & climate frameworks, multi-asset risk; $15T+ benchmarked |
| **Natixis Investment Managers** | SSR (requests) | Monthly | Tactical Take podcast — macro/portfolio strategy with Janasiewicz & Hess |
| **Apollo Global Management** | SSR (AEM) | Weekly | Private credit ABF, secondaries, View from Apollo, Apollo Academy |
| **KKR** | Playwright (Next.js) | Weekly | Private equity / infrastructure / private credit / Global Atlantic insurance; NYC, 1976, ~$600B |
| **Janus Henderson Investors** | SSR (requests) | Weekly | Equities / fixed income / multi-asset; global rates + geopolitics frame; London/Denver, 2017 merger, ~$370B |

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
5. **Email report** — HTML summary with color-coded quality/status; includes a dedicated '✅ Trial Passed — Promote?' section for funds awaiting human promotion (separated from the Queue of candidates waiting to start trials)

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

`gmia-trial-manager.py` — 3-day live trial window for candidate funds. Supports up to 3 concurrent trials (`MAX_CONCURRENT_TRIALS=3`). Runs daily via registered FETCHERS (Playwright/RSS/API — same fetchers as the main pipeline); falls back to httpx for sources without a registered fetcher. Requires articles on ≥2 of 3 days to pass quantity gate. Performs Haiku quality sampling on Day 1, Day 2, and Day 3 (3 articles each, relevance/depth/extractable scores) with cross-day URL dedup so up to 9 distinct articles are scored per trial; slow-updating sources naturally produce smaller samples on later days. Outcomes: APPROVE (add to sources), REJECT (remove from candidates).

## Fetcher Synthesis

`synthesize_fetchers.py` + `fetcher-synthesis/program.md` — weekly Sunday agent that auto-writes Playwright fetchers for `inaccessible` candidates (sites where listing pages are JS-only or selectors are broken), then promotes them back into trial. After `MAX_SYNTHESIS_FAILURES=3` recorded failures in `logs/fetcher-synthesis-history.jsonl`, the candidate is auto-rejected on the next run — bounding wasted agent invocations on candidates the agent cannot solve (e.g. IP/WAF-layer blocks Playwright cannot bypass). Manual override: restore status to `inaccessible` *and* remove the candidate's failed history entries.

## Tests

425 passing, 15 deselected — unit, functional, and integration tests (live/nightly tests excluded by default via pytest.ini). Contract tests enforce `sources.json` stays in sync with the `FETCHERS` / `CONTENT_FETCHERS` dispatcher dicts and `BADGE_COLORS` palette, so adding a new production source without wiring the full pipeline fails fast at pytest time. Consistency tests (`tests/test_config_consistency.py`) additionally guard against frequency-vs-observed-cadence drift, validated-candidate URL invariants, and fund-profile coverage (every `sources.json` id must have a profile in `publish._FUND_PROFILES` or `pending_profiles/<id>.json`).

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
