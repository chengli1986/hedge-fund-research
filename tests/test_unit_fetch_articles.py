"""Unit tests for fetch_articles.py — article_id, parse_date, _validate_hostname, load_existing_ids, entrypoints."""

import json
from unittest.mock import MagicMock, patch
import pytest
from fetch_articles import (
    article_id, parse_date, _validate_hostname, load_existing_ids, fetch_oaktree, fetch_wellington,
    fetch_troweprice, _is_date_eyebrow, DATA_FILE,
    load_entrypoints, get_source_url, record_quality_metrics, check_anomalies,
)


# ---------------------------------------------------------------------------
# article_id
# ---------------------------------------------------------------------------

class TestArticleId:
    def test_deterministic(self):
        """Same inputs always produce the same ID."""
        id1 = article_id("aqr", "https://www.aqr.com/foo")
        id2 = article_id("aqr", "https://www.aqr.com/foo")
        assert id1 == id2

    def test_unique_across_urls(self):
        """Different URLs produce different IDs for the same source."""
        id1 = article_id("aqr", "https://www.aqr.com/foo")
        id2 = article_id("aqr", "https://www.aqr.com/bar")
        assert id1 != id2

    def test_unique_across_sources(self):
        """Same URL with different sources produces different IDs."""
        id1 = article_id("aqr", "https://example.com/article")
        id2 = article_id("gmo", "https://example.com/article")
        assert id1 != id2

    def test_length(self):
        """IDs are 16 hex characters."""
        aid = article_id("man-group", "https://www.man.com/insights/test")
        assert len(aid) == 16
        assert all(c in "0123456789abcdef" for c in aid)


# ---------------------------------------------------------------------------
# parse_date
# ---------------------------------------------------------------------------

class TestParseDate:
    def test_long_month_day_year(self):
        assert parse_date("March 18, 2026") == "2026-03-18"

    def test_short_month_day_year(self):
        assert parse_date("Mar 18, 2026") == "2026-03-18"

    def test_day_long_month_year(self):
        assert parse_date("18 March 2026") == "2026-03-18"

    def test_iso_format(self):
        assert parse_date("2026-03-18") == "2026-03-18"

    def test_month_year_only(self):
        result = parse_date("March 2026")
        assert result == "2026-03-01"

    def test_invalid_returns_none(self):
        assert parse_date("not a date") is None

    def test_empty_returns_none(self):
        assert parse_date("") is None

    def test_whitespace_stripped(self):
        assert parse_date("  March 18, 2026  ") == "2026-03-18"


# ---------------------------------------------------------------------------
# _validate_hostname
# ---------------------------------------------------------------------------

class TestValidateHostname:
    def test_www_subdomain_match(self):
        """www.aqr.com ends with .aqr.com, so it matches."""
        assert _validate_hostname("https://www.aqr.com/Insights/Research", "aqr.com") is True

    def test_subdomain_match(self):
        assert _validate_hostname("https://papers.aqr.com/article", "aqr.com") is True

    def test_bare_domain_match(self):
        assert _validate_hostname("https://aqr.com/insights", "aqr.com") is True

    def test_mismatch_rejected(self):
        assert _validate_hostname("https://www.oaktreecapital.com/memo", "aqr.com") is False

    def test_different_tld_rejected(self):
        assert _validate_hostname("https://www.aqr.org/insights", "aqr.com") is False

    def test_partial_name_rejected(self):
        """notaqr.com should not match aqr.com."""
        assert _validate_hostname("https://notaqr.com/page", "aqr.com") is False

    def test_empty_url(self):
        assert _validate_hostname("", "aqr.com") is False


# ---------------------------------------------------------------------------
# load_existing_ids
# ---------------------------------------------------------------------------

class TestLoadExistingIds:
    def test_empty_file(self, tmp_path, monkeypatch):
        empty_file = tmp_path / "articles.jsonl"
        empty_file.write_text("")
        monkeypatch.setattr("fetch_articles.DATA_FILE", empty_file)
        assert load_existing_ids() == set()

    def test_missing_file(self, tmp_path, monkeypatch):
        missing_file = tmp_path / "nonexistent.jsonl"
        monkeypatch.setattr("fetch_articles.DATA_FILE", missing_file)
        assert load_existing_ids() == set()

    def test_parses_ids_from_jsonl(self, tmp_path, monkeypatch):
        jsonl_file = tmp_path / "articles.jsonl"
        lines = [
            json.dumps({"id": "abc123", "title": "Test 1"}),
            json.dumps({"id": "def456", "title": "Test 2"}),
        ]
        jsonl_file.write_text("\n".join(lines) + "\n")
        monkeypatch.setattr("fetch_articles.DATA_FILE", jsonl_file)
        ids = load_existing_ids()
        assert ids == {"abc123", "def456"}

    def test_skips_invalid_json(self, tmp_path, monkeypatch):
        jsonl_file = tmp_path / "articles.jsonl"
        jsonl_file.write_text('{"id": "good"}\nnot json\n{"id": "also_good"}\n')
        monkeypatch.setattr("fetch_articles.DATA_FILE", jsonl_file)
        ids = load_existing_ids()
        assert ids == {"good", "also_good"}


class TestFetchOaktree:
    def test_filters_external_links_before_truncation(self):
        html = """
        <div class="insight-item">
          <a href="https://www.bloomberg.com/news/videos/foo">
            <span class="title-link">External Video</span>
            <span class="read-more">Watch</span>
            <time class="date" datetime="2026-03-13T00:00:00Z">March 13, 2026</time>
          </a>
        </div>
        <div class="insight-item">
          <a href="/insights/memo-one">
            <span class="title-link">Memo One</span>
            <span class="read-more">Read</span>
            <time class="date" datetime="2026-03-14T00:00:00Z">March 14, 2026</time>
          </a>
        </div>
        <div class="insight-item">
          <a href="/insights/memo-two">
            <span class="title-link">Memo Two</span>
            <span class="read-more">Read</span>
            <time class="date" datetime="2026-03-15T00:00:00Z">March 15, 2026</time>
          </a>
        </div>
        """
        source = {
            "id": "oaktree",
            "url": "https://www.oaktreecapital.com/insights",
            "max_articles": 2,
            "expected_hostname": "oaktreecapital.com",
        }

        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_oaktree(source)

        assert len(articles) == 2
        assert [a["title"] for a in articles] == ["Memo One", "Memo Two"]
        assert all("oaktreecapital.com" in a["url"] for a in articles)


# ---------------------------------------------------------------------------
# Entrypoints integration
# ---------------------------------------------------------------------------

class TestLoadEntrypoints:
    def test_loads_from_file(self, tmp_path, monkeypatch):
        ep_file = tmp_path / "entrypoints.json"
        ep_file.write_text(json.dumps({
            "version": 1,
            "sources": {
                "aqr": {
                    "entrypoints": [
                        {"url": "https://www.aqr.com/new-research", "content_type": "research_index",
                         "confidence": 0.9, "active": True}
                    ]
                }
            }
        }))
        monkeypatch.setattr("fetch_articles.ENTRYPOINTS_FILE", ep_file)
        ep = load_entrypoints()
        assert "aqr" in ep["sources"]

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("fetch_articles.ENTRYPOINTS_FILE", tmp_path / "nope.json")
        ep = load_entrypoints()
        assert ep == {"version": 1, "sources": {}}


class TestGetSourceUrl:
    def test_uses_entrypoint_when_available(self):
        ep = {"version": 1, "sources": {
            "aqr": {"entrypoints": [
                {"url": "https://www.aqr.com/new", "active": True, "confidence": 0.9, "content_type": "research_index"}
            ]}
        }}
        source = {"id": "aqr", "url": "https://www.aqr.com/old"}
        assert get_source_url(source, ep) == "https://www.aqr.com/new"

    def test_fallback_to_source_url(self):
        ep = {"version": 1, "sources": {}}
        source = {"id": "aqr", "url": "https://www.aqr.com/old"}
        assert get_source_url(source, ep) == "https://www.aqr.com/old"

    def test_skips_inactive_entrypoint(self):
        ep = {"version": 1, "sources": {
            "aqr": {"entrypoints": [
                {"url": "https://www.aqr.com/new", "active": False, "confidence": 0.9, "content_type": "research_index"}
            ]}
        }}
        source = {"id": "aqr", "url": "https://www.aqr.com/old"}
        assert get_source_url(source, ep) == "https://www.aqr.com/old"


class TestRecordQualityMetrics:
    def test_records_metrics(self, tmp_path, monkeypatch):
        state_file = tmp_path / "inspection_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr("fetch_articles.INSPECTION_STATE_FILE", state_file)
        record_quality_metrics("aqr", total_found=5, new_count=3, gated_count=0, mismatch_count=0)
        state = json.loads(state_file.read_text())
        assert state["aqr"]["last_article_count"] == 5
        assert state["aqr"]["consecutive_zero_count"] == 0

    def test_increments_consecutive_zero(self, tmp_path, monkeypatch):
        state_file = tmp_path / "inspection_state.json"
        state_file.write_text(json.dumps({"aqr": {"consecutive_zero_count": 1, "last_article_count": 0}}))
        monkeypatch.setattr("fetch_articles.INSPECTION_STATE_FILE", state_file)
        record_quality_metrics("aqr", total_found=0, new_count=0, gated_count=0, mismatch_count=0)
        state = json.loads(state_file.read_text())
        assert state["aqr"]["consecutive_zero_count"] == 2

    def test_resets_consecutive_zero_on_success(self, tmp_path, monkeypatch):
        state_file = tmp_path / "inspection_state.json"
        state_file.write_text(json.dumps({"aqr": {"consecutive_zero_count": 3, "last_article_count": 0}}))
        monkeypatch.setattr("fetch_articles.INSPECTION_STATE_FILE", state_file)
        record_quality_metrics("aqr", total_found=5, new_count=2, gated_count=0, mismatch_count=0)
        state = json.loads(state_file.read_text())
        assert state["aqr"]["consecutive_zero_count"] == 0


class TestCheckAnomalies:
    def test_no_anomaly(self):
        metrics = {"consecutive_zero_count": 0, "last_article_count": 5,
                   "last_valid_body_ratio": 0.8, "last_gated_ratio": 0.0, "last_mismatch_count": 0}
        assert check_anomalies(metrics) == []

    def test_consecutive_zero(self):
        metrics = {"consecutive_zero_count": 2, "last_article_count": 0,
                   "last_valid_body_ratio": 1.0, "last_gated_ratio": 0.0, "last_mismatch_count": 0}
        alerts = check_anomalies(metrics)
        assert any("zero" in a.lower() for a in alerts)

    def test_high_gated_ratio(self):
        metrics = {"consecutive_zero_count": 0, "last_article_count": 10,
                   "last_valid_body_ratio": 0.4, "last_gated_ratio": 0.6, "last_mismatch_count": 0}
        alerts = check_anomalies(metrics)
        assert any("gated" in a.lower() for a in alerts)

    def test_high_mismatch(self):
        metrics = {"consecutive_zero_count": 0, "last_article_count": 10,
                   "last_valid_body_ratio": 0.8, "last_gated_ratio": 0.0, "last_mismatch_count": 5}
        alerts = check_anomalies(metrics)
        assert any("mismatch" in a.lower() for a in alerts)


class TestFetchWellington:
    def test_parses_articles(self):
        html = """
        <html><body>
        <section class="insight article has-image">
          <div class="insight__content">
            <div class="insight__head">
              <div class="insight__contentType"><span>Article</span></div>
              <div class="insight__date">
                <date datetime="2026-04-08"><span>April 2026</span></date>
              </div>
            </div>
            <a class="insight__title" href="/en/insights/quarterly-outlook-q2-2026">
              Quarterly Asset Allocation Outlook Q2 2026
            </a>
            <a class="insight__link" href="/en/insights/quarterly-outlook-q2-2026">Read more</a>
          </div>
        </section>
        <section class="insight article has-image">
          <div class="insight__content">
            <div class="insight__head">
              <div class="insight__contentType"><span>Whitepaper</span></div>
              <div class="insight__date">
                <date datetime="2026-03-15"><span>March 2026</span></date>
              </div>
            </div>
            <a class="insight__title" href="/en/insights/credit-outlook-2026">
              Credit Outlook 2026
            </a>
            <a class="insight__link" href="/en/insights/credit-outlook-2026">Read more</a>
          </div>
        </section>
        </body></html>
        """
        source = {
            "id": "wellington",
            "url": "https://www.wellington.com/en/insights",
            "max_articles": 10,
            "expected_hostname": "wellington.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_wellington(source)

        assert len(articles) == 2
        assert articles[0]["title"] == "Quarterly Asset Allocation Outlook Q2 2026"
        assert articles[0]["url"] == "https://www.wellington.com/en/insights/quarterly-outlook-q2-2026"
        assert articles[0]["date"] == "2026-04-08"
        assert articles[0]["category"] == "Article"
        assert articles[1]["title"] == "Credit Outlook 2026"
        assert articles[1]["date"] == "2026-03-15"

    def test_respects_max_articles(self):
        html = """
        <html><body>
        <section class="insight article">
          <a class="insight__title" href="/en/insights/a1">Article One</a>
          <a class="insight__link" href="/en/insights/a1">Read</a>
          <div class="insight__date"><date datetime="2026-04-01"></date></div>
          <div class="insight__contentType"><span>Article</span></div>
        </section>
        <section class="insight article">
          <a class="insight__title" href="/en/insights/a2">Article Two</a>
          <a class="insight__link" href="/en/insights/a2">Read</a>
          <div class="insight__date"><date datetime="2026-03-01"></date></div>
          <div class="insight__contentType"><span>Article</span></div>
        </section>
        <section class="insight article">
          <a class="insight__title" href="/en/insights/a3">Article Three</a>
          <a class="insight__link" href="/en/insights/a3">Read</a>
          <div class="insight__date"><date datetime="2026-02-01"></date></div>
          <div class="insight__contentType"><span>Article</span></div>
        </section>
        </body></html>
        """
        source = {
            "id": "wellington",
            "url": "https://www.wellington.com/en/insights",
            "max_articles": 2,
            "expected_hostname": "wellington.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_wellington(source)
        assert len(articles) == 2

    def test_skips_external_urls(self):
        html = """
        <html><body>
        <section class="insight article">
          <a class="insight__title" href="https://other-site.com/article">External</a>
          <a class="insight__link" href="https://other-site.com/article">Read</a>
          <div class="insight__date"><date datetime="2026-04-01"></date></div>
          <div class="insight__contentType"><span>Article</span></div>
        </section>
        <section class="insight article">
          <a class="insight__title" href="/en/insights/valid">Valid Article</a>
          <a class="insight__link" href="/en/insights/valid">Read</a>
          <div class="insight__date"><date datetime="2026-04-02"></date></div>
          <div class="insight__contentType"><span>Article</span></div>
        </section>
        </body></html>
        """
        source = {
            "id": "wellington",
            "url": "https://www.wellington.com/en/insights",
            "max_articles": 10,
            "expected_hostname": "wellington.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_wellington(source)
        assert len(articles) == 1
        assert "wellington.com" in articles[0]["url"]


class TestIsDateEyebrow:
    def test_month_name_is_date(self):
        assert _is_date_eyebrow("April 17, 2026") is True

    def test_abbreviated_month_is_date(self):
        assert _is_date_eyebrow("Jan 2026") is True

    def test_digit_start_is_date(self):
        assert _is_date_eyebrow("2026-04-17") is True

    def test_category_is_not_date(self):
        assert _is_date_eyebrow("Markets & Economy") is False

    def test_empty_string_is_not_date(self):
        assert _is_date_eyebrow("") is False

    def test_may_auxiliary_known_limitation(self):
        # "may" as auxiliary verb is a known false positive
        assert _is_date_eyebrow("You May Also Like") is True


class TestFetchTroweprice:
    def test_parses_articles(self):
        html = """
        <html><body>
        <div class="b-grid-item--12-col">
          <a href="/personal-investing/insights/markets-and-economy/q2-outlook-2026">Read</a>
          <span class="cmp-tile__heading">Q2 2026 Market Outlook</span>
          <span class="cmp-tile__eyebrow">Markets &amp; Economy</span>
          <span class="cmp-tile__eyebrow">April 17, 2026</span>
        </div>
        <div class="b-grid-item--12-col">
          <a href="/personal-investing/insights/fixed-income/bond-outlook">Read</a>
          <span class="cmp-tile__heading">Fixed Income Perspectives</span>
          <span class="cmp-tile__eyebrow">Fixed Income</span>
          <span class="cmp-tile__eyebrow">March 5, 2026</span>
        </div>
        </body></html>
        """
        source = {
            "id": "troweprice",
            "url": "https://www.troweprice.com/personal-investing/insights.html",
            "max_articles": 10,
            "expected_hostname": "troweprice.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_troweprice(source)

        assert len(articles) == 2
        assert articles[0]["title"] == "Q2 2026 Market Outlook"
        assert articles[0]["url"] == "https://www.troweprice.com/personal-investing/insights/markets-and-economy/q2-outlook-2026"
        assert articles[0]["date"] == "2026-04-17"
        assert articles[0]["category"] == "Markets & Economy"
        assert articles[1]["title"] == "Fixed Income Perspectives"
        assert articles[1]["date"] == "2026-03-05"

    def test_skips_cards_without_insights_link(self):
        html = """
        <html><body>
        <div class="b-grid-item--12-col">
          <a href="/personal-investing/navigation-link">Nav Item</a>
          <span class="cmp-tile__heading">Navigation</span>
        </div>
        <div class="b-grid-item--12-col">
          <a href="/personal-investing/insights/equity/growth-2026">Read</a>
          <span class="cmp-tile__heading">Growth Equity Outlook</span>
          <span class="cmp-tile__eyebrow">Equity</span>
          <span class="cmp-tile__eyebrow">April 10, 2026</span>
        </div>
        </body></html>
        """
        source = {
            "id": "troweprice",
            "url": "https://www.troweprice.com/personal-investing/insights.html",
            "max_articles": 10,
            "expected_hostname": "troweprice.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_troweprice(source)

        assert len(articles) == 1
        assert articles[0]["title"] == "Growth Equity Outlook"

    def test_respects_max_articles(self):
        cards = "".join(
            f'<div class="b-grid-item--12-col">'
            f'<a href="/personal-investing/insights/article-{i}">Read</a>'
            f'<span class="cmp-tile__heading">Article {i}</span>'
            f'<span class="cmp-tile__eyebrow">April {i}, 2026</span>'
            f'</div>'
            for i in range(1, 6)
        )
        html = f"<html><body>{cards}</body></html>"
        source = {
            "id": "troweprice",
            "url": "https://www.troweprice.com/personal-investing/insights.html",
            "max_articles": 3,
            "expected_hostname": "troweprice.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_troweprice(source)

        assert len(articles) == 3
        assert articles[0]["title"] == "Article 1"
