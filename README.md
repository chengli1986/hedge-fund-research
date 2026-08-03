# GMIA — Global Market Insight Aggregator

Tracks and aggregates research insights, market commentary, and papers from top hedge funds. Summarizes via LLM and publishes a bilingual (CN/EN) dashboard.

## Sources (36)

| Fund | Method | Frequency | Notable |
|------|--------|-----------|---------|
| **Man Group** | SSR (requests) | Weekly | Macro, quant, systematic trading |
| **Bridgewater Associates** | SSR (requests) | Monthly | Macro, risk parity, All Weather — full content + LLM analysis |
| **AQR Capital** | SSR (requests) | Monthly | Factor investing, quantitative research; Citrix WAF blocks EC2 IPs intermittently — nightly test auto-skips when 0 articles |
| **GMO LLC** | JSON API | Quarterly | Value contrarian, 7-Year forecasts |
| **Oaktree Capital** | Playwright (CSR) | Monthly | Howard Marks memos, credit/distressed |
| **ARK Invest** | RSS feed | Weekly | Analyst Research, Market Commentary |
| **Cambridge Associates** | SSR (requests) | Monthly | Private equity, venture capital, private credit |
| **Wellington Management** | Playwright (AEM) | Weekly | Equity, macro, fixed income, multi-asset, ESG |
| **Amundi Research Center** | RSS | Weekly | Macro, ESG, emerging markets, fixed income |
| **T. Rowe Price** | Playwright (AEM) | Weekly | Equity, fixed income, active management |
| **PIMCO** | Playwright (Coveo) | Weekly | World's largest fixed-income manager; macro, credit, secular outlook |
| **Aberdeen Investments** | Playwright (Next.js) | Weekly | EM debt, multi-asset, sustainable investing |
| **Brookfield Asset Management** | SSR (Drupal) | Monthly | Real assets, infrastructure, renewable power, private equity |
| **J.P. Morgan Asset Management** | AEM JSON API | Weekly | Multi-asset, fixed income, market insights, Long-Term Capital Market Assumptions |
| **Verdad Capital** | SSR (requests) | Monthly | Quant value/factor, emerging markets, private equity replication; Boston, ~$1B |
| **MSCI Research** | Playwright (Next.js) | Weekly | Index/factor research, ESG & climate frameworks, multi-asset risk; $18T+ benchmarked |
| **Natixis Investment Managers** | SSR (requests) | Monthly | Tactical Take podcast — macro/portfolio strategy with Janasiewicz & Hess |
| **Apollo Global Management** | SSR (AEM) | Weekly | Private credit ABF, secondaries, View from Apollo, Apollo Academy |
| **KKR** | Playwright (Next.js) | Weekly | Private equity / infrastructure / private credit / Global Atlantic insurance; NYC, 1976, ~$758B |
| **Janus Henderson Investors** | SSR (requests) | Weekly | Equities / fixed income / multi-asset; global rates + geopolitics frame; London/Denver, 2017 merger, ~$480B (take-private pending) |
| **Research Affiliates** | Playwright (Next.js) | Monthly | Quantitative research / index licensor; RAFI fundamental indices + Smart Beta; Newport Beach, 2002, ~$159B licensed (Rob Arnott) |
| **Goldman Sachs Asset Management** | JSON API | Weekly | Multi-asset, equity, fixed income, alternatives, liquidity; macro outlooks + market commentary; ~$3.3T |
| **Robeco** | Playwright | Weekly | Quant equity pioneer, sustainability/ESG integration, EM equity & global credit; Rotterdam, 1929, ~€228B |
| **D. E. Shaw** | Playwright | Annual | Quant multi-strategy; computational/statistical-arbitrage; Library publishes 1-3 long-form papers/year; 1988, ~$65B |
| **MetLife Investment Management** | SSR (AEM listing JSON + requests) | Weekly | Public fixed income, private credit, real estate, agricultural finance, multi-asset macro; absorbed PineBridge's research teams after completing the acquisition 30 Dec 2025 (replaced the `pinebridge` source on 7-27); moved from `investments.metlife.com` to `www.metlife.com/investments/en-us/` on 7-31 behind a disclaimer cookie gate (8-03 `543cc4d`); Whippany NJ, ~$700B |
| **Ares Management** | SSR (requests) | Weekly | Private credit / private equity / real assets / infrastructure; US direct-lending leader; 1997, NYSE: ARES, ~$644B |
| **Matthews Asia** | SSR (requests) | Monthly | Asia & emerging-markets equity specialist; active bottom-up; SF/HK, 1991, ~$6.6B |
| **Capital Group** | Playwright (AEM) | Weekly | One of the world's largest active managers (~$3.3T); American Funds; 1931; "The Capital System" multi-manager approach; global equity, fixed income, multi-asset |
| **Acadian Asset Management** | Playwright | Monthly | Quantitative/systematic manager (~$120B); Boston, 1986; factor-based equity (valuation/quality/growth/sentiment) across global/EM/frontier markets; systematic credit + multi-asset |
| **Lazard Asset Management** | SSR (requests/AEM) | Weekly | EM equity specialist + macro fixed-income; founded 1848, New York, ~$260B; Lazard Perspective research on geopolitics, EM fundamentals, global monetary policy |
| **Rothschild & Co Asset Management** | SSR (requests) | Weekly | Active boutique within the 200-year-old Rothschild & Co group; Paris, ~€38B; conviction-driven European/thematic equity, multi-asset/macro, credit/fixed income |
| **Goehring & Rozencwajg** | SSR (requests/sitemap) | Weekly | Contrarian natural-resources boutique; energy, precious metals, uranium, agriculture; quarterly commentaries read by 10,000+ professionals; New York, 2015, ~$1.5B |
| **Cohen & Steers** | Playwright | Weekly | Real assets & alternative income specialist; listed REITs/infrastructure pioneer, preferred securities; New York, 1986, ~$94B |
| **Principal Asset Management** | Playwright | Weekly | Global multi-asset manager, asset-management arm of Principal Financial Group; macro, multi-asset, real assets, fixed income; Des Moines, 1879, ~$578B |
| **Franklin Templeton** | Playwright | Weekly | One of the largest independent asset managers (~$1.66T); multi-boutique model — Templeton EM value, Western Asset / Brandywine fixed income, Clarion real estate, Legg Mason + Putnam franchises; San Mateo, 1947 |
| **Partners Group** | SSR (requests) | Monthly | Swiss-listed private-markets specialist (~$186B); thematic transformational investing across private equity, private credit, infrastructure and real estate; pioneered evergreen semi-liquid vehicles; Baar-Zug, 1996 |

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

## Profile Refresh

`scripts/wrapper-profile-refresh.sh` + `auto-promote/refresh-program.md` — monthly agent that keeps the Sources-tab fund profiles fresh. A headless Claude (Max Plan, `--print`) web-verifies each fund's **time-sensitive** facts (AUM + corporate events: M&A, delisting/take-private, rebrand) and writes a `pending_profiles/<id>.refresh.json` draft only when something changed materially. Static facts (founders, history) are never touched.

Each draft is gated by `validate_refresh()` (reuses the auto-promote evidence gate: every changed fact needs a source URL, AUM magnitude/self-consistency sane, no uncertainty markers; prose rewrites with no corporate-event reason are diff-capped). `scripts/apply_refresh.py` applies a passing draft to the existing `publish._FUND_PROFILES` entry — **change_log is the single source of truth**, so only listed fields change and every other field stays byte-identical; the embedded AUM figure in `config/sources.json` is synced too. `scripts/send_refresh_summary.py` emails an applied/flagged summary (notification only).

```bash
# Manual run (Phase 1 alert-only: gate + email, no write/publish)
ALERT_ONLY=1 bash scripts/wrapper-profile-refresh.sh

# Cron: monthly, 1st at 09:00 BJT (gmia-profile-refresh)
# Phased rollout: ALERT_ONLY=1 (dry-run + email) -> flip to ALERT_ONLY=0 for auto-apply + publish
```

## Tests

674 passing, 15 deselected — unit, functional, and integration tests (live/nightly tests excluded by default via pytest.ini). `TestFetchMetlifeIm` / `TestMetLifeIMFetcher` (rewritten 8-03 `543cc4d`) cover the MetLife IM re-point after MIM retired `investments.metlife.com` — the fetcher now drives off the AEM listing servlet (day-precise `publishedDate`; the new sitemap's `<lastmod>` is a migration crawl stamp shared by 38 of 46 URLs, and article pages lost their JSON-LD), and both the article and content fetchers must send the `culture` + `disclaimer` cookie pair or every URL 302s to the country/role interstitial and the source silently returns 0 articles. A dedicated test also pins the deliberate absence of a `main p` content fallback, which would let 260-400 char PDF-teaser blurbs through as if they were the research. `tests/test_unit_guard_candidate_status.py` (added 7-15) covers `guard_candidate_status.py`'s `PIPELINE_STATUS_TRANSITIONS` check — a status transition to a pipeline-owned value (screened/screen_failed/visitable/inaccessible) is only accepted when the corresponding script-owned freshness field (`last_screened_at` / `last_validated_at`) actually advanced in that session, distinguishing a legitimate Phase 1 pipeline run from the discovery agent hand-editing the status field to match; `promoted` is never accepted this way since only `gmia-trial-manager.py` sets it. `tests/test_config_consistency.py`'s cadence check (added 7-14) now detects sources where 80%+ of articles carry only month/year date precision (e.g. troweprice, kkr — real sites, not a scraper bug) and judges staleness in whole calendar months instead of days, since day-gap math manufactures fake 14-60d gaps purely from calendar-boundary rounding. `tests/test_unit_status_util.py` / `tests/test_unit_detect_stalled_candidates.py` / `tests/test_unit_backfill_status_since.py` (added 7-15) cover the `status_since` stall-detection feature — every candidate status write now stamps `status_since` only on an actual change (via `status_util.set_status()`), `detect_stalled_candidates.py` auto-routes candidates stuck too long (seed with a confirmed `research_url` → `discovered`; `screen_failed` → `inaccessible` + `needs_playwright`), and `backfill_status_since.py` one-time-backfilled the field for all pre-existing candidates via a `last_*_at` fallback chain. `test_pool_articles_carry_data_seq_in_global_date_order` / `test_timeline_populate_sorts_pool_by_seq` (added 7-06 `d77d369`) cover the timeline chronological-order fix — each pool article carries `data-seq` (its global date-descending rank) and the timeline view sorts by it before appending, because `returnArticlesToPool()` scrambles pool DOM order after any Themes/Funds hydration (regression showed the timeline as fund-grouped blocks, e.g. all PGIM first, instead of a global date feed). `test_probe_respects_per_source_content_probe_top_n_override` / `test_probe_without_override_still_defaults_to_top_3` (added 7-04) cover the fetcher-health per-source `content_probe_top_n` override in `sources.json` — added for matthews-asia (probes top 6 instead of the global default top 3) after 2 consecutive daily false-positive FAILs from short teaser/video pages ahead of the next full-length article. `TestFetchSourceIntraRunDedup` (added 7-03) covers intra-run article dedup in `fetch_source` — a fetcher returning the same URL twice in one result list (wellington did) previously appended duplicate jsonl rows / duplicate HTML ids. `tests/test_candidate_probe_pool.py` (added 7-02 `0416089`) covers the fetcher-health `--include-validated` probe pool — candidates in an active trial are excluded (the trial exercises real fetching daily, incl. Playwright CF bypass), so trial-period Cloudflare 403s no longer fail the nightly health run. Contract tests enforce `sources.json` stays in sync with the `FETCHERS` / `CONTENT_FETCHERS` dispatcher dicts and `BADGE_COLORS` palette, so adding a new production source without wiring the full pipeline fails fast at pytest time. Consistency tests (`tests/test_config_consistency.py`) additionally guard against frequency-vs-observed-cadence drift, validated-candidate URL invariants, and fund-profile coverage (every `sources.json` id must have a profile in `publish._FUND_PROFILES` or `pending_profiles/<id>.json`). `TestOlderArticleFolding` covers the `data-age` recency split (90d boundary inclusive after the 5-29 `f561da8` Show-older fold tightening, no-date defaults to recent, page-level toggle button render/suppress). `tests/test_validate_pending_profile.py` (added 5-29 `13c51f0`) enforces auto-graduate evidence gates — `aum_source` / `founded_source` URL/domain citations, AUM↔desc_zh currency consistency, $10M-$20T magnitude bounds. `tests/test_trial_auth_gate.py` (added 6-02) covers trial-manager auth-gate detection — cookie content-gates (Akamai-style 302→200 redirects) are retried with a persistent-cookie session instead of being misclassified as JS-only / LOW QUALITY, plus day-1 fail-fast for fetcher-less candidates and inconclusive/aborted email labeling. `tests/test_apply_refresh.py`, `tests/test_validate_refresh.py`, `tests/test_send_refresh_summary.py`, and `tests/test_profile_static_facts.py` (added 6-08) cover the monthly profile-refresh path — change_log-driven entry updates that leave every unchanged field byte-identical, per-changed-field evidence gating (drops `validate_profile`'s unconditional source demands), and a static-fact guard that fails if a hand-audited founder/claim (e.g. GMO's `Eyk van Otterloo`) is ever regressed.

```bash
python3 -m pytest tests/ -q
```

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
