"""The third template type: JSON listing APIs (2026-09-26).

jpmam and mfs both GET a JSON endpoint and map three fields out of a list of
records. The other four JSON sources do not fit and stay hand-written: gsam
builds gsam_summary by joining two fields and fetch_content reads it,
metlife-im rewrites AEM repository paths into public URLs, gmo scrapes its
endpoint out of a data-endpoint attribute, and principal-am scavenges a Coveo
bearer token from a live browser session.
"""
import json

import pytest

import listing_templates as lt

SRC = {"id": "t", "url": "https://site.test/insights", "expected_hostname": "site.test",
       "max_articles": 10}
SPEC = {"type": "api_json", "endpoint": "/api/list", "items": "pages",
        "title": "title", "url": "url", "date": "displayDate"}


def _payload(*pages: dict) -> dict:
    return {"pages": list(pages)}


def _page(title="T", url="/a", date="09/21/2026") -> dict:
    return {"title": title, "url": url, "displayDate": date}


class TestParseItems:
    def test_a_record_becomes_a_row(self):
        rows = lt.parse_items(_payload(_page()), SRC, SPEC)
        assert rows == [{"title": "T", "url": "https://site.test/a", "date": "2026-09-21",
                         "date_raw": "09/21/2026"}]

    def test_a_nested_items_path_is_followed(self):
        spec = dict(SPEC, items="response.docs")
        payload = {"response": {"docs": [{"title": "T", "url": "https://site.test/a",
                                          "displayDate": "09/21/2026"}]}}
        assert len(lt.parse_items(payload, SRC, spec)) == 1

    def test_a_missing_items_path_yields_nothing_rather_than_raising(self):
        assert lt.parse_items({"other": []}, SRC, SPEC) == []

    def test_an_items_path_pointing_at_a_non_list_yields_nothing(self):
        for payload in ({"pages": {"a": 1}}, {"pages": "abc"}, {"pages": None}):
            assert lt.parse_items(payload, SRC, SPEC) == [], payload

    def test_an_absolute_url_is_left_alone(self):
        rows = lt.parse_items(_payload(_page(url="https://site.test/deep/a")), SRC, SPEC)
        assert rows[0]["url"] == "https://site.test/deep/a"

    def test_a_record_on_another_host_is_dropped(self):
        rows = lt.parse_items(_payload(_page(url="https://elsewhere.test/a"), _page()), SRC, SPEC)
        assert [r["url"] for r in rows] == ["https://site.test/a"]

    def test_records_without_a_title_or_url_are_skipped(self):
        rows = lt.parse_items(_payload(_page(title=""), _page(url=""), _page()), SRC, SPEC)
        assert len(rows) == 1

    def test_the_same_url_twice_yields_one_row(self):
        assert len(lt.parse_items(_payload(_page(), _page()), SRC, SPEC)) == 1

    def test_max_articles_caps_the_result(self):
        pages = [_page(url=f"/{i}") for i in range(5)]
        assert len(lt.parse_items(_payload(*pages), dict(SRC, max_articles=3), SPEC)) == 3

    def test_a_missing_date_keeps_the_row(self):
        rows = lt.parse_items(_payload(_page(date="")), SRC, SPEC)
        assert rows[0]["date"] is None and rows[0]["date_raw"] == ""

    def test_an_iso_date_is_understood(self):
        rows = lt.parse_items(_payload(_page(date="2026-09-21T10:07:00Z")), SRC, SPEC)
        assert rows[0]["date"] == "2026-09-21"


class TestFieldMapping:
    def test_a_field_can_name_a_fallback_chain(self):
        spec = dict(SPEC, title=["title", "summaryTitle"])
        rows = lt.parse_items(_payload({"summaryTitle": "S", "url": "/a"}), SRC, spec)
        assert rows[0]["title"] == "S"

    def test_the_first_field_of_a_chain_wins(self):
        spec = dict(SPEC, title=["title", "summaryTitle"])
        rows = lt.parse_items(_payload({"title": "T", "summaryTitle": "S", "url": "/a"}),
                              SRC, spec)
        assert rows[0]["title"] == "T"

    def test_a_title_wrapped_in_markup_becomes_plain_text(self):
        """MFS's CMS field is rich text: "<p>Market Pulse</p>" arrives verbatim."""
        rows = lt.parse_items(_payload(_page(title="<p>Market Pulse</p>")), SRC, SPEC)
        assert rows[0]["title"] == "Market Pulse"

    def test_entities_in_a_plain_title_are_unescaped(self):
        rows = lt.parse_items(_payload(_page(title="Relative Value &amp; Credit")), SRC, SPEC)
        assert rows[0]["title"] == "Relative Value & Credit"

    def test_a_non_string_field_does_not_crash_the_run(self):
        rows = lt.parse_items(_payload({"title": 42, "url": "/a"}), SRC, SPEC)
        assert rows == []

    def test_sorting_happens_before_the_cap(self):
        payload = _payload(_page(url="/old", date="09/01/2026"),
                           _page(url="/new", date="09/21/2026"))
        rows = lt.parse_items(payload, dict(SRC, max_articles=1), dict(SPEC, sort="date_desc"))
        assert [r["url"] for r in rows] == ["https://site.test/new"]


class TestApiSpecValidation:
    def test_endpoint_and_items_and_title_and_url_are_required(self):
        for missing in ("endpoint", "items", "title", "url"):
            spec = {k: v for k, v in SPEC.items() if k != missing}
            with pytest.raises(ValueError, match=missing):
                lt.validate_spec(spec)

    def test_a_valid_spec_passes(self):
        lt.validate_spec(SPEC)
        lt.validate_spec(dict(SPEC, params={"q": "*"}, sort="date_desc",
                              title=["title", "summaryTitle"]))


class TestApiFetch:
    class _R:
        def __init__(self, payload):
            self._p = payload
        def raise_for_status(self): pass
        def json(self): return self._p

    def test_the_endpoint_is_joined_onto_the_site(self, monkeypatch):
        seen = {}

        def fake_get(url, **kw):
            seen["url"], seen["kw"] = url, kw
            return self._R(_payload(_page()))

        self._R = TestApiFetch._R
        monkeypatch.setattr(lt.requests, "get", fake_get)
        rows = lt.fetch(dict(SRC, listing_template=SPEC))
        assert seen["url"] == "https://site.test/api/list" and len(rows) == 1

    def test_an_absolute_endpoint_is_used_as_is(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(lt.requests, "get", lambda url, **kw: (
            seen.setdefault("url", url), TestApiFetch._R(_payload()))[1])
        lt.fetch(dict(SRC, listing_template=dict(SPEC, endpoint="https://api.test/v1")))
        assert seen["url"] == "https://api.test/v1"

    def test_params_are_sent(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(lt.requests, "get", lambda url, **kw: (
            seen.update(kw), TestApiFetch._R(_payload()))[1])
        lt.fetch(dict(SRC, listing_template=dict(SPEC, params={"q": "*"})))
        assert seen["params"] == {"q": "*"}
        assert seen["headers"]["Accept"] == "application/json"


class TestTitleWhitespace:
    """All three types normalise title whitespace the same way.

    MFS's Solr docs carry a literal "&nbsp;" in one headline. The
    hand-written fetcher stored the entity verbatim (publish html-escapes it,
    so the reader saw the six characters), and unescaping it only moves the
    problem to a U+00A0. A title is text: runs of whitespace collapse.
    """

    def test_an_nbsp_entity_becomes_a_plain_space(self):
        rows = lt.parse_items(_payload(_page(title="Who Owns the Outcome?&nbsp;AI")),
                              SRC, SPEC)
        assert rows[0]["title"] == "Who Owns the Outcome? AI"

    def test_a_newline_in_a_json_title_collapses(self):
        rows = lt.parse_items(_payload(_page(title="Two\n   lines")), SRC, SPEC)
        assert rows[0]["title"] == "Two lines"

    def test_a_feed_title_normalises_the_same_way(self):
        """&nbsp; is not valid XML -- a feed writes it as &#160; or raw."""
        xml = ('<rss><channel><item><title>A&#160;B</title>'
               '<link>https://site.test/a</link></item></channel></rss>')
        assert lt.parse_feed(xml, SRC, {"type": "rss_feed", "feed": "url"})[0]["title"] == "A B"
