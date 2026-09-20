"""A wait strategy that never settles must slow a fetch down, not fail it.

Playwright's wait_until="networkidle" needs 500ms of network silence. Sites
with always-on analytics never give it: the page is rendered seconds in, and
the fetch fails at the 30s timeout anyway. The repo already knows this -- eight
fetchers carry a comment explaining why they had to be moved off networkidle,
one per incident (BlackRock, Capital Group, PIMCO, T. Rowe Price, MSCI,
Wellington, Franklin, and cambridge-associates on 2026-09-20). Eleven listing
fetchers and three content fetchers still ride the default.

Flipping all fourteen would mean changing fourteen sources on the strength of
one, and each needs its own live evidence that the text comes out the same.
The cheaper and safer move is to make the failure degrade: if networkidle times
out, load the page again with domcontentloaded and carry on, and say so in the
log. A source that settles is untouched; a source that never settles stops
failing; and the log names exactly which sources to migrate next, with dates.

Only a TIMEOUT falls back. A DNS failure or a refused connection is not fixed
by waiting differently, and retrying it would just double the wait.
"""
import logging

import pytest

import playwright_nav


class FakePage:
    def __init__(self, fail_first=None):
        self.calls = []
        self.fail_first = fail_first

    def goto(self, url, wait_until=None, timeout=None):
        self.calls.append(wait_until)
        if self.fail_first and len(self.calls) == 1:
            raise self.fail_first


class FakeTimeout(Exception):
    pass


FakeTimeout.__name__ = "TimeoutError"      # what playwright raises


def test_a_page_that_settles_is_loaded_once():
    page = FakePage()
    used = playwright_nav.goto_with_fallback(page, "https://x/a")
    assert page.calls == ["networkidle"] and used == "networkidle"


def test_a_timeout_falls_back_to_domcontentloaded(caplog):
    page = FakePage(fail_first=FakeTimeout("Timeout 30000ms exceeded"))
    with caplog.at_level(logging.WARNING):
        used = playwright_nav.goto_with_fallback(page, "https://x/a")
    assert page.calls == ["networkidle", "domcontentloaded"]
    assert used == "domcontentloaded"
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "https://x/a" in msg and "networkidle" in msg


def test_a_dns_failure_is_not_retried_differently():
    """Waiting differently does not make a host resolve."""
    page = FakePage(fail_first=RuntimeError("net::ERR_NAME_NOT_RESOLVED"))
    with pytest.raises(RuntimeError):
        playwright_nav.goto_with_fallback(page, "https://x/a")
    assert page.calls == ["networkidle"]


def test_a_fallback_that_also_fails_is_raised():
    class AlwaysTimeout(FakePage):
        def goto(self, url, wait_until=None, timeout=None):
            self.calls.append(wait_until)
            raise FakeTimeout("Timeout 30000ms exceeded")

    page = AlwaysTimeout()
    with pytest.raises(Exception):
        playwright_nav.goto_with_fallback(page, "https://x/a")
    assert page.calls == ["networkidle", "domcontentloaded"]


def test_a_strategy_with_nothing_to_fall_back_to_is_not_retried():
    page = FakePage(fail_first=FakeTimeout("Timeout 30000ms exceeded"))
    with pytest.raises(Exception):
        playwright_nav.goto_with_fallback(page, "https://x/a", wait_until="domcontentloaded")
    assert page.calls == ["domcontentloaded"]


class TestEveryNetworkidleCallerUsesIt:
    """A helper nothing calls is not a safety net."""
    def test_the_shared_listing_helper_uses_it(self):
        import ast, inspect, textwrap
        import fetch_articles
        src = ast.unparse(ast.parse(textwrap.dedent(
            inspect.getsource(fetch_articles._get_playwright_page))))
        assert "goto_with_fallback" in src
        assert "page.goto(" not in src

    @pytest.mark.parametrize("name", ["_fetch_content_oaktree", "_fetch_content_aqr",
                                      "_fetch_content_aberdeen"])
    def test_the_content_fetchers_on_networkidle_use_it(self, name):
        import ast, inspect, textwrap
        import fetch_content
        src = ast.unparse(ast.parse(textwrap.dedent(inspect.getsource(getattr(fetch_content, name)))))
        assert "goto_with_fallback" in src, f"{name} still calls page.goto directly"
        assert "page.goto(" not in src
