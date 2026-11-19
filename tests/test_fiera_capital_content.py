"""Fiera Capital: the body is the linked PDF, and the site rate-limits hard.

Measured 2026-11-19: an insight page carries only a lead (203-1,443 chars);
the piece is the PDF behind "Download PDF" (17,628 / 30,503 chars, opening
with the title). About the sixth request inside a minute gets 429 with
Retry-After 60; asking on gets Retry-After 3600. Stage 2 labels a 429
blocked_by_bot_protection and waits 7 days, so the extractor keeps its own
pace (20s between requests) and waits out one short 429.
"""
from __future__ import annotations

import pytest

import fetch_content as fc

LEAD = "<p>" + "Short lead. " * 20 + "</p>"
PAGE = ("<html><body><main>" + LEAD +
        '<a class="btn download" href="/wp-content/uploads/insights/6800/paper.pdf">Download PDF</a>'
        "</main></body></html>")
ART = {"id": "fieratest0000001", "url": "https://www.fieracapital.com/en/insights/x/",
       "title": "Made in Canada"}


class _Resp:
    def __init__(self, status=200, text=PAGE, headers=None):
        self.status_code, self.text, self.headers = status, text, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def clock(monkeypatch):
    """A fake clock: sleep advances it, nothing really waits."""
    state = {"now": 1000.0, "sleeps": []}

    def sleep(s):
        state["sleeps"].append(s)
        state["now"] += s

    monkeypatch.setattr(fc.time, "monotonic", lambda: state["now"])
    monkeypatch.setattr(fc.time, "sleep", sleep)
    monkeypatch.setattr(fc._fetch_content_fiera_capital, "_last_request", 0.0, raising=False)
    return state


@pytest.fixture
def pages(monkeypatch):
    queue: list[_Resp] = []
    calls: list[str] = []

    def get(url, **kw):
        calls.append(url)
        return queue.pop(0) if queue else _Resp()

    monkeypatch.setattr(fc.requests, "get", get)
    return queue, calls


def _pdf(monkeypatch, text):
    seen = {}

    def fake(link, page_url, title, label, cookies=None):
        seen.update(href=link["href"], title=title)
        return text

    monkeypatch.setattr(fc, "_linked_article_pdf", fake)
    return seen


def test_the_pdf_is_the_body(monkeypatch, clock, pages):
    seen = _pdf(monkeypatch, "Made in Canada " + "body " * 2000)
    with fc.isolated_content_dir():
        path, status = fc._fetch_content_fiera_capital(ART)
        body = path.read_text(encoding="utf-8")
    assert status == "ok" and body.startswith("Made in Canada") and len(body) > 5000
    assert seen == {"href": "/wp-content/uploads/insights/6800/paper.pdf", "title": "Made in Canada"}


def test_a_lead_alone_is_refused(monkeypatch, clock, pages):
    _pdf(monkeypatch, "")   # PDF missing or not this article
    with fc.isolated_content_dir():
        assert fc._fetch_content_fiera_capital(ART) is None


def test_a_long_page_is_kept_when_the_pdf_is_shorter(monkeypatch, clock, pages):
    queue, _ = pages
    queue.append(_Resp(text="<main><p>" + "Long page text. " * 100 + "</p>"
                       '<a class="btn download" href="/a.pdf">x</a></main>'))
    _pdf(monkeypatch, "Made in Canada short pdf")
    with fc.isolated_content_dir():
        path, _ = fc._fetch_content_fiera_capital(ART)
        assert path.read_text(encoding="utf-8").startswith("Long page text.")


def test_requests_are_twenty_seconds_apart(monkeypatch, clock, pages):
    _pdf(monkeypatch, "Made in Canada " + "body " * 200)
    with fc.isolated_content_dir():
        fc._fetch_content_fiera_capital(ART)
        fc._fetch_content_fiera_capital(dict(ART, id="fieratest0000002"))
    # first page: no wait; its PDF and every later request: a 20s gap
    assert clock["sleeps"] == [20.0, 20.0, 20.0]


def test_a_short_429_is_waited_out_once(monkeypatch, clock, pages):
    queue, calls = pages
    queue.extend([_Resp(429, headers={"Retry-After": "60"}), _Resp()])
    _pdf(monkeypatch, "Made in Canada " + "body " * 200)
    with fc.isolated_content_dir():
        assert fc._fetch_content_fiera_capital(ART)[1] == "ok"
    assert len(calls) == 2 and 61 in clock["sleeps"]


def test_an_hour_long_429_is_not_waited_for(monkeypatch, clock, pages):
    queue, calls = pages
    queue.append(_Resp(429, headers={"Retry-After": "3600"}))
    _pdf(monkeypatch, "unused")
    with fc.isolated_content_dir():
        assert fc._fetch_content_fiera_capital(ART) is None
    assert len(calls) == 1 and max(clock["sleeps"], default=0) < 3600


def test_the_source_is_wired():
    assert fc.CONTENT_FETCHERS["fiera-capital"] is fc._fetch_content_fiera_capital
