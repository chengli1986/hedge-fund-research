"""Unit tests for publish.py — Stage 4 HTML dashboard."""

from pathlib import Path
import pytest

from publish import BADGE_COLORS, generate_html, publish_html

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
        assert "Bulletin Feed" in result
        assert "Funds" in result
        assert "Themes" in result


class TestBilingualContent:
    def test_bilingual_content_present(self) -> None:
        result = generate_html(SAMPLE_ARTICLES)
        assert "AI valuations face mean reversion risk." in result
        assert "Man Group analyzes AI investment cycle risks." in result
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
    def test_badge_colors_cover_all_production_sources(self) -> None:
        """Every source in sources.json should have an explicit badge color.

        Missing entries fall back to gray (#8b949e), which is not a failure
        but a visual regression signal. This test is kept non-fatal by
        asserting on the contract rather than an exact set.
        """
        import json as _json
        from pathlib import Path as _Path
        config = _json.loads(
            (_Path(__file__).resolve().parent.parent / "config" / "sources.json").read_text()
        )
        source_ids = {s["id"] for s in config["sources"]}
        missing = sorted(source_ids - set(BADGE_COLORS))
        assert not missing, (
            f"Sources without an explicit BADGE_COLORS entry: {missing}. "
            f"Add a color (fallback is gray #8b949e, not a hard failure)."
        )


class TestIndexOnly:
    def test_bridgewater_index_only(self) -> None:
        result = generate_html(SAMPLE_ARTICLES)
        assert 'class="index-chip">Index</span>' in result


class TestThemeGrouping:
    def test_theme_grouping(self) -> None:
        result = generate_html(SAMPLE_ARTICLES)
        assert "AI/Tech" in result
        assert "China/EM" in result
        assert "Equities/Value" in result
        assert "filter-pill" in result


class TestBulletinLayout:
    def test_summary_is_in_collapsible_panel(self) -> None:
        result = generate_html(SAMPLE_ARTICLES)
        assert 'class="summary-panel"' in result
        assert 'class="row-toggle"' in result

    def test_sidebar_fund_panels_present(self) -> None:
        result = generate_html(SAMPLE_ARTICLES)
        assert 'class="fund-panel"' in result
        assert "tracked" in result


class TestEmptyArticles:
    def test_empty_articles_graceful(self) -> None:
        result = generate_html([])
        assert "<html" in result
        assert "</html>" in result
        assert "0 articles" in result


class TestPublishHtml:
    def test_writes_html_and_gzip(self, tmp_path) -> None:
        output = tmp_path / "dashboard.html"
        gzip_path = publish_html(output, "<html>ok</html>")

        assert output.read_text(encoding="utf-8") == "<html>ok</html>"
        assert gzip_path == Path(str(output) + ".gz")
        assert gzip_path.exists()


class TestArticlePool:
    """After the size-reduction refactor, each article card is rendered exactly once
    in a hidden #article-pool; view containers reference articles by id so JS can
    move/return article DOM nodes on view switch."""

    def test_each_article_rendered_exactly_once(self) -> None:
        """Each article's id appears exactly once as an <article> element."""
        result = generate_html(SAMPLE_ARTICLES)
        for a in SAMPLE_ARTICLES:
            occurrences = result.count(f'id="a-{a["id"]}"')
            assert occurrences == 1, (
                f"Article {a['id']} rendered {occurrences} times, expected 1"
            )

    def test_pool_is_hidden_by_default(self) -> None:
        """The article pool itself is display:none (articles move out via JS)."""
        result = generate_html(SAMPLE_ARTICLES)
        assert 'id="article-pool"' in result
        import re
        pool_tag = re.search(r'<div[^>]*id="article-pool"[^>]*>', result)
        assert pool_tag is not None, "article-pool container missing"
        assert 'display:none' in pool_tag.group(0).replace(' ', ''), (
            f"article-pool tag missing display:none — got: {pool_tag.group(0)}"
        )

    def test_pool_articles_carry_filter_data_attributes(self) -> None:
        """Each pool article carries data-source-id, data-date, data-themes
        so view-switching JS can move the right articles into the right views."""
        result = generate_html(SAMPLE_ARTICLES)
        import re
        tag = re.search(r'<article[^>]*id="a-aaa111"[^>]*>', result)
        assert tag is not None, "Pool article aaa111 missing"
        tag_str = tag.group(0)
        assert 'data-source-id="man-group"' in tag_str
        assert 'data-date="2026-03-28"' in tag_str
        assert 'data-themes="ai-tech equities-value"' in tag_str or \
               'data-themes="equities-value ai-tech"' in tag_str

    def test_theme_clusters_reference_article_ids(self) -> None:
        """Themes view clusters carry data-article-ids referencing pool items
        instead of inlining the full article HTML."""
        result = generate_html(SAMPLE_ARTICLES)
        import re
        containers = re.findall(
            r'<div class="cluster-articles"[^>]*data-article-ids="([^"]*)"',
            result,
        )
        assert len(containers) > 0, (
            "Themes view should emit cluster-articles containers with "
            "data-article-ids attributes"
        )
        all_ids = set()
        for c in containers:
            all_ids.update(c.split())
        for a in SAMPLE_ARTICLES:
            if a.get("summarized") and a.get("themes"):
                assert a["id"] in all_ids, (
                    f"Article {a['id']} not referenced by any cluster container"
                )

    def test_funds_view_references_article_ids(self) -> None:
        """Funds view containers also reference pool articles by id."""
        result = generate_html(SAMPLE_ARTICLES)
        import re
        fund_containers = re.findall(
            r'<section class="cluster fund-section"[^>]*data-source-id="([^"]+)"[\s\S]*?'
            r'<div class="cluster-articles"[^>]*data-article-ids="([^"]*)"',
            result,
        )
        assert len(fund_containers) > 0, (
            "Funds view should emit fund-section containers with "
            "data-source-id and data-article-ids"
        )
        seen_sources = {src for src, _ in fund_containers}
        for a in SAMPLE_ARTICLES:
            assert a["source_id"] in seen_sources, (
                f"Fund {a['source_id']} has no fund-section container"
            )

    def test_timeline_wrap_is_empty_container(self) -> None:
        """Timeline view contains an empty .timeline-wrap (articles injected by JS)."""
        result = generate_html(SAMPLE_ARTICLES)
        import re
        m = re.search(
            r'<div class="timeline-wrap"[^>]*>([\s\S]*?)</div>',
            result,
        )
        assert m is not None, "timeline-wrap missing"
        inner = m.group(1)
        assert '<article' not in inner, (
            "Timeline wrap should start empty — articles are moved in by JS on "
            f"view switch. Found article tag inside: {inner[:200]}"
        )


class TestFundDistributionChart:
    """The Funds view opens with a compact horizontal bar chart showing how many
    articles each fund has — pure CSS, no JS dependency."""

    SKEWED_ARTICLES = [
        {"id": "m1", "source_id": "man-group", "source_name": "Man", "title": "A", "url": "u", "date": "2026-03-01", "summarized": False},
        {"id": "m2", "source_id": "man-group", "source_name": "Man", "title": "B", "url": "u", "date": "2026-03-02", "summarized": False},
        {"id": "m3", "source_id": "man-group", "source_name": "Man", "title": "C", "url": "u", "date": "2026-03-03", "summarized": False},
        {"id": "m4", "source_id": "man-group", "source_name": "Man", "title": "D", "url": "u", "date": "2026-03-04", "summarized": False},
        {"id": "b1", "source_id": "bridgewater", "source_name": "Bridgewater", "title": "E", "url": "u", "date": "2026-03-01", "summarized": False},
        {"id": "b2", "source_id": "bridgewater", "source_name": "Bridgewater", "title": "F", "url": "u", "date": "2026-03-02", "summarized": False},
        {"id": "g1", "source_id": "gmo", "source_name": "GMO", "title": "G", "url": "u", "date": "2026-03-01", "summarized": False},
    ]

    def test_distribution_container_present(self) -> None:
        result = generate_html(self.SKEWED_ARTICLES)
        assert 'class="fund-distribution"' in result, (
            "Funds view should include a .fund-distribution chart container"
        )

    def test_row_per_fund_with_articles(self) -> None:
        """Each fund with >=1 article gets a .fund-dist-row."""
        result = generate_html(self.SKEWED_ARTICLES)
        import re
        rows = re.findall(
            r'<div class="fund-dist-row"[^>]*data-source-id="([^"]+)"',
            result,
        )
        assert set(rows) == {"man-group", "bridgewater", "gmo"}, (
            f"Expected rows for man-group/bridgewater/gmo, got {rows}"
        )

    def test_counts_displayed_in_rows(self) -> None:
        """Each row displays the article count in a .fund-dist-count span."""
        result = generate_html(self.SKEWED_ARTICLES)
        import re
        for sid, expected_count in [("man-group", 4), ("bridgewater", 2), ("gmo", 1)]:
            m = re.search(
                rf'<div class="fund-dist-row"[^>]*data-source-id="{sid}"[\s\S]*?'
                rf'<span class="fund-dist-count"[^>]*>(\d+)</span>',
                result,
            )
            assert m is not None, f"No count span found for {sid}"
            assert int(m.group(1)) == expected_count, (
                f"{sid}: expected count {expected_count}, got {m.group(1)}"
            )

    def test_bar_width_proportional_to_max(self) -> None:
        """The top fund's bar is 100%; others are (count/max)*100%."""
        result = generate_html(self.SKEWED_ARTICLES)
        import re
        m_top = re.search(
            r'<div class="fund-dist-row"[^>]*data-source-id="man-group"[\s\S]*?'
            r'<div class="fund-dist-bar"[^>]*style="[^"]*width:\s*([\d.]+)%',
            result,
        )
        assert m_top is not None, "man-group bar not found"
        assert float(m_top.group(1)) == 100.0, (
            f"Top fund bar should be 100%, got {m_top.group(1)}%"
        )
        m_mid = re.search(
            r'<div class="fund-dist-row"[^>]*data-source-id="bridgewater"[\s\S]*?'
            r'<div class="fund-dist-bar"[^>]*style="[^"]*width:\s*([\d.]+)%',
            result,
        )
        assert m_mid is not None, "bridgewater bar not found"
        assert 49.0 <= float(m_mid.group(1)) <= 51.0, (
            f"bridgewater bar should be ~50%, got {m_mid.group(1)}%"
        )
