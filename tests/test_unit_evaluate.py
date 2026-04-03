"""Unit tests for evaluate_entrypoints.py — 4 tests covering compute_yield."""

import pytest
from evaluate_entrypoints import compute_yield


def _make_article(
    source_id: str,
    summarized: bool = True,
    key_takeaway_en: str = "This is a substantive market insight with plenty of text.",
) -> dict:
    return {
        "source_id": source_id,
        "summarized": summarized,
        "key_takeaway_en": key_takeaway_en,
    }


class TestComputeYield:
    def test_perfect_yield(self) -> None:
        """All articles summarized with real takeaways → yield 1.0."""
        articles = [
            _make_article("man-group"),
            _make_article("man-group"),
            _make_article("bridgewater"),
        ]
        result = compute_yield(articles)

        assert result["man-group"]["yield"] == 1.0
        assert result["man-group"]["total"] == 2
        assert result["man-group"]["quality_articles"] == 2
        assert result["man-group"]["noise_articles"] == 0

        assert result["bridgewater"]["yield"] == 1.0

    def test_mixed_yield(self) -> None:
        """Some not summarized, some empty takeaway → yield < 1.0."""
        articles = [
            _make_article("aqr", summarized=True),
            _make_article("aqr", summarized=False),  # not summarized → not quality
            _make_article("aqr", summarized=True, key_takeaway_en="short"),  # <20 chars
            _make_article("aqr", summarized=True, key_takeaway_en=""),  # empty
        ]
        result = compute_yield(articles)

        assert result["aqr"]["total"] == 4
        assert result["aqr"]["quality_articles"] == 1
        assert result["aqr"]["yield"] == pytest.approx(0.25)

    def test_empty_articles(self) -> None:
        """No articles → empty result dict."""
        result = compute_yield([])
        assert result == {}

    def test_disclaimer_detected(self) -> None:
        """Article with 'legal disclaimer' in takeaway counts as noise, not quality."""
        articles = [
            _make_article(
                "oaktree",
                summarized=True,
                key_takeaway_en="This page contains a legal disclaimer and terms.",
            ),
            _make_article("oaktree"),  # genuine quality article
        ]
        result = compute_yield(articles)

        assert result["oaktree"]["total"] == 2
        assert result["oaktree"]["noise_articles"] == 1
        assert result["oaktree"]["quality_articles"] == 1
        assert result["oaktree"]["yield"] == pytest.approx(0.5)
