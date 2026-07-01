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
            "status": "visitable",
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

def test_constants_match_7day_window():
    assert tm.TRIAL_DAYS == 7, "TRIAL_DAYS must be 7"
    assert tm.SAMPLE_DAYS == {1, 2, 3, 4, 5, 6, 7}, \
        "SAMPLE_DAYS must sample every day so cross-day dedup yields 21 unique scores"
    assert tm.MIN_DAYS_WITH_ARTICLES == 4, "MIN_DAYS_WITH_ARTICLES must be 4"


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
    """A trial with enough articles but zero quality scores must NOT pass —
    and must be labeled inconclusive (sampling failure), not fail_quality."""
    today = datetime.now(BJT)
    start = today - timedelta(days=8)

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
                for i in range(8)
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
    # 0 个样本 = 采样失败，不是质量判定 — 不能叫 fail_quality（2026-06-02
    # capital-group 教训：采样器 bug 被误报成 "FAILED — LOW QUALITY"）
    assert state["history"][0]["outcome"] == "inconclusive"

    candidates = json.loads(tm.CANDIDATES_FILE.read_text())
    test_candidate = next(c for c in candidates if c["id"] == "test-fund")
    assert test_candidate["status"] == "watchlist"


def test_trial_fails_with_low_quality_scores(trial_env, monkeypatch):
    """A trial with enough articles but low quality scores must fail."""
    today = datetime.now(BJT)
    start = today - timedelta(days=8)

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
                for i in range(8)
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


def test_trial_fail_quantity_zero_articles_routes_to_inaccessible(trial_env, monkeypatch):
    """A trial that detected 0 articles across all days must route to
    status='inaccessible' (not 'watchlist') so fetcher-synthesis picks it up.

    This is the fix for the "watchlist orphan" chain break: previously, all
    fail_quantity outcomes flipped status to watchlist, where they sat
    indefinitely because fetcher-synthesis only looks at status='inaccessible'.
    """
    today = datetime.now(BJT)
    start = today - timedelta(days=8)

    trial_state = {
        "active_trials": [{
            "id": "test-fund",
            "name": "Test Fund",
            "research_url": "https://example.com/research",
            "homepage_url": "https://example.com",
            "fit_score": 0.95,
            "quality": "MEDIUM",
            "topics": "equities",
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": None,
            "daily_checks": {
                # accessible=True (got 200) but article_count=0 — selector broken
                # or page is fully JS-rendered and httpx returns empty index.
                (start + timedelta(days=i)).strftime("%Y-%m-%d"): {
                    "accessible": True, "article_count": 0,
                    "date_count": 0, "error": None,
                }
                for i in range(8)
            },
            "quality_samples": [],
            "auto_decided": False,
            "outcome": None,
        }],
        "history": [],
    }

    tm.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))
    monkeypatch.setattr(tm, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 0, "date_count": 0,
        "error": None, "fetcher_used": False})
    monkeypatch.setattr(tm, "sample_article_quality", lambda url, trial=None: {
        "sampled": 0, "articles": [], "avg_score": 0.0,
        "error": "No article links found on index page"})
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)

    tm.cmd_run()

    state = tm.load_state()
    assert state["history"][0]["outcome"] == "fail_quantity"
    assert state["history"][0]["total_articles"] == 0

    candidates = json.loads(tm.CANDIDATES_FILE.read_text())
    test_candidate = next(c for c in candidates if c["id"] == "test-fund")
    assert test_candidate["status"] == "inaccessible", (
        f"Zero-article fail_quantity must route to 'inaccessible' "
        f"(got '{test_candidate['status']}'). Otherwise fetcher-synthesis "
        f"will not pick it up and the candidate is orphaned in watchlist."
    )
    assert "fetcher-synthesis" in test_candidate["notes"]


def test_trial_fail_quantity_some_articles_stays_watchlist(trial_env, monkeypatch):
    """A trial that detected some articles but not on enough days stays in
    watchlist (genuine slow-publisher pattern, not an access issue).
    """
    today = datetime.now(BJT)
    start = today - timedelta(days=8)

    # Articles on day 1 only — a publisher with one piece all month;
    # MIN_DAYS_WITH_ARTICLES=4 requires articles on ≥4 of 7 days.
    daily = {}
    for i in range(8):
        daily[(start + timedelta(days=i)).strftime("%Y-%m-%d")] = {
            "accessible": True,
            "article_count": 5 if i == 0 else 0,
            "date_count": 5 if i == 0 else 0,
            "error": None,
        }

    trial_state = {
        "active_trials": [{
            "id": "slow-fund",
            "name": "Slow Fund",
            "research_url": "https://example.com/quarterly-only",
            "homepage_url": "https://example.com",
            "fit_score": 0.90,
            "quality": "HIGH",
            "topics": "macro",
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": None,
            "daily_checks": daily,
            "quality_samples": [],
            "auto_decided": False,
            "outcome": None,
        }],
        "history": [],
    }

    # Seed candidates file with this fund
    candidates = json.loads(tm.CANDIDATES_FILE.read_text())
    candidates.append({
        "id": "slow-fund", "name": "Slow Fund", "status": "visitable",
        "quality": "HIGH", "fit_score": 0.90,
        "research_url": "https://example.com/quarterly-only",
        "homepage_url": "https://example.com", "topics": "macro",
    })
    tm.CANDIDATES_FILE.write_text(json.dumps(candidates))
    tm.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))

    monkeypatch.setattr(tm, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 0, "date_count": 0,
        "error": None, "fetcher_used": False})
    monkeypatch.setattr(tm, "sample_article_quality", lambda url, trial=None: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "no new links"})
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)

    tm.cmd_run()

    state = tm.load_state()
    history_entry = next(h for h in state["history"] if h["id"] == "slow-fund")
    assert history_entry["outcome"] == "fail_quantity"
    assert history_entry["total_articles"] == 5  # got some, just not enough days

    candidates = json.loads(tm.CANDIDATES_FILE.read_text())
    test_candidate = next(c for c in candidates if c["id"] == "slow-fund")
    assert test_candidate["status"] == "watchlist", (
        f"Some-articles fail_quantity must stay 'watchlist' "
        f"(got '{test_candidate['status']}'). This is a slow-publisher pattern, "
        f"not an access issue, so fetcher-synthesis should NOT touch it."
    )


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
            "status": "visitable",
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
    start = today - timedelta(days=8)

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
                for i in range(8)
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
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {"test-fund": lambda s: []})

    tm.cmd_run()

    state = tm.load_state()
    assert len(state["active_trials"]) == 0
    assert len(state["history"]) == 1
    assert state["history"][0]["outcome"] == "pass"
    assert state["history"][0]["avg_quality_score"] > 0

    candidates = json.loads(tm.CANDIDATES_FILE.read_text())
    test_candidate = next(c for c in candidates if c["id"] == "test-fund")
    assert test_candidate["status"] == "promoted", (
        "Trial PASS must update candidate.status to 'promoted'. "
        "Without this, downstream promotion-gap audit cannot distinguish "
        "trial-passed candidates from validated-but-not-yet-trialed ones."
    )
    assert "RECOMMEND" in test_candidate.get("notes", "")


# ── Queue priority: quality_score vs label fallback ─────────────────────────

def _make_candidate(id_, quality, fit_score, quality_score=None):
    c = {
        "id": id_, "name": id_, "status": "visitable",
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


def test_queue_skipped_history_does_not_lock_out_candidate(tmp_path, monkeypatch):
    """A skip is "free the slot, not a verdict" — candidate must remain trial-eligible.

    Regression for 2026-04-19 incident where goldman-sam + research-affiliates were
    skipped via cmd_skip, which appended outcome="skipped" history entries. The
    queue logic was excluding ALL history ids, permanently locking these candidates
    out of trial. After the fix, only non-skipped history entries (pass / fail_*)
    should disqualify a candidate from re-trial.
    """
    candidates = [
        _make_candidate("was-skipped", "HIGH", fit_score=0.90, quality_score=0.80),
        _make_candidate("really-failed", "HIGH", fit_score=0.85, quality_score=0.75),
        _make_candidate("really-passed", "HIGH", fit_score=0.80, quality_score=0.70),
    ]
    cands_file = tmp_path / "fund_candidates.json"
    cands_file.write_text(json.dumps(candidates))
    (tmp_path / "sources.json").write_text(json.dumps({"sources": []}))
    monkeypatch.setattr(tm, "CANDIDATES_FILE", cands_file)
    monkeypatch.setattr(tm, "SOURCES_FILE", tmp_path / "sources.json")

    state = {
        "active_trials": [],
        "history": [
            {"id": "was-skipped", "outcome": "skipped", "end_date": "2026-04-19"},
            {"id": "really-failed", "outcome": "fail_quality", "end_date": "2026-05-01"},
            {"id": "really-passed", "outcome": "pass", "end_date": "2026-05-04"},
        ],
    }
    queue = tm.get_trial_queue(state)
    queue_ids = [c["id"] for c in queue]

    assert "was-skipped" in queue_ids, (
        "candidate with outcome=skipped should re-enter queue (skips don't disqualify)"
    )
    assert "really-failed" not in queue_ids, (
        "candidate with outcome=fail_* must stay out (real verdict)"
    )
    assert "really-passed" not in queue_ids, (
        "candidate with outcome=pass must stay out (already promoted)"
    )


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
            "id": "test-fund", "name": "Test Fund", "status": "visitable",
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
    def __init__(self, text: str, status_code: int = 200, url: str = "https://example.com/article"):
        self.text = text
        self.status_code = status_code
        self.url = url


def _patch_httpx_get(monkeypatch, html_body: str, status_code: int = 200,
                     url: str = "https://example.com/article"):
    """Patch _get_with_auth_retry (the new entry point replacing httpx.get)."""
    def fake_retry(u, timeout=20):
        return _StubResponse(html_body, status_code, url=u)
    monkeypatch.setattr(tm, "_get_with_auth_retry", fake_retry)


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


# ── Cross-day sampling dedup (exclude_urls) ─────────────────────────────────

def test_sample_excludes_previously_sampled_urls(monkeypatch):
    """Day 2/3 sampling must skip articles already scored on earlier days.

    Without this, a source whose listing changes slowly would have Day 1/2/3
    all score the same top-of-list articles, so the avg over 9 samples would
    just be the avg over 3 articles repeated thrice.
    """
    fake_links = [
        "https://example.com/research/a",
        "https://example.com/research/b",
        "https://example.com/research/c",
        "https://example.com/research/d",
        "https://example.com/research/e",
    ]
    monkeypatch.setattr(tm, "_get_article_links_for_sampling", lambda trial: fake_links)
    extracted = []

    def fake_extract(url, timeout=20):
        extracted.append(url)
        return "Long article body " * 50

    monkeypatch.setattr(tm, "_extract_article_text", fake_extract)
    monkeypatch.setattr(tm, "_call_haiku", lambda prompt, **kw: {
        "articles": [
            {"article_num": i + 1, "relevance": 0.7, "depth": 0.7,
             "extractable": 0.9, "notes": f"a{i+1}"}
            for i in range(3)
        ]
    })

    # Day 1 sampled first 3 URLs already
    exclude = {fake_links[0], fake_links[1], fake_links[2]}
    result = tm.sample_article_quality(
        "https://example.com/research", trial={"id": "test-fund"},
        exclude_urls=exclude,
    )

    assert result["error"] is None
    assert result["sampled"] == 2, \
        "Should have sampled the 2 remaining un-sampled URLs (d, e)"
    sampled_urls = {a["url"] for a in result["articles"]}
    assert sampled_urls.isdisjoint(exclude), \
        "exclude_urls must NOT appear in current sample"
    assert sampled_urls == {fake_links[3], fake_links[4]}


def test_sample_returns_error_when_all_links_excluded(monkeypatch):
    """If every available link was already sampled, return a clear error
    rather than re-scoring or returning empty silently."""
    fake_links = [
        "https://example.com/research/a",
        "https://example.com/research/b",
    ]
    monkeypatch.setattr(tm, "_get_article_links_for_sampling", lambda trial: fake_links)

    result = tm.sample_article_quality(
        "https://example.com/research", trial={"id": "test-fund"},
        exclude_urls=set(fake_links),
    )

    assert result["sampled"] == 0
    assert result["articles"] == []
    assert result["error"] == "No new (un-sampled) article links available"


def test_sample_default_no_exclude_unchanged(monkeypatch):
    """Backwards compat: omitting exclude_urls preserves existing behaviour."""
    fake_links = ["https://example.com/research/a", "https://example.com/research/b"]
    monkeypatch.setattr(tm, "_get_article_links_for_sampling", lambda trial: fake_links)
    monkeypatch.setattr(tm, "_extract_article_text", lambda u, **k: "x" * 500)
    monkeypatch.setattr(tm, "_call_haiku", lambda prompt, **kw: {
        "articles": [{"article_num": 1, "relevance": 0.7, "depth": 0.7,
                      "extractable": 0.9, "notes": "ok"},
                     {"article_num": 2, "relevance": 0.6, "depth": 0.6,
                      "extractable": 0.8, "notes": "ok"}]
    })

    result = tm.sample_article_quality(
        "https://example.com/research", trial={"id": "test-fund"},
    )
    assert result["sampled"] == 2
    assert result["error"] is None


# ── Haiku retry on transient failures ───────────────────────────────────────

def test_call_haiku_retries_on_invalid_json(monkeypatch):
    """When Haiku returns text that has no parseable JSON, retry once before
    giving up. Without retry, one bad response auto-rejects an otherwise-good
    source (regression: research-affiliates 2026-04-19)."""
    monkeypatch.setattr(tm, "load_env", lambda: {"ANTHROPIC_API_KEY": "test-key"})

    call_count = {"n": 0}

    def fake_post(*args, **kwargs):
        call_count["n"] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        if call_count["n"] == 1:
            # First call: no JSON braces at all — would fail .search
            resp.json = lambda: {"content": [{"text": "Sorry, I can't help with that."}]}
        else:
            resp.json = lambda: {"content": [{"text":
                '{"articles":[{"article_num":1,"relevance":0.8,"depth":0.7,'
                '"extractable":0.9,"notes":"ok"}]}'}]}
        return resp

    monkeypatch.setattr(tm.httpx, "post", fake_post)

    result = tm._call_haiku("dummy prompt")
    assert result is not None, "Should have retried and succeeded on attempt 2"
    assert call_count["n"] == 2
    assert result["articles"][0]["relevance"] == 0.8


def test_call_haiku_retries_on_request_exception(monkeypatch):
    """Transient network/HTTP errors trigger one retry."""
    monkeypatch.setattr(tm, "load_env", lambda: {"ANTHROPIC_API_KEY": "test-key"})

    call_count = {"n": 0}

    def fake_post(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise tm.httpx.ConnectError("transient")
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"content": [{"text":
            '{"articles":[{"article_num":1,"relevance":0.5,"depth":0.5,'
            '"extractable":0.5,"notes":"x"}]}'}]}
        return resp

    monkeypatch.setattr(tm.httpx, "post", fake_post)

    result = tm._call_haiku("dummy prompt")
    assert result is not None
    assert call_count["n"] == 2


def test_call_haiku_returns_none_after_all_retries_fail(monkeypatch):
    """After max_retries+1 attempts, give up and return None (existing
    failure semantics preserved for callers)."""
    monkeypatch.setattr(tm, "load_env", lambda: {"ANTHROPIC_API_KEY": "test-key"})

    call_count = {"n": 0}

    def fake_post(*args, **kwargs):
        call_count["n"] += 1
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {"content": [{"text": "no json here at all"}]}
        return resp

    monkeypatch.setattr(tm.httpx, "post", fake_post)

    result = tm._call_haiku("dummy prompt", max_retries=1)
    assert result is None
    assert call_count["n"] == 2  # 1 initial + 1 retry


# ── _extract_article_links: bio/team filtering + research-path preference ───
# Lesson from 2026-05-04: PineBridge trial scored 0.24 because /en/bio/* links
# polluted the sample pool. _extract_article_links must skip non-research path
# segments and prefer research-path links when available.

class TestExtractArticleLinksFiltering:

    @staticmethod
    def _soup(html_str: str):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html_str, "html.parser")

    def test_skips_bio_team_people_paths(self):
        """PineBridge-style: /en/bio/* and /en/team/* links must be filtered."""
        html_str = """
        <a href="/en/insights/capital-market-line">Capital Market Line</a>
        <a href="/en/bio/kelly-michael">Michael Kelly</a>
        <a href="/en/bio/redha-hani">Hani Redha</a>
        <a href="/en/team/portfolio-managers">Team</a>
        <a href="/en/insights/macro-outlook-q2-2026">Macro Outlook Q2</a>
        """
        links = tm._extract_article_links(
            "https://www.pinebridge.com/en/insights/", self._soup(html_str)
        )
        assert all("/bio/" not in u for u in links)
        assert all("/team/" not in u for u in links)
        assert any("capital-market-line" in u for u in links)
        assert any("macro-outlook-q2-2026" in u for u in links)
        assert len(links) == 2

    def test_prefers_research_segments_when_available(self):
        """When ≥SAMPLE_SIZE research-path links exist, return only those."""
        html_str = """
        <a href="/insights/article-1">Article 1</a>
        <a href="/insights/article-2">Article 2</a>
        <a href="/insights/article-3">Article 3</a>
        <a href="/random-page-1">Random 1</a>
        <a href="/random-page-2">Random 2</a>
        """
        links = tm._extract_article_links(
            "https://example.com/insights/", self._soup(html_str)
        )
        assert len(links) == 3
        assert all("/insights/" in u for u in links)
        assert all("random-page" not in u for u in links)

    def test_falls_back_to_neutral_links_when_no_research_paths(self):
        """If no research-segment links found, neutral links are still returned."""
        html_str = """
        <a href="/articles-overview/q1-review">Q1 Review</a>
        <a href="/quarterly-letter/2026-q1">2026 Q1 Letter</a>
        """
        # Note: /articles-overview/ doesn't match /article/ or /articles/ exactly;
        # /quarterly-letter/ doesn't match /letter/ exactly. Both treated neutral.
        # With <SAMPLE_SIZE research links, neutral are kept.
        links = tm._extract_article_links(
            "https://example.com/insights/", self._soup(html_str)
        )
        assert len(links) >= 1, "neutral links should be kept as fallback"

    def test_blackstone_marketing_paths_filtered(self):
        """Blackstone-style: /tommy-fleetwood/ (golf sponsorship) is hard to
        catch (no segment match), but /careers/, /events/, /contact/ are."""
        html_str = """
        <a href="/insights/article/private-credit-myth">Private Credit Myth</a>
        <a href="/insights/article/ipo-outlook">IPO Outlook</a>
        <a href="/careers/analyst-program">Careers</a>
        <a href="/events/private-credit-webinar">Webinar</a>
        <a href="/contact/investor-relations">Contact</a>
        """
        links = tm._extract_article_links(
            "https://www.blackstone.com/insights/", self._soup(html_str)
        )
        assert all("/careers/" not in u for u in links)
        assert all("/events/" not in u for u in links)
        assert all("/contact/" not in u for u in links)
        assert len(links) == 2

    def test_existing_filters_still_active(self):
        """Original filters (/tag/, /category/, /author/, /login, /search) still skip."""
        html_str = """
        <a href="/insights/real-article">Real</a>
        <a href="/tag/macro">Tag</a>
        <a href="/category/equity">Category</a>
        <a href="/author/john-smith">Author</a>
        <a href="/login">Login</a>
        <a href="/search?q=foo">Search</a>
        """
        links = tm._extract_article_links(
            "https://example.com/insights/", self._soup(html_str)
        )
        assert len(links) == 1
        assert "real-article" in links[0]

    def test_dedupes_canonical_paths(self):
        """Same canonical URL with different anchors/queries collapses to one."""
        html_str = """
        <a href="/insights/article-x?utm=email">Read</a>
        <a href="/insights/article-x#section-2">Section</a>
        <a href="/insights/article-x">Direct</a>
        """
        links = tm._extract_article_links(
            "https://example.com/", self._soup(html_str)
        )
        assert len(links) == 1

    def test_caps_at_sample_size_times_3(self):
        """Returns at most SAMPLE_SIZE * 3 links (headroom for cross-day dedup)."""
        items = "".join(
            f'<a href="/insights/article-{i}">A{i}</a>' for i in range(20)
        )
        links = tm._extract_article_links(
            "https://example.com/insights/", self._soup(items)
        )
        assert len(links) == tm.SAMPLE_SIZE * 3


# ---------------------------------------------------------------------------
# _is_likely_js_only: detect JS-shell pattern (HTTP 200 + large HTML + no text)
# ---------------------------------------------------------------------------

def test_is_likely_js_only_true_for_large_html_no_extractable_content(monkeypatch):
    """HTTP 200 + HTML body > 1000 chars + _extract_article_text returns None → True."""
    large_html = "<html><body>" + "<div class='nav'>" * 100 + "</div>" * 100 + "</body></html>"
    assert len(large_html) > 1000
    url = "https://example.com/article"

    monkeypatch.setattr(tm, "_get_with_auth_retry",
                        lambda u, timeout=15: _StubResponse(large_html, 200, url=u))
    monkeypatch.setattr(tm, "_extract_article_text", lambda url, **k: None)
    assert tm._is_likely_js_only(url) is True


def test_is_likely_js_only_false_for_http_non200(monkeypatch):
    """HTTP non-200 (auth wall, geoblock) → False, not a JS-rendering issue."""
    url = "https://example.com/article"
    monkeypatch.setattr(tm, "_get_with_auth_retry",
                        lambda u, timeout=15: _StubResponse("<html>Forbidden</html>", 403, url=u))
    assert tm._is_likely_js_only(url) is False


def test_is_likely_js_only_false_for_small_html_body(monkeypatch):
    """HTTP 200 but tiny HTML body (< 1000 chars) → False (network/empty response)."""
    url = "https://example.com/article"
    monkeypatch.setattr(tm, "_get_with_auth_retry",
                        lambda u, timeout=15: _StubResponse("<html><body>tiny</body></html>", 200, url=u))
    assert tm._is_likely_js_only(url) is False


def test_is_likely_js_only_false_on_exception(monkeypatch):
    """Network exception → False (not a JS-rendering signal)."""
    def _raise(u, timeout=15):
        raise Exception("timeout")
    monkeypatch.setattr(tm, "_get_with_auth_retry", _raise)
    assert tm._is_likely_js_only("https://example.com/article") is False


def test_sample_article_quality_tracks_js_only_count(monkeypatch):
    """js_only_count in return dict reflects how many extraction failures were JS shells."""
    monkeypatch.setattr(tm, "_get_article_links_for_sampling",
                        lambda trial: ["https://example.com/a1", "https://example.com/a2"])
    monkeypatch.setattr(tm, "_extract_article_text", lambda url, **k: None)
    monkeypatch.setattr(tm, "_is_likely_js_only", lambda url, **k: True)
    monkeypatch.setattr(tm, "_call_haiku", lambda *a, **k: None)

    result = tm.sample_article_quality("https://example.com", trial={"id": "test"})

    assert result["js_only_count"] == 2
    assert result["js_only_checked"] == 2
    assert result["error"] == "Could not extract text from any article"


# ---------------------------------------------------------------------------
# Trial pass: synthesis_priority and requires_playwright flags
# ---------------------------------------------------------------------------

def _make_trial_state_pass(start_days_ago=8, js_only_count=0, js_only_checked=3):
    """Build a trial_state dict for a PASS scenario with js_only tracking."""
    BJT_local = timezone(timedelta(hours=8))
    today = datetime.now(BJT_local)
    start = today - timedelta(days=start_days_ago)
    return {
        "active_trials": [{
            "id": "test-fund",
            "name": "Test Fund",
            "research_url": "https://example.com/research",
            "homepage_url": "https://example.com",
            "fit_score": 0.90,
            "quality": "HIGH",
            "topics": "equities",
            "start_date": start.strftime("%Y-%m-%d"),
            "end_date": None,
            "daily_checks": {
                (start + timedelta(days=i)).strftime("%Y-%m-%d"): {
                    "accessible": True, "article_count": 10,
                    "date_count": 5, "error": None,
                }
                for i in range(8)
            },
            "quality_samples": [{
                "day": 1,
                "date": start.strftime("%Y-%m-%d"),
                "sampled": 2,
                "articles": [
                    {"url": "https://example.com/a1", "relevance": 0.9,
                     "depth": 0.8, "extractable": 0.9, "overall": 0.86, "notes": "good"},
                ],
                "avg_score": 0.86,
                "error": None,
                "js_only_count": js_only_count,
                "js_only_checked": js_only_checked,
            }],
            "auto_decided": False,
            "outcome": None,
        }],
        "history": [],
    }


def test_trial_pass_sets_synthesis_priority_when_no_fetcher(trial_env, monkeypatch):
    """On PASS with no registered fetcher, synthesis_priority is set on the candidate."""
    trial_state = _make_trial_state_pass()
    tm.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))

    monkeypatch.setattr(tm, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 10, "date_count": 5,
        "error": None, "fetcher_used": False})
    monkeypatch.setattr(tm, "sample_article_quality", lambda *a, **k: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "already sampled",
        "js_only_count": 0, "js_only_checked": 0})
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {})

    tm.cmd_run()

    candidates = json.loads(tm.CANDIDATES_FILE.read_text())
    c = next(x for x in candidates if x["id"] == "test-fund")
    assert c.get("synthesis_priority") is True
    assert "synthesis_priority_set_at" in c


def test_trial_pass_no_synthesis_priority_when_has_fetcher(trial_env, monkeypatch):
    """On PASS when fetcher already registered, synthesis_priority is NOT set."""
    trial_state = _make_trial_state_pass()
    tm.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))

    monkeypatch.setattr(tm, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 10, "date_count": 5,
        "error": None, "fetcher_used": True})
    monkeypatch.setattr(tm, "sample_article_quality", lambda *a, **k: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "already sampled",
        "js_only_count": 0, "js_only_checked": 0})
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {"test-fund": lambda s: []})

    tm.cmd_run()

    candidates = json.loads(tm.CANDIDATES_FILE.read_text())
    c = next(x for x in candidates if x["id"] == "test-fund")
    assert not c.get("synthesis_priority")


def test_trial_pass_sets_requires_playwright_when_majority_js_only(trial_env, monkeypatch):
    """On PASS where >50% sampled articles were JS-only, requires_playwright is set."""
    trial_state = _make_trial_state_pass(js_only_count=3, js_only_checked=3)
    tm.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))

    monkeypatch.setattr(tm, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 10, "date_count": 5,
        "error": None, "fetcher_used": False})
    monkeypatch.setattr(tm, "sample_article_quality", lambda *a, **k: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "already sampled",
        "js_only_count": 0, "js_only_checked": 0})
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {})

    tm.cmd_run()

    candidates = json.loads(tm.CANDIDATES_FILE.read_text())
    c = next(x for x in candidates if x["id"] == "test-fund")
    assert c.get("requires_playwright") is True


# ── Task 3: inconclusive history does not block re-trial ─────────────────────

def test_inconclusive_history_does_not_block_retrial(trial_env):
    """An inconclusive trial (no verdict) must not permanently lock the candidate
    out of the queue — once a human flips status back to visitable, re-trial works.

    Contrast: fail_quality / fail_quantity / pass DO lock (they are verdicts)."""
    state = {
        "active_trials": [],
        "history": [
            {"id": "test-fund", "name": "Test Fund", "outcome": "inconclusive"},
        ],
    }
    tm.TRIAL_STATE_FILE.write_text(json.dumps(state))

    queue = tm.get_trial_queue(tm.load_state())
    assert [c["id"] for c in queue] == ["test-fund"]


def test_fail_quality_history_still_blocks_retrial(trial_env):
    """Real verdicts (fail_quality) must still lock the candidate out."""
    state = {
        "active_trials": [],
        "history": [
            {"id": "test-fund", "name": "Test Fund", "outcome": "fail_quality"},
        ],
    }
    tm.TRIAL_STATE_FILE.write_text(json.dumps(state))

    queue = tm.get_trial_queue(tm.load_state())
    assert queue == []


# ── Task 1: extract _extract_body_from_soup helper ───────────────────────────

def test_extract_body_from_soup_longest_article():
    """Test that _extract_body_from_soup finds the longest <article> tag."""
    from bs4 import BeautifulSoup
    html = """
    <html><body>
      <nav>menu junk</nav>
      <article>tiny teaser</article>
      <article><p>%s</p></article>
      <footer>footer junk</footer>
    </body></html>
    """ % ("Real investment analysis. " * 20)
    soup = BeautifulSoup(html, "html.parser")
    text = tm._extract_body_from_soup(soup)
    assert text is not None
    assert "Real investment analysis." in text
    assert "menu junk" not in text and "footer junk" not in text


def test_extract_body_from_soup_returns_none_when_too_short():
    """Test that _extract_body_from_soup returns None for text shorter than 200 chars."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup("<html><body><p>hi</p></body></html>", "html.parser")
    assert tm._extract_body_from_soup(soup) is None


# ── Task 2: Playwright-based body extractor ─────────────────────────────────

def test_extract_article_text_playwright_extracts_body(monkeypatch):
    """Render a page in a real browser (bypasses CF/JS) and extract body."""
    html = "<html><body><article><p>%s</p></article></body></html>" % ("Deep macro research. " * 20)
    monkeypatch.setattr("fetch_articles._get_playwright_page", lambda url, **kw: html)
    text = tm._extract_article_text_playwright("https://blocked.example/insight")
    assert text is not None and "Deep macro research." in text


def test_extract_article_text_playwright_none_on_error(monkeypatch):
    """When Playwright fails, return None."""
    def boom(url, **kw):
        raise RuntimeError("CF hard-block")
    monkeypatch.setattr("fetch_articles._get_playwright_page", boom)
    assert tm._extract_article_text_playwright("https://blocked.example/x") is None
