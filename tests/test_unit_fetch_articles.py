"""Unit tests for fetch_articles.py — article_id, parse_date, _validate_hostname, load_existing_ids, entrypoints."""

import json
from unittest.mock import MagicMock, patch
import pytest
from fetch_articles import (
    article_id, parse_date, _validate_hostname, load_existing_ids, fetch_oaktree, fetch_wellington,
    fetch_franklin_templeton,
    fetch_troweprice, fetch_researchaffiliates, fetch_pimco, _is_date_eyebrow, DATA_FILE,
    load_entrypoints, get_source_url, record_quality_metrics, check_anomalies,
    fetch_blackstone, fetch_gsam, _fetch_article_date_jsonld,
    fetch_amundi, fetch_jpmam, fetch_pgim, fetch_aberdeen,
    fetch_cambridge_associates, fetch_verdad,
    fetch_metlife_im,
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
        # Month-only dates use last day of month so staleness checks don't
        # penalise sources 30 days earlier than needed (KKR / Cambridge pattern)
        assert parse_date("March 2026") == "2026-03-31"

    def test_month_year_only_abbrev(self):
        assert parse_date("Mar 2026") == "2026-03-31"

    def test_month_year_only_end_of_month(self):
        assert parse_date("May 2026") == "2026-05-31"

    def test_month_year_only_feb_leap(self):
        assert parse_date("Feb 2024") == "2024-02-29"

    def test_month_year_only_feb_nonleap(self):
        assert parse_date("Feb 2025") == "2025-02-28"

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


class TestFetchFranklinTempleton:
    def test_parses_cards_drops_hubs_and_sorts(self):
        # Two real posts + one hub tile (no date, /articles/hubs/) that must drop.
        html = """
        <div class="card-article">
          <a class="card-article__inner" href="/articles/2026/equity/older-post">
            <p class="card-article__date">JULY 14, 2026</p>
            <h4 class="title--h4">Older Post</h4>
          </a>
        </div>
        <div class="card-article">
          <a class="card-article__inner" href="/articles/2026/multi-asset/newer-post">
            <p class="card-article__date">JULY 20, 2026</p>
            <h4 class="title--h4">Newer Post</h4>
          </a>
        </div>
        <div class="card-article">
          <a class="card-article__inner" href="/articles/hubs/clarion">
            <h4 class="title--h4">Clarion Partners Insights</h4>
          </a>
        </div>
        """
        source = {
            "id": "franklin-templeton",
            "url": "https://www.franklintempletonglobal.com/articles",
            "max_articles": 10,
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_franklin_templeton(source)

        # Hub tile (no date) is dropped; posts sorted newest-first.
        assert [a["title"] for a in articles] == ["Newer Post", "Older Post"]
        assert articles[0]["date"] == "2026-07-20"
        assert articles[1]["date"] == "2026-07-14"
        assert all(
            a["url"].startswith("https://www.franklintempletonglobal.com/articles/")
            for a in articles
        )
        assert not any("/hubs/" in a["url"] for a in articles)

    def test_respects_max_articles(self):
        cards = "".join(
            f'<div class="card-article"><a href="/articles/2026/x/post-{i}">'
            f'<p class="card-article__date">JULY {i:02d}, 2026</p>'
            f'<h4 class="title--h4">Post {i}</h4></a></div>'
            for i in range(1, 6)
        )
        source = {"id": "franklin-templeton",
                  "url": "https://www.franklintempletonglobal.com/articles",
                  "max_articles": 2}
        with patch("fetch_articles._get_playwright_page", return_value=cards):
            articles = fetch_franklin_templeton(source)
        assert len(articles) == 2


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
          <h2 class="beacon-article-tile__title">
            <a href="/en/us/insights/q2-2026-market-outlook">Q2 2026 Market Outlook</a>
          </h2>
          <span class="beacon-article-tile__eyebrow">April 17, 2026 · Markets &amp; Economy</span>
        </div>
        <div class="b-grid-item--12-col">
          <h2 class="beacon-article-tile__title">
            <a href="/en/us/insights/bond-outlook">Fixed Income Perspectives</a>
          </h2>
          <span class="beacon-article-tile__eyebrow">Mar 2026 · Fixed Income</span>
        </div>
        </body></html>
        """
        source = {
            "id": "troweprice",
            "url": "https://www.troweprice.com/en/us/insights",
            "max_articles": 10,
            "expected_hostname": "troweprice.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_troweprice(source)

        assert len(articles) == 2
        assert articles[0]["title"] == "Q2 2026 Market Outlook"
        assert articles[0]["url"] == "https://www.troweprice.com/en/us/insights/q2-2026-market-outlook"
        assert articles[0]["date"] == "2026-04-17"
        assert articles[0]["category"] == "Markets & Economy"
        assert articles[1]["title"] == "Fixed Income Perspectives"

    def test_skips_cards_without_beacon_title(self):
        html = """
        <html><body>
        <div class="b-grid-item--12-col">
          <p>Navigation item with no title</p>
          <a href="/en/us/insights/orphan-link">Link without beacon title</a>
        </div>
        <div class="b-grid-item--12-col">
          <h2 class="beacon-article-tile__title">
            <a href="/en/us/insights/growth-2026">Growth Equity Outlook</a>
          </h2>
          <span class="beacon-article-tile__eyebrow">Apr 2026 · Equity</span>
        </div>
        </body></html>
        """
        source = {
            "id": "troweprice",
            "url": "https://www.troweprice.com/en/us/insights",
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
            f'<h2 class="beacon-article-tile__title">'
            f'<a href="/en/us/insights/article-{i}">Article {i}</a>'
            f'</h2>'
            f'<span class="beacon-article-tile__eyebrow">April {i}, 2026 · Markets</span>'
            f'</div>'
            for i in range(1, 6)
        )
        html = f"<html><body>{cards}</body></html>"
        source = {
            "id": "troweprice",
            "url": "https://www.troweprice.com/en/us/insights",
            "max_articles": 3,
            "expected_hostname": "troweprice.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_troweprice(source)

        assert len(articles) == 3
        assert articles[0]["title"] == "Article 1"


# ---------------------------------------------------------------------------
# fetch_researchaffiliates
# ---------------------------------------------------------------------------

class TestFetchResearchAffiliates:
    SOURCE = {
        "id": "research-affiliates",
        "url": "https://www.researchaffiliates.com/insights/publications",
        "max_articles": 10,
        "expected_hostname": "researchaffiliates.com",
    }

    def test_parses_articles(self):
        html = """
        <html><body>
        <a href="/insights/publications/articles/1111-when-will-ai-be-profitable">
          <div class="flex gap-4 cursor-pointer">
            <div><img alt="When Will AI Be Both Powerful and Profitable?" /></div>
            <div class="flex flex-col flex-1">
              <div>APR 2026</div>
              <div>When Will AI Be Both Powerful and Profitable?</div>
            </div>
          </div>
        </a>
        <a href="/insights/publications/articles/1112-winning-long-game">
          <div class="flex gap-4 cursor-pointer">
            <div><img alt="Winning the Long Game with RAFI" /></div>
            <div class="flex flex-col flex-1">
              <div>MAR 2026</div>
              <div>Winning the Long Game with RAFI</div>
            </div>
          </div>
        </a>
        </body></html>
        """
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_researchaffiliates(self.SOURCE)

        assert len(articles) == 2
        assert articles[0]["title"] == "When Will AI Be Both Powerful and Profitable?"
        assert articles[0]["url"] == "https://www.researchaffiliates.com/insights/publications/articles/1111-when-will-ai-be-profitable"
        assert articles[0]["date"] == "2026-04-30"
        assert articles[0]["date_raw"] == "APR 2026"
        assert articles[1]["title"] == "Winning the Long Game with RAFI"
        assert articles[1]["date"] == "2026-03-31"

    def test_dedups_repeated_anchors(self):
        """Site renders the same article in multiple rails — dedup by URL."""
        html = """
        <html><body>
        <a href="/insights/publications/articles/1111-article">
          <div class="flex flex-col flex-1"><div>APR 2026</div><div>Article 1</div></div>
        </a>
        <a href="/insights/publications/articles/1111-article">
          <div class="flex flex-col flex-1"><div>APR 2026</div><div>Article 1</div></div>
        </a>
        <a href="/insights/publications/articles/1112-other">
          <div class="flex flex-col flex-1"><div>MAR 2026</div><div>Article 2</div></div>
        </a>
        </body></html>
        """
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_researchaffiliates(self.SOURCE)

        assert len(articles) == 2
        assert articles[0]["title"] == "Article 1"
        assert articles[1]["title"] == "Article 2"

    def test_falls_back_to_text_block_when_img_missing(self):
        """Without <img alt>, title comes from second div in flex-col block."""
        html = """
        <html><body>
        <a href="/insights/publications/articles/1113-no-image">
          <div class="flex flex-col flex-1">
            <div>FEB 2026</div>
            <div>Article Without Image</div>
          </div>
        </a>
        </body></html>
        """
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_researchaffiliates(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["title"] == "Article Without Image"
        assert articles[0]["date"] == "2026-02-28"

    def test_respects_max_articles(self):
        items = "".join(
            f'<a href="/insights/publications/articles/{i}-article">'
            f'<div class="flex flex-col flex-1"><div>APR 2026</div>'
            f'<div>Article {i}</div></div></a>'
            for i in range(1, 8)
        )
        html = f"<html><body>{items}</body></html>"
        source = {**self.SOURCE, "max_articles": 4}
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_researchaffiliates(source)

        assert len(articles) == 4


# ---------------------------------------------------------------------------
# fetch_pimco
# ---------------------------------------------------------------------------

class TestFetchPimco:
    SOURCE = {
        "id": "pimco",
        "url": "https://www.pimco.com/us/en/insights",
        "max_articles": 10,
        "expected_hostname": "pimco.com",
    }

    def _card(self, title: str, href: str, date: str) -> str:
        return (
            f'<div class="coveo-list-layout CoveoResult">'
            f'<a class="CoveoResultLink" href="{href}">{title}</a>'
            f'<div class="coveo-result-row result-date">{date}</div>'
            f'</div>'
        )

    def test_parses_articles(self):
        html = "<html><body>" + \
            self._card("Why the Fed Could Shrink Its Balance Sheet",
                       "https://www.pimco.com/us/en/insights/why-the-fed",
                       "4/16/2026") + \
            self._card("Layered Uncertainty: Conflict, Credit Stress, and AI",
                       "https://www.pimco.com/us/en/insights/layered-uncertainty",
                       "3/25/2026") + \
            "</body></html>"
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_pimco(self.SOURCE)

        assert len(articles) == 2
        assert articles[0]["title"] == "Why the Fed Could Shrink Its Balance Sheet"
        assert articles[0]["url"] == "https://www.pimco.com/us/en/insights/why-the-fed"
        assert articles[0]["date"] == "2026-04-16"
        assert articles[0]["date_raw"] == "4/16/2026"
        assert articles[1]["date"] == "2026-03-25"

    def test_skips_cards_without_link(self):
        html = "<html><body>" + \
            '<div class="coveo-list-layout CoveoResult"><div class="result-date">4/1/2026</div></div>' + \
            self._card("Valid Article",
                       "https://www.pimco.com/us/en/insights/valid-article",
                       "4/1/2026") + \
            "</body></html>"
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_pimco(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["title"] == "Valid Article"

    def test_respects_max_articles(self):
        cards = "".join(
            self._card(f"Article {i}",
                       f"https://www.pimco.com/us/en/insights/article-{i}",
                       f"4/{i}/2026")
            for i in range(1, 8)
        )
        html = f"<html><body>{cards}</body></html>"
        source = {**self.SOURCE, "max_articles": 5}
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_pimco(source)

        assert len(articles) == 5
        assert articles[0]["title"] == "Article 1"


# ---------------------------------------------------------------------------
# fetch_blackstone
# ---------------------------------------------------------------------------

class TestFetchBlackstone:
    SOURCE = {
        "url": "https://www.blackstone.com/insights/",
        "max_articles": 10,
        "expected_hostname": "blackstone.com",
    }

    def _card(self, title: str, href: str, date: str) -> str:
        return (
            f'<article class="bx-article-card">'
            f'<h4 class="bx-article-title"><a href="{href}">{title}</a></h4>'
            f'<time datetime="{date}">{date}</time>'
            f'</article>'
        )

    def test_parses_articles(self):
        html = "<html><body>" + \
            self._card("Private Credit: Myth vs. Fact",
                       "/insights/article/private-credit-myth-vs-fact/",
                       "April 16, 2026") + \
            self._card("Real Estate Enters the Next Phase",
                       "/insights/article/real-estate-next-phase/",
                       "March 31, 2026") + \
            "</body></html>"
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_blackstone(self.SOURCE)

        assert len(articles) == 2
        assert articles[0]["title"] == "Private Credit: Myth vs. Fact"
        assert articles[0]["url"] == "https://www.blackstone.com/insights/article/private-credit-myth-vs-fact/"
        assert articles[0]["date"] == "2026-04-16"
        assert articles[1]["date"] == "2026-03-31"

    def test_skips_cards_without_link(self):
        html = (
            "<html><body>"
            '<article class="bx-article-card"><h4 class="bx-article-title"></h4></article>'
            + self._card("Valid Article", "/insights/valid/", "April 1, 2026")
            + "</body></html>"
        )
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_blackstone(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["title"] == "Valid Article"

    def test_respects_max_articles(self):
        cards = "".join(
            self._card(f"Article {i}", f"/insights/article-{i}/", f"April {i}, 2026")
            for i in range(1, 8)
        )
        html = f"<html><body>{cards}</body></html>"
        source = {**self.SOURCE, "max_articles": 4}
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_blackstone(source)

        assert len(articles) == 4


# ---------------------------------------------------------------------------
# _fetch_article_date_jsonld
# ---------------------------------------------------------------------------

class TestFetchArticleDateJsonld:
    def _mock_response(self, body: str, status: int = 200):
        resp = MagicMock()
        resp.status_code = status
        resp.text = body
        resp.raise_for_status.return_value = None
        return resp

    def test_extracts_date_from_jsonld_object(self):
        html = (
            '<html><head>'
            '<script type="application/ld+json">'
            '{"@type": "Article", "datePublished": "2026-04-15T00:00:00Z"}'
            '</script>'
            '</head></html>'
        )
        with patch("fetch_articles.requests.get", return_value=self._mock_response(html)):
            result = _fetch_article_date_jsonld("https://am.gs.com/en-us/insights/test")
        assert result == "2026-04-15"

    def test_extracts_date_from_jsonld_list(self):
        html = (
            '<html><head>'
            '<script type="application/ld+json">'
            '[{"@type": "BreadcrumbList"}, {"@type": "Article", "datePublished": "2026-03-20"}]'
            '</script>'
            '</head></html>'
        )
        with patch("fetch_articles.requests.get", return_value=self._mock_response(html)):
            result = _fetch_article_date_jsonld("https://am.gs.com/en-us/insights/test")
        assert result == "2026-03-20"

    def test_returns_none_when_no_jsonld(self):
        html = "<html><head></head><body>No JSON-LD here</body></html>"
        with patch("fetch_articles.requests.get", return_value=self._mock_response(html)):
            result = _fetch_article_date_jsonld("https://am.gs.com/en-us/insights/test")
        assert result is None

    def test_returns_none_on_request_failure(self):
        with patch("fetch_articles.requests.get", side_effect=Exception("network error")):
            result = _fetch_article_date_jsonld("https://am.gs.com/en-us/insights/test")
        assert result is None


# ---------------------------------------------------------------------------
# fetch_gsam
# ---------------------------------------------------------------------------

class TestFetchGsam:
    SOURCE = {
        "url": "https://am.gs.com/en-us/advisors/insights/list",
        "max_articles": 10,
        "expected_hostname": "am.gs.com",
    }

    def _api_response(self, hits: list) -> dict:
        return {"nbHits": len(hits), "insights": {"hits": hits, "nbHits": len(hits)}}

    def _hit(self, title: str, page_path: str, publish_date: str) -> dict:
        return {"title": title, "pagePath": page_path, "publishDate": publish_date}

    def _mock_resp(self, data: dict):
        resp = MagicMock()
        resp.json.return_value = data
        resp.raise_for_status.return_value = None
        return resp

    def test_parses_articles(self):
        api_data = self._api_response([
            self._hit("Fixed Income Outlook 2Q 2026",
                      "/en-us/advisors/insights/article/fixed-income-outlook",
                      "2026-04-17T04:00:00.000Z"),
            self._hit("Investment Outlook 2026",
                      "/en-us/advisors/insights/article/investment-outlook",
                      "2026-01-10T00:00:00.000Z"),
        ])
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(api_data)):
            articles = fetch_gsam(self.SOURCE)

        assert len(articles) == 2
        assert articles[0]["title"] == "Fixed Income Outlook 2Q 2026"
        assert articles[0]["url"] == "https://am.gs.com/en-us/advisors/insights/article/fixed-income-outlook"
        assert articles[0]["date"] == "2026-04-17"
        assert articles[1]["date"] == "2026-01-10"

    def test_skips_hits_without_title(self):
        api_data = self._api_response([
            {"title": "", "pagePath": "/en-us/advisors/insights/article/no-title", "publishDate": "2026-04-01T00:00:00.000Z"},
            self._hit("Valid Article", "/en-us/advisors/insights/article/valid", "2026-04-15T00:00:00.000Z"),
        ])
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(api_data)):
            articles = fetch_gsam(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["title"] == "Valid Article"

    def test_respects_max_articles(self):
        api_data = self._api_response([
            self._hit(f"Article {i}", f"/en-us/advisors/insights/article/article-{i}",
                      f"2026-04-{i:02d}T00:00:00.000Z")
            for i in range(1, 8)
        ])
        source = {**self.SOURCE, "max_articles": 5}
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(api_data)):
            articles = fetch_gsam(source)

        assert len(articles) == 5


# ---------------------------------------------------------------------------
# fetch_amundi
# ---------------------------------------------------------------------------

class TestFetchAmundi:
    SOURCE = {
        "url": "https://research-center.amundi.com",
        "rss_url": "https://research-center.amundi.com/rss.xml",
        "max_articles": 10,
        "expected_hostname": "amundi.com",
    }

    def _rss(self, items: list[tuple[str, str, str]]) -> str:
        items_xml = "".join(
            f"<item><title>{t}</title><link>{u}</link><pubDate>{d}</pubDate></item>"
            for t, u, d in items
        )
        return f'<?xml version="1.0"?><rss><channel>{items_xml}</channel></rss>'

    def _mock_resp(self, xml: str):
        resp = MagicMock()
        resp.text = xml
        resp.raise_for_status.return_value = None
        return resp

    def test_parses_articles(self):
        xml = self._rss([
            ("AI Boom or Bubble?",
             "https://research-center.amundi.com/article/ai-boom-or-bubble",
             "Thu, 17 Apr 2026 10:07:00 +0200"),
            ("Cross Asset Investment Strategy",
             "https://research-center.amundi.com/article/cross-asset-april-2026",
             "Wed, 16 Apr 2026 09:08:00 +0200"),
        ])
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(xml)):
            articles = fetch_amundi(self.SOURCE)

        assert len(articles) == 2
        assert articles[0]["title"] == "AI Boom or Bubble?"
        assert articles[0]["url"] == "https://research-center.amundi.com/article/ai-boom-or-bubble"
        assert articles[0]["date"] == "2026-04-17"
        assert articles[1]["date"] == "2026-04-16"

    def test_skips_items_without_title_or_link(self):
        xml = self._rss([
            ("", "https://research-center.amundi.com/article/no-title", "Thu, 17 Apr 2026 10:00:00 +0000"),
            ("Valid Article", "https://research-center.amundi.com/article/valid", "Thu, 17 Apr 2026 09:00:00 +0000"),
        ])
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(xml)):
            articles = fetch_amundi(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["title"] == "Valid Article"

    def test_respects_max_articles(self):
        xml = self._rss([
            (f"Article {i}", f"https://research-center.amundi.com/article/article-{i}",
             f"Thu, {i:02d} Apr 2026 10:00:00 +0000")
            for i in range(1, 8)
        ])
        source = {**self.SOURCE, "max_articles": 5}
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(xml)):
            articles = fetch_amundi(source)

        assert len(articles) == 5


# ---------------------------------------------------------------------------
# fetch_jpmam
# ---------------------------------------------------------------------------

class TestFetchJpmam:
    SOURCE = {
        "url": "https://am.jpmorgan.com/us/en/asset-management/adv/insights/market-insights/market-updates/",
        "max_articles": 10,
        "expected_hostname": "am.jpmorgan.com",
    }

    def _api_response(self, pages: list) -> dict:
        return {"pages": pages}

    def test_parses_articles(self):
        api_data = self._api_response([
            {
                "title": "On the Minds of Investors: Tariff update",
                "url": "/us/en/asset-management/adv/insights/market-insights/market-updates/on-the-minds-of-investors/tariff-update.html",
                "displayDate": "04/17/2026",
            },
            {
                "title": "Eye on the Market Outlook 2026",
                "url": "/us/en/asset-management/adv/insights/market-insights/market-updates/outlook-2026.html",
                "displayDate": "01/06/2026",
            },
        ])
        mock_resp = MagicMock()
        mock_resp.json.return_value = api_data
        mock_resp.raise_for_status.return_value = None

        with patch("fetch_articles.requests.get", return_value=mock_resp):
            articles = fetch_jpmam(self.SOURCE)

        assert len(articles) == 2
        assert articles[0]["title"] == "On the Minds of Investors: Tariff update"
        assert articles[0]["url"] == "https://am.jpmorgan.com/us/en/asset-management/adv/insights/market-insights/market-updates/on-the-minds-of-investors/tariff-update.html"
        assert articles[0]["date"] == "2026-04-17"
        assert articles[0]["date_raw"] == "04/17/2026"
        assert articles[1]["date"] == "2026-01-06"

    def test_skips_pages_without_title(self):
        api_data = self._api_response([
            {"title": "", "url": "/some/path.html", "displayDate": "04/01/2026"},
            {"title": "Valid Article", "url": "/valid/path.html", "displayDate": "04/15/2026"},
        ])
        mock_resp = MagicMock()
        mock_resp.json.return_value = api_data
        mock_resp.raise_for_status.return_value = None

        with patch("fetch_articles.requests.get", return_value=mock_resp):
            articles = fetch_jpmam(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["title"] == "Valid Article"

    def test_respects_max_articles(self):
        api_data = self._api_response([
            {"title": f"Article {i}", "url": f"/path/article-{i}.html", "displayDate": f"04/{i:02d}/2026"}
            for i in range(1, 8)
        ])
        mock_resp = MagicMock()
        mock_resp.json.return_value = api_data
        mock_resp.raise_for_status.return_value = None

        source = {**self.SOURCE, "max_articles": 5}
        with patch("fetch_articles.requests.get", return_value=mock_resp):
            articles = fetch_jpmam(source)

        assert len(articles) == 5


# ---------------------------------------------------------------------------
# fetch_pgim
# ---------------------------------------------------------------------------

class TestFetchPgim:
    SOURCE = {
        "url": "https://www.pgim.com/us/en/institutional/insights",
        "max_articles": 10,
        "expected_hostname": "pgim.com",
    }

    def _item(self, title: str, href: str, date: str, item_id: str = "x-up-001") -> str:
        import json as _json
        data = {item_id: {"itemTitle": title, "publishDate": date, "xdm:linkURL": href}}
        layer = _json.dumps(data).replace('"', "&quot;")
        return f'<li class="cmp-list__item" data-cmp-data-layer=\'{_json.dumps(data)}\'></li>'

    def _mock_resp(self, html: str):
        resp = MagicMock()
        resp.text = html
        resp.raise_for_status.return_value = None
        return resp

    def _insights_item(self, title: str, href: str, date: str, item_id: str = "x-001") -> str:
        import json as _json
        data = {item_id: {"itemTitle": title, "publishDate": date, "xdm:linkURL": href}}
        return f'<li class="cmp-list__item" data-cmp-data-layer=\'{_json.dumps(data)}\'></li>'

    def test_parses_articles(self):
        # Fetcher scrapes 4 asset-class pages; mock returns articles on first page only.
        items = (
            self._insights_item("Steeper Yield Curve Risks", "/us/en/institutional/insights/asset-class/fixed-income/long-story-short-blog/steeper-curve", "April 20, 2026", "a")
            + self._insights_item("Investors at the Private Credit Gate", "/us/en/institutional/insights/asset-class/fixed-income/private-credit", "April 7, 2026", "b")
        )
        full_html = f"<html><body><ul>{items}</ul></body></html>"
        side = [self._mock_resp(full_html)] + [self._mock_resp("<html></html>")] * 3
        with patch("fetch_articles.requests.get", side_effect=side):
            articles = fetch_pgim(self.SOURCE)

        assert len(articles) == 2
        assert articles[0]["title"] == "Steeper Yield Curve Risks"
        assert "/insights/" in articles[0]["url"]
        assert articles[0]["date"] == "2026-04-20"
        assert articles[0]["date_raw"] == "April 20, 2026"

    def test_skips_items_without_date_or_non_insights_url(self):
        import json as _json
        # No publishDate → nav/category link, must be skipped
        no_date = f'<li class="cmp-list__item" data-cmp-data-layer=\'{_json.dumps({"n": {"itemTitle": "Fixed Income", "xdm:linkURL": "/us/en/institutional/insights/asset-class/fixed-income"}})}\'></li>'
        # Has date but URL is newsroom, not /insights/ → must be skipped
        non_insights = self._insights_item("Press Release", "/us/en/institutional/about/newsroom/press-release", "April 1, 2026", "m")
        valid = self._insights_item("Valid Research", "/us/en/institutional/insights/asset-class/equity/article", "April 1, 2026", "v")
        html = f"<html><body><ul>{no_date}{non_insights}{valid}</ul></body></html>"
        side = [self._mock_resp(html)] + [self._mock_resp("<html></html>")] * 3
        with patch("fetch_articles.requests.get", side_effect=side):
            articles = fetch_pgim(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["title"] == "Valid Research"

    def test_respects_max_articles(self):
        items = "".join(
            self._insights_item(f"Article {i}", f"/us/en/institutional/insights/asset-class/fixed-income/article-{i}", f"April {i}, 2026", f"id{i}")
            for i in range(1, 8)
        )
        html = f"<html><body><ul>{items}</ul></body></html>"
        source = {**self.SOURCE, "max_articles": 5}
        side = [self._mock_resp(html)] + [self._mock_resp("<html></html>")] * 3
        with patch("fetch_articles.requests.get", side_effect=side):
            articles = fetch_pgim(source)

        assert len(articles) == 5

    def test_skips_weekly_view_paywall_paths(self):
        # /weekly-view/ pages render only a 250-char teaser; full text is gated.
        # These URLs poison quality sampling (depth=0.1, extractable=0.0) and
        # also waste content-fetcher cycles in production. Drop at fetcher layer.
        weekly_view = self._insights_item(
            "Macro Market Dissonance",
            "/us/en/institutional/insights/asset-class/fixed-income/weekly-view/macro-dissonance",
            "April 20, 2026", "wv1")
        weekly_view2 = self._insights_item(
            "Blockades Economic Ripples",
            "/us/en/institutional/insights/asset-class/fixed-income/weekly-view/blockades",
            "April 18, 2026", "wv2")
        substantive = self._insights_item(
            "Yield Curve Drivers",
            "/us/en/institutional/insights/more-insights/long-story-short-blog/yield-curve",
            "April 15, 2026", "lss1")
        html = f"<html><body><ul>{weekly_view}{weekly_view2}{substantive}</ul></body></html>"
        side = [self._mock_resp(html)] + [self._mock_resp("<html></html>")] * 3
        with patch("fetch_articles.requests.get", side_effect=side):
            articles = fetch_pgim(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["title"] == "Yield Curve Drivers"
        assert "/weekly-view/" not in articles[0]["url"]


# ---------------------------------------------------------------------------
# fetch_aberdeen
# ---------------------------------------------------------------------------

class TestFetchAberdeen:
    SOURCE = {
        "url": "https://www.aberdeeninvestments.com/en-us/institutional/insights-and-research/insights",
        "max_articles": 10,
        "expected_hostname": "aberdeeninvestments.com",
    }

    def _card(self, title: str, href: str, date: str) -> str:
        return (
            f'<a href="{href}">'
            f'<h5 class="ArticleCard_article-card__title__pOQTa">{title}</h5>'
            f'<time class="ms-auto">{date}</time>'
            f'</a>'
        )

    def test_parses_articles(self):
        html = "<html><body>" + \
            self._card("Navigating Tariff Uncertainty",
                       "/en-us/institutional/insights-and-research/insights/navigating-tariff-uncertainty",
                       "Apr 15, 2026") + \
            self._card("Credit Market Perspectives Q1 2026",
                       "/en-us/institutional/insights-and-research/insights/credit-market-perspectives",
                       "Mar 28, 2026") + \
            "</body></html>"
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_aberdeen(self.SOURCE)

        assert len(articles) == 2
        assert articles[0]["title"] == "Navigating Tariff Uncertainty"
        assert articles[0]["url"] == "https://www.aberdeeninvestments.com/en-us/institutional/insights-and-research/insights/navigating-tariff-uncertainty"
        assert articles[0]["date"] == "2026-04-15"
        assert articles[0]["date_raw"] == "Apr 15, 2026"
        assert articles[1]["date"] == "2026-03-28"

    def test_skips_time_without_parent_link(self):
        html = (
            "<html><body>"
            '<div><time class="ms-auto">Apr 1, 2026</time></div>'
            + self._card("Valid Article",
                         "/en-us/institutional/insights-and-research/insights/valid",
                         "Apr 1, 2026")
            + "</body></html>"
        )
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_aberdeen(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["title"] == "Valid Article"

    def test_deduplicates_same_url(self):
        # Two time elements inside same card → should appear only once
        html = (
            "<html><body>"
            '<a href="/en-us/institutional/insights-and-research/insights/article-a">'
            '<h5 class="ArticleCard_article-card__title__pOQTa">Article A</h5>'
            '<time class="ms-auto">Apr 10, 2026</time>'
            '<time class="ms-auto">Apr 10, 2026</time>'
            '</a>'
            "</body></html>"
        )
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_aberdeen(self.SOURCE)

        assert len(articles) == 1

    def test_respects_max_articles(self):
        cards = "".join(
            self._card(f"Article {i}",
                       f"/en-us/institutional/insights-and-research/insights/article-{i}",
                       f"Apr {i}, 2026")
            for i in range(1, 8)
        )
        html = f"<html><body>{cards}</body></html>"
        source = {**self.SOURCE, "max_articles": 5}
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_aberdeen(source)

        assert len(articles) == 5


# ---------------------------------------------------------------------------
# fetch_cambridge_associates
# ---------------------------------------------------------------------------

class TestFetchCambridgeAssociates:
    SOURCE = {
        "url": "https://www.cambridgeassociates.com/insights/private-investments/",
        "max_articles": 10,
        "expected_hostname": "cambridgeassociates.com",
    }

    def _card(self, title: str, href: str, date: str) -> str:
        return (
            f'<article class="c-list-article u-mb40">'
            f'<div class="o-grid">'
            f'<div class="o-grid__col">'
            f'<h2><a class="c-link" href="{href}">{title}</a></h2>'
            f'<p class="c-type c-type--body-sm u-mv12 u-color-grey-dk-80">{date}</p>'
            f'</div>'
            f'</div>'
            f'</article>'
        )

    def _mock_resp(self, html: str):
        resp = MagicMock()
        resp.text = html
        resp.raise_for_status.return_value = None
        return resp

    def test_parses_articles(self):
        html = "<html><body>" + \
            self._card("Has Private Equity Hit Peak Software?",
                       "https://www.cambridgeassociates.com/insight/has-private-equity-hit-peak-software",
                       "February 2026") + \
            self._card("2026 Outlook: Private Equity & Venture Capital",
                       "https://www.cambridgeassociates.com/insight/2026-outlook-private-equity",
                       "December 2025") + \
            "</body></html>"
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(html)):
            articles = fetch_cambridge_associates(self.SOURCE)

        assert len(articles) == 2
        assert articles[0]["title"] == "Has Private Equity Hit Peak Software?"
        assert articles[0]["url"] == "https://www.cambridgeassociates.com/insight/has-private-equity-hit-peak-software"
        assert articles[0]["date"] == "2026-02-28"
        assert articles[0]["date_raw"] == "February 2026"
        assert articles[1]["date"] == "2025-12-31"

    def test_skips_cards_without_link(self):
        html = (
            "<html><body>"
            '<article class="c-list-article"><div class="o-grid"></div></article>'
            + self._card("Valid Article",
                         "https://www.cambridgeassociates.com/insight/valid-article",
                         "March 2026")
            + "</body></html>"
        )
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(html)):
            articles = fetch_cambridge_associates(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["title"] == "Valid Article"

    def test_respects_max_articles(self):
        cards = "".join(
            self._card(f"Article {i}",
                       f"https://www.cambridgeassociates.com/insight/article-{i}",
                       f"March 2026")
            for i in range(1, 8)
        )
        html = f"<html><body>{cards}</body></html>"
        source = {**self.SOURCE, "max_articles": 5}
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(html)):
            articles = fetch_cambridge_associates(source)

        assert len(articles) == 5


# ---------------------------------------------------------------------------
# fetch_verdad
# ---------------------------------------------------------------------------

class TestFetchVerdad:
    SOURCE = {
        "url": "https://verdadcap.com/weekly-research",
        "max_articles": 10,
        "expected_hostname": "verdadcap.com",
    }

    def _mock_resp(self, html: str) -> MagicMock:
        m = MagicMock()
        m.text = html
        m.raise_for_status.return_value = None
        return m

    def _archive_item(self, slug: str, title: str, date_str: str) -> str:
        return (
            f'<li class="archive-item archive-item--show-date">'
            f'<span class="archive-item-date-before">{date_str}</span>'
            f'<a class="archive-item-link" href="/archive/{slug}">{title}</a>'
            f'<span class="archive-item-date-after">{date_str}</span>'
            f'</li>'
        )

    def test_parses_articles(self):
        items = (
            self._archive_item("the-challenge-of-diversification",
                               "The Challenge of Diversification",
                               "Oct 20, 2025") +
            self._archive_item("total-concentration",
                               "Total Concentration",
                               "Sep 15, 2025")
        )
        html = f'<html><body><ul class="archive-item-list">{items}</ul></body></html>'
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(html)):
            articles = fetch_verdad(self.SOURCE)

        assert len(articles) == 2
        assert articles[0]["title"] == "The Challenge of Diversification"
        assert articles[0]["url"] == "https://verdadcap.com/archive/the-challenge-of-diversification"
        assert articles[0]["date"] == "2025-10-20"
        assert articles[0]["date_raw"] == "Oct 20, 2025"
        assert articles[1]["date"] == "2025-09-15"

    def test_skips_items_without_date(self):
        # Item missing date_span — should be skipped (defensive)
        bad_item = (
            '<li class="archive-item">'
            '<a class="archive-item-link" href="/archive/no-date-slug">No Date</a>'
            '</li>'
        )
        good_item = self._archive_item("real-article", "Real Article", "Jul 9, 2025")
        html = f'<html><body><ul>{bad_item}{good_item}</ul></body></html>'
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(html)):
            articles = fetch_verdad(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["title"] == "Real Article"

    def test_skips_items_without_archive_prefix(self):
        # Non-/archive/ links must be skipped (e.g. /about, /team)
        items = (
            '<li class="archive-item">'
            '<span class="archive-item-date-before">Mar 1, 2026</span>'
            '<a class="archive-item-link" href="/about">About Us</a></li>' +
            self._archive_item("good", "Good Article", "Mar 2, 2026")
        )
        html = f'<html><body><ul>{items}</ul></body></html>'
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(html)):
            articles = fetch_verdad(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["title"] == "Good Article"

    def test_dedupes_same_url(self):
        # Same href appearing twice should be deduped
        item = self._archive_item("dup-slug", "Duplicate", "Feb 1, 2026")
        html = f'<html><body><ul>{item}{item}</ul></body></html>'
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(html)):
            articles = fetch_verdad(self.SOURCE)

        assert len(articles) == 1

    def test_respects_max_articles(self):
        items = "".join(
            self._archive_item(f"slug-{i}", f"Article {i}", f"Apr {i}, 2026")
            for i in range(1, 16)
        )
        source = {**self.SOURCE, "max_articles": 5}
        html = f'<html><body><ul>{items}</ul></body></html>'
        with patch("fetch_articles.requests.get", return_value=self._mock_resp(html)):
            articles = fetch_verdad(source)

        assert len(articles) == 5


class TestFetchSourceIntraRunDedup:
    """fetch_source must not emit the same article id twice in one run.

    Root cause of the 2026-07 articles.jsonl duplicates: the existing_ids
    check skipped articles already on disk, but ids accepted earlier in the
    SAME loop were never added to the set, so a fetcher returning the same
    URL multiple times (wellington did) appended each occurrence.
    """

    SOURCE = {
        "id": "dup-test-source",
        "name": "Dup Test",
        "short_name": "DupTest",
        "method": "scrape",
    }

    def _run(self, raw_articles, existing_ids):
        from fetch_articles import fetch_source, FETCHERS
        with patch.dict(FETCHERS, {"dup-test-source": lambda s: raw_articles}):
            return fetch_source(self.SOURCE, existing_ids)

    def test_same_url_twice_in_one_fetch_yields_one_article(self):
        art = {"title": "Same Article", "url": "https://example.com/a", "date": "2026-07-01"}
        new = self._run([art, dict(art)], set())
        assert len(new) == 1

    def test_accepted_ids_added_to_existing_ids(self):
        from fetch_articles import article_id
        art = {"title": "T", "url": "https://example.com/b", "date": "2026-07-01"}
        existing: set = set()
        self._run([art], existing)
        assert article_id("dup-test-source", art["url"]) in existing

    def test_distinct_urls_unaffected(self):
        arts = [
            {"title": "A", "url": "https://example.com/1", "date": "2026-07-01"},
            {"title": "B", "url": "https://example.com/2", "date": "2026-07-01"},
        ]
        new = self._run(arts, set())
        assert len(new) == 2


# ---------------------------------------------------------------------------
# fetch_metlife_im — AEM listing servlet (MetLife Investment Management insights)
#
# Context (2026-07-27): PineBridge stopped publishing on pinebridge.com after
# 2026-06-09 — MetLife IM completed its acquisition 2025-12-30 and the research
# (same teams, e.g. "PineBridge Investments, a MetLife Investment Management
# company") moved to MIM's own site.
#
# Rewritten 2026-08-03: MIM retired `investments.metlife.com` and folded the
# site into `www.metlife.com/investments/en-us/` behind a country+role
# disclaimer interstitial, which returned 0 articles for 3 consecutive runs.
# The sitemap survived the move but its <lastmod> is now a migration crawl
# stamp (38 of 46 URLs share one date) and article pages lost their JSON-LD, so
# neither can date an article. We drive off the CSR listing's backing servlet
# instead: title + day-precise publishedDate + repository path, newest-first.
# ---------------------------------------------------------------------------

class TestFetchMetlifeIm:
    SOURCE = {
        "url": "https://www.metlife.com/investments/en-us/insights/",
        "max_articles": 3,
        "expected_hostname": "www.metlife.com",
    }

    ROOT = "/content/metlife/mim/investments/en-us/homepage/"

    def _entry(self, slug: str, title: str, published: str,
               topic: str = "outlooks-and-market-updates") -> dict:
        return {
            "headlineTitle": title,
            "publishedDate": published,
            "path": f"{self.ROOT}insights/{topic}/{slug}/",
            "type": "article",
        }

    def _mock_get(self, entries, capture: dict | None = None):
        def _get(url, **kwargs):
            if capture is not None:
                capture["url"] = url
                capture.update(kwargs)
            resp = MagicMock(status_code=200)
            resp.raise_for_status.return_value = None
            resp.json.return_value = {"articles": entries,
                                      "totalResults": len(entries),
                                      "articleSize": len(entries)}
            return resp
        return _get

    def test_maps_repository_path_to_public_url_and_parses_date(self):
        entries = [self._entry("summer-heat", "Global Risks Mid-Year Update",
                               "JUL 16, 2026", topic="macro-strategy")]
        with patch("fetch_articles.requests.get", side_effect=self._mock_get(entries)):
            articles = fetch_metlife_im(self.SOURCE)

        assert len(articles) == 1
        assert articles[0]["url"] == (
            "https://www.metlife.com/investments/en-us/insights/"
            "macro-strategy/summer-heat/"
        )
        assert articles[0]["title"] == "Global Risks Mid-Year Update"
        assert articles[0]["date"] == "2026-07-16"
        assert articles[0]["date_raw"] == "JUL 16, 2026"

    def test_sends_disclaimer_cookies(self):
        """Regression guard for the 2026-08-03 outage.

        Without BOTH cookies every /investments/ URL 302s to the disclaimer
        interstitial and the fetcher silently returns 0 articles.
        """
        capture: dict = {}
        entries = [self._entry("x", "X", "JUL 16, 2026")]
        with patch("fetch_articles.requests.get",
                   side_effect=self._mock_get(entries, capture)):
            fetch_metlife_im(self.SOURCE)

        assert capture["cookies"] == {"culture": "en-us",
                                      "disclaimer": "en-us/Institution"}
        assert capture["url"].startswith(
            "https://www.metlife.com/bin/MLApp/globalMarketingPlatform/")

    def test_requests_newest_first_from_the_servlet(self):
        capture: dict = {}
        with patch("fetch_articles.requests.get",
                   side_effect=self._mock_get([], capture)):
            fetch_metlife_im(self.SOURCE)

        assert capture["params"]["sortby"] == "date"
        assert capture["params"]["dateSort"] == "desc"
        assert capture["params"]["enableArticles"] == "true"

    def test_preserves_servlet_order_and_caps_at_max_articles(self):
        entries = [
            self._entry(f"piece-{i}", f"Piece {i}", d)
            for i, d in enumerate(["JUL 23, 2026", "JUL 20, 2026", "JUL 01, 2026",
                                   "JUN 15, 2026", "MAY 02, 2026"])
        ]
        with patch("fetch_articles.requests.get", side_effect=self._mock_get(entries)):
            articles = fetch_metlife_im(self.SOURCE)

        assert len(articles) == 3
        assert [a["date"] for a in articles] == ["2026-07-23", "2026-07-20", "2026-07-01"]

    def test_unescapes_html_entities_in_titles(self):
        """The servlet returns escaped headlines — &amp; and &nbsp; reach the UI raw."""
        entries = [self._entry("rvtaa", "Relative Value &amp; Tactical&nbsp; Allocation",
                               "JUN 30, 2026")]
        with patch("fetch_articles.requests.get", side_effect=self._mock_get(entries)):
            articles = fetch_metlife_im(self.SOURCE)

        assert articles[0]["title"] == "Relative Value & Tactical Allocation"

    def test_skips_entries_outside_the_mim_content_tree(self):
        """A path outside /content/metlife/mim/... must never become an article."""
        entries = [
            {"headlineTitle": "Spoofed", "publishedDate": "JUL 20, 2026",
             "path": "/content/evil/insights/spoofed/"},
            self._entry("good", "Good One", "JUL 16, 2026"),
        ]
        with patch("fetch_articles.requests.get", side_effect=self._mock_get(entries)):
            articles = fetch_metlife_im(self.SOURCE)

        assert [a["title"] for a in articles] == ["Good One"]

    def test_skips_entry_without_title(self):
        entries = [
            {"headlineTitle": "", "publishedDate": "JUL 20, 2026",
             "path": f"{self.ROOT}insights/equity/untitled/"},
        ]
        with patch("fetch_articles.requests.get", side_effect=self._mock_get(entries)):
            assert fetch_metlife_im(self.SOURCE) == []

    def test_empty_listing_returns_empty_list(self):
        with patch("fetch_articles.requests.get", side_effect=self._mock_get([])):
            assert fetch_metlife_im(self.SOURCE) == []
