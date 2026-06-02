"""Tests for auth-gate detection and cookie-retry fetching in GMIA Trial Manager.

Root cause (2026-06-02): Capital Group articles sit behind an Akamai
"Content Gate" — cookie-less requests get 302→200 redirected to
/advisor/public/authentication-0.htm. The old stateless httpx.get() always
landed on the gate, and _is_likely_js_only() misread the gate as a JS shell,
flipping a sampler failure into a (wrong) LOW QUALITY verdict.
"""

import importlib.util
from pathlib import Path

import pytest

# Import the module with dashes in name
_spec = importlib.util.spec_from_file_location(
    "trial_manager",
    str(Path(__file__).resolve().parent.parent / "gmia-trial-manager.py"),
)
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)

GATE_URL = ("https://www.capitalgroup.com/advisor/public/authentication-0.htm"
            "?next=/advisor/insights/articles/foo.html")
ARTICLE_URL = "https://www.capitalgroup.com/advisor/insights/articles/foo.html"

REAL_CONTENT_HTML = (
    "<html><body><article>" + "real investment research content. " * 60
    + "</article></body></html>"
)
GATE_HTML = "<html><body>" + "Please sign in to continue. " * 60 + "</body></html>"


# ── fakes ─────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, url: str, status_code: int = 200, text: str = ""):
        self.url = url
        self.status_code = status_code
        self.text = text


class _CookieGateClient:
    """Simulates Akamai content gate: first GET (no cookies) lands on the auth
    page and sets a cookie; subsequent GETs pass through to real content."""

    def __init__(self, *args, **kwargs):
        self.cookies = {}
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        self.calls += 1
        if not self.cookies:
            self.cookies["ADVISOR_CHECK"] = "1"
            return _FakeResponse(url=GATE_URL, text=GATE_HTML)
        return _FakeResponse(url=url, text=REAL_CONTENT_HTML)


class _HardGateClient(_CookieGateClient):
    """A gate that never lets you through (real login wall)."""

    def get(self, url):
        self.calls += 1
        self.cookies["SESSION"] = "1"
        return _FakeResponse(url=GATE_URL, text=GATE_HTML)


class _NoGateClient(_CookieGateClient):
    """No gate at all — content served on first request."""

    def get(self, url):
        self.calls += 1
        return _FakeResponse(url=url, text=REAL_CONTENT_HTML)


# ── _is_auth_gate_url ─────────────────────────────────────────────────────────

def test_auth_gate_detects_capital_group_redirect():
    assert tm._is_auth_gate_url(ARTICLE_URL, GATE_URL) is True


def test_auth_gate_same_url_is_not_gate():
    assert tm._is_auth_gate_url(ARTICLE_URL, ARTICLE_URL) is False


def test_auth_gate_author_page_is_not_gate():
    # "/author/" contains the substring "auth" but is not an auth gate
    assert tm._is_auth_gate_url(
        "https://example.com/insights/foo.html",
        "https://example.com/author/jane-doe/",
    ) is False


def test_auth_gate_canonical_redirect_is_not_gate():
    assert tm._is_auth_gate_url(
        "https://example.com/insights/foo",
        "https://example.com/insights/foo-new-slug",
    ) is False


def test_auth_gate_login_redirect_is_gate():
    assert tm._is_auth_gate_url(
        "https://example.com/research/paper-1",
        "https://example.com/login?return=/research/paper-1",
    ) is True


# ── _get_with_auth_retry ──────────────────────────────────────────────────────

def test_retry_passes_cookie_gate(monkeypatch):
    monkeypatch.setattr(tm.httpx, "Client", _CookieGateClient)
    resp = tm._get_with_auth_retry(ARTICLE_URL)
    assert resp is not None
    assert "real investment research" in resp.text
    assert "authentication" not in str(resp.url)


def test_retry_gives_up_on_hard_gate(monkeypatch):
    monkeypatch.setattr(tm.httpx, "Client", _HardGateClient)
    resp = tm._get_with_auth_retry(ARTICLE_URL)
    # 重试一次后仍在 gate 上 → 返回 gate response（调用方负责判断）
    assert resp is not None
    assert tm._is_auth_gate_url(ARTICLE_URL, str(resp.url)) is True


def test_no_gate_only_fetches_once(monkeypatch):
    holder = {}

    class _Recording(_NoGateClient):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            holder["client"] = self

    monkeypatch.setattr(tm.httpx, "Client", _Recording)
    resp = tm._get_with_auth_retry(ARTICLE_URL)
    assert resp is not None
    assert holder["client"].calls == 1
