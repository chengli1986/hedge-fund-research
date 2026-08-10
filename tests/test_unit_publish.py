"""Unit tests for publish.py — Stage 4 HTML dashboard."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest

from publish import BADGE_COLORS, generate_html, publish_html

BJT = timezone(timedelta(hours=8))


def _date_str(days_ago: int) -> str:
    """Return an ISO date string for `days_ago` days before today (BJT)."""
    return (datetime.now(BJT) - timedelta(days=days_ago)).strftime("%Y-%m-%d")

# --- Sample data ---

# Dates are relative, not literal: fixed dates silently cross the RECENT_DAYS
# boundary as the calendar advances, which is exactly what broke these tests on
# 2026-08-10 when older articles stopped being rendered into the initial DOM.
SAMPLE_ARTICLES = [
    {
        "id": "aaa111",
        "source_id": "man-group",
        "source_name": "Man",
        "title": "AI Boom or Bust?",
        "url": "https://man.com/ai-boom",
        "date": _date_str(8),
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
        "date": _date_str(12),
        "summarized": False,
    },
    {
        "id": "ccc333",
        "source_id": "gmo",
        "source_name": "GMO",
        "title": "Value in Emerging Markets",
        "url": "https://gmo.com/em-value",
        "date": _date_str(5),
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
        assert "<body" in result  # may carry classes (e.g. hide-older)
        assert "</html>" in result
        assert "<!DOCTYPE html>" in result
        assert "Bulletin Feed" in result
        assert "Funds" in result
        assert "Themes" in result


class TestBilingualContent:
    def test_bilingual_content_present(self) -> None:
        # Use a recent date: publish.py excludes the analysis BODY (summary) of
        # articles older than RECENT_DAYS (90) from the lazy-hydration JSON island
        # to keep initial parse cost flat (inline takeaways still render at any
        # age). SAMPLE_ARTICLES carries hard-coded 2026-03 dates that have since
        # aged past 90d, which would drop the summary from the HTML — so date the
        # article relative to "now" to exercise the full bilingual body + takeaway.
        article = {**SAMPLE_ARTICLES[0], "date": _date_str(2)}
        result = generate_html([article])
        assert "AI valuations face mean reversion risk." in result  # key_takeaway_en (inline)
        assert "Man Group analyzes AI investment cycle risks." in result  # summary_en (island)
        assert "AI估值面临均值回归风险。" in result  # key_takeaway_zh (inline)
        assert "Man Group分析了AI投资周期的风险。" in result  # summary_zh (island)


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
        assert f'data-date="{_date_str(8)}"' in tag_str
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

    def test_pool_articles_carry_data_seq_in_global_date_order(self) -> None:
        """Each pool article carries data-seq = its position in the global
        date-descending order, so the timeline view can restore chronological
        order no matter how theme/fund hydration has shuffled the pool DOM."""
        result = generate_html(SAMPLE_ARTICLES)
        import re
        seqs = {}
        for m in re.finditer(
            r'<article id="a-([^"]+)" class="pool-article"[^>]*data-seq="(\d+)"',
            result,
        ):
            seqs[m.group(1)] = int(m.group(2))
        assert set(seqs) == {a["id"] for a in SAMPLE_ARTICLES}, (
            f"Every pool article needs a data-seq attribute — got {seqs}"
        )
        # Date-descending: ccc333 (03-30) < aaa111 (03-28) < bbb222 (03-25)
        assert seqs["ccc333"] < seqs["aaa111"] < seqs["bbb222"], (
            f"data-seq must follow global date-descending order, got {seqs}"
        )

    def test_timeline_populate_sorts_pool_by_seq(self) -> None:
        """The timeline branch of populateViewFromPool must sort pool articles
        by data-seq before appending — pool DOM order is scrambled after any
        themes/funds hydration (returnArticlesToPool appends in document order),
        so appending unsorted breaks the chronological feed."""
        result = generate_html(SAMPLE_ARTICLES)
        timeline_branch = result.split("if (viewName === 'timeline')")[1] \
                                .split("else if")[0]
        assert "dataset.seq" in timeline_branch and ".sort(" in timeline_branch, (
            "Timeline populate must sort articles by dataset.seq — relying on "
            "pool DOM order regresses to fund/theme-grouped output"
        )


class TestFundDistributionChart:
    """The Funds view opens with a compact horizontal bar chart showing how many
    articles each fund has — pure CSS, no JS dependency."""

    SKEWED_ARTICLES = [
        {"id": "m1", "source_id": "man-group", "source_name": "Man", "title": "A", "url": "u", "date": _date_str(23), "summarized": False},
        {"id": "m2", "source_id": "man-group", "source_name": "Man", "title": "B", "url": "u", "date": _date_str(22), "summarized": False},
        {"id": "m3", "source_id": "man-group", "source_name": "Man", "title": "C", "url": "u", "date": _date_str(21), "summarized": False},
        {"id": "m4", "source_id": "man-group", "source_name": "Man", "title": "D", "url": "u", "date": _date_str(20), "summarized": False},
        {"id": "b1", "source_id": "bridgewater", "source_name": "Bridgewater", "title": "E", "url": "u", "date": _date_str(23), "summarized": False},
        {"id": "b2", "source_id": "bridgewater", "source_name": "Bridgewater", "title": "F", "url": "u", "date": _date_str(22), "summarized": False},
        {"id": "g1", "source_id": "gmo", "source_name": "GMO", "title": "G", "url": "u", "date": _date_str(23), "summarized": False},
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


class TestOlderArticleFolding:
    """UI folds articles older than RECENT_DAYS (90d) by default. Pool articles
    carry data-age="recent"|"older"; body starts with class hide-older; a toolbar
    button reveals older articles when present. Older articles' LLM analysis
    bodies are also excluded from the inline JSON island to keep parse cost flat
    (2026-05-29 — tightened from 180d to 90d for perf)."""

    # Articles spanning recent (≤90d) and older (>90d) buckets.
    # Dates computed dynamically to keep tests stable across calendar drift.
    MIXED_ARTICLES = [
        {  # Recent — well inside 90d window
            "id": "rec1", "source_id": "man-group", "source_name": "Man",
            "title": "Recent Macro Note", "url": "https://man.com/recent",
            "date": _date_str(30), "summarized": True,
            "summary_en": "Recent macro analysis.", "summary_zh": "近期宏观分析。",
            "key_takeaway_en": "Macro tailwinds.", "key_takeaway_zh": "宏观顺风。",
            "themes": ["Macro/Rates"],
        },
        {  # Recent — close to 90d boundary (89d ago, should still be recent)
            "id": "rec2", "source_id": "bridgewater", "source_name": "Bridgewater",
            "title": "Boundary Recent", "url": "https://bridgewater.com/boundary",
            "date": _date_str(89), "summarized": False,
        },
        {  # Older — just past 90d boundary (91d ago)
            "id": "old1", "source_id": "gmo", "source_name": "GMO",
            "title": "Just Over Boundary", "url": "https://gmo.com/just-over",
            "date": _date_str(91), "summarized": True,
            "summary_en": "Older but still relevant.", "summary_zh": "较旧但仍相关。",
            "key_takeaway_en": "Long-term thesis.", "key_takeaway_zh": "长期论点。",
            "themes": ["Equities/Value"],
        },
        {  # Older — way out (1 year ago)
            "id": "old2", "source_id": "ark-invest", "source_name": "ARK",
            "title": "Year Old Report", "url": "https://ark.com/year-old",
            "date": _date_str(365), "summarized": False,
        },
    ]

    ALL_RECENT_ARTICLES = [
        {"id": "r1", "source_id": "man-group", "source_name": "Man",
         "title": "Fresh A", "url": "u", "date": _date_str(10), "summarized": False},
        {"id": "r2", "source_id": "gmo", "source_name": "GMO",
         "title": "Fresh B", "url": "u", "date": _date_str(60), "summarized": False},
    ]

    def test_recent_article_has_data_age_recent(self) -> None:
        result = generate_html(self.MIXED_ARTICLES)
        import re
        for aid in ("rec1", "rec2"):
            tag = re.search(rf'<article[^>]*id="a-{aid}"[^>]*>', result)
            assert tag is not None, f"pool article {aid} missing"
            assert 'data-age="recent"' in tag.group(0), (
                f"{aid} should be tagged data-age=recent (it's ≤90d), got: {tag.group(0)}"
            )

    def test_older_article_has_data_age_older(self) -> None:
        """Older articles keep data-age="older", but since 2026-08-10 they live
        in the #older-articles-data island rather than the initial DOM, so the
        markup is asserted there."""
        result = generate_html(self.MIXED_ARTICLES)
        import re
        island = result.split('id="older-articles-data">', 1)[1].split("</script>", 1)[0]
        for aid in ("old1", "old2"):
            assert re.search(rf'<article[^>]*id=\\"a-{aid}\\"[^>]*>', island) \
                or re.search(rf'<article[^>]*id="a-{aid}"[^>]*>', island), \
                f"{aid} missing from the deferred-articles island"
            assert 'data-age=' in island and 'older' in island, (
                f"{aid} should still carry data-age=older inside the island"
            )
            assert f'id="a-{aid}"' not in result.split('id="older-articles-data"')[0], (
                f"{aid} must not be in the initial DOM any more"
            )

    def test_90d_boundary_inclusive(self) -> None:
        """Articles dated exactly 90 days ago count as recent (>= cutoff)."""
        articles = [
            {"id": "edge", "source_id": "man-group", "source_name": "Man",
             "title": "Edge", "url": "u", "date": _date_str(90), "summarized": False},
        ]
        result = generate_html(articles)
        import re
        tag = re.search(r'<article[^>]*id="a-edge"[^>]*>', result)
        assert tag is not None
        assert 'data-age="recent"' in tag.group(0), (
            f"Article exactly 90d old should be recent (inclusive boundary), got: {tag.group(0)}"
        )

    def test_older_article_details_excluded_from_json_island(self) -> None:
        """Articles >RECENT_DAYS have their LLM analysis bodies excluded from the
        inline JSON island. The <article> shell still renders (so 'Show older'
        reveals title + source link), but bodies don't add parse cost upfront.

        old1 is summarized + has body text "Older but still relevant." — this
        text must NOT appear anywhere in the rendered HTML, because the article
        was excluded from the JSON island."""
        import json, re
        result = generate_html(self.MIXED_ARTICLES)

        # rec1 (≤90d summarized) MUST be in JSON island
        m = re.search(r'<script[^>]*id="article-details-data"[^>]*>(.*?)</script>',
                      result, re.S)
        assert m, "JSON island must be present"
        inline = json.loads(m.group(1))
        assert "a-rec1" in inline, \
            f"recent summarized article should be in JSON island, got keys: {list(inline)}"

        # old1 (>90d summarized) MUST NOT be in JSON island
        assert "a-old1" not in inline, \
            f"older summarized article should be excluded, got keys: {list(inline)}"

        # And old1's body text must not appear anywhere in HTML
        assert "Older but still relevant." not in result, (
            "Older article body should be entirely absent from HTML"
        )
        # But old1's <article> shell must still ship — since 2026-08-10 inside the
        # deferred island (JSON-escaped), which "Show older" injects on demand.
        older_island = result.split('id="older-articles-data">', 1)[1].split("</script>", 1)[0]
        assert "a-old1" in older_island, (
            "older article shell must ship in the deferred island so Show older can inject it"
        )

    def test_js_renders_older_notice_when_details_missing(self) -> None:
        """JS hydrate function falls back to a bilingual 'see source above' notice
        when the JSON island has no entry for an article (older case). Verify the
        notice text appears in the JS template (the older-notice class is the marker)."""
        result = generate_html(self.MIXED_ARTICLES)
        assert "older-notice" in result, \
            "JS hydrate fallback (older-notice class) must be present in JS template"
        assert "Older article — see source link above" in result
        assert "请查看上方源链接" in result

    def test_unknown_date_defaults_to_recent(self) -> None:
        """Missing/empty date field — fall back to 'recent' so data isn't silently hidden."""
        articles = [
            {"id": "nodate", "source_id": "man-group", "source_name": "Man",
             "title": "No Date", "url": "u", "date": "", "summarized": False},
        ]
        result = generate_html(articles)
        import re
        tag = re.search(r'<article[^>]*id="a-nodate"[^>]*>', result)
        assert tag is not None
        assert 'data-age="recent"' in tag.group(0), (
            "Articles without a parseable date should default to 'recent' "
            "(safer: visible by default, not silently folded)"
        )

    def test_body_has_hide_older_class_by_default(self) -> None:
        """Body element carries `hide-older` class on initial render so CSS
        folds older articles by default."""
        result = generate_html(self.MIXED_ARTICLES)
        import re
        body_tag = re.search(r'<body[^>]*>', result)
        assert body_tag is not None, "body tag missing"
        assert 'hide-older' in body_tag.group(0), (
            f"body should start with class hide-older, got: {body_tag.group(0)}"
        )

    def test_hide_older_css_rule_present(self) -> None:
        """CSS hides data-age=older articles when body has hide-older class."""
        result = generate_html(self.MIXED_ARTICLES)
        # Normalize whitespace for robust matching
        compact = " ".join(result.split())
        assert 'body.hide-older' in compact, "missing body.hide-older CSS scope"
        assert 'data-age="older"' in compact or "data-age='older'" in compact

    def test_show_older_toggle_button_rendered_when_older_exist(self) -> None:
        """Toolbar shows a toggle button when there are older articles."""
        result = generate_html(self.MIXED_ARTICLES)
        import re
        btn = re.search(
            r'<button[^>]*id="btn-show-older"[^>]*>([\s\S]*?)</button>',
            result,
        )
        assert btn is not None, "btn-show-older button missing when 2 older articles exist"
        body = btn.group(1)
        assert "(2)" in body, (
            f"button body should display older count (2), got: {body!r}"
        )
        # Bilingual labels expected (lang-en/lang-zh spans toggle visibility via toggleLang).
        assert "Show older" in body, f"missing English label, got: {body!r}"
        assert "显示更旧" in body, f"missing Chinese label, got: {body!r}"

    def test_no_toggle_button_when_all_recent(self) -> None:
        """When 0 older articles exist, the toggle button is suppressed."""
        result = generate_html(self.ALL_RECENT_ARTICLES)
        assert 'id="btn-show-older"' not in result, (
            "button must not appear when there are no older articles"
        )

    def test_toggle_button_wires_to_toggleOlder(self) -> None:
        """Button onclick (or addEventListener target) names toggleOlder."""
        result = generate_html(self.MIXED_ARTICLES)
        assert 'toggleOlder' in result, (
            "publish.py must emit a toggleOlder() JS handler for the button"
        )


# --- Audit fixes 2026-08-10 (docs page audit) ---------------------------------

class TestMonthOnlyDates:
    """Month-granularity dates must not be rendered as a specific future day.

    fetch_articles.parse_date deliberately normalises "Aug 2026" to the LAST day
    of the month so staleness checks don't penalise month-granularity publishers
    ~30 days early. That is right for the backend, but the dashboard was showing
    the normalised value verbatim, so on 2026-08-10 the top of the page read
    "2026-08-31" — three weeks in the future. date_raw carries the original.
    """

    def _article(self, **over):
        a = {
            "id": "m1", "source_id": "man-group", "source_name": "Man",
            "title": "Portable Alpha", "url": "https://man.com/pa",
            "date": "2026-08-31", "date_raw": "Aug 2026", "summarized": False,
        }
        a.update(over)
        return a

    def test_month_only_date_shows_raw_not_normalised_day(self):
        html = generate_html([self._article()])
        assert "Aug 2026" in html, "should surface the original month-granularity label"
        assert ">2026-08-31<" not in html, "must not render the month-end normalised day"

    def test_day_precision_date_still_renders_iso(self):
        html = generate_html([self._article(date="2026-08-04", date_raw="August 4, 2026")])
        assert "2026-08-04" in html

    def test_missing_date_raw_falls_back_to_iso(self):
        a = self._article()
        del a["date_raw"]
        html = generate_html([a])
        assert "2026-08-31" in html, "without date_raw there is nothing better to show"


class TestNewThisWeekBounds:
    """`new this week` counted `date >= week_ago` with no upper bound, so the
    month-end normalised articles above inflated it (46 shown vs 41 real)."""

    def test_future_dated_articles_are_not_counted_as_new(self):
        future = (datetime.now(BJT) + timedelta(days=21)).strftime("%Y-%m-%d")
        arts = [
            {"id": "f1", "source_id": "man-group", "source_name": "Man", "title": "Future",
             "url": "https://man.com/f", "date": future, "date_raw": "Aug 2026", "summarized": False},
            {"id": "r1", "source_id": "man-group", "source_name": "Man", "title": "Recent",
             "url": "https://man.com/r", "date": _date_str(2), "summarized": False},
        ]
        html = generate_html(arts)
        assert "1 new this week" in html, "only the genuinely recent article counts"


class TestUnregisteredSourcesFiltered:
    """Sources dropped from sources.json (e.g. pgim, demoted 2026-07) kept their
    historical articles on the page, so the header said 39 funds while 40 were
    actually displayed."""

    def test_article_from_unknown_source_is_not_rendered(self):
        arts = [
            {"id": "k1", "source_id": "man-group", "source_name": "Man", "title": "Known",
             "url": "https://man.com/k", "date": _date_str(1), "summarized": False},
            {"id": "u1", "source_id": "pgim", "source_name": "PGIM", "title": "Retired Source",
             "url": "https://pgim.com/u", "date": _date_str(1), "summarized": False},
        ]
        html = generate_html(arts)
        assert "Known" in html
        assert "Retired Source" not in html, "demoted source must not appear on the page"


class TestOlderArticlesLazyLoaded:
    """Articles past RECENT_DAYS must not be in the initial DOM.

    They were rendered in full and merely hidden with `display:none`, so the
    browser still parsed and built every node — 409 of 1074 articles (~17k DOM
    nodes) for content nobody had asked to see. They now ship as HTML strings in
    a JSON island and are injected on first "Show older" click.
    """

    def _arts(self):
        return [
            {"id": "recent1", "source_id": "man-group", "source_name": "Man",
             "title": "Recent Piece", "url": "https://man.com/r",
             "date": _date_str(5), "summarized": False},
            {"id": "old1", "source_id": "man-group", "source_name": "Man",
             "title": "Ancient Piece", "url": "https://man.com/o",
             "date": _date_str(400), "summarized": False},
        ]

    def test_older_article_not_in_initial_pool(self):
        html = generate_html(self._arts())
        import re
        # Only the <article> cards matter here; the compact table and fund list
        # still render every article server-side (deliberately out of scope).
        initial = html.split('id="older-articles-data"')[0]
        cards = "".join(re.findall(r"<article\b.*?</article>", initial, re.S))
        assert "Recent Piece" in cards
        assert "Ancient Piece" not in cards, "older article must not be an <article> in the initial DOM"

    def test_older_article_present_in_data_island(self):
        html = generate_html(self._arts())
        assert 'id="older-articles-data"' in html, "island holding deferred articles must exist"
        island = html.split('id="older-articles-data">', 1)[1].split("</script>", 1)[0]
        assert "Ancient Piece" in island
        assert "Recent Piece" not in island

    def test_no_island_when_nothing_is_old(self):
        html = generate_html([self._arts()[0]])
        assert 'id="older-articles-data"' not in html

    def test_toggle_injects_before_revealing(self):
        html = generate_html(self._arts())
        assert "older-articles-data" in html
        # the toggle must consult the island, not just flip a CSS class
        assert "ensureOlderLoaded" in html
