"""A trial must not PASS a source whose newest article is already stale.

2026-11-13 Graham Capital: its RSS listed the same 10 articles every day, so
the trial counted 7/7 "days with articles" and passed (Q=0.71). The newest
was 2026-06-04, 162 days old. auto-promote wired it, the post-commit health
probe WARNed "stale" and reverted it (exit 3). The trial now applies the
health check's staleness rule before PASS and parks such a source on the
low-cadence watchlist instead.
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("trial_manager_stale", ROOT / "gmia-trial-manager.py")
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)

BJT = timezone(timedelta(hours=8))


@pytest.fixture
def trial_env(tmp_path, monkeypatch):
    (tmp_path / "fund_candidates.json").write_text(json.dumps([{
        "id": "slow-fund", "name": "Slow Fund", "status": "visitable", "quality": "MEDIUM",
        "research_url": "https://example.com/feed/", "homepage_url": "https://example.com"}]))
    (tmp_path / "sources.json").write_text(json.dumps({"sources": []}))
    monkeypatch.setattr(tm, "CANDIDATES_FILE", tmp_path / "fund_candidates.json")
    monkeypatch.setattr(tm, "SOURCES_FILE", tmp_path / "sources.json")
    monkeypatch.setattr(tm, "TRIAL_STATE_FILE", tmp_path / "trial-state.json")
    monkeypatch.setattr(tm, "ENV_FILE", tmp_path / "nonexistent.env")
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)
    return tmp_path


def _run_last_day(monkeypatch, newest: str | None) -> tuple[dict, dict]:
    today = datetime.now(BJT)
    start = today - timedelta(days=tm.TRIAL_DAYS - 1)
    check = {"accessible": True, "article_count": 10, "date_count": 10 if newest else 0,
             "error": None, "newest_date": newest}
    state = {"active_trials": [{
        "id": "slow-fund", "name": "Slow Fund", "research_url": "https://example.com/feed/",
        "homepage_url": "https://example.com", "quality": "MEDIUM",
        "start_date": start.strftime("%Y-%m-%d"), "end_date": None,
        "daily_checks": {(start + timedelta(days=i)).strftime("%Y-%m-%d"): dict(check)
                         for i in range(tm.TRIAL_DAYS - 1)},
        "quality_samples": [{"day": d, "date": "x", "sampled": 1, "avg_score": 0.8, "error": None,
                             "articles": [{"url": f"https://example.com/a{d}", "overall": 0.8,
                                           "notes": "ok"}]} for d in range(1, tm.TRIAL_DAYS + 1)],
        "auto_decided": False, "outcome": None}], "history": []}
    tm.TRIAL_STATE_FILE.write_text(json.dumps(state))
    monkeypatch.setattr(tm, "count_articles_with_fetcher", lambda trial: dict(check, fetcher_used=True))
    monkeypatch.setattr(tm, "sample_article_quality", lambda *a, **k: {
        "sampled": 0, "articles": [], "avg_score": 0.0, "error": None})
    tm.cmd_run()
    st = tm.load_state()
    trial = next(h for h in st["history"] if h["id"] == "slow-fund")
    cand = next(c for c in json.loads(tm.CANDIDATES_FILE.read_text()) if c["id"] == "slow-fund")
    return trial, cand


def _days_ago(n: int) -> str:
    return (datetime.now(BJT) - timedelta(days=n)).strftime("%Y-%m-%d")


def test_stale_newest_article_does_not_pass(trial_env, monkeypatch):
    trial, cand = _run_last_day(monkeypatch, _days_ago(162))
    assert trial["outcome"] == "fail_stale"
    assert cand["status"] == "watchlist"
    assert _days_ago(162) in cand["notes"] and "162" in cand["notes"]
    assert tm._summary_bucket(trial) == "low_cadence"


def test_fresh_newest_article_still_passes(trial_env, monkeypatch):
    trial, cand = _run_last_day(monkeypatch, _days_ago(10))
    assert trial["outcome"] == "pass"
    assert cand["status"] == "promoted"


def test_listing_without_dates_is_not_judged_stale(trial_env, monkeypatch):
    """No dates (e.g. Fairtree's cards): nothing to measure, the old rule stands."""
    trial, cand = _run_last_day(monkeypatch, None)
    assert trial["outcome"] == "pass"


def test_daily_check_records_the_newest_listed_date():
    arts = [{"url": "u1", "date": "2025-06-24"}, {"url": "u2", "date": "2026-06-04T10:00:00Z"},
            {"url": "u3", "date": None}, {"url": "u4", "date": "June 2026"}]
    assert tm._count_with(lambda: arts)["newest_date"] == "2026-06-04"
    assert tm._count_with(lambda: [{"url": "u"}])["newest_date"] is None


def test_threshold_matches_the_health_probe_default():
    spec = importlib.util.spec_from_file_location("gfh_stale", ROOT / "scripts" / "gmia-fetcher-health.py")
    gfh = importlib.util.module_from_spec(spec)
    sys.modules["gfh_stale"] = gfh
    spec.loader.exec_module(gfh)
    assert tm.TRIAL_MAX_NEWEST_AGE_DAYS == gfh.DEFAULT_STALE_DAYS


def test_generic_page_counter_reads_the_newest_written_date():
    """Candidates without a fetcher/template (Fiera, 11-13) are counted from page text."""
    future = (datetime.now(BJT) + timedelta(days=30)).strftime("%Y-%m-%d")
    text = f"Outlook 2026-06-04 · Notes June 4, 2025 · Brief 3 Jul 2026 · Event {future}"
    assert tm._page_newest_date(text) == "2026-07-03"
    assert tm._page_newest_date("no dates here") is None
