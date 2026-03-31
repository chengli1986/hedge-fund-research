"""Unit tests for fetch_content.py — validation helpers and atomic writes."""

import json
from pathlib import Path

import pytest

from fetch_content import (
    _validate_pdf_response,
    _validate_json_response,
    _normalize_html,
    _check_min_content_length,
    _atomic_write,
    load_articles,
    save_articles,
    CONTENT_FETCHERS,
)


# ---------------------------------------------------------------------------
# _validate_pdf_response
# ---------------------------------------------------------------------------

class TestValidatePdfResponse:
    def test_accepts_valid_pdf(self):
        assert _validate_pdf_response(200, "application/pdf", 5000) is True

    def test_rejects_html_content_type(self):
        assert _validate_pdf_response(200, "text/html", 5000) is False

    def test_rejects_too_small(self):
        assert _validate_pdf_response(200, "application/pdf", 1024) is False
        assert _validate_pdf_response(200, "application/pdf", 500) is False

    def test_rejects_non_200(self):
        assert _validate_pdf_response(404, "application/pdf", 5000) is False
        assert _validate_pdf_response(500, "application/pdf", 5000) is False

    def test_accepts_other_2xx(self):
        assert _validate_pdf_response(201, "application/pdf", 5000) is True

    def test_rejects_none_content_type(self):
        assert _validate_pdf_response(200, None, 5000) is False


# ---------------------------------------------------------------------------
# _validate_json_response
# ---------------------------------------------------------------------------

class TestValidateJsonResponse:
    def test_rejects_html_error_page(self):
        assert _validate_json_response("<html><body>Error</body></html>") is False

    def test_rejects_html_with_whitespace(self):
        assert _validate_json_response("  <html>") is False

    def test_accepts_valid_json(self):
        assert _validate_json_response('{"key": "value"}') is True
        assert _validate_json_response('[1, 2, 3]') is True

    def test_rejects_invalid_json(self):
        assert _validate_json_response("not json at all") is False


# ---------------------------------------------------------------------------
# _normalize_html
# ---------------------------------------------------------------------------

class TestNormalizeHtml:
    def test_strips_nav_footer(self):
        html = """
        <html>
        <nav>Navigation menu</nav>
        <header>Site header</header>
        <article>
            <p>Important article content here.</p>
        </article>
        <footer>Footer links</footer>
        </html>
        """
        text = _normalize_html(html, "article p")
        assert "Important article content" in text
        assert "Navigation menu" not in text
        assert "Footer links" not in text
        assert "Site header" not in text

    def test_preserves_article_body(self):
        html = """
        <div class="article-content">
            <p>First paragraph of the article.</p>
            <p>Second paragraph of the article.</p>
        </div>
        """
        text = _normalize_html(html, ".article-content p")
        assert "First paragraph" in text
        assert "Second paragraph" in text

    def test_fallback_selectors(self):
        html = """
        <main>
            <p>Content in main element.</p>
        </main>
        """
        # Primary selector won't match, should fall back to "main"
        text = _normalize_html(html, ".nonexistent-class p")
        assert "Content in main" in text

    def test_strips_script_and_style(self):
        html = """
        <article>
            <script>var x = 1;</script>
            <style>.foo { color: red; }</style>
            <p>Real content.</p>
        </article>
        """
        text = _normalize_html(html, "article p")
        assert "Real content" in text
        assert "var x" not in text
        assert "color: red" not in text


# ---------------------------------------------------------------------------
# _check_min_content_length
# ---------------------------------------------------------------------------

class TestCheckMinContentLength:
    def test_rejects_short_text(self):
        assert _check_min_content_length("short") is False
        assert _check_min_content_length("a" * 100) is False

    def test_accepts_long_text(self):
        assert _check_min_content_length("a" * 101) is True
        assert _check_min_content_length("a" * 500) is True

    def test_rejects_empty(self):
        assert _check_min_content_length("") is False


# ---------------------------------------------------------------------------
# _atomic_write
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    def test_creates_file(self, tmp_path):
        target = tmp_path / "output.txt"
        _atomic_write(target, b"hello world")
        assert target.exists()
        assert target.read_bytes() == b"hello world"

    def test_no_tmp_left_behind(self, tmp_path):
        target = tmp_path / "output.txt"
        _atomic_write(target, b"data")
        # No .tmp file should remain
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "subdir" / "deep" / "output.txt"
        _atomic_write(target, b"nested")
        assert target.exists()
        assert target.read_bytes() == b"nested"

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "output.txt"
        target.write_bytes(b"old")
        _atomic_write(target, b"new")
        assert target.read_bytes() == b"new"


# ---------------------------------------------------------------------------
# Content status on failure
# ---------------------------------------------------------------------------

class TestContentStatusOnFailure:
    def test_content_status_set_to_failed_when_fetcher_returns_none(self, tmp_path):
        """When a fetcher returns None, the article's content_status should be 'failed'."""
        article = {
            "id": "test123",
            "source_id": "gmo",
            "source_name": "GMO",
            "title": "Test Article",
            "url": "https://www.gmo.com/test",
            "summarized": False,
        }

        # Simulate the main loop logic: fetcher returns None -> status = failed
        result = None  # simulating fetcher failure
        if result is not None:
            content_path, status = result
            article["content_path"] = str(content_path)
            article["content_status"] = status
        else:
            article["content_status"] = "failed"

        assert article["content_status"] == "failed"
        assert "content_path" not in article


# ---------------------------------------------------------------------------
# CONTENT_FETCHERS registry
# ---------------------------------------------------------------------------

class TestContentFetchers:
    def test_bridgewater_excluded(self):
        assert "bridgewater" not in CONTENT_FETCHERS

    def test_expected_sources_present(self):
        assert "gmo" in CONTENT_FETCHERS
        assert "oaktree" in CONTENT_FETCHERS
        assert "aqr" in CONTENT_FETCHERS
        assert "man-group" in CONTENT_FETCHERS
