"""Tests for fetcher-health staleness gate (catches 'site still up, content frozen').

Without this, a fund could stop publishing for 6+ months and fetcher-health stays
green (because the OLD article index still serves articles). Lazard AM was already
in this state on 2026-05-07.
"""

import importlib.util
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "gmia-fetcher-health.py"

spec = importlib.util.spec_from_file_location("gfh", SCRIPT)
gfh = importlib.util.module_from_spec(spec)
sys.modules["gfh"] = gfh
spec.loader.exec_module(gfh)


def test_parse_iso_date():
    d = gfh._parse_article_date("2026-05-04")
    assert d is not None
    assert d.date() == date(2026, 5, 4)


def test_parse_with_time_component():
    d = gfh._parse_article_date("2026-05-04T10:30:00")
    assert d is not None
    assert d.date() == date(2026, 5, 4)


def test_parse_returns_none_on_garbage():
    assert gfh._parse_article_date("") is None
    assert gfh._parse_article_date(None) is None
    assert gfh._parse_article_date("not a date") is None


def test_parse_returns_none_on_non_string():
    assert gfh._parse_article_date(12345) is None


def test_threshold_lookup_known_frequencies():
    assert gfh._stale_threshold_days({"frequency": "weekly"}) == 30
    assert gfh._stale_threshold_days({"frequency": "monthly"}) == 90
    assert gfh._stale_threshold_days({"frequency": "quarterly"}) == 240
    assert gfh._stale_threshold_days({"frequency": "daily"}) == 14


def test_threshold_default_when_missing():
    assert gfh._stale_threshold_days({}) == gfh.DEFAULT_STALE_DAYS
    assert gfh._stale_threshold_days({"frequency": "irregular"}) == gfh.DEFAULT_STALE_DAYS


def test_threshold_case_insensitive():
    assert gfh._stale_threshold_days({"frequency": "WEEKLY"}) == 30
    assert gfh._stale_threshold_days({"frequency": " Monthly "}) == 90


def test_known_silent_period_aqr_does_not_trip(monkeypatch):
    """AQR's known silent period is ~50d (quarterly cadence). Threshold 240d
    means the natural cadence does NOT trigger WARN."""
    today = datetime(2026, 5, 8, tzinfo=timezone.utc)
    parsed = gfh._parse_article_date("2026-03-18")
    assert parsed is not None
    age_days = (today.date() - parsed.date()).days
    assert age_days < gfh._stale_threshold_days({"frequency": "quarterly"})


def test_lazard_style_freeze_trips(monkeypatch):
    """Lazard AM stopped publishing on 2024-06; that's ~340d on 2026-05-08.
    With monthly cadence threshold 90d → must WARN."""
    today = date(2026, 5, 8)
    parsed_date = date(2024, 6, 1)
    age_days = (today - parsed_date).days
    threshold = gfh._stale_threshold_days({"frequency": "monthly"})
    assert age_days > threshold


def test_unknown_frequency_uses_default():
    today = date(2026, 5, 8)
    very_old = date(2025, 1, 1)
    age_days = (today - very_old).days
    threshold = gfh._stale_threshold_days({})
    # >120d → trips on default
    assert age_days > threshold


# ── _probe_once integration tests ──────────────────────────────────────────────

class _FakeFetchArticles:
    """Stand-in for fetch_articles module — lets tests inject FETCHERS w/o
    actually loading the heavy import chain."""
    def __init__(self, fetchers):
        self.FETCHERS = fetchers


class _FakeFetchContent:
    """Stand-in for fetch_content module."""
    def __init__(self, content_fetchers, min_chars=100):
        self.CONTENT_FETCHERS = content_fetchers
        self.MIN_CONTENT_LENGTH = min_chars
        self.CONTENT_DIR = Path("/tmp")


def _install_fakes(monkeypatch, articles_returned, content_chars=500):
    """Install fake fetch_articles + fetch_content modules for one source."""
    sid = "fake-fund"
    fetcher = lambda src: articles_returned

    def content_fetcher(article):
        # Write a real file with `content_chars` worth of text
        p = Path("/tmp") / f"{article['id']}.txt"
        p.write_text("x" * content_chars)
        return (p, "ok")

    fake_fa = _FakeFetchArticles({sid: fetcher})
    fake_fc = _FakeFetchContent({sid: content_fetcher}, min_chars=100)
    monkeypatch.setitem(sys.modules, "fetch_articles", fake_fa)
    monkeypatch.setitem(sys.modules, "fetch_content", fake_fc)
    return sid


def test_probe_warns_on_stale(monkeypatch):
    sid = _install_fakes(monkeypatch, [
        {"title": "Old article", "url": "http://x", "date": "2024-06-01"}
    ])
    source = {"id": sid, "frequency": "monthly"}
    result = gfh._probe_once(source)
    assert result["status"] == "WARN", f"expected WARN, got {result}"
    assert "stale" in result["reason"].lower()
    assert "frequency=monthly" in result["reason"]


def test_probe_ok_when_recent(monkeypatch):
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = _install_fakes(monkeypatch, [
        {"title": "Fresh article", "url": "http://x", "date": today_iso}
    ])
    source = {"id": sid, "frequency": "weekly"}
    result = gfh._probe_once(source)
    assert result["status"] == "OK", f"expected OK, got {result}"
    assert result["most_recent_age_days"] == 0


def test_probe_warn_for_no_date_takes_precedence_over_stale(monkeypatch):
    """If date is unparseable, WARN reason is 'no parsed date', not 'stale' —
    we don't report a fake age based on missing data."""
    sid = _install_fakes(monkeypatch, [
        {"title": "Mystery date", "url": "http://x", "date": None}
    ])
    source = {"id": sid, "frequency": "weekly"}
    result = gfh._probe_once(source)
    assert result["status"] == "WARN"
    assert "no parsed date" in result["reason"]
    # most_recent_age_days should NOT be set
    assert "most_recent_age_days" not in result
