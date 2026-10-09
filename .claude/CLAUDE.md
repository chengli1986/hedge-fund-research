# Hedge Fund Research (GMIA)

## Overview
GMIA (Global Market Insight Aggregator) tracks research/commentary from 42 top
hedge funds, summarizes each article via LLM, and publishes a bilingual (CN/EN)
HTML dashboard. Stack: Python 3.12; requests + BeautifulSoup for SSR sites,
Playwright (Chromium) for JS/CSR sites; multi-model LLM chain for summaries.

## Develop / Test
```bash
python3 -m pytest tests/ -q                       # 2225 passed, 16 deselected
bash run_pipeline.sh                              # full 4-stage pipeline
python3 fetch_articles.py --list                  # list configured sources
python3 fetch_articles.py --source <id> --dry-run # one source, no save
```
- `pytest.ini` excludes `live` + `nightly` markers by default (they hit the web).
- Playwright sources need `playwright install chromium`.

## Architecture
Daily 4-stage pipeline (`run_pipeline.sh`):
1. `fetch_articles.py` — scrape metadata (title/url/date), dedup, write `data/articles.jsonl`
1b. `scripts/refine_dates.py` — a month-only listing date (`date_raw` "October 2026", stored as the month end) gets the day from the article page (datePublished etc., only inside that month and ≤ first-seen+7d) or URL, else first-seen/month-end; sets `date`, `date_listed`, `date_basis`. **Never touches `date_raw`**: fetch_articles reads it to treat the listing's next "October 2026" as the same article, not a new issue, and compares a listing date with `date_listed` as well as `date` (else a retitled refined row reads as a new issue: 2026-10-09 Baillie Gifford duplicate). Warn-only.
2. `fetch_content.py` — download + normalize full text → `content/*.txt`
3. `analyze_articles.py` — CN+EN summaries; `MODEL_CHAIN` = **gpt-5.6-luna → gpt-4.1-mini**, both OpenAI. Gemini was removed 2026-09-21 and the unwired `_call_anthropic` on 2026-09-30 (audit A5), so summarisation has **one provider** and no cross-provider fallback. Adding one means a client, a `model_to_caller` entry, a `_USAGE_FIELDS` entry and a key — not just a model name. (The `ANTHROPIC_API_KEY` in `scripts/wrapper-*.sh` is for the Claude Code agent workflows, unrelated to this chain.) Exit codes: 2 = quota/auth error (`is_fatal`, shared with 3b) stopped the run at the first such call; 1 = articles were sent to the models and none got an answer, or `MAX_CONSECUTIVE_UNANSWERED` (3) in a row did (stops the stage so 3b/4 still run inside the 30-min cron limit). Duplicate/title-only declines are decided without a model and do not count as answers. "Duplicate" is `text_identity` (shared with stage 2's page_not_updated): same source, identical body, or shingle Jaccard ≥0.85 under the same title / ≥0.90 under another title when the body has ≥3,000 chars (retitled copies were published twice until 2026-10-09). A refusal (HTTP 200, no text) raises `EmptyAnswer` carrying the usage, which is booked before retrying. A wording-rejected summary never passes to the weaker tier (stage-3 audit 2026-10-09).
3b. `scripts/tag_articles.py --nightly` — tags every summarised article that has none (41-tag `taxonomy.py`; each asset/topic/method tag must quote the text or is dropped; series dates in the prompt). Exit 1 = some left untagged, retried next night (logged only); 2 = quota/auth stop; 3 = an article failed its `MAX_TAG_NIGHTS`th night and is no longer asked (`tag_failures` on the row; clear it to retry — a night of only network/timeout/5xx/429 faults neither counts nor clears, a 4xx or local error counts); 4 = damaged store or a crash outside the per-article loop — 2/3/4 alert. A retry tells the model what the rejected answer broke; a quote not found in the text gets one request for another passage before that tag is dropped (`taxonomy._letters` NFKC-folds ligatures). Re-tag after a definition change: `--retag-before <ISO>` (+ `--only-series --also-type event` for type-only fixes).
4. `publish.py` — render bilingual HTML dashboard + fund-profile cards. Default view = Tags (rail + intersection filter + search, logic in `TAGS_VIEW_JS`, tested in a browser by `tests/test_tags_view_browser.py`). The old `themes` field is still written by Stage 3 (exact allowlist match only) but no longer shown; drop it after 2026-11-08.

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
- `scripts/gmia-fetcher-health.py` content-probe defaults to the top 3 most-recent articles (`CONTENT_PROBE_TOP_N`); a per-source `"content_probe_top_n"` override in `sources.json` lets feeds that regularly interleave several short teaser/video pages ahead of the next full piece probe deeper (matthews-asia=6, added 2026-07-04 after 2 consecutive daily false-positive FAILs — its top 3 are always short, article[3] is the first full-length one). If every probed article is labelled `media_without_text` (a media player was detected), the verdict is WARN "content mix", not FAIL (2026-10-07: matthews' top 6 were all videos, and FAIL made the job exit 1, which the liveness audit re-reported daily as BAIL). Any other label among the attempts keeps the FAIL.
- Ares (`fetch_ares_management`) reads two sitemap shapes: `/us/news-and-insights/perspectives/<slug>` and, since 2026-08-27 when new pieces stopped landing there, top-level `/us/news-and-insights/<slug>`. Top-level pages are kept only with a page date >= `ARES_TOP_LEVEL_SINCE` and a `share-print-tag` other than Media. In the Gaps PDFs are marked confidential and are not collected.
- **Calling a content extractor outside the nightly run: wrap it in `with fetch_content.isolated_content_dir():`.** Every extractor writes `CONTENT_DIR/<id>.txt` as a side effect (42 per-source extractors, one per source, plus the PDF-URL and ARK-fallback paths: 46 write statements in all); under a real article id that silently overwrites the stored body, under a made-up one it leaves an orphan. The health probe, `content_audit` and `compare_extractors` use it; a REPL or one-off script must too. Removing or re-id'ing rows must move/delete their `content/` files in the same step — the health email reports orphans new since its last run (audit C2/C3).
- Every candidate status write must go through `status_util.set_status()` (not direct `c["status"] = X`) — it only stamps `status_since` on an actual change, which `detect_stalled_candidates.py` and the discovery email's ⚠️ Nd badge both rely on to tell "just added" apart from "stuck for weeks". The discovery agent bypasses this (edits `fund_candidates.json` directly), so `guard_candidate_status.py` backfills the stamp for the agent's legal edits using its own before/after snapshot.
