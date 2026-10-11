"""fetch_principal_am must say why it found nothing (code review 2026-10-10).

The Coveo path had three silent exits to an empty list: no bearer token
scavenged, a non-OK search response (the JS returned null), and any exception
from page.evaluate()/json.loads() (``except Exception: raw_results = []``).
Each recorded a zero for Principal with no reason in the log. They now raise;
fetch_source logs the message and still records the zero metric.
"""
import json

import playwright.sync_api as pw
import pytest

import fetch_articles as fa

SOURCE = {"id": "principal-am", "url": "https://www.principalam.com/us/insights",
          "expected_hostname": "principalam.com", "max_articles": 10}

GOOD = json.dumps({"results": [{
    "clickUri": "https://www.principalam.com/us/insights/macro/some-piece",
    "title": "Some piece", "raw": {"publishdate": 1790000000000}}]})


class _Req:
    url = "https://principalfinancialgroupqpnzn1vj.org.coveo.com/rest/search/v2/querySuggest"
    headers = {"authorization": "Bearer t"}


def _install(monkeypatch, *, token=True, evaluate=None):
    class Page:
        def on(self, _event, cb):
            self._cb = cb

        def goto(self, *a, **k):
            if token:
                self._cb(_Req())

        def wait_for_load_state(self, *a, **k):
            pass

        def wait_for_timeout(self, *a, **k):
            pass

        def evaluate(self, *a, **k):
            return evaluate()

    class Browser:
        def new_context(self, **k):
            return type("Ctx", (), {"new_page": lambda self: Page()})()

        def close(self):
            pass

    class PW:
        chromium = type("C", (), {"launch": lambda self, **k: Browser()})()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(pw, "sync_playwright", lambda: PW())


def test_a_working_search_returns_articles(monkeypatch):
    _install(monkeypatch, evaluate=lambda: {"status": 200, "text": GOOD})
    out = fa.fetch_principal_am(SOURCE)
    assert [a["title"] for a in out] == ["Some piece"]


def test_an_empty_index_is_not_an_error(monkeypatch):
    _install(monkeypatch, evaluate=lambda: {"status": 200, "text": '{"results": []}'})
    assert fa.fetch_principal_am(SOURCE) == []


def test_no_token_says_so(monkeypatch):
    _install(monkeypatch, token=False, evaluate=lambda: pytest.fail("searched without a token"))
    with pytest.raises(RuntimeError, match="token"):
        fa.fetch_principal_am(SOURCE)


def test_a_non_ok_search_reports_the_status(monkeypatch):
    _install(monkeypatch, evaluate=lambda: {"status": 401, "text": None})
    with pytest.raises(RuntimeError, match="401"):
        fa.fetch_principal_am(SOURCE)


def test_an_evaluate_failure_keeps_its_cause(monkeypatch):
    def boom():
        raise ValueError("Target page, context or browser has been closed")
    _install(monkeypatch, evaluate=boom)
    with pytest.raises(RuntimeError, match="has been closed"):
        fa.fetch_principal_am(SOURCE)


def test_an_unparseable_answer_says_so(monkeypatch):
    _install(monkeypatch, evaluate=lambda: {"status": 200, "text": "<html>maintenance</html>"})
    with pytest.raises(RuntimeError, match="(?i)json"):
        fa.fetch_principal_am(SOURCE)
