"""Tests for GMIA Trial Manager quality sampling logic.

Covers the three critical behaviors:
1. New trial creation triggers day-1 quality sampling
2. No quality scores → trial cannot pass (quality gate enforced)
3. Fallback links used when early article extractions fail
4. Overall score computed locally, not trusted from Haiku
"""

import importlib.util
import json
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Import the module with dashes in name
_spec = importlib.util.spec_from_file_location(
    "trial_manager",
    str(Path(__file__).resolve().parent.parent / "gmia-trial-manager.py"),
)
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)

BJT = timezone(timedelta(hours=8))


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_candidates_file(tmp_path: Path) -> Path:
    f = tmp_path / "fund_candidates.json"
    f.write_text(json.dumps([
        {
            "id": "test-fund",
            "name": "Test Fund",
            "status": "validated",
            "quality": "HIGH",
            "fit_score": 0.95,
            "research_url": "https://example.com/research",
            "homepage_url": "https://example.com",
            "topics": "equities",
        },
    ]))
    return f


def _make_sources_file(tmp_path: Path) -> Path:
    f = tmp_path / "sources.json"
    f.write_text(json.dumps({"sources": []}))
    return f


def _make_trial_state(tmp_path: Path, state: dict | None = None) -> Path:
    f = tmp_path / "trial-state.json"
    f.write_text(json.dumps(state or {"active_trials": [], "history": []}))
    return f


@pytest.fixture
def trial_env(tmp_path, monkeypatch):
    """Set up trial manager to use temp files."""
    monkeypatch.setattr(tm, "CANDIDATES_FILE", _make_candidates_file(tmp_path))
    monkeypatch.setattr(tm, "SOURCES_FILE", _make_sources_file(tmp_path))
    monkeypatch.setattr(tm, "TRIAL_STATE_FILE", _make_trial_state(tmp_path))
    monkeypatch.setattr(tm, "ENV_FILE", tmp_path / "nonexistent.env")
    return tmp_path


# ── Constants validation ─────────────────────────────────────────────────────

def test_constants_match_3day_window():
    assert tm.TRIAL_DAYS == 3, "TRIAL_DAYS must be 3"
    assert tm.SAMPLE_DAYS == {1, 3}, "SAMPLE_DAYS must sample day 1 and day 3"
    assert tm.MIN_DAYS_WITH_ARTICLES == 2, "MIN_DAYS_WITH_ARTICLES must be 2"


# ── Bug 1: Day 1 quality sampling on new trial ──────────────────────────────

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


# ── Bug 2: No quality scores → trial must fail ──────────────────────────────

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


# ── Bug 3: Fallback links when extraction fails ─────────────────────────────

def test_fallback_links_used_on_extraction_failure(monkeypatch):
    """When early article links fail text extraction, later links should be tried."""
    extraction_attempts = []

    def mock_extract_text(url, timeout=20):
        extraction_attempts.append(url)
        # First 2 links fail, 3rd-5th succeed
        if len(extraction_attempts) <= 2:
            return None
        return f"Good article text from {url} " * 50

    def mock_haiku(prompt):
        return {
            "articles": [
                {"article_num": 1, "relevance": 0.8, "depth": 0.7,
                 "extractable": 0.9, "notes": "article 1"},
            ]
        }

    monkeypatch.setattr(tm, "_extract_article_text", mock_extract_text)
    monkeypatch.setattr(tm, "_call_haiku", mock_haiku)

    # Simulate: index page returns 6 links
    import httpx
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    from bs4 import BeautifulSoup
    html = """<html><body>
    <a href="/research/article-1">A1</a>
    <a href="/research/article-2">A2</a>
    <a href="/research/sub/article-3">A3</a>
    <a href="/research/sub/article-4">A4</a>
    <a href="/research/sub/article-5">A5</a>
    <a href="/research/sub/article-6">A6</a>
    </body></html>"""
    mock_resp.text = html

    monkeypatch.setattr(httpx, "get", lambda *a, **k: mock_resp)

    result = tm.sample_article_quality("https://example.com/research/")

    # Should have tried more than SAMPLE_SIZE links to get enough texts
    assert len(extraction_attempts) >= 3, \
        f"Only tried {len(extraction_attempts)} links, should try fallbacks"
    assert result["sampled"] >= 1, "Should have at least 1 successful extraction"
    assert result["error"] is None


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


# ── Bug 4: Overall score computed locally ────────────────────────────────────

def test_overall_score_computed_locally(monkeypatch):
    """overall must be 0.4*rel + 0.4*depth + 0.2*ext, not Haiku's value."""

    def mock_haiku(prompt):
        return {
            "articles": [
                {
                    "article_num": 1,
                    "relevance": 1.0,
                    "depth": 0.5,
                    "extractable": 0.0,
                    "overall": 0.99,  # Haiku returns wrong value
                    "notes": "test article",
                },
            ]
        }

    monkeypatch.setattr(tm, "_call_haiku", mock_haiku)
    monkeypatch.setattr(tm, "_extract_article_text", lambda url, **k:
                        "Some article text " * 50)

    import httpx
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<html><body><a href="/research/sub/art-1">A</a></body></html>'
    monkeypatch.setattr(httpx, "get", lambda *a, **k: mock_resp)

    result = tm.sample_article_quality("https://example.com/research/")

    assert result["articles"], "Should have scored articles"
    art = result["articles"][0]
    expected = round(0.4 * 1.0 + 0.4 * 0.5 + 0.2 * 0.0, 3)
    assert art["overall"] == expected, \
        f"overall should be {expected} (locally computed), got {art['overall']}"
    assert art["overall"] != 0.99, "Should NOT use Haiku's overall value"


# ── Integration: trial passes with quantity AND quality ──────────────────────

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


# ── Queue priority: quality_score vs label fallback ─────────────────────────

def _make_candidate(id_, quality, fit_score, quality_score=None):
    c = {
        "id": id_, "name": id_, "status": "validated",
        "quality": quality, "fit_score": fit_score,
        "research_url": f"https://{id_}.com/research",
        "homepage_url": f"https://{id_}.com",
    }
    if quality_score is not None:
        c["quality_score"] = quality_score
    return c


def test_queue_uses_quality_score_over_label(tmp_path, monkeypatch):
    """When both candidates have quality_score, numeric value decides rank regardless of label."""
    candidates = [
        _make_candidate("alpha", "HIGH",   fit_score=0.80, quality_score=0.72),  # HIGH label but lower score
        _make_candidate("beta",  "MEDIUM", fit_score=0.80, quality_score=0.92),  # MEDIUM label but higher score
    ]
    cands_file = tmp_path / "fund_candidates.json"
    cands_file.write_text(json.dumps(candidates))
    (tmp_path / "sources.json").write_text(json.dumps({"sources": []}))
    monkeypatch.setattr(tm, "CANDIDATES_FILE", cands_file)
    monkeypatch.setattr(tm, "SOURCES_FILE", tmp_path / "sources.json")

    state = {"active_trials": [], "history": []}
    queue = tm.get_trial_queue(state)

    assert queue[0]["id"] == "beta", (
        "beta (MEDIUM label, quality_score=0.92) should beat alpha (HIGH label, quality_score=0.72)"
    )


def test_queue_falls_back_to_label_when_no_quality_score(tmp_path, monkeypatch):
    """Without quality_score, HIGH beats MEDIUM at equal fit."""
    candidates = [
        _make_candidate("alpha", "MEDIUM", fit_score=0.80),
        _make_candidate("beta",  "HIGH",   fit_score=0.80),
    ]
    cands_file = tmp_path / "fund_candidates.json"
    cands_file.write_text(json.dumps(candidates))
    (tmp_path / "sources.json").write_text(json.dumps({"sources": []}))
    monkeypatch.setattr(tm, "CANDIDATES_FILE", cands_file)
    monkeypatch.setattr(tm, "SOURCES_FILE", tmp_path / "sources.json")

    state = {"active_trials": [], "history": []}
    queue = tm.get_trial_queue(state)

    assert queue[0]["id"] == "beta", "HIGH label should rank above MEDIUM when no quality_score"


def test_queue_composite_formula(tmp_path, monkeypatch):
    """Priority = 0.6×quality + 0.4×fit, verified numerically."""
    candidates = [
        _make_candidate("low-fit-high-q",  "HIGH",   fit_score=0.50, quality_score=0.90),  # 0.6*0.9+0.4*0.5=0.74
        _make_candidate("high-fit-med-q",  "MEDIUM", fit_score=0.95, quality_score=0.60),  # 0.6*0.6+0.4*0.95=0.74 tie → stable
        _make_candidate("mid-both",        "HIGH",   fit_score=0.70, quality_score=0.70),  # 0.6*0.7+0.4*0.7=0.70
    ]
    cands_file = tmp_path / "fund_candidates.json"
    cands_file.write_text(json.dumps(candidates))
    (tmp_path / "sources.json").write_text(json.dumps({"sources": []}))
    monkeypatch.setattr(tm, "CANDIDATES_FILE", cands_file)
    monkeypatch.setattr(tm, "SOURCES_FILE", tmp_path / "sources.json")

    state = {"active_trials": [], "history": []}
    queue = tm.get_trial_queue(state)

    assert queue[-1]["id"] == "mid-both", "mid-both (priority 0.70) should be last"


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


# ── _extract_article_text DOM extraction strategy ───────────────────────────

class _StubResponse:
    """Minimal httpx.Response stand-in for _extract_article_text tests."""
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def _patch_httpx_get(monkeypatch, html_body: str, status_code: int = 200):
    def fake_get(url, headers=None, timeout=20, follow_redirects=True):
        return _StubResponse(html_body, status_code)
    monkeypatch.setattr(tm.httpx, "get", fake_get)


def test_extract_text_handles_aem_multi_teaser_articles(monkeypatch):
    """Wellington-style AEM pages have many tiny <article> teasers + .cmp-text body.

    The old logic picked the first <article> (a 65-char author teaser), failed
    the >200 char gate, and returned None even though real body text was
    present in .cmp-text components. Fix: longest <article> first, then
    AEM-style component selectors as fallback.
    """
    html_body = """
    <html><body>
      <nav>top nav</nav>
      <main>
        <article class="teaser">Author A <span>Head of Equity</span></article>
        <article class="teaser">Author B <span>Portfolio Manager</span></article>
        <div class="cmp-text">Subscribe to our insights Close dialog Related menu</div>
        <div class="cmp-text">Market leadership has shifted in 2026, challenging some of
          the most reliable winners of the post-pandemic period. We see sector rotation,
          shifting energy dynamics, and an evolving AI narrative as the three key drivers
          for the next six months. This paragraph has well over one hundred characters so
          it survives the boilerplate filter.</div>
        <div class="cmp-text">Investment implications include regional rebalancing toward
          Europe and reassessment of US exceptionalism. Our quantitative framework suggests
          that valuation compression in US large-cap technology could create meaningful
          dispersion in sector returns through year-end.</div>
        <div class="cmp-text">Short note under 100 chars.</div>
      </main>
      <footer>legal disclaimers</footer>
    </body></html>
    """
    _patch_httpx_get(monkeypatch, html_body)
    result = tm._extract_article_text("https://www.wellington.com/en/insights/x")
    assert result is not None, "Should extract .cmp-text body content"
    assert "Market leadership has shifted" in result, "Main body missing"
    assert "Investment implications" in result, "Second body paragraph missing"
    assert "Short note under" not in result, "Sub-100-char boilerplate should be filtered"
    assert len(result) > 200


def test_extract_text_picks_longest_article_not_first(monkeypatch):
    """Multiple <article> tags → pick the longest (real body), not the first (teaser)."""
    body_text = "Real article body. " * 50  # ~950 chars
    html_body = f"""
    <html><body>
      <article>Teaser 1: tiny author card</article>
      <article>Teaser 2: also tiny</article>
      <article>{body_text}</article>
      <article>Another teaser</article>
    </body></html>
    """
    _patch_httpx_get(monkeypatch, html_body)
    result = tm._extract_article_text("https://example.com/article")
    assert result is not None
    assert "Real article body" in result
    assert "Teaser" not in result, "Should have picked the longest <article>, not a teaser"


def test_extract_text_standard_article_unchanged(monkeypatch):
    """Regression guard: standard <article> pages (Amundi/T.Rowe/Cambridge style)
    with a single substantial <article> tag still work as before."""
    body_text = "This is a full article on capital market assumptions. " * 40
    html_body = f"""
    <html><body>
      <nav>nav</nav>
      <article>{body_text}</article>
      <footer>footer</footer>
    </body></html>
    """
    _patch_httpx_get(monkeypatch, html_body)
    result = tm._extract_article_text("https://example.com/article")
    assert result is not None
    assert "capital market assumptions" in result
    assert len(result) > 200


def test_extract_text_falls_back_to_body_when_no_article_or_cmp(monkeypatch):
    """No <article>, no component-framework markers → fall back to <body>."""
    body_text = "Plain HTML page with long content. " * 30
    html_body = f"<html><body><div class='content'>{body_text}</div></body></html>"
    _patch_httpx_get(monkeypatch, html_body)
    result = tm._extract_article_text("https://example.com/article")
    assert result is not None
    assert "Plain HTML page" in result


def test_extract_text_returns_none_when_body_too_short(monkeypatch):
    """Pages with <200 chars of usable content return None (preserved behaviour)."""
    html_body = "<html><body><main>tiny</main></body></html>"
    _patch_httpx_get(monkeypatch, html_body)
    result = tm._extract_article_text("https://example.com/article")
    assert result is None


def test_extract_text_returns_none_on_non_200(monkeypatch):
    """HTTP errors return None (preserved behaviour)."""
    _patch_httpx_get(monkeypatch, "<html><body>error</body></html>", status_code=500)
    result = tm._extract_article_text("https://example.com/article")
    assert result is None


def test_extract_text_matches_schema_org_itemprop_articleBody(monkeypatch):
    """schema.org microdata [itemprop='articleBody'] — used by Reuters, NYT, WSJ,
    and many major news publishers. This is an intentional author-declared
    signal for "this element IS the article body"."""
    body = (
        "This article examines the implications of monetary policy shifts on "
        "emerging-market debt portfolios. " * 10
    )
    html_body = f"""
    <html><body>
      <nav>site nav</nav>
      <article>
        <h1>Article Title</h1>
        <div itemprop="articleBody">{body}</div>
      </article>
      <footer>legal</footer>
    </body></html>
    """
    _patch_httpx_get(monkeypatch, html_body)
    result = tm._extract_article_text("https://example-news.com/article")
    assert result is not None
    assert "monetary policy shifts" in result
    assert "site nav" not in result


def test_extract_text_matches_wordpress_entry_content(monkeypatch):
    """WordPress default themes wrap post bodies in .entry-content. The outer
    <article> here contains only a title (short), so we must fall past Layer 1
    into Layer 2's .entry-content selector."""
    body = "Post-pandemic equity returns have shown increased dispersion. " * 15
    html_body = f"""
    <html><body>
      <article>
        <h1 class="entry-title">A Post Title</h1>
        <div class="entry-content">{body}</div>
      </article>
    </body></html>
    """
    _patch_httpx_get(monkeypatch, html_body)
    result = tm._extract_article_text("https://example-wp.com/post")
    assert result is not None
    # The outer <article> is ~1000 chars (including entry-content) so Layer 1
    # may win first — either path is acceptable as long as body text is present.
    assert "Post-pandemic equity returns" in result
