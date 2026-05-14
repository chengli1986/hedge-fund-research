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


def test_probe_ok_when_no_publish_dates_flag_set(monkeypatch):
    """Sources with no_publish_dates=True must not WARN on date=None.
    Capital Group intentionally omits dates from its listing page."""
    sid = _install_fakes(monkeypatch, [
        {"title": "Undated article", "url": "http://x", "date": None}
    ])
    source = {"id": sid, "frequency": "weekly", "no_publish_dates": True}
    result = gfh._probe_once(source)
    assert result["status"] == "OK", f"expected OK for no_publish_dates source, got {result}"
    assert "most_recent_age_days" not in result


# ── top-N content probe tests (Apollo 2026-05-13 incident) ─────────────────────
#
# Apollo started publishing podcast/video preview cards (<1500 chars filtered by
# APOLLO_MIN_CONTENT) as the newest items in its article list. The probe was
# firing FAIL because articles[0] happened to be a preview card, even though
# articles[2] was a normal full-text article. Probe now tries top N=3 and passes
# on first success, since "fetcher is healthy" ≠ "most recent article happens
# to be a full-text post".


def _install_per_article_fakes(monkeypatch, articles_returned, outcomes):
    """Install fakes where content_fetcher behavior is per-article-URL.

    `outcomes` maps article URL → callable(probe_article) returning either
    (Path, status) tuple or None or raising. Articles not in `outcomes` map
    return None.
    """
    sid = "fake-fund"
    fetcher = lambda src: articles_returned

    def content_fetcher(article):
        url = article.get("url", "")
        handler = outcomes.get(url)
        if handler is None:
            return None
        return handler(article)

    fake_fa = _FakeFetchArticles({sid: fetcher})
    fake_fc = _FakeFetchContent({sid: content_fetcher}, min_chars=100)
    monkeypatch.setitem(sys.modules, "fetch_articles", fake_fa)
    monkeypatch.setitem(sys.modules, "fetch_content", fake_fc)
    return sid


def _write_chars(article, n):
    """Helper: write n chars to /tmp and return (path, 'ok')."""
    p = Path("/tmp") / f"{article['id']}.txt"
    p.write_text("x" * n)
    return (p, "ok")


def test_probe_passes_when_first_article_is_short_but_second_ok(monkeypatch):
    """Apollo case: articles[0] is a 50-char preview card (filtered),
    articles[1] is a 500-char full article. Probe should pass."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = _install_per_article_fakes(
        monkeypatch,
        [
            {"title": "preview", "url": "http://a/preview", "date": today_iso},
            {"title": "full", "url": "http://a/full", "date": today_iso},
        ],
        outcomes={
            "http://a/preview": lambda art: _write_chars(art, 50),  # below min_chars=100
            "http://a/full": lambda art: _write_chars(art, 500),
        },
    )
    source = {"id": sid, "frequency": "weekly"}
    result = gfh._probe_once(source)
    assert result["status"] == "OK", f"expected OK, got {result}"
    assert result["content_chars"] == 500
    assert result.get("content_probe_index") == 1


def test_probe_passes_when_first_article_returns_none_but_second_ok(monkeypatch):
    """articles[0] returns None (e.g., HTTP 404 or selector miss),
    articles[1] returns 500 chars. Probe should pass."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = _install_per_article_fakes(
        monkeypatch,
        [
            {"title": "broken", "url": "http://a/broken", "date": today_iso},
            {"title": "full", "url": "http://a/full", "date": today_iso},
        ],
        outcomes={
            "http://a/broken": lambda art: None,
            "http://a/full": lambda art: _write_chars(art, 500),
        },
    )
    source = {"id": sid, "frequency": "weekly"}
    result = gfh._probe_once(source)
    assert result["status"] == "OK", f"expected OK, got {result}"
    assert result.get("content_probe_index") == 1


def test_probe_succeeds_on_third_article(monkeypatch):
    """Apollo's actual situation: [0]+[1] are short previews, [2] is full text."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = _install_per_article_fakes(
        monkeypatch,
        [
            {"title": "preview1", "url": "http://a/preview1", "date": today_iso},
            {"title": "preview2", "url": "http://a/preview2", "date": today_iso},
            {"title": "full", "url": "http://a/full", "date": today_iso},
        ],
        outcomes={
            "http://a/preview1": lambda art: _write_chars(art, 50),
            "http://a/preview2": lambda art: _write_chars(art, 80),
            "http://a/full": lambda art: _write_chars(art, 500),
        },
    )
    source = {"id": sid, "frequency": "weekly"}
    result = gfh._probe_once(source)
    assert result["status"] == "OK", f"expected OK, got {result}"
    assert result.get("content_probe_index") == 2


def test_probe_fails_when_all_top_3_short(monkeypatch):
    """If first 3 articles are all below MIN_CONTENT_LENGTH, FAIL with
    aggregated diagnostics."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = _install_per_article_fakes(
        monkeypatch,
        [
            {"title": "p1", "url": "http://a/p1", "date": today_iso},
            {"title": "p2", "url": "http://a/p2", "date": today_iso},
            {"title": "p3", "url": "http://a/p3", "date": today_iso},
            {"title": "p4", "url": "http://a/p4", "date": today_iso},  # would pass but not probed
        ],
        outcomes={
            "http://a/p1": lambda art: _write_chars(art, 50),
            "http://a/p2": lambda art: _write_chars(art, 60),
            "http://a/p3": lambda art: _write_chars(art, 70),
            "http://a/p4": lambda art: _write_chars(art, 500),
        },
    )
    source = {"id": sid, "frequency": "weekly"}
    result = gfh._probe_once(source)
    assert result["status"] == "FAIL", f"expected FAIL, got {result}"
    assert "top 3" in result["reason"] or "3 articles" in result["reason"]


def test_probe_fails_when_all_top_3_return_none(monkeypatch):
    """Genuine fetcher breakage: all 3 attempts return None."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = _install_per_article_fakes(
        monkeypatch,
        [
            {"title": "a", "url": "http://a/1", "date": today_iso},
            {"title": "b", "url": "http://a/2", "date": today_iso},
            {"title": "c", "url": "http://a/3", "date": today_iso},
        ],
        outcomes={
            "http://a/1": lambda art: None,
            "http://a/2": lambda art: None,
            "http://a/3": lambda art: None,
        },
    )
    source = {"id": sid, "frequency": "weekly"}
    result = gfh._probe_once(source)
    assert result["status"] == "FAIL"
    assert "returned None" in result["reason"]


def test_probe_only_tries_top_n_articles(monkeypatch):
    """Even if articles[3] would pass, probe must FAIL when articles[0..2] fail —
    we don't endlessly walk the list (would mask a genuine fetch regression)."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    attempt_counter = {"n": 0}

    def short_then_count(article):
        attempt_counter["n"] += 1
        return _write_chars(article, 50)

    sid = _install_per_article_fakes(
        monkeypatch,
        [
            {"title": "p1", "url": "http://a/1", "date": today_iso},
            {"title": "p2", "url": "http://a/2", "date": today_iso},
            {"title": "p3", "url": "http://a/3", "date": today_iso},
            {"title": "p4", "url": "http://a/4", "date": today_iso},
            {"title": "p5", "url": "http://a/5", "date": today_iso},
        ],
        outcomes={
            "http://a/1": short_then_count,
            "http://a/2": short_then_count,
            "http://a/3": short_then_count,
            "http://a/4": short_then_count,
            "http://a/5": short_then_count,
        },
    )
    source = {"id": sid, "frequency": "weekly"}
    result = gfh._probe_once(source)
    assert result["status"] == "FAIL"
    assert attempt_counter["n"] == gfh.CONTENT_PROBE_TOP_N == 3


def test_probe_staleness_uses_article_0_not_successful_one(monkeypatch):
    """Staleness check must use articles[0]'s date (the genuinely most recent
    article), not the date of whichever article happened to pass the content
    probe. Otherwise a fresh-but-preview-card top article would mask staleness
    of older content."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = _install_per_article_fakes(
        monkeypatch,
        [
            # articles[0] is fresh but a preview card → content probe skips it
            {"title": "fresh preview", "url": "http://a/fresh", "date": today_iso},
            # articles[1] passes content probe but is 2 years old
            {"title": "old full", "url": "http://a/old", "date": "2024-01-01"},
        ],
        outcomes={
            "http://a/fresh": lambda art: _write_chars(art, 50),
            "http://a/old": lambda art: _write_chars(art, 500),
        },
    )
    source = {"id": sid, "frequency": "weekly"}
    result = gfh._probe_once(source)
    # Content probe succeeded on articles[1], status=OK because articles[0]
    # date is today (not stale). most_recent_date should reflect articles[0].
    assert result["status"] == "OK", f"expected OK, got {result}"
    assert result["most_recent_date"] == today_iso
    assert result["most_recent_age_days"] == 0


def test_probe_handles_fewer_than_n_articles(monkeypatch):
    """If fetch_articles returns only 1 article and it passes, status=OK."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = _install_per_article_fakes(
        monkeypatch,
        [{"title": "lone", "url": "http://a/lone", "date": today_iso}],
        outcomes={"http://a/lone": lambda art: _write_chars(art, 500)},
    )
    source = {"id": sid, "frequency": "weekly"}
    result = gfh._probe_once(source)
    assert result["status"] == "OK"
    assert result.get("content_probe_index", 0) == 0


def test_probe_records_non_transient_exception_in_fail_reason(monkeypatch):
    """If all top-N attempts raise non-transient exceptions, FAIL with the
    exception types surfaced."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")

    def raise_value_error(article):
        raise ValueError("selector returned empty")

    sid = _install_per_article_fakes(
        monkeypatch,
        [
            {"title": "a", "url": "http://a/1", "date": today_iso},
            {"title": "b", "url": "http://a/2", "date": today_iso},
            {"title": "c", "url": "http://a/3", "date": today_iso},
        ],
        outcomes={
            "http://a/1": raise_value_error,
            "http://a/2": raise_value_error,
            "http://a/3": raise_value_error,
        },
    )
    source = {"id": sid, "frequency": "weekly"}
    result = gfh._probe_once(source)
    assert result["status"] == "FAIL"
    assert "ValueError" in result["reason"]
