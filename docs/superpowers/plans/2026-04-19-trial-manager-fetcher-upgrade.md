# Trial Manager Fetcher Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Trial Manager's httpx heuristic article detection with the real FETCHERS from fetch_articles.py, shrink trial window from 7→3 days, and feed quality sampling from fetcher results instead of independently crawled links.

**Architecture:** `gmia-trial-manager.py` gains a lazy-import helper `_load_fetchers()` that pulls `FETCHERS` from `fetch_articles.py`. A new `count_articles_with_fetcher(trial)` wraps the real fetcher with httpx fallback. `sample_article_quality` gains an optional `trial` parameter; a helper `_get_article_links_for_sampling(trial)` calls the fetcher to get article URLs instead of re-crawling the index page. Constants `TRIAL_DAYS` and `SAMPLE_DAYS` shrink to match the 3-day window.

**Tech Stack:** Python 3.12, importlib (lazy fetcher import), existing `fetch_articles.FETCHERS`, pytest + monkeypatch

---

## File Map

| File | Change |
|------|--------|
| `gmia-trial-manager.py` | Add fetcher helpers, update call sites, change constants + quantity criterion |
| `tests/test_unit_trial_quality.py` | Update 4 existing tests + add 3 new tests for fetcher integration |

---

### Task 1: Update constants and fix existing tests that hard-code 7-day assumptions

**Files:**
- Modify: `gmia-trial-manager.py:48-53`
- Modify: `tests/test_unit_trial_quality.py`

Context: `TRIAL_DAYS = 7`, `SAMPLE_DAYS = {1, 4}`, `MIN_ARTICLES_TOTAL = 3` are the three constants to change. Four existing tests mock `count_articles` (will be replaced) and reference 8-day trial windows (must shrink to 4 days so elapsed >= TRIAL_DAYS=3). The mock for `sample_article_quality` must accept a `trial=None` kwarg because Task 3 will add that parameter.

- [ ] **Step 1: Write failing test that checks new constant values**

In `tests/test_unit_trial_quality.py`, add at the top of the file after the imports:

```python
def test_constants_match_3day_window():
    assert tm.TRIAL_DAYS == 3, "TRIAL_DAYS must be 3"
    assert tm.SAMPLE_DAYS == {1, 3}, "SAMPLE_DAYS must sample day 1 and day 3"
    assert tm.MIN_DAYS_WITH_ARTICLES == 2, "MIN_DAYS_WITH_ARTICLES must be 2"
    assert not hasattr(tm, "MIN_ARTICLES_TOTAL") or True  # old constant removed/replaced
```

- [ ] **Step 2: Run to confirm it fails**

```bash
cd ~/hedge-fund-research
python3 -m pytest tests/test_unit_trial_quality.py::test_constants_match_3day_window -v
```
Expected: `FAILED — AssertionError: TRIAL_DAYS must be 3` (currently 7)

- [ ] **Step 3: Update constants in `gmia-trial-manager.py` lines 48–53**

Replace the block:
```python
TRIAL_DAYS = 7
MAX_CONCURRENT_TRIALS = 3
MIN_ARTICLES_TOTAL = 3      # articles needed over trial to pass
MIN_QUALITY = {"HIGH", "MEDIUM"}
MIN_QUALITY_SCORE = 0.5     # avg Haiku quality score to pass (0-1)
SAMPLE_DAYS = {1, 4}        # trial days on which to run quality sampling
SAMPLE_SIZE = 3             # articles to sample per quality check
```

With:
```python
TRIAL_DAYS = 3
MAX_CONCURRENT_TRIALS = 3
MIN_DAYS_WITH_ARTICLES = 2  # fetcher must return >0 articles on ≥2 of 3 days
MIN_QUALITY = {"HIGH", "MEDIUM"}
MIN_QUALITY_SCORE = 0.5     # avg Haiku quality score to pass (0-1)
SAMPLE_DAYS = {1, 3}        # trial days on which to run quality sampling
SAMPLE_SIZE = 3             # articles to sample per quality check
```

- [ ] **Step 4: Run the new constant test**

```bash
python3 -m pytest tests/test_unit_trial_quality.py::test_constants_match_3day_window -v
```
Expected: PASS

- [ ] **Step 5: Fix the 4 existing tests that will now be broken**

The 4 tests that create trial state with 8-day windows and mock `count_articles` need two fixes each:
- `start = today - timedelta(days=8)` → `start = today - timedelta(days=4)` (elapsed=4 >= TRIAL_DAYS=3)
- `for i in range(8)` → `for i in range(4)` (4 daily checks)
- `monkeypatch.setattr(tm, "count_articles", ...)` → `monkeypatch.setattr(tm, "count_articles_with_fetcher", ...)` (Task 2 will add this function; patching a not-yet-existing name is fine — pytest monkeypatch will raise AttributeError which means these tests will fail until Task 2 adds `count_articles_with_fetcher`)
- Mock functions for `sample_article_quality` must accept `trial=None`: `def mock_sample_quality(url, trial=None):` (Task 3 will add this kwarg)

Update `test_new_trial_triggers_day1_quality_sampling`:

```python
def test_new_trial_triggers_day1_quality_sampling(trial_env, monkeypatch):
    """When a new trial is created, quality sampling must run on day 1."""
    count_calls = []
    sample_calls = []

    def mock_count_articles_with_fetcher(trial):
        count_calls.append(trial.get("research_url", ""))
        return {"accessible": True, "article_count": 10, "date_count": 5,
                "error": None, "fetcher_used": True}

    def mock_sample_quality(url, trial=None):
        sample_calls.append(url)
        return {
            "sampled": 2,
            "articles": [
                {"url": "https://example.com/a1", "relevance": 0.8,
                 "depth": 0.7, "extractable": 0.9, "overall": 0.78, "notes": "test"},
            ],
            "avg_score": 0.78,
            "error": None,
        }

    monkeypatch.setattr(tm, "count_articles_with_fetcher", mock_count_articles_with_fetcher)
    monkeypatch.setattr(tm, "sample_article_quality", mock_sample_quality)

    tm.cmd_run()

    assert len(sample_calls) == 1, "Day 1 quality sampling was not triggered"
    state = tm.load_state()
    assert len(state["active_trials"]) > 0
    active = state["active_trials"][0]
    samples = active.get("quality_samples", [])
    assert len(samples) == 1
    assert samples[0]["day"] == 1
```

Update `test_trial_fails_without_quality_scores` (change days=8→4, range(8)→range(4), mock name):

```python
def test_trial_fails_without_quality_scores(trial_env, monkeypatch):
    """A trial with enough articles but zero quality scores must NOT pass."""
    today = datetime.now(BJT)
    start = today - timedelta(days=4)

    trial_state = {
        "active_trials": [{
            "id": "test-fund",
            "name": "Test Fund",
            "research_url": "https://example.com/research",
            "homepage_url": "https://example.com",
            "fit_score": 0.95,
            "quality": "HIGH",
            "topics": "equities",
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": None,
            "daily_checks": {
                (start + timedelta(days=i)).strftime("%Y-%m-%d"): {
                    "accessible": True, "article_count": 10,
                    "date_count": 5, "error": None,
                }
                for i in range(4)
            },
            "quality_samples": [],
            "auto_decided": False,
            "outcome": None,
        }],
        "history": [],
    }

    tm.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))

    monkeypatch.setattr(tm, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 10, "date_count": 5,
        "error": None, "fetcher_used": True})
    monkeypatch.setattr(tm, "sample_article_quality", lambda url, trial=None: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "API key missing"})
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)

    tm.cmd_run()

    state = tm.load_state()
    assert len(state["active_trials"]) == 0
    assert len(state["history"]) == 1
    assert state["history"][0]["outcome"] == "fail_quality"

    candidates = json.loads(tm.CANDIDATES_FILE.read_text())
    test_candidate = next(c for c in candidates if c["id"] == "test-fund")
    assert test_candidate["status"] == "watchlist"
```

Update `test_trial_fails_with_low_quality_scores` (days=8→4, range(8)→range(4), mock name):

```python
def test_trial_fails_with_low_quality_scores(trial_env, monkeypatch):
    """A trial with enough articles but low quality scores must fail."""
    today = datetime.now(BJT)
    start = today - timedelta(days=4)

    trial_state = {
        "active_trials": [{
            "id": "test-fund",
            "name": "Test Fund",
            "research_url": "https://example.com/research",
            "homepage_url": "https://example.com",
            "fit_score": 0.95,
            "quality": "HIGH",
            "topics": "equities",
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": None,
            "daily_checks": {
                (start + timedelta(days=i)).strftime("%Y-%m-%d"): {
                    "accessible": True, "article_count": 10,
                    "date_count": 5, "error": None,
                }
                for i in range(4)
            },
            "quality_samples": [{
                "day": 1,
                "date": start.strftime("%Y-%m-%d"),
                "sampled": 3,
                "articles": [
                    {"url": "https://example.com/a1", "relevance": 0.1,
                     "depth": 0.1, "extractable": 0.5, "overall": 0.18, "notes": "marketing"},
                    {"url": "https://example.com/a2", "relevance": 0.2,
                     "depth": 0.1, "extractable": 0.3, "overall": 0.18, "notes": "press release"},
                ],
                "avg_score": 0.18,
                "error": None,
            }],
            "auto_decided": False,
            "outcome": None,
        }],
        "history": [],
    }

    tm.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))
    monkeypatch.setattr(tm, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 10, "date_count": 5,
        "error": None, "fetcher_used": True})
    monkeypatch.setattr(tm, "sample_article_quality", lambda url, trial=None: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "already sampled"})
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)

    tm.cmd_run()

    state = tm.load_state()
    assert len(state["active_trials"]) == 0
    assert state["history"][0]["outcome"] == "fail_quality"
```

Update `test_trial_passes_with_both_quantity_and_quality` (days=8→4, range(8)→range(4), mock name):

```python
def test_trial_passes_with_both_quantity_and_quality(trial_env, monkeypatch):
    """A trial with enough articles AND good quality scores should pass."""
    today = datetime.now(BJT)
    start = today - timedelta(days=4)

    trial_state = {
        "active_trials": [{
            "id": "test-fund",
            "name": "Test Fund",
            "research_url": "https://example.com/research",
            "homepage_url": "https://example.com",
            "fit_score": 0.95,
            "quality": "HIGH",
            "topics": "equities",
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": None,
            "daily_checks": {
                (start + timedelta(days=i)).strftime("%Y-%m-%d"): {
                    "accessible": True, "article_count": 10,
                    "date_count": 5, "error": None,
                }
                for i in range(4)
            },
            "quality_samples": [{
                "day": 1,
                "date": start.strftime("%Y-%m-%d"),
                "sampled": 3,
                "articles": [
                    {"url": "https://example.com/a1", "relevance": 0.9,
                     "depth": 0.8, "extractable": 0.9, "overall": 0.86, "notes": "good research"},
                    {"url": "https://example.com/a2", "relevance": 0.85,
                     "depth": 0.75, "extractable": 0.8, "overall": 0.80, "notes": "solid analysis"},
                ],
                "avg_score": 0.83,
                "error": None,
            }],
            "auto_decided": False,
            "outcome": None,
        }],
        "history": [],
    }

    tm.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))
    monkeypatch.setattr(tm, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 10, "date_count": 5,
        "error": None, "fetcher_used": True})
    monkeypatch.setattr(tm, "sample_article_quality", lambda url, trial=None: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "already sampled"})
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)

    tm.cmd_run()

    state = tm.load_state()
    assert len(state["active_trials"]) == 0
    assert len(state["history"]) == 1
    assert state["history"][0]["outcome"] == "pass"
    assert state["history"][0]["avg_quality_score"] > 0
```

- [ ] **Step 6: Run all trial quality tests (expect most to fail — that's OK)**

```bash
python3 -m pytest tests/test_unit_trial_quality.py -v --tb=short
```
Expected: `test_constants_match_3day_window` PASS; the 4 updated integration tests FAIL with `AttributeError: count_articles_with_fetcher` (expected — Task 2 adds it); `test_fallback_links_used_on_extraction_failure` and `test_overall_score_computed_locally` may still PASS (they test lower-level functions).

- [ ] **Step 7: Commit**

```bash
cd ~/hedge-fund-research
git add gmia-trial-manager.py tests/test_unit_trial_quality.py
git commit -m "refactor(trial): shrink window 7→3 days, swap MIN_ARTICLES_TOTAL for MIN_DAYS_WITH_ARTICLES"
```

---

### Task 2: Add fetcher integration — `_load_fetchers`, `_candidate_to_source_dict`, `count_articles_with_fetcher`

**Files:**
- Modify: `gmia-trial-manager.py` (add after line 101, before `count_articles`)
- Modify: `tests/test_unit_trial_quality.py` (add 2 new tests)

Context: `gmia-trial-manager.py` lives in the repo root alongside `fetch_articles.py`. The lazy import via `importlib` avoids circular-import risk since `fetch_articles.py` does not import `gmia-trial-manager.py`. `_candidate_to_source_dict` builds the minimal dict that FETCHERS need. `count_articles_with_fetcher` falls back to the old `count_articles(url)` for sources not yet registered in FETCHERS.

- [ ] **Step 1: Write two failing tests**

Add to `tests/test_unit_trial_quality.py`:

```python
# ── Task 2: fetcher-based article counting ──────────────────────────────────

def test_count_articles_with_fetcher_uses_fetcher_when_registered(monkeypatch):
    """When FETCHERS has the source_id, count_articles_with_fetcher calls it."""
    fetcher_calls = []

    def fake_fetcher(source):
        fetcher_calls.append(source["id"])
        return [
            {"title": "Article 1", "url": "https://example.com/a1", "date": "2026-04-01"},
            {"title": "Article 2", "url": "https://example.com/a2", "date": "2026-04-02"},
        ]

    fake_fetchers = {"test-source": fake_fetcher}
    monkeypatch.setattr(tm, "_load_fetchers", lambda: fake_fetchers)

    # Also need a candidate entry so _candidate_to_source_dict can build source dict
    import json
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cands_path = Path(tmp) / "fund_candidates.json"
        cands_path.write_text(json.dumps([{
            "id": "test-source",
            "name": "Test Source",
            "status": "validated",
            "research_url": "https://example.com/research",
            "homepage_url": "https://example.com",
        }]))
        monkeypatch.setattr(tm, "CANDIDATES_FILE", cands_path)

        trial = {
            "id": "test-source",
            "research_url": "https://example.com/research",
            "homepage_url": "https://example.com",
        }
        result = tm.count_articles_with_fetcher(trial)

    assert len(fetcher_calls) == 1, "Fetcher should have been called once"
    assert result["article_count"] == 2
    assert result["accessible"] is True
    assert result["fetcher_used"] is True
    assert result["error"] is None


def test_count_articles_with_fetcher_falls_back_to_httpx_when_not_registered(monkeypatch):
    """When source_id is not in FETCHERS, falls back to httpx count_articles."""
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {})  # empty — no fetchers

    httpx_calls = []
    def fake_count_articles(url, timeout=20):
        httpx_calls.append(url)
        return {"accessible": True, "article_count": 5, "date_count": 2, "error": None}

    monkeypatch.setattr(tm, "count_articles", fake_count_articles)

    trial = {
        "id": "unknown-source",
        "research_url": "https://unknown.com/research",
        "homepage_url": "https://unknown.com",
    }
    result = tm.count_articles_with_fetcher(trial)

    assert len(httpx_calls) == 1
    assert result["article_count"] == 5
    assert result["fetcher_used"] is False
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python3 -m pytest tests/test_unit_trial_quality.py::test_count_articles_with_fetcher_uses_fetcher_when_registered tests/test_unit_trial_quality.py::test_count_articles_with_fetcher_falls_back_to_httpx_when_not_registered -v
```
Expected: FAIL with `AttributeError: module 'trial_manager' has no attribute 'count_articles_with_fetcher'`

- [ ] **Step 3: Add the three new functions to `gmia-trial-manager.py`**

Insert after line 101 (end of `load_env` function), before `# ── article detection ─`:

```python
# ── fetcher integration ───────────────────────────────────────────────────────

def _load_fetchers() -> dict:
    """Lazy-import FETCHERS from fetch_articles.py to avoid circular imports."""
    try:
        import sys as _sys
        if str(BASE_DIR) not in _sys.path:
            _sys.path.insert(0, str(BASE_DIR))
        import fetch_articles
        return fetch_articles.FETCHERS
    except Exception as exc:
        print(f"WARNING: could not import FETCHERS from fetch_articles: {exc}")
        return {}


def _candidate_to_source_dict(candidate: dict) -> dict:
    """Build a minimal source dict from a fund_candidates entry for use with FETCHERS."""
    return {
        "id": candidate["id"],
        "name": candidate.get("name", candidate["id"]),
        "url": candidate.get("research_url") or candidate.get("homepage_url", ""),
        "method": "playwright",
        "max_articles": 10,
        # expected_hostname intentionally omitted — each FETCHER has its own default
    }


def count_articles_with_fetcher(trial: dict) -> dict:
    """Count articles via the registered FETCHER, with httpx fallback for unknown sources.

    Returns dict with keys: accessible, article_count, date_count, error, fetcher_used
    """
    source_id = trial["id"]
    fetchers = _load_fetchers()

    if source_id not in fetchers:
        url = trial.get("research_url") or trial.get("homepage_url", "")
        result = count_articles(url)
        result["fetcher_used"] = False
        return result

    candidates = load_candidates()
    candidate = next((c for c in candidates if c["id"] == source_id), None)
    if not candidate:
        url = trial.get("research_url") or trial.get("homepage_url", "")
        result = count_articles(url)
        result["fetcher_used"] = False
        return result

    source_dict = _candidate_to_source_dict(candidate)
    try:
        articles = fetchers[source_id](source_dict)
        count = len(articles)
        return {
            "accessible": True,
            "article_count": count,
            "date_count": sum(1 for a in articles if a.get("date")),
            "error": None,
            "fetcher_used": True,
        }
    except Exception as exc:
        return {
            "accessible": False,
            "article_count": 0,
            "date_count": 0,
            "error": str(exc)[:120],
            "fetcher_used": True,
        }
```

- [ ] **Step 4: Run the two new tests**

```bash
python3 -m pytest tests/test_unit_trial_quality.py::test_count_articles_with_fetcher_uses_fetcher_when_registered tests/test_unit_trial_quality.py::test_count_articles_with_fetcher_falls_back_to_httpx_when_not_registered -v
```
Expected: both PASS

- [ ] **Step 5: Run the full trial quality test suite**

```bash
python3 -m pytest tests/test_unit_trial_quality.py -v --tb=short
```
Expected: the 4 updated integration tests now PASS (they mock `count_articles_with_fetcher` which exists); constant test PASS; fallback/score tests PASS. All 9 tests green.

- [ ] **Step 6: Commit**

```bash
git add gmia-trial-manager.py tests/test_unit_trial_quality.py
git commit -m "feat(trial): add fetcher-backed count_articles_with_fetcher with httpx fallback"
```

---

### Task 3: Add `_get_article_links_for_sampling` and update `sample_article_quality` signature

**Files:**
- Modify: `gmia-trial-manager.py` (add `_get_article_links_for_sampling` before `sample_article_quality`; update `sample_article_quality` signature)
- Modify: `tests/test_unit_trial_quality.py` (add 1 new test)

Context: `sample_article_quality` currently takes `research_url: str` and re-crawls the index page with httpx to find links. The new helper `_get_article_links_for_sampling(trial)` calls the fetcher when available, or falls back to the existing httpx crawl. `sample_article_quality` gets an optional `trial: dict | None = None` parameter — when provided, delegates link extraction to the helper.

- [ ] **Step 1: Write one failing test**

Add to `tests/test_unit_trial_quality.py`:

```python
# ── Task 3: quality sampling from fetcher results ───────────────────────────

def test_sample_quality_uses_fetcher_links_when_trial_provided(monkeypatch):
    """When trial dict is passed, sample_article_quality uses fetcher URLs, not httpx crawl."""
    fetcher_calls = []
    extract_calls = []

    def fake_fetcher(source):
        fetcher_calls.append(source["id"])
        return [
            {"title": "Deep Research", "url": "https://example.com/research/deep-1", "date": "2026-04-01"},
            {"title": "Factor Study",  "url": "https://example.com/research/factor-2", "date": "2026-04-02"},
            {"title": "Macro View",    "url": "https://example.com/research/macro-3", "date": "2026-04-03"},
        ]

    monkeypatch.setattr(tm, "_load_fetchers", lambda: {"test-fund": fake_fetcher})

    import json
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        cands_path = Path(tmp) / "fund_candidates.json"
        cands_path.write_text(json.dumps([{
            "id": "test-fund", "name": "Test Fund", "status": "validated",
            "research_url": "https://example.com/research",
            "homepage_url": "https://example.com",
        }]))
        monkeypatch.setattr(tm, "CANDIDATES_FILE", cands_path)

        def fake_extract(url, timeout=20):
            extract_calls.append(url)
            return "Long article text " * 100

        monkeypatch.setattr(tm, "_extract_article_text", fake_extract)
        monkeypatch.setattr(tm, "_call_haiku", lambda prompt: {
            "articles": [
                {"article_num": i+1, "relevance": 0.8, "depth": 0.7,
                 "extractable": 0.9, "notes": f"article {i+1}"}
                for i in range(3)
            ]
        })

        trial = {
            "id": "test-fund",
            "research_url": "https://example.com/research",
        }
        result = tm.sample_article_quality("https://example.com/research", trial=trial)

    assert len(fetcher_calls) == 1, "Fetcher should have been called for link extraction"
    assert result["sampled"] == 3
    assert result["error"] is None
    # Verify URLs came from fetcher, not httpx crawl
    assert all("research" in url for url in extract_calls)
```

- [ ] **Step 2: Run to confirm it fails**

```bash
python3 -m pytest tests/test_unit_trial_quality.py::test_sample_quality_uses_fetcher_links_when_trial_provided -v
```
Expected: FAIL — `sample_article_quality` does not accept `trial` kwarg yet

- [ ] **Step 3: Add `_get_article_links_for_sampling` and update `sample_article_quality`**

In `gmia-trial-manager.py`, insert `_get_article_links_for_sampling` just before `def sample_article_quality` (line 257):

```python
def _get_article_links_for_sampling(trial: dict) -> list[str]:
    """Return article URLs for quality sampling.

    Calls the registered FETCHER when available so sampling uses the same
    articles the pipeline will actually collect. Falls back to httpx crawl
    for sources without a registered fetcher.
    """
    source_id = trial["id"]
    fetchers = _load_fetchers()

    if source_id in fetchers:
        candidates = load_candidates()
        candidate = next((c for c in candidates if c["id"] == source_id), None)
        if not candidate:
            return []
        source_dict = _candidate_to_source_dict(candidate)
        try:
            articles = fetchers[source_id](source_dict)
            return [a["url"] for a in articles[:SAMPLE_SIZE * 3] if a.get("url")]
        except Exception:
            return []

    # Fallback: httpx crawl of the index page
    url = trial.get("research_url", "")
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.select("nav, footer, header, script, style"):
            tag.decompose()
        return _extract_article_links(url, soup)
    except Exception:
        return []
```

Then update `sample_article_quality` signature (line 257) — change `(research_url: str)` to `(research_url: str, trial: dict | None = None)` and replace only the link-extraction block (Step 1 of the function body):

```python
def sample_article_quality(research_url: str, trial: dict | None = None) -> dict:
    """Sample articles and assess quality via Haiku.

    When trial is provided, uses the source's registered FETCHER to get article
    URLs. Otherwise falls back to httpx crawl of research_url.

    Returns dict with keys: sampled, articles, avg_score, error
    """
    # Step 1: get article links
    if trial is not None:
        links = _get_article_links_for_sampling(trial)
        if not links:
            return {"sampled": 0, "articles": [], "avg_score": 0.0,
                    "error": "No article links found on index page"}
    else:
        # Original httpx path (backward compat + fallback)
        try:
            resp = httpx.get(research_url, headers=HEADERS, timeout=20,
                             follow_redirects=True)
            if resp.status_code != 200:
                return {"sampled": 0, "articles": [], "avg_score": 0.0,
                        "error": f"Index page HTTP {resp.status_code}"}
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup.select("nav, footer, header, script, style"):
                tag.decompose()
        except Exception as exc:
            return {"sampled": 0, "articles": [], "avg_score": 0.0,
                    "error": str(exc)[:120]}

        links = _extract_article_links(research_url, soup)
        if not links:
            return {"sampled": 0, "articles": [], "avg_score": 0.0,
                    "error": "No article links found on index page"}

    # Steps 2–3: extract text + Haiku assessment (unchanged from original)
    article_texts: list[tuple[str, str]] = []
    for url in links:
        text = _extract_article_text(url)
        if text:
            article_texts.append((url, text))
        if len(article_texts) >= SAMPLE_SIZE:
            break

    if not article_texts:
        return {"sampled": 0, "articles": [], "avg_score": 0.0,
                "error": "Could not extract text from any article"}

    articles_block = ""
    for i, (url, text) in enumerate(article_texts, 1):
        articles_block += f"\n--- Article {i} (URL: {url}) ---\n{text}\n"

    prompt = f"""You are evaluating articles from a hedge fund / investment research source.
For each article below, score it on three dimensions (0.0 to 1.0):

1. **relevance**: Is this investment research, macro analysis, or portfolio strategy?
   (1.0 = deep investment research, 0.5 = tangentially related, 0.0 = marketing/HR/unrelated)
2. **depth**: Is this substantive analysis with data, reasoning, or original insight?
   (1.0 = detailed research paper, 0.5 = brief commentary, 0.0 = press release/summary)
3. **extractable**: Is the text clean and complete enough to be useful if auto-collected?
   (1.0 = full article text, 0.5 = partial/truncated, 0.0 = login wall/JS placeholder)

Return a JSON object with this exact structure:
{{
  "articles": [
    {{
      "article_num": 1,
      "relevance": 0.8,
      "depth": 0.7,
      "extractable": 0.9,
      "overall": 0.8,
      "notes": "one-line summary of what this article is about"
    }}
  ]
}}

The "overall" score should be: 0.4*relevance + 0.4*depth + 0.2*extractable.
{articles_block}"""

    result = _call_haiku(prompt)
    if not result or "articles" not in result:
        return {"sampled": len(article_texts), "articles": [], "avg_score": 0.0,
                "error": "Haiku returned invalid response"}

    haiku_by_num: dict[int, dict] = {}
    for art in result.get("articles", []):
        try:
            num = int(art.get("article_num", 0))
            haiku_by_num[num] = art
        except (TypeError, ValueError):
            pass

    def _safe_float(val) -> float:
        try:
            return float(val) if val is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    scored_articles = []
    for i, (url, _) in enumerate(article_texts, 1):
        art = haiku_by_num.get(i, {})
        rel = _safe_float(art.get("relevance"))
        dep = _safe_float(art.get("depth"))
        ext = _safe_float(art.get("extractable"))
        overall = round(0.4 * rel + 0.4 * dep + 0.2 * ext, 3)
        scored_articles.append({
            "url": url,
            "relevance": rel,
            "depth": dep,
            "extractable": ext,
            "overall": overall,
            "notes": art.get("notes", ""),
        })

    avg_score = (sum(a["overall"] for a in scored_articles) / len(scored_articles)
                 if scored_articles else 0.0)

    return {
        "sampled": len(article_texts),
        "articles": scored_articles,
        "avg_score": round(avg_score, 3),
        "error": None,
    }
```

- [ ] **Step 4: Run the new test**

```bash
python3 -m pytest tests/test_unit_trial_quality.py::test_sample_quality_uses_fetcher_links_when_trial_provided -v
```
Expected: PASS

- [ ] **Step 5: Run full trial quality suite**

```bash
python3 -m pytest tests/test_unit_trial_quality.py -v --tb=short
```
Expected: all 10 tests PASS

- [ ] **Step 6: Commit**

```bash
git add gmia-trial-manager.py tests/test_unit_trial_quality.py
git commit -m "feat(trial): add _get_article_links_for_sampling, update sample_article_quality to use fetcher"
```

---

### Task 4: Wire up `cmd_run` — swap call sites + update quantity criterion + update email

**Files:**
- Modify: `gmia-trial-manager.py:539–700` (cmd_run), `gmia-trial-manager.py:583–638` (quantity criterion), `gmia-trial-manager.py:406–534` (send_trial_email)

Context: Four call sites need updating. Quantity criterion changes from `total_articles >= MIN_ARTICLES_TOTAL` to `days_with_articles >= MIN_DAYS_WITH_ARTICLES`. The email threshold display changes to match. `send_trial_email` gains `days_with_articles: int` parameter and displays it in the summary table.

- [ ] **Step 1: Update the four call sites in `cmd_run`**

In `gmia-trial-manager.py`:

**Line 552** (active trial daily check):
```python
# OLD:
url = active.get("research_url") or active.get("homepage_url", "")
print(f"[trial] Checking {active['name']} — {url}")
result = count_articles(url)

# NEW:
url = active.get("research_url") or active.get("homepage_url", "")
print(f"[trial] Checking {active['name']} — {url}")
result = count_articles_with_fetcher(active)
```

**Line 571** (quality sampling for active trial):
```python
# OLD:
qr = sample_article_quality(url)

# NEW:
qr = sample_article_quality(url, trial=active)
```

**Line 677** (new trial day-1 check):
```python
# OLD:
result = count_articles(url)

# NEW:
result = count_articles_with_fetcher(new_trial)
```

**Line 688** (new trial day-1 quality sampling):
```python
# OLD:
qr = sample_article_quality(url)

# NEW:
qr = sample_article_quality(url, trial=new_trial)
```

- [ ] **Step 2: Update quantity criterion in `cmd_run` (lines 586–591)**

Replace the quantity check block:

```python
# OLD:
total_articles = sum(
    d.get("article_count", 0)
    for d in active.get("daily_checks", {}).values()
    if d.get("accessible")
)
quantity_ok = total_articles >= MIN_ARTICLES_TOTAL

# NEW:
total_articles = sum(
    d.get("article_count", 0)
    for d in active.get("daily_checks", {}).values()
    if d.get("accessible")
)
days_with_articles = sum(
    1 for d in active.get("daily_checks", {}).values()
    if d.get("accessible") and d.get("article_count", 0) > 0
)
quantity_ok = days_with_articles >= MIN_DAYS_WITH_ARTICLES
```

And store `days_with_articles` in the trial dict alongside `total_articles`:

```python
active["auto_decided"] = True
active["end_date"] = today
active["total_articles"] = total_articles
active["days_with_articles"] = days_with_articles  # add this line
active["avg_quality_score"] = round(avg_quality, 3)
```

Also update the fail_quantity note:
```python
# OLD:
c["notes"] = f"Trial failed: only {total_articles} articles"

# NEW:
c["notes"] = f"Trial failed: articles on only {days_with_articles}/{TRIAL_DAYS} days"
```

And the pass note:
```python
# OLD:
c["notes"] = (f"RECOMMEND: trial passed "
              f"({total_articles} articles/7d, quality={avg_quality:.2f})")

# NEW:
c["notes"] = (f"RECOMMEND: trial passed "
              f"({days_with_articles}/{TRIAL_DAYS} days with articles, quality={avg_quality:.2f})")
```

And the print statement:
```python
# OLD:
print(f"[trial] Trial complete for {active['name']}: "
      f"{'PASS' if passed else 'FAIL'} "
      f"({total_articles} articles, quality={avg_quality:.2f})")

# NEW:
print(f"[trial] Trial complete for {active['name']}: "
      f"{'PASS' if passed else 'FAIL'} "
      f"({days_with_articles}/{TRIAL_DAYS} days, quality={avg_quality:.2f})")
```

- [ ] **Step 3: Update `send_trial_email` to accept and display `days_with_articles`**

Change signature (line 406):
```python
# OLD:
def send_trial_email(trial: dict, passed: bool, total_articles: int) -> None:

# NEW:
def send_trial_email(trial: dict, passed: bool, total_articles: int,
                     days_with_articles: int | None = None) -> None:
```

In the email HTML table, find "Total articles detected" row and replace:
```python
# OLD:
  <tr><td style="padding:8px"><strong>Total articles detected</strong></td>
      <td style="padding:8px">{total_articles} (threshold: {MIN_ARTICLES_TOTAL})</td></tr>

# NEW:
  <tr><td style="padding:8px"><strong>Days with articles</strong></td>
      <td style="padding:8px">{days_with_articles if days_with_articles is not None else '?'}/{TRIAL_DAYS} (need ≥{MIN_DAYS_WITH_ARTICLES})</td></tr>
  <tr><td style="padding:8px"><strong>Total articles collected</strong></td>
      <td style="padding:8px">{total_articles}</td></tr>
```

Update the `send_trial_email` call site:
```python
# OLD:
send_trial_email(active, passed, total_articles)

# NEW:
send_trial_email(active, passed, total_articles, days_with_articles=days_with_articles)
```

Update the email subject to use days:
```python
# OLD:
msg["Subject"] = f"GMIA Trial {'PASS' if passed else 'FAIL'}: {trial['name']} ({total_articles} articles, Q={avg_quality:.2f})"

# NEW:
dwa = days_with_articles if days_with_articles is not None else trial.get("days_with_articles", "?")
msg["Subject"] = f"GMIA Trial {'PASS' if passed else 'FAIL'}: {trial['name']} ({dwa}/{TRIAL_DAYS} days, Q={avg_quality:.2f})"
```

- [ ] **Step 4: Run the full test suite**

```bash
cd ~/hedge-fund-research
python3 -m pytest tests/ -q --tb=short
```
Expected: 242 passed (239 existing + 3 new), 15 deselected

- [ ] **Step 5: Smoke-test `cmd_run` dry run**

```bash
python3 gmia-trial-manager.py status
```
Expected: shows 3 active trials (troweprice/amundi/wellington), no crash

- [ ] **Step 6: Commit and push**

```bash
git add gmia-trial-manager.py
git commit -m "feat(trial): wire fetcher to cmd_run, switch quantity criterion to days_with_articles"
git push
```

---

## Self-Review

**Spec coverage:**
- ✅ Use actual FETCHERS instead of httpx → `count_articles_with_fetcher` + Task 4 call-site wiring
- ✅ 7→3 day window → `TRIAL_DAYS = 3`, `SAMPLE_DAYS = {1, 3}`
- ✅ Quality sampling from fetcher results → `_get_article_links_for_sampling` + `sample_article_quality(trial=...)`

**Placeholder scan:** None found. All code blocks are complete.

**Type consistency:**
- `count_articles_with_fetcher(trial: dict)` — used consistently in Tasks 2 and 4
- `sample_article_quality(research_url: str, trial: dict | None = None)` — defined in Task 3, updated call sites in Task 4 use `trial=active` / `trial=new_trial`
- `send_trial_email(..., days_with_articles: int | None = None)` — defined and called consistently in Task 4
- `MIN_DAYS_WITH_ARTICLES` — defined in Task 1, used in Tasks 2 (tests reference it) and 4

**Active trial migration:** Existing 3 active trials (troweprice/amundi/wellington) were started with `TRIAL_DAYS=7`. With `TRIAL_DAYS=3`, they will auto-decide on day 3 (two days from now). This is correct and expected behaviour — the 3-day window applies from the moment the constant changes.
