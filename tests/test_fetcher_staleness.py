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

    def listing_fetcher(self, source):
        """Delegate to the real rule, never a copy of it.

        The probe asks which fetcher production would run. A fake that
        reimplements that choice can drift from it, which is exactly how a
        probe ends up exercising code the nightly run does not -- the defect
        this method exists to prevent. So the real function decides, against
        this fake's FETCHERS.
        """
        return _pick(_real_fetch_articles, self.FETCHERS, source)


import fetch_articles as _real_fetch_articles  # noqa: E402  (before the fakes swap sys.modules)
import fetch_content as _real_fetch_content  # noqa: E402  (kept before the fakes swap sys.modules)


def _pick(real_module, fetchers, source):
    """Run the real listing_fetcher with this fake's FETCHERS in place."""
    saved = real_module.FETCHERS
    real_module.FETCHERS = fetchers
    try:
        return real_module.listing_fetcher(source)
    finally:
        real_module.FETCHERS = saved


class _FakeFetchContent:
    """Stand-in for fetch_content module."""
    def __init__(self, content_fetchers, min_chars=100):
        self.CONTENT_FETCHERS = content_fetchers
        self.MIN_CONTENT_LENGTH = min_chars
        self.CONTENT_DIR = Path("/tmp")
        self.extraction_paths: list[str] = []

    def drain_extraction_paths(self):
        paths, self.extraction_paths = self.extraction_paths, []
        return paths

    def fetch_with_evidence(self, article, fetcher):
        # The real wrapper: it only needs `requests` and the real module's
        # logger, so the probe is exercised against the same evidence contract
        # production uses. Paths a fake fetcher pushed here ride along.
        result, evidence = _real_fetch_content.fetch_with_evidence(article, fetcher)
        evidence["extraction_paths"] = self.drain_extraction_paths() + evidence["extraction_paths"]
        return result, evidence


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


def test_probe_respects_per_source_content_probe_top_n_override(monkeypatch):
    """Matthews Asia case (2026-07-04): articles[0..2] are short teaser/video
    pages every day, article[3] is the first full-length one. Default top-3
    would FAIL forever; a source-level "content_probe_top_n" override lets
    this specific feed probe deeper without changing the global default."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = _install_per_article_fakes(
        monkeypatch,
        [
            {"title": "p1", "url": "http://a/1", "date": today_iso},
            {"title": "p2", "url": "http://a/2", "date": today_iso},
            {"title": "p3", "url": "http://a/3", "date": today_iso},
            {"title": "full", "url": "http://a/4", "date": today_iso},
        ],
        outcomes={
            "http://a/1": lambda art: _write_chars(art, 50),
            "http://a/2": lambda art: _write_chars(art, 50),
            "http://a/3": lambda art: _write_chars(art, 50),
            "http://a/4": lambda art: _write_chars(art, 500),
        },
    )
    source = {"id": sid, "frequency": "weekly", "content_probe_top_n": 6}
    result = gfh._probe_once(source)
    assert result["status"] == "OK", f"expected OK, got {result}"
    assert result.get("content_probe_index") == 3


def test_probe_without_override_still_defaults_to_top_3(monkeypatch):
    """Sources without a "content_probe_top_n" key keep the global default —
    the override is opt-in per source, not a behavior change for everyone."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = _install_per_article_fakes(
        monkeypatch,
        [
            {"title": "p1", "url": "http://a/1", "date": today_iso},
            {"title": "p2", "url": "http://a/2", "date": today_iso},
            {"title": "p3", "url": "http://a/3", "date": today_iso},
            {"title": "full", "url": "http://a/4", "date": today_iso},
        ],
        outcomes={
            "http://a/1": lambda art: _write_chars(art, 50),
            "http://a/2": lambda art: _write_chars(art, 50),
            "http://a/3": lambda art: _write_chars(art, 50),
            "http://a/4": lambda art: _write_chars(art, 500),
        },
    )
    source = {"id": sid, "frequency": "weekly"}  # no override
    result = gfh._probe_once(source)
    assert result["status"] == "FAIL", f"expected FAIL, got {result}"


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


# ── observed-cadence diagnostic on stale WARN ───────────────────────────────────
# A bursty publisher (e.g. Matthews Asia) clusters several posts within days, then
# goes quiet for weeks. The single-most-recent staleness gate trips during the
# quiet stretch even though that's normal cadence. Appending the observed cadence
# (article count + median/max gap from THIS fetch) to the WARN lets the on-call
# tell a normal quiet stretch from a genuine multi-month freeze at a glance.

def test_stale_warn_appends_observed_cadence(monkeypatch):
    today = datetime.now(gfh.BJT).date()

    def d(days_ago):
        return (today - timedelta(days=days_ago)).strftime("%Y-%m-%d")

    # Newest is 100d old → trips monthly (90d) threshold. Articles themselves are
    # clustered 4–8d apart → small median gap, the hallmark of a bursty source.
    sid = _install_fakes(monkeypatch, [
        {"title": "a", "url": "http://x/1", "date": d(100)},
        {"title": "b", "url": "http://x/2", "date": d(104)},
        {"title": "c", "url": "http://x/3", "date": d(110)},
        {"title": "e", "url": "http://x/4", "date": d(118)},
    ])
    source = {"id": sid, "frequency": "monthly"}
    result = gfh._probe_once(source)

    assert result["status"] == "WARN"
    assert "stale" in result["reason"].lower()
    # New diagnostic: observed cadence appended
    reason = result["reason"].lower()
    assert "observed cadence" in reason, f"no cadence note: {result['reason']}"
    assert "4 dated articles" in reason
    assert "median gap 6d" in reason
    assert "max gap 8d" in reason


def test_ok_result_has_no_cadence_note(monkeypatch):
    """The cadence note is scoped to stale WARNs — a healthy source's reason
    stays empty (no diagnostic noise on OK)."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = _install_fakes(monkeypatch, [
        {"title": "fresh", "url": "http://x/1", "date": today_iso},
        {"title": "fresh2", "url": "http://x/2", "date": today_iso},
    ])
    source = {"id": sid, "frequency": "monthly"}
    result = gfh._probe_once(source)
    assert result["status"] == "OK"
    assert "observed cadence" not in (result["reason"] or "").lower()


# ── zero-article diagnosis (MetLife IM 2026-08-03 incident) ───────────────────
#
# metlife-im returned 0 articles for 3 consecutive runs and the WARN said only
# "fetch_articles returned 0 articles" — nothing about WHY. The actual cause was
# visible in a single request: MIM had retired investments.metlife.com, so the
# configured URL 301'd to a new host and then 302'd to a country/role disclaimer
# interstitial. Distinguishing "site moved" / "content-gated" from "genuinely
# empty" is what turns a 20-minute investigation into a glance at the email.


class _FakeResp:
    def __init__(self, url):
        self.url = url


def _install_requests(monkeypatch, final_url=None, raises=None):
    """Swap in a fake `requests` module for the lazy import inside the helper."""
    import types

    fake = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    def _get(url, **kwargs):
        if raises is not None:
            raise raises
        return _FakeResp(final_url if final_url is not None else url)

    fake.get = _get
    fake.RequestException = RequestException
    monkeypatch.setitem(sys.modules, "requests", fake)
    return fake


def test_diagnose_reports_off_host_redirect(monkeypatch):
    """The MetLife case: configured URL now lands on a different hostname."""
    _install_requests(
        monkeypatch,
        final_url="https://www.metlife.com/investments/en-us/disclaimer/",
    )
    note = gfh._diagnose_zero_articles(
        {"url": "https://investments.metlife.com/insights/"}
    )
    assert "www.metlife.com/investments/en-us/disclaimer/" in note
    assert "host" in note.lower()


def test_diagnose_reports_gate_page_on_same_host(monkeypatch):
    """Same host but the final path is an attestation/consent interstitial."""
    _install_requests(
        monkeypatch,
        final_url="https://example.com/insights/disclaimer?redirect=insights",
    )
    note = gfh._diagnose_zero_articles({"url": "https://example.com/insights/"})
    assert "gate" in note.lower()
    assert "disclaimer" in note


def test_diagnose_silent_when_url_unchanged(monkeypatch):
    """No redirect, no gate — the page really is empty. Add nothing."""
    _install_requests(monkeypatch)
    assert gfh._diagnose_zero_articles({"url": "https://example.com/insights/"}) == ""


def test_diagnose_reports_plain_redirect_same_host(monkeypatch):
    _install_requests(monkeypatch, final_url="https://example.com/research/")
    note = gfh._diagnose_zero_articles({"url": "https://example.com/insights/"})
    assert "https://example.com/research/" in note


def test_diagnose_never_raises_when_probe_fails(monkeypatch):
    """A diagnostic must never turn a WARN into a crashed health run."""
    _install_requests(monkeypatch, raises=OSError("connection reset"))
    note = gfh._diagnose_zero_articles({"url": "https://example.com/insights/"})
    assert "OSError" in note or note == ""


def test_diagnose_returns_empty_without_configured_url(monkeypatch):
    _install_requests(monkeypatch)
    assert gfh._diagnose_zero_articles({}) == ""


def test_probe_zero_articles_reason_carries_the_diagnosis(monkeypatch):
    """End to end: the WARN a human reads must name the redirect target."""
    sid = _install_fakes(monkeypatch, [])
    _install_requests(
        monkeypatch,
        final_url="https://www.metlife.com/investments/en-us/disclaimer/",
    )
    result = gfh._probe_once(
        {"id": sid, "url": "https://investments.metlife.com/insights/"}
    )
    assert result["status"] == "WARN"
    assert "0 articles" in result["reason"]
    assert "www.metlife.com/investments/en-us/disclaimer/" in result["reason"]


# ── extraction-path probe (2026-09-13: seven sources silently on a fallback) ──
#
# A content fetcher whose selector had stopped matching still returned "ok":
# _normalize_html fell back to <main> or the whole page, and navigation text
# alone clears MIN_CONTENT_LENGTH. The probe passed; the stored bodies were
# cookie banners and link lists. The probe now asks fetch_content which path
# the successful extraction took.


def _install_path_fakes(monkeypatch, paths_by_url, date_iso=None):
    date_iso = date_iso or datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = "fake-fund"
    articles = [{"title": u, "url": u, "date": date_iso} for u in paths_by_url]
    fake_fc = _FakeFetchContent({}, min_chars=100)

    def content_fetcher(article):
        entry = paths_by_url[article["url"]]
        fake_fc.extraction_paths.extend(entry["paths"])
        return _write_chars(article, entry["chars"])

    fake_fc.CONTENT_FETCHERS[sid] = content_fetcher
    monkeypatch.setitem(sys.modules, "fetch_articles", _FakeFetchArticles({sid: lambda s: articles}))
    monkeypatch.setitem(sys.modules, "fetch_content", fake_fc)
    return sid


def test_probe_ok_when_extraction_used_the_primary_selector(monkeypatch):
    sid = _install_path_fakes(monkeypatch, {"http://a/1": {"paths": ["primary"], "chars": 500}})
    result = gfh._probe_once({"id": sid, "frequency": "weekly"})
    assert result["status"] == "OK", result


def test_probe_warns_when_extraction_fell_back_to_main(monkeypatch):
    sid = _install_path_fakes(monkeypatch, {"http://a/1": {"paths": ["fallback:main"], "chars": 500}})
    result = gfh._probe_once({"id": sid, "frequency": "weekly"})
    assert result["status"] == "WARN", result
    assert "fallback:main" in result["reason"]
    assert "selector" in result["reason"]


def test_probe_judges_only_the_attempt_that_succeeded(monkeypatch):
    """A short teaser that happened to hit the fallback before the real article
    must not taint a clean extraction -- the drain has to happen per attempt."""
    sid = _install_path_fakes(monkeypatch, {
        "http://a/teaser": {"paths": ["fallback:main"], "chars": 50},
        "http://a/full": {"paths": ["primary"], "chars": 500},
    })
    result = gfh._probe_once({"id": sid, "frequency": "weekly"})
    assert result["status"] == "OK", result
    assert result["content_probe_index"] == 1


def test_fallback_warning_survives_a_stale_warning(monkeypatch):
    """Step 4 used to assign result["reason"] outright; a stale source on a
    fallback must report both, or fixing the staleness hides the selector."""
    sid = _install_path_fakes(
        monkeypatch, {"http://a/1": {"paths": ["fallback:main"], "chars": 500}},
        date_iso="2024-06-01")
    result = gfh._probe_once({"id": sid, "frequency": "monthly"})
    assert result["status"] == "WARN", result
    assert "stale" in result["reason"]
    assert "fallback:main" in result["reason"]


def test_fallback_warning_survives_a_missing_date(monkeypatch):
    sid = _install_path_fakes(monkeypatch, {"http://a/1": {"paths": ["fallback:main"], "chars": 500}})
    import fetch_articles as fa  # the fake installed above
    fa.FETCHERS[sid] = lambda s: [{"title": "x", "url": "http://a/1", "date": None}]
    result = gfh._probe_once({"id": sid, "frequency": "weekly"})
    assert result["status"] == "WARN", result
    assert "no parsed date" in result["reason"]
    assert "fallback:main" in result["reason"]


def test_probe_is_wired_to_the_real_extractor(monkeypatch, tmp_path):
    """Tests above use a fake fetch_content. This one runs a content fetcher
    that calls the real _normalize_html, so a renamed or unwired recorder
    cannot leave the WARN unable to fire."""
    import fetch_articles as real_fa
    import fetch_content as real_fc
    monkeypatch.setitem(sys.modules, "fetch_articles", real_fa)
    monkeypatch.setitem(sys.modules, "fetch_content", real_fc)
    sid = "fake-fund"
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    monkeypatch.setitem(real_fa.FETCHERS, sid,
                        lambda s: [{"title": "t", "url": "http://a/1", "date": today_iso}])

    def content_fetcher(article):
        text = real_fc._normalize_html(
            "<main><p>" + "Real words here. " * 20 + "</p></main>", ".gone p")
        p = real_fc.CONTENT_DIR / f"{article['id']}.txt"
        p.write_text(text)
        return (p, "ok")

    monkeypatch.setitem(real_fc.CONTENT_FETCHERS, sid, content_fetcher)
    result = gfh._probe_once({"id": sid, "frequency": "weekly"})
    assert result["status"] == "WARN", result
    assert "fallback:main" in result["reason"]


# ── swallowed timeouts (wellington 2026-09-24) ─────────────────────────────────
#
# 42 of the 44 content fetchers catch every exception and return None, so a
# Playwright "Page.goto: Timeout 30000ms exceeded" reached the probe as a bare
# None. The probe reported "returned None (selector regression or HTTP error)",
# treated it as non-transient and never used its one retry. The probe now runs
# the fetcher through fetch_content.fetch_with_evidence and labels the failure
# with failure_labels, the same way stage 2 does.

_TIMEOUT_MSG = ("  Wellington: Playwright fetch failed: Page.goto: Timeout 30000ms exceeded.\n"
                "Call log:\n  - navigating to \"http://a/1\", waiting until \"load\"")


def _install_timeout_fakes(monkeypatch, succeed_on_call=None):
    """Every article: log a Playwright timeout on the real fetch_content logger
    and return None, exactly like a production fetcher. If `succeed_on_call`
    is set, the Nth fetcher call (1-based) returns a full body instead."""
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    sid = "fake-fund"
    articles = [{"title": f"a{i}", "url": f"http://a/{i}", "date": today_iso} for i in range(1, 4)]
    calls = {"n": 0}

    def content_fetcher(article):
        calls["n"] += 1
        if succeed_on_call is not None and calls["n"] >= succeed_on_call:
            return _write_chars(article, 500)
        _real_fetch_content.log.error(_TIMEOUT_MSG)
        return None

    monkeypatch.setitem(sys.modules, "fetch_articles", _FakeFetchArticles({sid: lambda s: articles}))
    monkeypatch.setitem(sys.modules, "fetch_content", _FakeFetchContent({sid: content_fetcher}, min_chars=100))
    monkeypatch.setattr(gfh, "RETRY_SLEEP_S", 0)
    return sid, calls


def test_a_swallowed_timeout_is_reported_as_a_fetch_error_not_a_selector_regression(monkeypatch):
    sid, _ = _install_timeout_fakes(monkeypatch)
    result = gfh._probe_once({"id": sid, "frequency": "weekly"})
    assert result["status"] == "FAIL"
    assert "fetch_error" in result["reason"] and "Timeout 30000ms" in result["reason"], result["reason"]
    assert "selector regression" not in result["reason"]


def test_a_swallowed_timeout_counts_as_transient_so_the_probe_retries(monkeypatch):
    sid, calls = _install_timeout_fakes(monkeypatch, succeed_on_call=4)
    result = gfh.probe_source({"id": sid, "frequency": "weekly"})
    assert result["status"] == "OK", result
    assert calls["n"] == 4  # three timeouts, then the retry's first article


def test_a_raised_non_transient_error_still_gets_no_retry(monkeypatch):
    today_iso = datetime.now(gfh.BJT).strftime("%Y-%m-%d")
    calls = {"n": 0}

    def boom(article):
        calls["n"] += 1
        raise ValueError("selector returned empty")

    sid = _install_per_article_fakes(
        monkeypatch,
        [{"title": "a", "url": "http://a/1", "date": today_iso}],
        outcomes={"http://a/1": boom})
    monkeypatch.setattr(gfh, "RETRY_SLEEP_S", 0)
    result = gfh.probe_source({"id": sid, "frequency": "weekly"})
    assert result["status"] == "FAIL" and "ValueError" in result["reason"]
    assert calls["n"] == 1
