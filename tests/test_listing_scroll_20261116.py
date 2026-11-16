"""Listings that render only after a scroll (troweprice, 2026-11-16).

T. Rowe Price moved its insights tiles behind lazy loading: the page answers
200, the grid is there, and the article tiles appear only once the page is
scrolled. Measured 2026-11-16 with the fetcher's own user agent and viewport:
no scroll, 0 titles; 3 x 1000px wheel steps, 6 titles in 3 runs of 3. Both the
listing template and the hand-written fetch_troweprice read the page without
scrolling and returned 0 on two consecutive daily runs, so falling back to the
hand-written fetcher was no way out. Wheeling at once after domcontentloaded,
500ms apart, still found 0; 1s apart, stopping once the titles show, found 6
after 4 steps in 4 runs of 4.

The knob is off by default: the other sources do not scroll and must not
start.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import fetch_articles as fa
import listing_templates as lt

REPO = Path(__file__).resolve().parent.parent


class _FakePage:
    def __init__(self, log: list, appears_after: int | None = None):
        self.log = log
        self.mouse = self
        self.appears_after = appears_after

    def query_selector(self, sel):
        wheels = sum(1 for e in self.log if e[0] == "wheel")
        return object() if self.appears_after is not None and wheels >= self.appears_after else None

    def wheel(self, dx, dy):
        self.log.append(("wheel", dy))

    def wait_for_selector(self, sel, timeout=None):
        self.log.append(("wait_for_selector", sel))

    def wait_for_timeout(self, ms):
        self.log.append(("wait", ms))

    def content(self):
        return "<html></html>"


class _FakeBrowser:
    def __init__(self, log):
        self.log = log

    def new_context(self, **kw):
        return self

    def add_init_script(self, s):
        pass

    def new_page(self):
        return _FakePage(self.log, getattr(self.log, "appears_after", None))

    def close(self):
        pass


class _FakePW:
    def __init__(self, log):
        self.chromium = self
        self.log = log

    def launch(self, **kw):
        return _FakeBrowser(self.log)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def page_log(monkeypatch):
    import playwright.sync_api as pw

    class _Log(list):
        appears_after = None

    log = _Log()
    monkeypatch.setattr(pw, "sync_playwright", lambda: _FakePW(log))
    monkeypatch.setattr(fa.playwright_nav, "goto_with_fallback",
                        lambda page, url, **k: log.append(("goto", url)))
    return log


class TestThePageHelper:
    def test_no_scroll_by_default(self, page_log):
        fa._get_playwright_page("https://site.test/i", wait_selector="div.x")
        assert not [e for e in page_log if e[0] == "wheel"]

    def test_it_scrolls_the_number_of_steps_asked_for(self, page_log):
        fa._get_playwright_page("https://site.test/i", wait_selector="div.x", scroll_steps=3)
        assert [e for e in page_log if e[0] == "wheel"] == [("wheel", 1000)] * 3

    def test_it_stops_once_the_selector_matches(self, page_log):
        page_log.appears_after = 4
        fa._get_playwright_page("https://site.test/i", wait_selector="div.x", scroll_steps=10)
        assert sum(1 for e in page_log if e[0] == "wheel") == 4

    def test_the_steps_are_a_second_apart(self, page_log):
        """500ms straight after domcontentloaded found nothing on troweprice."""
        fa._get_playwright_page("https://site.test/i", wait_selector="div.x", scroll_steps=2)
        kinds = [e for e in page_log]
        after_wheel = [kinds[i + 1] for i, e in enumerate(kinds) if e[0] == "wheel"]
        assert after_wheel == [("wait", 1000)] * 2

    def test_the_scroll_comes_after_navigation_and_before_the_selector_wait(self, page_log):
        fa._get_playwright_page("https://site.test/i", wait_selector="div.x", scroll_steps=2)
        kinds = [e[0] for e in page_log]
        assert kinds.index("goto") < kinds.index("wheel")
        assert max(i for i, k in enumerate(kinds) if k == "wheel") < kinds.index("wait_for_selector")


class TestTheTemplateKnob:
    BASE = {"type": "card_list", "fetch": "playwright", "card": "div.x"}

    def _fetch(self, monkeypatch, spec):
        seen = {}
        monkeypatch.setattr(lt, "_playwright_html",
                            lambda url, **k: seen.update(k) or "<html></html>")
        lt.fetch({"id": "t", "url": "https://site.test/i", "expected_hostname": "site.test",
                  "listing_template": spec})
        return seen

    def test_the_spec_can_ask_for_a_scroll(self, monkeypatch):
        assert self._fetch(monkeypatch, dict(self.BASE, scroll_steps=3))["scroll_steps"] == 3

    def test_no_scroll_unless_asked(self, monkeypatch):
        assert self._fetch(monkeypatch, dict(self.BASE)).get("scroll_steps", 0) == 0

    def test_the_helper_receives_it(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(fa, "_get_playwright_page", lambda url, **k: seen.update(k) or "")
        lt._playwright_html("https://site.test/i", scroll_steps=4)
        assert seen["scroll_steps"] == 4

    @pytest.mark.parametrize("bad", [-1, 21, "3", 2.5, True])
    def test_a_bad_value_is_refused(self, bad):
        with pytest.raises(ValueError, match="scroll_steps"):
            lt.validate_spec(dict(self.BASE, scroll_steps=bad))

    def test_scroll_needs_a_browser(self):
        with pytest.raises(ValueError, match="scroll_steps"):
            lt.validate_spec({"type": "card_list", "fetch": "requests", "card": "div.x",
                              "scroll_steps": 2})


class TestTrowePrice:
    def test_the_live_template_scrolls(self):
        src = next(s for s in json.loads((REPO / "config" / "sources.json").read_text())["sources"]
                   if s["id"] == "troweprice")
        lt.validate_spec(src["listing_template"])
        assert src["listing_template"].get("scroll_steps", 0) >= 1

    def test_the_hand_written_fetcher_scrolls_too(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(fa, "_get_playwright_page", lambda url, **k: seen.update(k) or "")
        fa.fetch_troweprice({"id": "troweprice", "url": "https://www.troweprice.com/en/us/insights",
                             "expected_hostname": "www.troweprice.com", "max_articles": 10})
        assert seen.get("scroll_steps", 0) >= 1
