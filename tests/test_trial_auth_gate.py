"""Tests for auth-gate detection and cookie-retry fetching in GMIA Trial Manager.

Root cause (2026-06-02): Capital Group articles sit behind an Akamai
"Content Gate" — cookie-less requests get 302→200 redirected to
/advisor/public/authentication-0.htm. The old stateless httpx.get() always
landed on the gate, and _is_likely_js_only() misread the gate as a JS shell,
flipping a sampler failure into a (wrong) LOW QUALITY verdict.
"""

import importlib.util
import json
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


# ── _extract_article_text / _is_likely_js_only 接线 ──────────────────────────

def test_extract_article_text_passes_cookie_gate(monkeypatch):
    """有 cookie 软门禁的文章：重试后应提取到正文（修复前返回 None）。"""
    monkeypatch.setattr(tm.httpx, "Client", _CookieGateClient)
    text = tm._extract_article_text(ARTICLE_URL)
    assert text is not None
    assert "real investment research" in text


def test_extract_article_text_returns_none_on_hard_gate(monkeypatch):
    """硬登录墙：重试后仍在 gate 上 → 返回 None（而不是把 gate 页面当正文）。"""
    monkeypatch.setattr(tm.httpx, "Client", _HardGateClient)
    assert tm._extract_article_text(ARTICLE_URL) is None


def test_is_likely_js_only_false_for_auth_gate(monkeypatch):
    """核心 bug 修复：auth gate（大 HTML + 无正文）不得再被判为 js_only。"""
    monkeypatch.setattr(tm.httpx, "Client", _HardGateClient)
    assert tm._is_likely_js_only(ARTICLE_URL) is False


def test_is_likely_js_only_still_true_for_real_js_shell(monkeypatch):
    """回归保护：真正的 JS shell（200 + 大 HTML + 无正文 + 无重定向）仍判为 True。"""

    class _JsShellClient(_CookieGateClient):
        def get(self, url):
            self.calls += 1
            # 同 URL 返回（无重定向），大体积 HTML 但没有可提取正文
            return _FakeResponse(
                url=url,
                text="<html><head>" + "<script>app.render();</script>" * 100
                     + "</head><body><div id='root'></div></body></html>",
            )

    monkeypatch.setattr(tm.httpx, "Client", _JsShellClient)
    assert tm._is_likely_js_only(ARTICLE_URL) is True


# ── day-1 fail-fast ───────────────────────────────────────────────────────────

def _make_candidates_file(tmp_path: Path) -> Path:
    f = tmp_path / "fund_candidates.json"
    f.write_text(json.dumps([
        {
            "id": "js-only-fund",
            "name": "JS Only Fund",
            "status": "visitable",
            "quality": "HIGH",
            "fit_score": 0.9,
            "research_url": "https://example.com/insights",
            "homepage_url": "https://example.com",
            "topics": "quant",
        },
    ]))
    return f


@pytest.fixture
def trial_env(tmp_path, monkeypatch):
    monkeypatch.setattr(tm, "CANDIDATES_FILE", _make_candidates_file(tmp_path))
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"sources": []}))
    monkeypatch.setattr(tm, "SOURCES_FILE", sources)
    state = tmp_path / "trial-state.json"
    state.write_text(json.dumps({"active_trials": [], "history": []}))
    monkeypatch.setattr(tm, "TRIAL_STATE_FILE", state)
    monkeypatch.setattr(tm, "ENV_FILE", tmp_path / "nonexistent.env")
    return tmp_path


def test_trial_aborts_day1_without_fetcher_and_zero_articles(trial_env, monkeypatch):
    """无注册 fetcher 且 index 0 文章的候选：day-1 中止，不浪费 7 天 trial。

    2026-05-26 acadian-asset 教训：promotion_notes 已写明需要 Playwright fetcher，
    但 trial 照样裸跑了 7 天 × 0 文章。
    """
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {})
    monkeypatch.setattr(tm, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 0, "date_count": 0,
        "article_urls": [], "error": None, "fetcher_used": False})
    monkeypatch.setattr(
        tm, "sample_article_quality",
        lambda url, trial=None, exclude_urls=None: pytest.fail(
            "quality sampling must not run for an aborted trial"))
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)
    monkeypatch.setattr(tm, "send_daily_summary_email", lambda *a, **k: None)

    tm.cmd_run()

    state = tm.load_state()
    assert state["active_trials"] == []
    assert len(state["history"]) == 1
    assert state["history"][0]["outcome"] == "skipped"
    assert state["history"][0]["skip_reason"] == "needs_fetcher"

    candidates = json.loads(tm.CANDIDATES_FILE.read_text())
    fund = next(c for c in candidates if c["id"] == "js-only-fund")
    assert fund["status"] == "inaccessible"
    assert "fetcher-synthesis" in fund["notes"]


def test_trial_continues_day1_with_fetcher_even_if_zero_articles(trial_env, monkeypatch):
    """有注册 fetcher 的候选即使 day-1 是 0 文章也正常跑 trial（可能是临时没新文章）。"""
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {"js-only-fund": lambda s: []})
    monkeypatch.setattr(tm, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 0, "date_count": 0,
        "article_urls": [], "error": None, "fetcher_used": True})
    monkeypatch.setattr(
        tm, "sample_article_quality",
        lambda url, trial=None, exclude_urls=None: {
            "sampled": 0, "articles": [], "avg_score": 0.0,
            "error": "No article links found on index page"})
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)
    monkeypatch.setattr(tm, "send_daily_summary_email", lambda *a, **k: None)

    tm.cmd_run()

    state = tm.load_state()
    assert len(state["active_trials"]) == 1  # trial 正常进行中
    assert state["history"] == []


# ── 邮件标签 ──────────────────────────────────────────────────────────────────

def test_result_label_inconclusive_not_low_quality():
    icon, text, color = tm._trial_result_label("inconclusive", passed=False)
    assert "LOW QUALITY" not in text
    assert "INCONCLUSIVE" in text
    assert icon == "⚠️"


def test_result_label_fail_quality_unchanged():
    icon, text, color = tm._trial_result_label("fail_quality", passed=False)
    assert text == "FAILED — LOW QUALITY"
    assert icon == "❌"


def test_result_label_fail_quantity_unchanged():
    icon, text, color = tm._trial_result_label("fail_quantity", passed=False)
    assert text == "FAILED — INSUFFICIENT CONTENT"


def test_result_label_pass_unchanged():
    icon, text, color = tm._trial_result_label("pass", passed=True)
    assert text == "READY TO INTEGRATE"
    assert icon == "✅"


def test_result_label_skipped_aborted():
    icon, text, color = tm._trial_result_label("skipped", passed=False)
    assert "ABORTED" in text or "SKIPPED" in text
    assert icon == "⚠️"


def test_summary_bucket_routing():
    assert tm._summary_bucket({"outcome": "pass"}) == "pass"
    assert tm._summary_bucket({"outcome": "inconclusive"}) == "inconclusive"
    assert tm._summary_bucket({"outcome": "fail_quality"}) == "fail_quality"
    assert tm._summary_bucket(
        {"outcome": "skipped", "skip_reason": "needs_fetcher"}) == "aborted_needs_fetcher"
    assert tm._summary_bucket({"outcome": "skipped"}) == "other"
    assert tm._summary_bucket(
        {"outcome": "fail_quantity", "total_articles": 0}) == "inaccessible"
    assert tm._summary_bucket(
        {"outcome": "fail_quantity", "total_articles": 12}) == "low_cadence"


def test_abort_history_entry_has_zero_counts(trial_env, monkeypatch):
    """abort 的 history 条目要有 total_articles=0 / days_with_articles=0 字段
    （否则 cmd_status 显示 '?' ）。"""
    monkeypatch.setattr(tm, "_load_fetchers", lambda: {})
    monkeypatch.setattr(tm, "count_articles_with_fetcher", lambda trial: {
        "accessible": True, "article_count": 0, "date_count": 0,
        "article_urls": [], "error": None, "fetcher_used": False})
    monkeypatch.setattr(
        tm, "sample_article_quality",
        lambda url, trial=None, exclude_urls=None: pytest.fail("must not sample"))
    monkeypatch.setattr(tm, "send_trial_email", lambda *a, **k: None)
    monkeypatch.setattr(tm, "send_daily_summary_email", lambda *a, **k: None)

    tm.cmd_run()

    state = tm.load_state()
    assert state["history"][0]["total_articles"] == 0
    assert state["history"][0]["days_with_articles"] == 0
