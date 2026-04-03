"""Unit tests for _classify_with_ai in discover_entrypoints.py — 3 tests."""

import pytest
from unittest.mock import patch
from discover_entrypoints import _classify_with_ai


class TestClassifyWithAi:
    def test_classify_research_page(self):
        """When LLM returns is_research_index=True, result is not None and flag is True."""
        mock_response = {
            "is_research_index": True,
            "confidence": 0.95,
            "reasoning": "Page lists multiple research articles with dates and titles.",
        }
        with patch("discover_entrypoints._call_llm", return_value=mock_response):
            result = _classify_with_ai(
                "https://example.com/research",
                "<html><body><article>Research 2026</article></body></html>",
            )
        assert result is not None
        assert result["is_research_index"] is True

    def test_classify_marketing_page(self):
        """When LLM returns is_research_index=False, result is not None and flag is False."""
        mock_response = {
            "is_research_index": False,
            "confidence": 0.92,
            "reasoning": "Page is a product landing page with no research content.",
        }
        with patch("discover_entrypoints._call_llm", return_value=mock_response):
            result = _classify_with_ai(
                "https://example.com/products",
                "<html><body><h1>Buy our product</h1></body></html>",
            )
        assert result is not None
        assert result["is_research_index"] is False

    def test_classify_llm_failure_returns_none(self):
        """When _call_llm raises an exception, _classify_with_ai returns None."""
        with patch("discover_entrypoints._call_llm", side_effect=Exception("API timeout")):
            result = _classify_with_ai(
                "https://example.com/insights",
                "<html><body></body></html>",
            )
        assert result is None
