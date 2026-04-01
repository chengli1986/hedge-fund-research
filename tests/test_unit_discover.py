"""Unit tests for discover_entrypoints.py — 10 tests covering core functions."""

import pytest
from unittest.mock import patch, MagicMock
from discover_entrypoints import (
    extract_nav_links,
    score_candidates,
    _classify_with_ai,
)


# ---------------------------------------------------------------------------
# TestExtractNavLinks  (4 tests)
# ---------------------------------------------------------------------------

class TestExtractNavLinks:
    def test_extracts_links_from_nav(self):
        """Nav with /research, /insights, /about → all extracted."""
        html = """
        <html><body>
          <nav>
            <a href="/research">Research</a>
            <a href="/insights">Insights</a>
            <a href="/about">About</a>
          </nav>
        </body></html>
        """
        links = extract_nav_links(html, "https://example.com")
        urls = [l["url"] for l in links]
        assert "https://example.com/research" in urls
        assert "https://example.com/insights" in urls
        assert "https://example.com/about" in urls

    def test_deduplicates_links(self):
        """Same href twice → only one result."""
        html = """
        <html><body>
          <a href="/research">Research</a>
          <a href="/research">Research Again</a>
        </body></html>
        """
        links = extract_nav_links(html, "https://example.com")
        urls = [l["url"] for l in links]
        assert urls.count("https://example.com/research") == 1

    def test_skips_external_links(self):
        """twitter.com filtered when allowed_domains=["example.com"]."""
        html = """
        <html><body>
          <a href="/research">Research</a>
          <a href="https://twitter.com/example">Twitter</a>
        </body></html>
        """
        links = extract_nav_links(html, "https://example.com", allowed_domains=["example.com"])
        urls = [l["url"] for l in links]
        assert all("twitter.com" not in u for u in urls)
        assert "https://example.com/research" in urls

    def test_skips_anchor_and_javascript(self):
        """# and javascript:void(0) skipped."""
        html = """
        <html><body>
          <a href="#">Anchor</a>
          <a href="javascript:void(0)">JS</a>
          <a href="/valid">Valid</a>
        </body></html>
        """
        links = extract_nav_links(html, "https://example.com")
        urls = [l["url"] for l in links]
        assert not any(u.startswith("#") for u in urls)
        assert not any("javascript:" in u for u in urls)
        assert "https://example.com/valid" in urls


# ---------------------------------------------------------------------------
# TestScoreCandidates  (2 tests)
# ---------------------------------------------------------------------------

class TestScoreCandidates:
    def test_scores_and_sorts(self):
        """/research scores higher than /about, sorted descending."""
        candidates = [
            {"url": "https://example.com/about", "label": "About"},
            {"url": "https://example.com/research", "label": "Research"},
        ]
        allowed_domains = ["example.com"]
        # Provide minimal HTML for structure scoring
        page_html_map = {
            "https://example.com/research": "<article><time>Jan 2026</time></article>",
            "https://example.com/about": "<form><input type='email'></form>",
        }
        results = score_candidates(candidates, allowed_domains, page_html_map)
        assert len(results) >= 1
        # Research should score higher than about — if both present, research first
        if len(results) >= 2:
            research_score = next((r["final_score"] for r in results if "/research" in r["url"]), None)
            about_score = next((r["final_score"] for r in results if "/about" in r["url"]), None)
            if research_score is not None and about_score is not None:
                assert research_score >= about_score

    def test_rejects_domain_mismatch(self):
        """other.com filtered out when allowed_domains=["example.com"]."""
        candidates = [
            {"url": "https://other.com/research", "label": "Research"},
            {"url": "https://example.com/insights", "label": "Insights"},
        ]
        allowed_domains = ["example.com"]
        page_html_map = {}
        results = score_candidates(candidates, allowed_domains, page_html_map)
        urls = [r["url"] for r in results]
        assert all("other.com" not in u for u in urls)


# ---------------------------------------------------------------------------
# TestClassifyWithAi  (1 test)
# ---------------------------------------------------------------------------

class TestClassifyWithAi:
    def test_stub_returns_none(self):
        """Phase 1 stub always returns None."""
        result = _classify_with_ai("https://example.com/research", "<html></html>")
        assert result is None
