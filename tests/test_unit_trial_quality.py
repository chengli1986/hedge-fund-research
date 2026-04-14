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
    f.write_text(json.dumps(state or {"active_trial": None, "history": []}))
    return f


@pytest.fixture
def trial_env(tmp_path, monkeypatch):
    """Set up trial manager to use temp files."""
    monkeypatch.setattr(tm, "CANDIDATES_FILE", _make_candidates_file(tmp_path))
    monkeypatch.setattr(tm, "SOURCES_FILE", _make_sources_file(tmp_path))
    monkeypatch.setattr(tm, "TRIAL_STATE_FILE", _make_trial_state(tmp_path))
    monkeypatch.setattr(tm, "ENV_FILE", tmp_path / "nonexistent.env")
    return tmp_path


# ── Bug 1: Day 1 quality sampling on new trial ──────────────────────────────

def test_new_trial_triggers_day1_quality_sampling(trial_env, monkeypatch):
    """When a new trial is created, quality sampling must run on day 1."""
    count_calls = []
    sample_calls = []

    def mock_count_articles(url, timeout=20):
        count_calls.append(url)
        return {"accessible": True, "article_count": 10, "date_count": 5, "error": None}

    def mock_sample_quality(url):
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

    monkeypatch.setattr(tm, "count_articles", mock_count_articles)
    monkeypatch.setattr(tm, "sample_article_quality", mock_sample_quality)

    tm.cmd_run()

    # Verify sample was called
    assert len(sample_calls) == 1, "Day 1 quality sampling was not triggered"

    # Verify it's stored in state
    state = tm.load_state()
    active = state["active_trial"]
    assert active is not None
    samples = active.get("quality_samples", [])
    assert len(samples) == 1
    assert samples[0]["day"] == 1


# ── Bug 2: No quality scores → trial must fail ──────────────────────────────

def test_trial_fails_without_quality_scores(trial_env, monkeypatch):
    """A trial with enough articles but zero quality scores must NOT pass."""
    today = datetime.now(BJT)
    start = today - timedelta(days=8)

    # Create a trial state that's past the 7-day window with no quality samples
    trial_state = {
        "active_trial": {
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
            "quality_samples": [],  # No quality data!
            "auto_decided": False,
            "outcome": None,
        },
        "history": [],
    }

    tm.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))

    # Mock to prevent actual network calls
    monkeypatch.setattr(tm, "count_articles", lambda *a, **k: {
        "accessible": True, "article_count": 10, "date_count": 5, "error": None})
    monkeypatch.setattr(tm, "sample_article_quality", lambda url: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "API key missing"})
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)

    tm.cmd_run()

    state = tm.load_state()
    # Trial should be decided and failed
    assert state["active_trial"] is None
    assert len(state["history"]) == 1
    assert state["history"][0]["outcome"] == "fail_quality"


def test_trial_fails_with_low_quality_scores(trial_env, monkeypatch):
    """A trial with enough articles but low quality scores must fail."""
    today = datetime.now(BJT)
    start = today - timedelta(days=8)

    trial_state = {
        "active_trial": {
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
        },
        "history": [],
    }

    tm.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))
    monkeypatch.setattr(tm, "count_articles", lambda *a, **k: {
        "accessible": True, "article_count": 10, "date_count": 5, "error": None})
    monkeypatch.setattr(tm, "sample_article_quality", lambda url: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "already sampled"})
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)

    tm.cmd_run()

    state = tm.load_state()
    assert state["active_trial"] is None
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
        "active_trial": {
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
        },
        "history": [],
    }

    tm.TRIAL_STATE_FILE.write_text(json.dumps(trial_state))
    monkeypatch.setattr(tm, "count_articles", lambda *a, **k: {
        "accessible": True, "article_count": 10, "date_count": 5, "error": None})
    monkeypatch.setattr(tm, "sample_article_quality", lambda url: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": "already sampled"})
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)

    tm.cmd_run()

    state = tm.load_state()
    assert state["active_trial"] is None
    assert len(state["history"]) == 1
    assert state["history"][0]["outcome"] == "pass"
    assert state["history"][0]["avg_quality_score"] > 0
