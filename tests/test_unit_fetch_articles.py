"""Unit tests for fetch_articles.py — article_id, parse_date, _validate_hostname, load_existing_ids."""

import json
from unittest.mock import MagicMock, patch
import pytest
from fetch_articles import article_id, parse_date, _validate_hostname, load_existing_ids, fetch_oaktree, DATA_FILE


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
