"""Unit tests for publish.py — Stage 4 HTML dashboard."""

import pytest

from publish import BADGE_COLORS, generate_html

# --- Sample data ---

SAMPLE_ARTICLES = [
    {
        "id": "aaa111",
        "source_id": "man-group",
        "source_name": "Man",
        "title": "AI Boom or Bust?",
        "url": "https://man.com/ai-boom",
        "date": "2026-03-28",
        "summarized": True,
        "summary_en": "Man Group analyzes AI investment cycle risks.",
        "summary_zh": "Man Group分析了AI投资周期的风险。",
        "key_takeaway_en": "AI valuations face mean reversion risk.",
        "key_takeaway_zh": "AI估值面临均值回归风险。",
        "themes": ["AI/Tech", "Equities/Value"],
    },
    {
        "id": "bbb222",
        "source_id": "bridgewater",
        "source_name": "Bridgewater",
        "title": "Global Macro Outlook Q2",
        "url": "https://bridgewater.com/macro-q2",
        "date": "2026-03-25",
        "summarized": False,
    },
    {
        "id": "ccc333",
        "source_id": "gmo",
        "source_name": "GMO",
        "title": "Value in Emerging Markets",
        "url": "https://gmo.com/em-value",
        "date": "2026-03-30",
        "summarized": True,
        "summary_en": "GMO makes the case for EM value stocks.",
        "summary_zh": "GMO论证了新兴市场价值股的投资理由。",
        "key_takeaway_en": "EM value is historically cheap.",
        "key_takeaway_zh": "新兴市场价值股处于历史低位。",
        "themes": ["China/EM", "Equities/Value"],
    },
]


class TestHtmlOutputValid:
    def test_html_output_valid(self) -> None:
        result = generate_html(SAMPLE_ARTICLES)
        assert "<html" in result
        assert "<body>" in result
        assert "</html>" in result
        assert "<!DOCTYPE html>" in result


class TestBilingualContent:
    def test_bilingual_content_present(self) -> None:
        result = generate_html(SAMPLE_ARTICLES)
        # EN summaries
        assert "AI valuations face mean reversion risk." in result
        assert "Man Group analyzes AI investment cycle risks." in result
        # ZH summaries
        assert "AI估值面临均值回归风险。" in result
        assert "Man Group分析了AI投资周期的风险。" in result


class TestTimelineSorted:
    def test_timeline_sorted_by_date(self) -> None:
        result = generate_html(SAMPLE_ARTICLES)
        # GMO 2026-03-30 should appear before Man 2026-03-28
        pos_gmo = result.index("Value in Emerging Markets")
        pos_man = result.index("AI Boom or Bust?")
        assert pos_gmo < pos_man, "Newer article (GMO 03-30) should appear before older (Man 03-28)"


class TestBadgeColors:
    def test_badge_colors(self) -> None:
        expected = {"man-group", "bridgewater", "aqr", "gmo", "oaktree"}
        assert set(BADGE_COLORS.keys()) == expected


class TestIndexOnly:
    def test_bridgewater_index_only(self) -> None:
        result = generate_html(SAMPLE_ARTICLES)
        assert "Index only" in result


class TestThemeGrouping:
    def test_theme_grouping(self) -> None:
        result = generate_html(SAMPLE_ARTICLES)
        assert "AI/Tech" in result
        assert "China/EM" in result
        assert "Equities/Value" in result


class TestEmptyArticles:
    def test_empty_articles_graceful(self) -> None:
        result = generate_html([])
        assert "<html" in result
        assert "</html>" in result
        assert "0 articles" in result
