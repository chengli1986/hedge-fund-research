"""Functional tests for SSR/API parsers using saved HTML/JSON fixtures."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# ---------------------------------------------------------------------------
# Man Group
# ---------------------------------------------------------------------------

class TestManGroupFixture:
    """Test fetch_man_group against a saved HTML fixture."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.html = (FIXTURES_DIR / "man-group-insights.html").read_text(encoding="utf-8")
        self.source = {
            "id": "man-group",
            "url": "https://www.man.com/insights",
            "max_articles": 10,
        }

    def _call(self):
        from fetch_articles import fetch_man_group

        mock_resp = MagicMock()
        mock_resp.text = self.html
        mock_resp.raise_for_status = MagicMock()

        with patch("fetch_articles.requests.get", return_value=mock_resp) as mock_get:
            articles = fetch_man_group(self.source)
            mock_get.assert_called_once()
        return articles

    def test_minimum_articles(self):
        articles = self._call()
        assert len(articles) >= 3, f"Expected >=3 articles, got {len(articles)}"

    def test_all_have_titles(self):
        for art in self._call():
            assert art["title"] and len(art["title"]) >= 5, f"Bad title: {art['title']!r}"

    def test_all_have_dates(self):
        for art in self._call():
            assert art["date"] is not None, f"Missing date for: {art['title']}"

    def test_urls_contain_man_com(self):
        for art in self._call():
            assert "man.com" in art["url"], f"URL missing man.com: {art['url']}"


# ---------------------------------------------------------------------------
# Bridgewater
# ---------------------------------------------------------------------------

class TestBridgewaterFixture:
    """Test fetch_bridgewater against a saved HTML fixture."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.html = (FIXTURES_DIR / "bridgewater-research.html").read_text(encoding="utf-8")
        self.source = {
            "id": "bridgewater",
            "url": "https://www.bridgewater.com/research-and-insights",
            "max_articles": 10,
        }

    def _call(self):
        from fetch_articles import fetch_bridgewater

        mock_resp = MagicMock()
        mock_resp.text = self.html
        mock_resp.raise_for_status = MagicMock()

        with patch("fetch_articles.requests.get", return_value=mock_resp) as mock_get:
            articles = fetch_bridgewater(self.source)
            mock_get.assert_called_once()
        return articles

    def test_minimum_articles(self):
        articles = self._call()
        assert len(articles) >= 3, f"Expected >=3 articles, got {len(articles)}"

    def test_all_have_titles(self):
        for art in self._call():
            assert art["title"] and len(art["title"]) >= 5, f"Bad title: {art['title']!r}"

    def test_urls_contain_bridgewater(self):
        for art in self._call():
            assert "bridgewater.com" in art["url"], f"URL missing bridgewater.com: {art['url']}"


# ---------------------------------------------------------------------------
# GMO API
# ---------------------------------------------------------------------------

class TestGmoApiFixture:
    """Test GMO API response structure (no parser call — direct JSON validation)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        with open(FIXTURES_DIR / "gmo-api-response.json") as f:
            self.data = json.load(f)

    def test_has_listing_key(self):
        assert "listing" in self.data, "Response missing 'listing' key"

    def test_minimum_items(self):
        assert len(self.data["listing"]) >= 5, (
            f"Expected >=5 items, got {len(self.data['listing'])}"
        )

    def test_each_item_has_title(self):
        for item in self.data["listing"]:
            assert item.get("Title"), f"Item missing Title: {item}"

    def test_each_item_has_date(self):
        for item in self.data["listing"]:
            has_date = item.get("Date") or item.get("dateData")
            assert has_date, f"Item missing Date/dateData: {item.get('Title')}"

    def test_each_item_has_url(self):
        for item in self.data["listing"]:
            assert item.get("URL"), f"Item missing URL: {item.get('Title')}"
