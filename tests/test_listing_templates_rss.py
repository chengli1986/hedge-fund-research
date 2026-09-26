"""The second template type: RSS/XML feeds (2026-09-26).

Five production sources (ark-invest, amundi, verdad-capital, loomis-sayles,
resonanz-capital) read an RSS 2.0 feed and do the same five things: strip the
BOM, walk <item>, take title/link/pubDate, validate the host, cap at
max_articles. They differ in four places only -- a category whitelist (ark),
a path prefix (loomis), sorting by date (three of them) and an ISO pubDate
(amundi) -- so those are the spec's knobs.
"""
import pytest

import listing_templates as lt

SRC = {"id": "t", "url": "https://site.test/insights", "rss_url": "https://site.test/feed",
       "expected_hostname": "site.test", "max_articles": 10}
SPEC = {"type": "rss_feed", "feed": "rss_url"}


def _feed(*items: str, bom: str = "") -> str:
    body = "".join(items)
    return f'{bom}<?xml version="1.0"?><rss version="2.0"><channel>{body}</channel></rss>'


def _item(title="T", link="https://site.test/a", pub="Mon, 21 Sep 2026 10:00:00 +0000",
          categories=()) -> str:
    cats = "".join(f"<category>{c}</category>" for c in categories)
    pub_el = f"<pubDate>{pub}</pubDate>" if pub else ""
    return f"<item><title>{title}</title><link>{link}</link>{pub_el}{cats}</item>"


class TestParseFeed:
    def test_an_item_becomes_a_row(self):
        rows = lt.parse_feed(_feed(_item()), SRC, SPEC)
        assert rows == [{"title": "T", "url": "https://site.test/a", "date": "2026-09-21",
                         "date_raw": "Mon, 21 Sep 2026 10:00:00 +0000",
                         "summary": "", "category": ""}]

    def test_a_byte_order_mark_does_not_break_parsing(self):
        assert len(lt.parse_feed(_feed(_item(), bom="﻿"), SRC, SPEC)) == 1

    def test_an_item_without_a_title_or_a_link_is_skipped(self):
        xml = _feed("<item><link>https://site.test/a</link></item>",
                    "<item><title>T</title></item>", _item())
        assert len(lt.parse_feed(xml, SRC, SPEC)) == 1

    def test_a_link_on_another_host_is_dropped(self):
        xml = _feed(_item(link="https://elsewhere.test/a"), _item())
        assert [r["url"] for r in lt.parse_feed(xml, SRC, SPEC)] == ["https://site.test/a"]

    def test_the_same_link_twice_yields_one_row(self):
        assert len(lt.parse_feed(_feed(_item(), _item()), SRC, SPEC)) == 1

    def test_max_articles_caps_the_result(self):
        xml = _feed(*[_item(link=f"https://site.test/{i}") for i in range(5)])
        assert len(lt.parse_feed(xml, dict(SRC, max_articles=3), SPEC)) == 3

    def test_entities_in_the_title_are_unescaped(self):
        """ARK's feed double-escapes: "ARK&amp;apos;s" was stored verbatim."""
        rows = lt.parse_feed(_feed(_item(title="ARK&amp;apos;s View")), SRC, SPEC)
        assert rows[0]["title"] == "ARK's View"

    def test_an_iso_pubdate_is_understood(self):
        rows = lt.parse_feed(_feed(_item(pub="2026-04-17T10:07:00+02:00")), SRC, SPEC)
        assert rows[0]["date"] == "2026-04-17"

    def test_an_unparseable_date_keeps_the_row(self):
        rows = lt.parse_feed(_feed(_item(pub="sometime last week")), SRC, SPEC)
        assert rows[0]["date"] is None and rows[0]["date_raw"] == "sometime last week"

    def test_an_item_with_no_pubdate_keeps_the_row(self):
        rows = lt.parse_feed(_feed(_item(pub="")), SRC, SPEC)
        assert rows[0]["date"] is None and rows[0]["date_raw"] == ""


class TestFeedKnobs:
    def test_a_category_whitelist_keeps_only_those_items(self):
        xml = _feed(_item(link="https://site.test/a", categories=["Analyst Research"]),
                    _item(link="https://site.test/b", categories=["Podcast"]),
                    _item(link="https://site.test/c", categories=[]))
        spec = dict(SPEC, categories=["analyst research"])
        assert [r["url"] for r in lt.parse_feed(xml, SRC, spec)] == ["https://site.test/a"]

    def test_the_category_whitelist_ignores_case(self):
        xml = _feed(_item(categories=["ANALYST RESEARCH"]))
        assert len(lt.parse_feed(xml, SRC, dict(SPEC, categories=["Analyst Research"]))) == 1

    def test_no_whitelist_keeps_items_that_have_no_category(self):
        assert len(lt.parse_feed(_feed(_item()), SRC, SPEC)) == 1

    def test_a_path_prefix_drops_other_paths(self):
        xml = _feed(_item(link="https://site.test/insights/a"),
                    _item(link="https://site.test/careers/b"))
        rows = lt.parse_feed(xml, SRC, dict(SPEC, path_prefix="/insights/"))
        assert [r["url"] for r in rows] == ["https://site.test/insights/a"]

    def test_sorting_happens_before_the_cap(self):
        """Without this the newest article can be cut off by max_articles."""
        xml = _feed(_item(link="https://site.test/old", pub="Mon, 01 Sep 2026 10:00:00 +0000"),
                    _item(link="https://site.test/new", pub="Mon, 21 Sep 2026 10:00:00 +0000"))
        rows = lt.parse_feed(xml, dict(SRC, max_articles=1), dict(SPEC, sort="date_desc"))
        assert [r["url"] for r in rows] == ["https://site.test/new"]

    def test_without_the_sort_knob_feed_order_is_kept(self):
        xml = _feed(_item(link="https://site.test/old", pub="Mon, 01 Sep 2026 10:00:00 +0000"),
                    _item(link="https://site.test/new", pub="Mon, 21 Sep 2026 10:00:00 +0000"))
        assert [r["url"] for r in lt.parse_feed(xml, SRC, SPEC)] == [
            "https://site.test/old", "https://site.test/new"]

    def test_an_undated_row_sorts_last_not_first(self):
        xml = _feed(_item(link="https://site.test/undated", pub=""),
                    _item(link="https://site.test/dated"))
        rows = lt.parse_feed(xml, SRC, dict(SPEC, sort="date_desc"))
        assert [r["url"] for r in rows] == ["https://site.test/dated", "https://site.test/undated"]


class TestFeedSpecValidation:
    def test_the_feed_key_is_required(self):
        with pytest.raises(ValueError, match="feed"):
            lt.validate_spec({"type": "rss_feed"})

    def test_the_feed_key_must_name_a_config_field(self):
        with pytest.raises(ValueError, match="feed"):
            lt.validate_spec({"type": "rss_feed", "feed": "https://site.test/feed"})

    def test_an_unknown_sort_is_refused(self):
        with pytest.raises(ValueError, match="sort"):
            lt.validate_spec({"type": "rss_feed", "feed": "url", "sort": "title"})

    def test_a_valid_spec_passes(self):
        lt.validate_spec({"type": "rss_feed", "feed": "url", "sort": "date_desc",
                          "categories": ["x"], "path_prefix": "/i/"})

    def test_a_card_list_spec_still_needs_its_own_keys(self):
        with pytest.raises(ValueError, match="card"):
            lt.validate_spec({"type": "card_list", "fetch": "requests"})


class TestFeedFetch:
    def test_fetch_reads_the_url_named_by_the_feed_key(self, monkeypatch):
        seen = {}

        class R:
            text = _feed(_item())
            def raise_for_status(self): pass

        monkeypatch.setattr(lt.requests, "get",
                            lambda url, **kw: (seen.setdefault("url", url), R())[1])
        rows = lt.fetch(dict(SRC, listing_template=dict(SPEC, feed="rss_url")))
        assert seen["url"] == "https://site.test/feed" and len(rows) == 1

    def test_feed_url_falls_back_to_the_source_url(self, monkeypatch):
        seen = {}

        class R:
            text = _feed(_item())
            def raise_for_status(self): pass

        monkeypatch.setattr(lt.requests, "get",
                            lambda url, **kw: (seen.setdefault("url", url), R())[1])
        lt.fetch(dict(SRC, listing_template=dict(SPEC, feed="url")))
        assert seen["url"] == "https://site.test/insights"


class TestFeedExtraFields:
    """A feed carries more than title/link/date, and one of those fields is read.

    _ark_metadata_fallback builds its body from the row's summary and
    category: ARK blocks the article pages, so 74% of its rows (29 of 39)
    are metadata_only and that body is all there is. The rss_feed template
    emitted neither, and on 2026-09-26 ark was switched to it with both
    fields declared droppable -- on the strength of a grep for
    `get("summary")` that missed `get("summary", "")`, the one consumer.
    """
    SRC = {"id": "t", "url": "https://site.test/feed", "rss_url": "https://site.test/feed",
           "expected_hostname": "site.test", "max_articles": 10}
    SPEC = {"type": "rss_feed", "feed": "rss_url"}

    def _rows(self, extra: str):
        xml = ('<rss><channel><item><title>T</title><link>https://site.test/a</link>'
               '<pubDate>Mon, 21 Sep 2026 10:00:00 +0000</pubDate>'
               f'{extra}</item></channel></rss>')
        return lt.parse_feed(xml, self.SRC, self.SPEC)

    def test_the_description_becomes_the_summary(self):
        assert self._rows("<description>Why rates matter</description>")[0]["summary"] == \
            "Why rates matter"

    def test_markup_in_the_description_is_stripped(self):
        """ARK's feed wraps its teaser in <p> and <a>; the stored body is text."""
        rows = self._rows("<description>&lt;p&gt;Now in the &lt;a&gt;letter&lt;/a&gt;&lt;/p&gt;</description>")
        assert rows[0]["summary"] == "Now in the letter"

    def test_categories_are_joined_like_the_hand_written_fetcher(self):
        rows = self._rows("<category>Market Commentary</category><category>Macro</category>")
        assert rows[0]["category"] == "Market Commentary, Macro"

    def test_an_item_without_them_carries_empty_strings(self):
        """Empty is not stored: fetch_source keeps a listing field only when truthy."""
        row = self._rows("")[0]
        assert row["summary"] == "" and row["category"] == ""

    def test_the_category_whitelist_still_reads_the_same_categories(self):
        xml = ('<rss><channel><item><title>T</title><link>https://site.test/a</link>'
               '<category>Podcast</category></item></channel></rss>')
        assert lt.parse_feed(xml, self.SRC, dict(self.SPEC, categories=["analyst research"])) == []


def test_ark_hand_written_summary_decodes_entities_like_the_template():
    """The A/B differed on five of ten rows, all of them entities: the
    bespoke fetcher unescapes its title but not its description, so
    "&apos;" and "&quot;" were stored raw -- the same defect as the mfs
    title, and publish escapes again, so a reader sees the entity."""
    import fetch_articles as fa
    from unittest.mock import patch

    feed = ('<rss><channel><item><title>T</title>'
            '<link>https://ark-invest.com/articles/a</link>'
            '<category>Market Commentary</category>'
            '<description>In this month&amp;apos;s &amp;quot;In The Know&amp;quot;</description>'
            '<pubDate>Mon, 21 Sep 2026 10:00:00 +0000</pubDate></item></channel></rss>')

    class R:
        text = feed
        def raise_for_status(self): pass

    src = {"id": "ark-invest", "url": "https://www.ark-invest.com/feed",
           "expected_hostname": "ark-invest.com", "max_articles": 10}
    with patch("fetch_articles.requests.get", return_value=R()):
        got = fa.fetch_ark_invest(src)
    assert got[0]["summary"] == 'In this month\'s "In The Know"'
