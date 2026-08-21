# Hedge Fund Research (GMIA)

## Overview
GMIA (Global Market Insight Aggregator) tracks research/commentary from 34 top
hedge funds, summarizes each article via LLM, and publishes a bilingual (CN/EN)
HTML dashboard. Stack: Python 3.12; requests + BeautifulSoup for SSR sites,
Playwright (Chromium) for JS/CSR sites; multi-model LLM chain for summaries.

## Develop / Test
```bash
python3 -m pytest tests/ -q                       # 714 passing, 15 deselected
bash run_pipeline.sh                              # full 4-stage pipeline
python3 fetch_articles.py --list                  # list configured sources
python3 fetch_articles.py --source <id> --dry-run # one source, no save
```
- `pytest.ini` excludes `live` + `nightly` markers by default (they hit the web).
- Playwright sources need `playwright install chromium`.

## Architecture
Daily 4-stage pipeline (`run_pipeline.sh`):
1. `fetch_articles.py` — scrape metadata (title/url/date), dedup, write `data/articles.jsonl`
2. `fetch_content.py` — download + normalize full text → `content/*.txt`
3. `analyze_articles.py` — CN+EN summaries; `MODEL_CHAIN` = Gemini 2.5 Pro → GPT-4.1 Mini → Claude Sonnet (fallback)
4. `publish.py` — render bilingual HTML dashboard + fund-profile cards

Source-acquisition lifecycle (status machine in `config/fund_candidates.json`):
- Candidate discovery (daily): crawl seed funds → rule screen → entrypoint scoring → LLM quality judge → email report → guard (revert illegal agent status changes) → `detect_stalled_candidates.py` (auto-route candidates stuck >= 3d: seed w/ research_url → discovered; screen_failed → inaccessible+needs_playwright)
- Trial manager (`gmia-trial-manager.py`): 3-day live trial, ≤3 concurrent (`MAX_CONCURRENT_TRIALS`); double-gate — quantity (articles on ≥2/3 days) + quality (Haiku sampling avg ≥0.5). PASS→promoted, 0 articles→inaccessible, low quality→watchlist
- Fetcher synthesis (weekly): agent writes Playwright fetchers for `inaccessible` candidates; auto-rejects after `MAX_SYNTHESIS_FAILURES=3`
- Auto-promote (daily): wires `promoted` candidates into production + auto-graduates their fund profile through the validation gate
- Profile refresh (monthly): agent web-verifies time-sensitive facts (AUM / M&A / rebrand), evidence-gated apply; static facts (founders/history) untouched

Key dirs/files:
- `config/sources.json` — production source config (single source of truth)
- `config/{entrypoints,fund_candidates,trial-state,inspection_state}.json` — config + runtime state
- `publish.py` `_FUND_PROFILES` — per-fund profile cards; `BADGE_COLORS` palette
- `scripts/` — wrappers + `apply_refresh.py` / `validate_*.py` helpers
- `{auto-promote,candidate-discovery,fetcher-synthesis,autoresearch}/program.md` — agent playbooks

## Key Facts / Gotchas
- `config/sources.json` is the single source of truth. Contract tests fail if a source isn't wired into the `FETCHERS` / `CONTENT_FETCHERS` dispatch dicts, the `BADGE_COLORS` palette, AND a profile (`_FUND_PROFILES` or `pending_profiles/<id>.json`). Adding a production source = wire all four or pytest fails fast.
- AUM in `_FUND_PROFILES` must stay synced with the AUM embedded in that source's `sources.json` description.
- Profile / auto-graduate edits pass an evidence gate: every changed fact needs a source URL, AUM magnitude in $10M–$20T, currency self-consistency, no uncertainty markers ("reportedly"/"estimated"/...).
- Profile-refresh applies are `change_log`-driven: only listed fields change, every other field stays byte-identical.
- Prefer SSR (requests+BS4); use Playwright only for JS/CSR sites. Some funds are WAF-gated (e.g. AQR, MSCI) — nightly tests auto-skip when 0 articles return; not a fetcher bug.
- Gitignored/not-committed-manually: `data/`, `logs/`, `content/`, `config/inspection_state.json`. Schedulers auto-commit `config/trial-state.json` + `config/fund_candidates.json` — concurrent edits happen, so always `git add <specific file>`, never `-A`.
- Notifications (discovery / trial / synthesis / refresh summaries) are email, notification-only; failures must not break the pipeline exit code.
- A source that changes host (rename, acquisition, move to a newsletter) keeps its **id** and gets the old host added to `"historical_hostnames"` in `sources.json`. `expected_hostname` stays the *current* fetch host — `_validate_hostname` must keep rejecting anything else — while `test_stored_article_hosts_are_declared` checks stored `articles.jsonl` rows against current + historical. Declaring a host is deliberate; an undeclared one means a half-done config edit or fetcher drift. (`test_no_cross_source_contamination` does NOT cover this: it is `nightly`-marked and inspects a live fetch, not what is on disk.)
- Changing a source's host means migrating stored data too: `id = sha256("<source_id>:<url>")[:16]`, so a new host yields new ids and the next fetch re-ingests every old article as a duplicate. Rewrite `url`/`id`/`content_path`, rename `content/<id>.txt`, and only ever rewrite to a URL you verified serves that same article (2026-08 precedent: 5 of 11 PineBridge articles were re-published on metlife.com and got rewritten; the other 6 were not, so they keep their dead pinebridge.com URLs).
- `scripts/gmia-fetcher-health.py` content-probe defaults to the top 3 most-recent articles (`CONTENT_PROBE_TOP_N`); a per-source `"content_probe_top_n"` override in `sources.json` lets feeds that regularly interleave several short teaser/video pages ahead of the next full piece probe deeper (matthews-asia=6, added 2026-07-04 after 2 consecutive daily false-positive FAILs — its top 3 are always short, article[3] is the first full-length one).
- Every candidate status write must go through `status_util.set_status()` (not direct `c["status"] = X`) — it only stamps `status_since` on an actual change, which `detect_stalled_candidates.py` and the discovery email's ⚠️ Nd badge both rely on to tell "just added" apart from "stuck for weeks". The discovery agent bypasses this (edits `fund_candidates.json` directly), so `guard_candidate_status.py` backfills the stamp for the agent's legal edits using its own before/after snapshot.
