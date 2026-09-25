"""One tool per page type, instead of one hand-written fetcher per source.

2026-09-21. Of the 42 production fetchers, 23 are the same shape -- a listing
page of cards, each card carrying a title, a link and a date -- written 23
times, 1,367 lines. The four things that differ are: how the HTML is obtained
(requests, or a browser waiting for a selector), and three CSS selectors. The
rest is identical boilerplate: validate the host, drop duplicates, parse the
date, cut to max_articles, build the same dict.

The cost of that duplication is on the record. The hardcoded-host sweep found
the same defect in 24 of 40 fetchers; the word-fusing bug lived in four custom
paragraph loops; networkidle sits in 27 places. One defect, twenty-four fixes,
and a few always missed.

This is the engine for that one type. It does NOT replace anything: a source
keeps its hand-written fetcher until an A/B run proves the template returns
exactly the same titles, urls and dates from the live site
(scripts/compare_fetchers.py), and only then is it switched over. Types that
are genuinely different -- JSON APIs, sitemaps, sites behind an attestation
form -- keep their own code; forcing them into a template would turn the
template into the forty-two if-statements it exists to remove.
"""
import pytest

import listing_templates as lt

CARD_HTML = """
<html><body>
  <article class="c-list-article">
    <h2><a href="https://site.test/insight/one">First piece</a></h2>
    <p class="c-type--body-sm">March 2026</p>
  </article>
  <article class="c-list-article">
    <h3><a href="/insight/two">Second piece</a></h3>
    <p class="c-type--body-sm">April 3, 2026</p>
  </article>
  <article class="c-list-article">
    <h2><a href="https://elsewhere.test/x">Not ours</a></h2>
    <p class="c-type--body-sm">May 2026</p>
  </article>
</body></html>
"""

SPEC = {"type": "card_list", "fetch": "requests", "card": "article.c-list-article",
        "link": "h2 a, h3 a", "date": "p.c-type--body-sm"}
SOURCE = {"id": "t", "url": "https://site.test/insights", "expected_hostname": "site.test",
          "max_articles": 10, "listing_template": SPEC}


class TestCardList:
    def test_it_reads_title_url_and_date(self):
        rows = lt.parse_cards(CARD_HTML, SOURCE, SPEC)
        assert [r["title"] for r in rows] == ["First piece", "Second piece"]
        assert rows[0]["url"] == "https://site.test/insight/one"
        assert rows[0]["date"] == "2026-03-31" and rows[0]["date_raw"] == "March 2026"
        assert rows[1]["date"] == "2026-04-03"

    def test_a_relative_link_is_resolved_against_the_listing(self):
        rows = lt.parse_cards(CARD_HTML, SOURCE, SPEC)
        assert rows[1]["url"] == "https://site.test/insight/two"

    def test_a_link_on_another_host_is_dropped(self):
        rows = lt.parse_cards(CARD_HTML, SOURCE, SPEC)
        assert all("elsewhere" not in r["url"] for r in rows)

    def test_the_same_url_twice_is_kept_once(self):
        html = CARD_HTML + CARD_HTML
        assert len(lt.parse_cards(html, SOURCE, SPEC)) == 2

    def test_max_articles_is_respected(self):
        rows = lt.parse_cards(CARD_HTML, dict(SOURCE, max_articles=1), SPEC)
        assert len(rows) == 1

    def test_a_card_without_a_link_or_title_is_skipped(self):
        html = '<article class="c-list-article"><p class="c-type--body-sm">May 2026</p></article>'
        assert lt.parse_cards(html, SOURCE, SPEC) == []

    def test_a_card_without_a_date_still_counts(self):
        html = '<article class="c-list-article"><h2><a href="/a">A</a></h2></article>'
        rows = lt.parse_cards(html, SOURCE, SPEC)
        assert len(rows) == 1 and rows[0]["date"] is None

    def test_the_anchor_itself_can_be_the_card(self):
        """aberdeen's shape: the card IS the <a>, found by the date inside it."""
        html = ('<a href="/insight/x"><h5>Card title</h5>'
                '<time class="ms-auto">Apr 15, 2026</time></a>')
        spec = {"type": "card_list", "fetch": "playwright", "wait_selector": "time.ms-auto",
                "card": "a:has(time.ms-auto)", "link": "self", "title": "h5",
                "date": "time.ms-auto"}
        rows = lt.parse_cards(html, SOURCE, spec)
        assert rows == [{"title": "Card title", "url": "https://site.test/insight/x",
                         "date": "2026-04-15", "date_raw": "Apr 15, 2026"}]

    def test_a_separate_title_selector_wins_over_the_link_text(self):
        html = '<article class="c-list-article"><h2><a href="/a">link text</a></h2><h4>Real title</h4></article>'
        rows = lt.parse_cards(html, SOURCE, dict(SPEC, title="h4"))
        assert rows[0]["title"] == "Real title"


class TestTheSpecIsChecked:
    @pytest.mark.parametrize("bad,missing", [
        ({"type": "card_list", "fetch": "requests", "link": "a"}, "card"),
        ({"type": "card_list", "card": "div", "link": "a"}, "fetch"),
        ({"type": "unknown_type", "fetch": "requests", "card": "div"}, "type"),
    ])
    def test_an_incomplete_spec_fails_loudly(self, bad, missing):
        with pytest.raises(ValueError) as e:
            lt.validate_spec(bad)
        assert missing in str(e.value)

    def test_a_playwright_spec_without_a_wait_selector_is_allowed(self):
        lt.validate_spec({"type": "card_list", "fetch": "playwright", "card": "div.card"})

    def test_every_template_in_the_production_config_is_valid(self):
        import json
        from pathlib import Path
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config" / "sources.json").read_text())
        for s in cfg["sources"]:
            spec = s.get("listing_template")
            if spec:
                lt.validate_spec(spec)          # raises if a hand edit broke one


class TestFetching:
    def test_requests_mode_asks_for_the_listing_url(self, monkeypatch):
        seen = {}

        class R:
            text = CARD_HTML
            def raise_for_status(self): pass

        monkeypatch.setattr(lt.requests, "get", lambda url, **k: seen.update(url=url) or R())
        rows = lt.fetch(SOURCE)
        assert seen["url"] == SOURCE["url"] and len(rows) == 2

    def test_playwright_mode_passes_the_wait_selector(self, monkeypatch):
        seen = {}
        spec = dict(SPEC, fetch="playwright", wait_selector="time.ms-auto")
        monkeypatch.setattr(lt, "_playwright_html",
                            lambda url, wait_selector=None, **k: seen.update(
                                url=url, sel=wait_selector) or CARD_HTML)
        rows = lt.fetch(dict(SOURCE, listing_template=spec))
        assert seen["sel"] == "time.ms-auto" and len(rows) == 2


class TestTheSwitch:
    """A spec in the config does not switch anything; a flag does.

    Writing the spec and proving it with scripts/compare_fetchers.py must be
    separable from using it, or there is no A/B -- and a template that breaks
    must NOT fall back to the hand-written fetcher in silence, because a silent
    fallback is how a broken template survives for months looking healthy.
    """
    SRC = {"id": "t", "name": "T", "short_name": "T", "method": "ssr",
           "url": "https://site.test/i", "expected_hostname": "site.test", "max_articles": 10,
           "listing_template": {"type": "card_list", "fetch": "requests", "card": "div.card",
                                "link": "a", "date": "span.date"}}

    def _run(self, monkeypatch, source):
        import fetch_articles as fa
        monkeypatch.setitem(fa.FETCHERS, "t", lambda s: [
            {"title": "hand-written", "url": "https://site.test/h", "date": "2026-09-01"}])
        monkeypatch.setattr(lt, "fetch", lambda s: [
            {"title": "template", "url": "https://site.test/t", "date": "2026-09-01"}])
        monkeypatch.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
        return fa.fetch_source(dict(source), set(), dry_run=True, existing_keys={}, existing_rows=[])

    def test_a_spec_alone_changes_nothing(self, monkeypatch):
        got = self._run(monkeypatch, self.SRC)
        assert [r["title"] for r in got] == ["hand-written"]

    def test_the_flag_switches_the_source_over(self, monkeypatch):
        got = self._run(monkeypatch, dict(self.SRC, listing_template_active=True))
        assert [r["title"] for r in got] == ["template"]

    def test_a_broken_template_does_not_fall_back_quietly(self, monkeypatch):
        import fetch_articles as fa
        monkeypatch.setitem(fa.FETCHERS, "t", lambda s: [
            {"title": "hand-written", "url": "https://site.test/h", "date": "2026-09-01"}])
        monkeypatch.setattr(lt, "fetch",
                            lambda s: (_ for _ in ()).throw(RuntimeError("selector gone")))
        monkeypatch.setattr(fa, "record_quality_metrics", lambda *a, **k: None)
        got = fa.fetch_source(dict(self.SRC, listing_template_active=True), set(), dry_run=True,
                              existing_keys={}, existing_rows=[])
        assert got == [], "a broken template quietly used the hand-written fetcher instead"

    def test_an_active_flag_without_a_spec_is_a_config_error(self):
        with pytest.raises(ValueError):
            lt.validate_spec({})

    def test_the_production_config_only_activates_what_it_declares(self):
        import json
        from pathlib import Path
        cfg = json.loads((Path(__file__).resolve().parent.parent / "config" / "sources.json").read_text())
        for s in cfg["sources"]:
            if s.get("listing_template_active"):
                assert s.get("listing_template"), f"{s['id']} activates a template it does not declare"


class TestTwoMoreKnobs:
    """Both are patterns, not site-specific hacks: "only links under this
    path" and "the date is in an attribute" recur across sites. Anything more
    particular than that -- brookfield's title in an aria-label and its date
    found by a regex over the card text, bridgewater's date living in the
    card's grandparent -- stays hand-written, or the template grows back into
    the forty-two if-statements it replaces.
    """
    HTML = """
    <a class="card" href="/insights/one"><h3>Kept</h3><time datetime="2026-09-18">18 Sep</time></a>
    <a class="card" href="/docs/viewer/two"><h3>A PDF viewer</h3><time datetime="2026-09-17">17 Sep</time></a>
    <a class="card" href="/insights/three"><h3>Also kept</h3><time>September 2026</time></a>
    """
    SRC = {"id": "t", "url": "https://site.test/insights", "expected_hostname": "site.test",
           "max_articles": 10}
    SPEC = {"type": "card_list", "fetch": "requests", "card": "a.card", "link": "self",
            "title": "h3", "date": "time", "date_attr": "datetime", "path_prefix": "/insights/"}

    def test_only_links_under_the_path_are_kept(self):
        rows = lt.parse_cards(self.HTML, self.SRC, self.SPEC)
        assert [r["title"] for r in rows] == ["Kept", "Also kept"]

    def test_the_date_comes_from_the_attribute(self):
        rows = lt.parse_cards(self.HTML, self.SRC, self.SPEC)
        assert rows[0]["date"] == "2026-09-18" and rows[0]["date_raw"] == "2026-09-18"

    def test_the_text_is_used_when_the_attribute_is_missing(self):
        rows = lt.parse_cards(self.HTML, self.SRC, self.SPEC)
        assert rows[1]["date"] == "2026-09-30" and rows[1]["date_raw"] == "September 2026"

    def test_without_the_knobs_nothing_changes(self):
        spec = {k: v for k, v in self.SPEC.items() if k not in ("date_attr", "path_prefix")}
        rows = lt.parse_cards(self.HTML, self.SRC, spec)
        assert len(rows) == 3 and rows[0]["date_raw"] == "18 Sep"


class TestWaitUntilKnob:
    """networkidle is the Playwright default and 27 places in this repo had to
    be moved off it (audit follow-up, 2026-09-20). A template must be able to
    say which wait it wants, or every source it takes over inherits the one
    that times out on beacon-heavy sites."""
    def test_the_spec_can_choose_the_wait(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(lt, "_playwright_html",
                            lambda url, wait_selector=None, wait_until="networkidle", **k:
                            seen.update(wait_until=wait_until) or "<html></html>")
        spec = {"type": "card_list", "fetch": "playwright", "card": "div.x",
                "wait_until": "domcontentloaded"}
        lt.fetch({"id": "t", "url": "https://site.test/i", "expected_hostname": "site.test",
                  "listing_template": spec})
        assert seen["wait_until"] == "domcontentloaded"

    def test_the_default_is_unchanged(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(lt, "_playwright_html",
                            lambda url, wait_selector=None, wait_until="networkidle", **k:
                            seen.update(wait_until=wait_until) or "<html></html>")
        spec = {"type": "card_list", "fetch": "playwright", "card": "div.x"}
        lt.fetch({"id": "t", "url": "https://site.test/i", "expected_hostname": "site.test",
                  "listing_template": spec})
        assert seen["wait_until"] == "networkidle"


class TestDeadAnchors:
    """An anchor that goes nowhere is not an article.

    oaktree puts external links in a data-link attribute and leaves href="#".
    The template turned that into urljoin(listing, "#") -- the listing page
    itself -- which passes the host check and becomes a row pointing at the
    index (caught by the A/B against the hand-written fetcher, 2026-09-25,
    where it appeared as a CNBC item whose url was /insights).
    """
    SRC = {"id": "t", "url": "https://site.test/insights", "expected_hostname": "site.test",
           "max_articles": 10}
    SPEC = {"type": "card_list", "fetch": "requests", "card": "div.card", "link": "a[href]",
            "title": "h3"}

    @pytest.mark.parametrize("href", ["#", "", "javascript:void(0)", "#section"])
    def test_an_anchor_that_goes_nowhere_is_skipped(self, href):
        html = f'<div class="card"><a href="{href}"><h3>Dead</h3></a></div>'
        assert lt.parse_cards(html, self.SRC, self.SPEC) == []

    def test_dead_anchors_are_skipped_without_a_host_check(self):
        """The host check is conditional, so it cannot be the only guard."""
        src = dict(self.SRC, expected_hostname="")
        html = '<div class="card"><a href="javascript:void(0)"><h3>Dead</h3></a></div>'
        assert lt.parse_cards(html, src, self.SPEC) == []

    def test_a_real_link_still_passes(self):
        html = '<div class="card"><a href="/insights/x"><h3>Real</h3></a></div>'
        assert len(lt.parse_cards(html, self.SRC, self.SPEC)) == 1
