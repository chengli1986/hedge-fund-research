# GMIA Trial Manager Auth-Gate Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 trial manager 把 cookie 软门禁误判为 js_only/LOW QUALITY 的 bug，加上 trial 前置检查与 inconclusive 标签，然后重置 capital-group 重新 trial。

**Architecture:** 所有改动集中在 `gmia-trial-manager.py` 单文件（仓库惯例：trial manager 自包含）。新增 3 个纯函数 helper（`_is_auth_gate_url` / `_get_with_auth_retry` / `_trial_result_label` + `_summary_bucket`），现有函数接线改造。新测试放 `tests/test_trial_auth_gate.py`（复用 `test_unit_trial_quality.py` 的 importlib + fixture 模式），1 个现有测试断言更新。

**Tech Stack:** Python 3.12, httpx, BeautifulSoup, pytest（469 个现有测试，标记 `not live and not nightly`）

**背景（2026-06-02 根因调查结论）:**
- Capital Group 文章页被 Akamai Content Gate 302 重定向到 `/advisor/public/authentication-0.htm`（最终状态 200）→ 无状态 httpx 单次请求永远撞墙 → `_is_likely_js_only()` 误判为 js_only → 7 天采样全失败 → 误报 "FAILED — LOW QUALITY"
- 已实测验证：httpx.Client 持久 cookie + 重试一次 → 第 2 次请求拿到完整文章（173KB，`.cmp-text` 提取 7,576 字符）
- Acadian 没有注册 fetcher 却进入 trial → index 是 Sitecore JS 渲染 → 0 文章 × 7 天注定失败（本计划的 fail-fast 防止未来再犯；Acadian 本身等 6-07 周日 synthesis 自愈）

---

## 文件结构

| 文件 | 操作 | 职责 |
|---|---|---|
| `gmia-trial-manager.py` | Modify | 全部代码改动（helpers + 接线） |
| `tests/test_trial_auth_gate.py` | Create | auth-gate 识别 / cookie 重试 / fail-fast / inconclusive / 邮件标签 测试 |
| `tests/test_unit_trial_quality.py` | Modify | `test_trial_fails_without_quality_scores` 断言 fail_quality → inconclusive |
| `config/trial-state.json` + `config/fund_candidates.json` | Modify (Task 6 脚本) | capital-group 状态重置 |
| `docs/superpowers/plans/2026-06-02-trial-auth-gate-fix.md` | Create | 本计划 |

---

### Task 1: auth-gate 识别 + cookie 重试 helper

**Files:**
- Modify: `gmia-trial-manager.py`（HEADERS 常量后 ~line 89 加常量；`_extract_article_text` 前 ~line 317 加 2 个 helper）
- Create: `tests/test_trial_auth_gate.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_trial_auth_gate.py`：

```python
"""Tests for auth-gate detection and cookie-retry fetching in GMIA Trial Manager.

Root cause (2026-06-02): Capital Group articles sit behind an Akamai
"Content Gate" — cookie-less requests get 302→200 redirected to
/advisor/public/authentication-0.htm. The old stateless httpx.get() always
landed on the gate, and _is_likely_js_only() misread the gate as a JS shell,
flipping a sampler failure into a (wrong) LOW QUALITY verdict.
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Import the module with dashes in name
_spec = importlib.util.spec_from_file_location(
    "trial_manager",
    str(Path(__file__).resolve().parent.parent / "gmia-trial-manager.py"),
)
tm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tm)

BJT = timezone(timedelta(hours=8))

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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_trial_auth_gate.py -v 2>&1 | tail -20
```

预期：8 个测试全部 ERROR/FAIL，报 `AttributeError: module 'trial_manager' has no attribute '_is_auth_gate_url'`

- [ ] **Step 3: 实现**

在 `gmia-trial-manager.py` HEADERS 常量定义之后（~line 89，空行后）插入：

```python
# Markers that identify an authentication/login gate page in a redirect target.
# Kept conservative: "auth" alone would false-positive on /author/ pages.
# Real-world case 2026-06-02: Capital Group's Akamai "Content Gate" edge worker
# 302-redirects cookie-less requests to /advisor/public/authentication-0.htm.
AUTH_GATE_MARKERS = (
    "authentication", "login", "logon", "signin", "sign-in",
    "/auth/", "/sso/", "/gateway/",
)
```

在 `_extract_article_text` 函数定义之前（quality sampling 区块内，~line 317）插入两个 helper：

```python
def _is_auth_gate_url(requested_url: str, final_url: str) -> bool:
    """Return True when a request was redirected to an auth/login gate page.

    A gate redirect = the final path differs from the requested path AND the
    final path contains an auth marker. Same-path responses and ordinary
    canonical-slug redirects are not gates.
    """
    req_path = urlparse(requested_url).path.rstrip("/").lower()
    fin_path = urlparse(final_url).path.rstrip("/").lower()
    if req_path == fin_path:
        return False
    return any(marker in fin_path for marker in AUTH_GATE_MARKERS)


def _get_with_auth_retry(url: str, timeout: int = 20):
    """GET a URL with cookie persistence and a single auth-gate retry.

    Sites behind a cookie content-gate (e.g. Capital Group's Akamai "Content
    Gate" edge worker) 302-redirect cookie-less first visits to an auth page
    that *sets* session cookies; a second request with those cookies passes
    through to the real content. A stateless httpx.get() therefore always
    lands on the gate — this helper detects that and retries once inside the
    same cookie session.

    Returns the final httpx.Response (which may still be the gate page for a
    hard login wall — callers must check with _is_auth_gate_url), or None on
    network error.
    """
    try:
        with httpx.Client(headers=HEADERS, timeout=timeout,
                          follow_redirects=True) as client:
            resp = client.get(url)
            if _is_auth_gate_url(url, str(resp.url)) and client.cookies:
                resp = client.get(url)
            return resp
    except Exception:
        return None
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_trial_auth_gate.py -v 2>&1 | tail -15
```

预期：8 passed

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research && git add gmia-trial-manager.py tests/test_trial_auth_gate.py && git commit -m "$(cat <<'EOF'
feat(trial): add auth-gate detection + cookie-retry fetch helpers

Capital Group's Akamai Content Gate 302-redirects cookie-less requests to
authentication-0.htm (final status 200), which the sampler misread as a JS
shell. _get_with_auth_retry() keeps cookies in an httpx.Client session and
retries once after the gate sets them — verified live 2026-06-02: second
request returns the full 173KB article.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `_extract_article_text` + `_is_likely_js_only` 接入 helper

**Files:**
- Modify: `gmia-trial-manager.py:317-392`（两个现有函数的开头部分）
- Modify: `tests/test_trial_auth_gate.py`（追加测试）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_trial_auth_gate.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_trial_auth_gate.py -v 2>&1 | tail -15
```

预期：前 8 个 pass；新 4 个中 `test_extract_article_text_passes_cookie_gate` 和 `test_is_likely_js_only_false_for_auth_gate` FAIL（旧代码用 `httpx.get`，monkeypatch 的 `httpx.Client` 不生效 → 真实网络请求或断言失败）。
注意：旧代码走 `httpx.get` 会发真实网络请求到 capitalgroup.com — 如果离线/被墙会 error，这同样算"失败"，目的达到即可。

- [ ] **Step 3: 实现**

修改 `_extract_article_text`（line ~329-333），把开头的：

```python
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
```

替换为：

```python
    try:
        resp = _get_with_auth_retry(url, timeout=timeout)
        if resp is None or resp.status_code != 200:
            return None
        if _is_auth_gate_url(url, str(resp.url)):
            return None  # still stuck on a login gate after cookie retry
        soup = BeautifulSoup(resp.text, "html.parser")
```

修改 `_is_likely_js_only`（line ~384-392），把：

```python
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=timeout, follow_redirects=True)
        if resp.status_code != 200:
            return False
        if len(resp.text) < 1000:
            return False
        return _extract_article_text(url, timeout=timeout) is None
    except Exception:
        return False
```

替换为：

```python
    try:
        resp = _get_with_auth_retry(url, timeout=timeout)
        if resp is None or resp.status_code != 200:
            return False
        if _is_auth_gate_url(url, str(resp.url)):
            return False  # auth gate ≠ JS shell — do not misclassify
        if len(resp.text) < 1000:
            return False
        return _extract_article_text(url, timeout=timeout) is None
    except Exception:
        return False
```

同时更新 `_is_likely_js_only` 的 docstring 第一段，在 "Three non-JS failure modes" 列表中追加一行：

```
    - HTTP 200 redirect to an auth/login gate (cookie content-gate) → check
      _is_auth_gate_url, return False
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_trial_auth_gate.py -v 2>&1 | tail -15
```

预期：12 passed

- [ ] **Step 5: 跑现有质量采样测试确认无回归**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_trial_quality.py -v 2>&1 | tail -20
```

预期：全部 pass（现有测试 mock 的是 `tm._extract_article_text` 整函数或 `httpx.get`，不受内部实现变化影响；若有 fail 检查是否 mock 了 `httpx.get` → 改为 mock `tm._get_with_auth_retry`）

- [ ] **Step 6: Commit**

```bash
cd ~/hedge-fund-research && git add gmia-trial-manager.py tests/test_trial_auth_gate.py && git commit -m "$(cat <<'EOF'
fix(trial): article sampling now survives cookie content-gates

_extract_article_text and _is_likely_js_only use _get_with_auth_retry
(persistent-cookie session + one retry) and explicitly classify auth-gate
redirects as not-extractable / not-js-only. Fixes Capital Group trial
2026-05-26→06-02 scoring 0.00 with all 9 articles misread as js_only.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: 0 个采样 → outcome = "inconclusive"（不再叫 fail_quality）

**Files:**
- Modify: `gmia-trial-manager.py:1199-1205`（cmd_run 的 outcome 赋值）
- Modify: `tests/test_unit_trial_quality.py:163-165`（现有断言更新）

- [ ] **Step 1: 更新现有测试断言（这就是失败测试）**

`tests/test_unit_trial_quality.py` 中 `test_trial_fails_without_quality_scores` 的断言部分，把：

```python
    state = tm.load_state()
    assert len(state["active_trials"]) == 0
    assert len(state["history"]) == 1
    assert state["history"][0]["outcome"] == "fail_quality"
```

替换为：

```python
    state = tm.load_state()
    assert len(state["active_trials"]) == 0
    assert len(state["history"]) == 1
    # 0 个样本 = 采样失败，不是质量判定 — 不能叫 fail_quality（2026-06-02
    # capital-group 教训：采样器 bug 被误报成 "FAILED — LOW QUALITY"）
    assert state["history"][0]["outcome"] == "inconclusive"
```

同时把测试的 docstring 从 `"""A trial with enough articles but zero quality scores must NOT pass."""` 改为：

```python
    """A trial with enough articles but zero quality scores must NOT pass —
    and must be labeled inconclusive (sampling failure), not fail_quality."""
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_trial_quality.py::test_trial_fails_without_quality_scores -v 2>&1 | tail -10
```

预期：FAIL，`assert 'fail_quality' == 'inconclusive'`

- [ ] **Step 3: 实现**

`gmia-trial-manager.py` cmd_run 中（~line 1199），把：

```python
            if not quantity_ok:
                active["outcome"] = "fail_quantity"
            elif not quality_ok:
                active["outcome"] = "fail_quality"
            else:
                active["outcome"] = "pass"
```

替换为：

```python
            if not quantity_ok:
                active["outcome"] = "fail_quantity"
            elif not all_scores:
                # Articles were detected on enough days but not a single one
                # could be sampled — that is a sampling failure, not a quality
                # verdict. Do not label it fail_quality / LOW QUALITY.
                active["outcome"] = "inconclusive"
            elif not quality_ok:
                active["outcome"] = "fail_quality"
            else:
                active["outcome"] = "pass"
```

（候选状态处置代码 ~line 1233 已有 `elif not all_scores: → watchlist + "Trial inconclusive"` 分支，无需改动 — outcome 与 status 现在语义一致。）

- [ ] **Step 4: 运行测试确认通过 + 相邻测试无回归**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_trial_quality.py -v 2>&1 | tail -20
```

预期：全部 pass（特别确认 `test_trial_fails_with_low_quality_scores` 仍 pass — 有真实低分时仍是 fail_quality）

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research && git add gmia-trial-manager.py tests/test_unit_trial_quality.py && git commit -m "$(cat <<'EOF'
fix(trial): label zero-sample trials inconclusive, not fail_quality

A trial where every sampling attempt errored never had its quality assessed.
Calling it "FAILED - LOW QUALITY" is a misdiagnosis that buries the real
problem (sampler/extraction failure) and wrongly taints the source.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: trial 启动 day-1 fail-fast（无 fetcher + 0 文章 → 直接转 synthesis）

**Files:**
- Modify: `gmia-trial-manager.py:1300-1320`（cmd_run Step 2 启动新 trial 处）
- Modify: `tests/test_trial_auth_gate.py`（追加测试）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_trial_auth_gate.py`（文件顶部已 import json/datetime；需补充 fixture）：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_trial_auth_gate.py -k "day1" -v 2>&1 | tail -15
```

预期：`test_trial_aborts_day1...` FAIL（active_trials == 1 而非 0）；`test_trial_continues_day1...` PASS（现状即如此）

- [ ] **Step 3: 实现**

`gmia-trial-manager.py` cmd_run Step 2 中（~line 1310），在 Day-1 check 之后、Day-1 quality sampling 之前：

```python
            url = new_trial["research_url"]
            result = count_articles_with_fetcher(new_trial)
            new_trial["daily_checks"][today] = result
            save_state(state)
            if result["accessible"]:
                print(f"[trial]   Day 1: {result['article_count']} articles detected")
            else:
                print(f"[trial]   Day 1: unreachable — {result['error']}")
```

之后插入：

```python
            # Fail-fast: index reachable but zero articles AND no registered
            # fetcher → static HTML has no article links (JS-rendered index).
            # A 7-day trial cannot succeed; route to fetcher-synthesis now
            # instead of wasting the window (lesson: acadian-asset 5-26→6-02
            # burned 7 days at 0 articles/day).
            has_fetcher = new_trial["id"] in _load_fetchers()
            if (not has_fetcher
                    and result.get("accessible")
                    and result.get("article_count", 0) == 0):
                print(f"[trial]   Aborting — no article links in static HTML and "
                      f"no registered fetcher; routing to fetcher-synthesis")
                new_trial["auto_decided"] = True
                new_trial["end_date"] = today
                new_trial["outcome"] = "skipped"
                new_trial["skip_reason"] = "needs_fetcher"
                candidates = load_candidates()
                for c in candidates:
                    if c["id"] == new_trial["id"]:
                        c["status"] = "inaccessible"
                        c["notes"] = ("Trial aborted on day 1: index reachable but "
                                      "no article links in static HTML — needs a "
                                      "fetcher. Routed to fetcher-synthesis queue.")
                        break
                save_candidates(candidates)
                state.setdefault("history", []).append(new_trial)
                state["active_trials"] = [
                    t for t in state["active_trials"] if t["id"] != new_trial["id"]
                ]
                save_state(state)
                continue
```

（outcome 用 `"skipped"`：`get_trial_queue` 不把 skipped 当作已 trial → synthesis 修好 fetcher 把状态改回 visitable 后即可重新入队，无需清 history。）

- [ ] **Step 4: 运行测试确认通过**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_trial_auth_gate.py tests/test_unit_trial_quality.py -v 2>&1 | tail -25
```

预期：全部 pass

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research && git add gmia-trial-manager.py tests/test_trial_auth_gate.py && git commit -m "$(cat <<'EOF'
feat(trial): abort day-1 when candidate has no fetcher and zero articles

A candidate whose index serves no article links via plain HTTP cannot pass a
7-day trial without a registered fetcher. Abort immediately (outcome=skipped,
skip_reason=needs_fetcher) and route to fetcher-synthesis, so the slot isn't
burned for a week. Lesson: acadian-asset 2026-05-26 -> 06-02, 0 articles x 7 days.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: 邮件标签 — INCONCLUSIVE 单独呈现 + abort 可见性

**Files:**
- Modify: `gmia-trial-manager.py:695-701`（send_trial_email 标签）、`:927-953`（summary 分桶）、`:1045-1104`（chips/sections/subject）
- Modify: `tests/test_trial_auth_gate.py`（追加测试）

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_trial_auth_gate.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_trial_auth_gate.py -k "label or bucket" -v 2>&1 | tail -15
```

预期：AttributeError（两个 helper 不存在）

- [ ] **Step 3: 实现 helper**

在 `gmia-trial-manager.py` 的 `send_trial_email` 函数定义之前（~line 684）插入：

```python
def _trial_result_label(outcome: str, passed: bool) -> tuple[str, str, str]:
    """Return (icon, text, color) for a trial verdict — shared by trial emails."""
    if passed:
        return "✅", "READY TO INTEGRATE", "#1a7f37"
    if outcome == "inconclusive":
        return ("⚠️", "INCONCLUSIVE — ARTICLES DETECTED BUT SAMPLING FAILED",
                "#9a6700")
    if outcome == "fail_quality":
        return "❌", "FAILED — LOW QUALITY", "#cf222e"
    return "❌", "FAILED — INSUFFICIENT CONTENT", "#cf222e"


def _summary_bucket(t: dict) -> str:
    """Classify a decided trial into a daily-summary-email section bucket."""
    outcome = t.get("outcome", "")
    if outcome == "pass":
        return "pass"
    if outcome == "inconclusive":
        return "inconclusive"
    if outcome == "fail_quality":
        return "fail_quality"
    if outcome == "skipped" and t.get("skip_reason") == "needs_fetcher":
        return "aborted_needs_fetcher"
    if outcome == "fail_quantity":
        return "inaccessible" if t.get("total_articles", 0) == 0 else "low_cadence"
    return "other"
```

- [ ] **Step 4: send_trial_email 接线**

把 `send_trial_email` 中（~line 695-703）：

```python
    now_bjt = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")
    result_icon = "✅" if passed else "❌"
    outcome = trial.get("outcome", "")
    if outcome == "fail_quality":
        result_text = "FAILED — LOW QUALITY"
    elif outcome.startswith("fail"):
        result_text = "FAILED — INSUFFICIENT CONTENT"
    else:
        result_text = "READY TO INTEGRATE"
    result_color = "#1a7f37" if passed else "#cf222e"
```

替换为：

```python
    now_bjt = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")
    outcome = trial.get("outcome", "")
    result_icon, result_text, result_color = _trial_result_label(outcome, passed)
```

- [ ] **Step 5: send_daily_summary_email 接线**

(a) 行分桶（~line 927-953），把：

```python
    fail_quality_rows = ""
    fail_quantity_inaccessible_rows = ""
    fail_quantity_watchlist_rows = ""
```

替换为：

```python
    fail_quality_rows = ""
    inconclusive_rows = ""
    aborted_rows = ""
    fail_quantity_inaccessible_rows = ""
    fail_quantity_watchlist_rows = ""
```

把分桶 if/elif 链：

```python
        if outcome == "pass":
            pass_rows += row
        elif outcome == "fail_quality":
            fail_quality_rows += row
        elif outcome == "fail_quantity":
            # Distinguish inaccessible (0 articles) vs watchlist (some but not enough days)
            total = t.get("total_articles", 0)
            if total == 0:
                fail_quantity_inaccessible_rows += row
            else:
                fail_quantity_watchlist_rows += row
```

替换为：

```python
        bucket = _summary_bucket(t)
        if bucket == "pass":
            pass_rows += row
        elif bucket == "fail_quality":
            fail_quality_rows += row
        elif bucket == "inconclusive":
            inconclusive_rows += row
        elif bucket == "aborted_needs_fetcher":
            aborted_rows += row
        elif bucket == "inaccessible":
            fail_quantity_inaccessible_rows += row
        elif bucket == "low_cadence":
            fail_quantity_watchlist_rows += row
```

(b) 计数（~line 1045），在 `n_fail_quality = ...` 之后加：

```python
    n_inconclusive = inconclusive_rows.count("<tr>")
    n_aborted = aborted_rows.count("<tr>")
```

(c) chips（~line 1069），在 `{n_fail_quality} watchlist (low quality)` 这个 span 之后追加两个 span（仅当计数 > 0 时显示，与 pending_chip 同样手法）：

在 `summary_chips = (...)` 赋值前加：

```python
    inconclusive_chip = ""
    if n_inconclusive:
        inconclusive_chip = (
            f"<span style='display:inline-block;padding:4px 10px;margin:2px;background:#fff8c5;"
            f"color:#9a6700;border-radius:12px;font-size:12px'>{n_inconclusive} inconclusive</span>"
        )
    aborted_chip = ""
    if n_aborted:
        aborted_chip = (
            f"<span style='display:inline-block;padding:4px 10px;margin:2px;background:#ffebe9;"
            f"color:#cf222e;border-radius:12px;font-size:12px'>{n_aborted} aborted (needs fetcher)</span>"
        )
```

然后在 `summary_chips` 字符串拼接中 `{pending_chip}` 前插入 `{inconclusive_chip}{aborted_chip}`。

(d) sections（~line 1084），在 `{section("⚠️ Watchlist — low quality", ...)}` 之后插入两行：

```python
{section("⚠️ Inconclusive — articles detected but sampling failed", inconclusive_rows, trial_header, "#9a6700")}
{section("🔧 Aborted day 1 — needs fetcher (routed to synthesis)", aborted_rows, trial_header, "#cf222e")}
```

(e) subject（~line 1100），把 `f"{n_fail_quality + n_fail_qty_watch}W / "` 改为 `f"{n_fail_quality + n_fail_qty_watch + n_inconclusive}W / "`。

- [ ] **Step 6: 运行全部测试**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_trial_auth_gate.py tests/test_unit_trial_quality.py -v 2>&1 | tail -25 && python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('tm', '/home/ubuntu/hedge-fund-research/gmia-trial-manager.py')
tm = importlib.util.module_from_spec(spec); spec.loader.exec_module(tm)
print('module loads OK')"
```

预期：全部 pass + module loads OK（确认 f-string/语法无误）

- [ ] **Step 7: Commit**

```bash
cd ~/hedge-fund-research && git add gmia-trial-manager.py tests/test_trial_auth_gate.py && git commit -m "$(cat <<'EOF'
feat(trial): surface inconclusive and aborted trials distinctly in emails

Inconclusive trials (sampling failed) no longer masquerade as
"FAILED - LOW QUALITY"; day-1 aborts (needs fetcher) get their own summary
section instead of disappearing.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: 全量回归 + capital-group 状态重置 + 立即 re-trial

**Files:**
- Modify: `config/trial-state.json`、`config/fund_candidates.json`（通过脚本）

- [ ] **Step 1: 全量回归**

```bash
cd ~/hedge-fund-research && python3 -m pytest 2>&1 | tail -5
```

预期：~482+ passed（469 + 13 新增），0 failed。任何 fail → 停下修复，不许继续。

- [ ] **Step 2: capital-group 状态重置**

```bash
cd ~/hedge-fund-research && python3 << 'PYEOF'
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "tm", "/home/ubuntu/hedge-fund-research/gmia-trial-manager.py")
tm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tm)

# 1) history: fail_quality → skipped（保留审计，解除 re-trial 锁定）
state = tm.load_state()
changed = 0
for h in state["history"]:
    if h["id"] == "capital-group" and h.get("outcome") == "fail_quality":
        h["outcome"] = "skipped"
        h["skip_reason"] = "sampler_cookie_gate_bug"
        h["skip_note"] = ("2026-06-02: original fail_quality was a sampler bug "
                          "(Akamai cookie gate misread as js_only), not a content "
                          "verdict. Sampler fixed; re-trial approved by user.")
        changed += 1
tm.save_state(state)
print(f"history entries updated: {changed}")

# 2) candidate: watchlist → visitable
cands = tm.load_candidates()
for c in cands:
    if c["id"] == "capital-group":
        print(f"before: status={c['status']!r}")
        c["status"] = "visitable"
        c["notes"] = ("Re-trial approved 2026-06-02: previous trial scored 0.00 "
                      "because the sampler hit the Akamai cookie gate on every "
                      "article (sampler bug, fixed). Not a content-quality verdict.")
        print(f"after:  status={c['status']!r}")
tm.save_candidates(cands)
print("candidates saved")
PYEOF
```

预期输出：`history entries updated: 1`、`before: status='watchlist'`、`after: status='visitable'`

- [ ] **Step 3: 手动触发 trial manager（启动 capital-group re-trial + Day-1 采样）**

```bash
cd ~/hedge-fund-research && timeout 600 python3 gmia-trial-manager.py run 2>&1 | tail -40
```

预期输出包含：
- `[trial] Already checked Matthews Asia today, skipping`（现有 active trial 幂等）
- `[trial] Starting trial for Capital Group (Capital Ideas) ...`
- `[trial]   Day 1: 9 articles detected`
- `[trial] Quality sampling day 1 ...`
- **`[trial]   Sampled 3 articles, avg quality score: 0.XX`**（> 0，不再是 error！）
- 3 行带分数的文章 notes

**这是修复生效的端到端证据。** 若仍报 "Could not extract text" → 停下，回到 Phase 1 重新调查（可能 EC2 IP 被 Akamai 拉黑等）。

- [ ] **Step 4: 验证 trial 状态**

```bash
cd ~/hedge-fund-research && python3 -c "
import json
state = json.load(open('config/trial-state.json'))
for t in state['active_trials']:
    if t['id'] == 'capital-group':
        print('active trial:', t['id'], 'start', t['start_date'])
        for s in t.get('quality_samples', []):
            print('  day', s['day'], 'sampled', s['sampled'], 'avg', s['avg_score'], 'err', s['error'])
"
```

预期：`sampled 3, avg > 0, err None`

- [ ] **Step 5: Commit 状态变更**

```bash
cd ~/hedge-fund-research && git status --short config/ && git add config/trial-state.json config/fund_candidates.json && git commit -m "$(cat <<'EOF'
trial(capital-group): reset for re-trial after sampler cookie-gate fix

Previous fail_quality (2026-05-26 -> 06-02) re-labeled skipped: all 9 articles
were misread as js_only by the stateless sampler hitting the Akamai content
gate. Re-trial started 2026-06-02 with the fixed cookie-retry sampler.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

注意：**只 add config/ 下这两个文件**，不要 `git add -A`（README.md 有其他会话的未提交改动）。

- [ ] **Step 6: Push**

```bash
cd ~/hedge-fund-research && git log origin/main..HEAD --oneline && git push origin main
```

预期：6 个新 commit push 成功

---

### Task 7: Post-commit checklist

**Files:**
- Modify: `~/.claude/projects/-home-ubuntu/memory/hedge-fund-research.md`
- Modify: `~/.claude/projects/-home-ubuntu/memory/MEMORY.md`（hedge-fund-research 行）
- Modify: `~/.claude/projects/-home-ubuntu/memory/daily/2026-06-02.md`（Development 段）
- Check: `~/hedge-fund-research/README.md`（test count 等）

- [ ] **Step 1: 更新项目记忆文件**

`memory/hedge-fund-research.md`：最新 commit 链、新测试数、auth-gate 修复说明、capital-group re-trial 中（预计 2026-06-09 出结果）、acadian 等 6-07 synthesis。

- [ ] **Step 2: 更新 MEMORY.md 索引行**

hedge-fund-research 条目：测试数 469 → 实际数、最新 commit hash、一句话描述 auth-gate fix。

- [ ] **Step 3: 更新今日 daily log**

`## Development (~HH:MM BJT, session 6a9cedcc)` 段：6 commits 摘要 + re-trial 启动证据。

- [ ] **Step 4: README 检查**

```bash
cd ~/hedge-fund-research && grep -n "469\|tests\|trial" README.md | head -20 && git status --short README.md
```

README.md 已有其他会话（5-30 updateMD）的未提交改动（453→469 test count）。处理：把 test count 更新为本次的最终数字，单独 commit README.md 并在 commit message 中注明包含 5-30 的同步 + 本次更新。

```bash
cd ~/hedge-fund-research && git add README.md && git commit -m "$(cat <<'EOF'
docs(readme): sync test count + trial manager auth-gate fix notes

Includes the uncommitted 2026-05-30 updateMD sync (453 -> 469) plus this
session's new trial auth-gate tests.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)" && git push origin main
```

---

## Self-Review 结果

- **Spec 覆盖**：✅ (1) cookie 会话+auth-gate → Task 1-2；(2) fetcher 前置检查 → Task 4；(3) inconclusive 标签 → Task 3+5；(4) capital-group 重置 re-trial → Task 6
- **占位符扫描**：✅ 无 TBD/TODO；所有步骤含完整代码
- **类型一致性**：✅ `_trial_result_label(outcome, passed) -> tuple[str, str, str]`、`_summary_bucket(t: dict) -> str`、`_get_with_auth_retry(url, timeout) -> Response | None` 在测试与实现中签名一致
- **已知风险**：(a) Task 6 Step 3 依赖真实网络 + ANTHROPIC_API_KEY（Haiku 采样）；若 Akamai 对 EC2 IP 行为与本地 curl 测试不同，Step 3 会暴露——这正是端到端验证的目的；(b) 现有测试若有直接 mock `httpx.get` 的（Task 2 Step 5 会暴露），改 mock `_get_with_auth_retry`
