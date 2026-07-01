# Cohen/Principal 正文抓取补齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 cohen-steers 通过 trial 质量门、并让 cohen-steers 与 principal-am 在生产 stage 2 能抓到正文。

**Architecture:** 给 `gmia-trial-manager.py` 的质量采样加"整批 httpx 失败才触发"的 Playwright 正文兜底（重构出共用 `_extract_body_from_soup`）；给 `fetch_content.py` 加两个 per-fund content fetcher（cohen=Playwright，principal=httpx）。两处 Playwright 均复用 `fetch_articles._get_playwright_page`（函数内局部 import，避免顶层循环依赖）。

**Tech Stack:** Python 3.12、requests、httpx、BeautifulSoup、Playwright(Chromium)、pytest。

## Global Constraints

- 守 `pytest tests/ -q` 全绿（当前 541 passed / 15 deselected；新增测试计入总数）。
- 正常源（httpx 首轮就抓到正文）**不得**引入任何 Playwright 开销。
- content fetcher 契约：返回 `(content_path, "ok")` 成功，`None` 失败（`main()` 据此记 `content_status`）。
- 复用既有 helper：`_normalize_html` / `_check_min_content_length` / `_atomic_write` / `CONTENT_DIR` / `HEADERS`（`fetch_content.py`）；`_get_playwright_page`（`fetch_articles.py`）。
- 提交只 `git add` 涉及的具体文件，绝不 `-A`（多会话/cron 并发写 `config/*.json` 的规矩）。
- trial-manager 测试用 `importlib` 载入模块（文件名带连字符），见 `tests/test_unit_trial_quality.py` 现有写法。

**细化说明（相对 spec）：** spec 改动②原写"文件内 `sync_playwright` 模式"；本 plan 改为复用 `fetch_articles._get_playwright_page`，理由=与改动①同一 mock 点、DRY、可测性更好。行为等价。

---

### Task 1: 重构 `_extract_article_text` → 抽出 `_extract_body_from_soup(soup)`

纯重构，为 Task 2 的 Playwright 版共用选择器逻辑做准备。行为对既有 httpx 调用**字节级不变**。

**Files:**
- Modify: `gmia-trial-manager.py`（`_extract_article_text`，约 365-420）
- Test: `tests/test_unit_trial_quality.py`

**Interfaces:**
- Produces: `_extract_body_from_soup(soup: BeautifulSoup) -> str | None` — 从已解析的 soup 提取正文（decompose 边栏 + 最长 `<article>` / 组件选择器 / `<main>` / `<body>`，>200 字截 3000）。
- `_extract_article_text(url, timeout=20) -> str | None` 保持签名不变，内部改为 `resp → soup → _extract_body_from_soup(soup)`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_unit_trial_quality.py （文件已 import tm）
def test_extract_body_from_soup_longest_article():
    from bs4 import BeautifulSoup
    html = """
    <html><body>
      <nav>menu junk</nav>
      <article>tiny teaser</article>
      <article><p>%s</p></article>
      <footer>footer junk</footer>
    </body></html>
    """ % ("Real investment analysis. " * 20)
    soup = BeautifulSoup(html, "html.parser")
    text = tm._extract_body_from_soup(soup)
    assert text is not None
    assert "Real investment analysis." in text
    assert "menu junk" not in text and "footer junk" not in text

def test_extract_body_from_soup_returns_none_when_too_short():
    from bs4 import BeautifulSoup
    soup = BeautifulSoup("<html><body><p>hi</p></body></html>", "html.parser")
    assert tm._extract_body_from_soup(soup) is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_unit_trial_quality.py::test_extract_body_from_soup_longest_article -q`
Expected: FAIL — `AttributeError: module 'trial_manager' has no attribute '_extract_body_from_soup'`

- [ ] **Step 3: 实现重构**

在 `gmia-trial-manager.py` 新增 `_extract_body_from_soup`，并把 `_extract_article_text` 内的提取逻辑替换为对它的调用：

```python
def _extract_body_from_soup(soup: "BeautifulSoup") -> str | None:
    """Extract clean body text from a parsed soup (shared by httpx + Playwright paths)."""
    for tag in soup.select("nav, footer, header, .nav, .footer, .header, "
                           "script, style, aside, .sidebar"):
        tag.decompose()

    articles = soup.find_all("article")
    if articles:
        longest = max(articles, key=lambda a: len(a.get_text(" ", strip=True)))
        text = longest.get_text(" ", strip=True)
        if len(text) > 200:
            return text[:3000]

    for selector in (
        ".cmp-text", "[itemprop='articleBody']", ".rich-text",
        ".article-body", ".article-content", ".entry-content", ".post-content",
    ):
        parts = soup.select(selector)
        if parts:
            joined = "\n\n".join(
                p.get_text(" ", strip=True)
                for p in parts
                if len(p.get_text(" ", strip=True)) > 100
            )
            if len(joined) > 200:
                return joined[:3000]

    content = soup.find("main") or soup.find("body")
    if not content:
        return None
    text = content.get_text(" ", strip=True)
    return text[:3000] if len(text) > 200 else None
```

`_extract_article_text` 改为：

```python
def _extract_article_text(url: str, timeout: int = 20) -> str | None:
    """Fetch a single article page (httpx) and extract clean body text."""
    try:
        resp = _get_with_auth_retry(url, timeout=timeout)
        if resp is None or resp.status_code != 200:
            return None
        if _is_auth_gate_url(url, str(resp.url)):
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        return _extract_body_from_soup(soup)
    except Exception:
        return None
```

- [ ] **Step 4: 运行确认通过 + 无回归**

Run: `python3 -m pytest tests/test_unit_trial_quality.py -q && python3 -m pytest tests/ -q`
Expected: PASS；总数 = 541 + 2。

- [ ] **Step 5: 提交**

```bash
git add gmia-trial-manager.py tests/test_unit_trial_quality.py
git commit -m "refactor(trial): extract _extract_body_from_soup for reuse by Playwright fallback"
```

---

### Task 2: `_extract_article_text_playwright(url)` helper

**Files:**
- Modify: `gmia-trial-manager.py`
- Test: `tests/test_unit_trial_quality.py`

**Interfaces:**
- Consumes: `_extract_body_from_soup`（Task 1）、`fetch_articles._get_playwright_page`（局部 import）。
- Produces: `_extract_article_text_playwright(url: str) -> str | None` — 真浏览器渲染后提取正文（过 CF/JS）。

- [ ] **Step 1: 写失败测试**

```python
def test_extract_article_text_playwright_extracts_body(monkeypatch):
    html = "<html><body><article><p>%s</p></article></body></html>" % ("Deep macro research. " * 20)
    monkeypatch.setattr("fetch_articles._get_playwright_page", lambda url, **kw: html)
    text = tm._extract_article_text_playwright("https://blocked.example/insight")
    assert text is not None and "Deep macro research." in text

def test_extract_article_text_playwright_none_on_error(monkeypatch):
    def boom(url, **kw): raise RuntimeError("CF hard-block")
    monkeypatch.setattr("fetch_articles._get_playwright_page", boom)
    assert tm._extract_article_text_playwright("https://blocked.example/x") is None
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_unit_trial_quality.py::test_extract_article_text_playwright_extracts_body -q`
Expected: FAIL — `has no attribute '_extract_article_text_playwright'`

- [ ] **Step 3: 实现**

```python
def _extract_article_text_playwright(url: str) -> str | None:
    """Render a single article page in a real browser (bypasses CF/JS) and extract body."""
    try:
        from fetch_articles import _get_playwright_page
        html = _get_playwright_page(url, wait_until="domcontentloaded", wait_ms=3000)
        if not html:
            return None
        return _extract_body_from_soup(BeautifulSoup(html, "html.parser"))
    except Exception:
        return None
```

- [ ] **Step 4: 运行确认通过 + 无回归**

Run: `python3 -m pytest tests/test_unit_trial_quality.py -q && python3 -m pytest tests/ -q`
Expected: PASS；总数 = 543 + 2。

- [ ] **Step 5: 提交**

```bash
git add gmia-trial-manager.py tests/test_unit_trial_quality.py
git commit -m "feat(trial): add Playwright body extractor for CF/JS-gated articles"
```

---

### Task 3: `sample_article_quality` 整批失败时触发 Playwright 兜底

**Files:**
- Modify: `gmia-trial-manager.py`（`sample_article_quality`，`if not article_texts:` 分支处，约 602）
- Test: `tests/test_unit_trial_quality.py`

**Interfaces:**
- Consumes: `_extract_article_text_playwright`（Task 2）、`SAMPLE_SIZE`。
- 行为：httpx 全失败时，遍历前 `min(SAMPLE_SIZE*2, len(links))` 篇用 Playwright 兜底，累计到 `SAMPLE_SIZE` 篇即停；仍空才返回既有错误。

- [ ] **Step 1: 写失败测试**

```python
def test_quality_gate_playwright_fallback_on_total_httpx_failure(trial_env, monkeypatch):
    """When httpx extracts nothing, Playwright fallback recovers bodies."""
    links = ["https://cf.example/a", "https://cf.example/b", "https://cf.example/c"]
    monkeypatch.setattr(tm, "_get_article_links_for_sampling", lambda trial: links)
    monkeypatch.setattr(tm, "_extract_article_text", lambda url, timeout=20: None)  # httpx 403
    monkeypatch.setattr(tm, "_is_likely_js_only", lambda url, timeout=15: False)
    monkeypatch.setattr(tm, "_extract_article_text_playwright",
                        lambda url: "Recovered CF body. " * 30)
    monkeypatch.setattr(tm, "_call_haiku", lambda prompt, max_retries=1: {
        "articles": [{"article_num": i, "relevance": 0.8, "depth": 0.7,
                      "extractable": 0.9, "overall": 0.8, "notes": "ok"} for i in (1, 2, 3)]})
    result = tm.sample_article_quality("https://cf.example/", trial={"id": "cohen-steers"})
    assert result["sampled"] == tm.SAMPLE_SIZE
    assert result.get("error") is None

def test_quality_gate_no_playwright_when_httpx_succeeds(trial_env, monkeypatch):
    """Healthy source: httpx works → Playwright fallback must NOT run (zero overhead)."""
    links = ["https://ok.example/a", "https://ok.example/b", "https://ok.example/c"]
    monkeypatch.setattr(tm, "_get_article_links_for_sampling", lambda trial: links)
    monkeypatch.setattr(tm, "_extract_article_text", lambda url, timeout=20: "Good body. " * 30)
    pw = MagicMock(side_effect=AssertionError("Playwright must not be called"))
    monkeypatch.setattr(tm, "_extract_article_text_playwright", pw)
    monkeypatch.setattr(tm, "_call_haiku", lambda prompt, max_retries=1: {
        "articles": [{"article_num": i, "relevance": 0.8, "depth": 0.7,
                      "extractable": 0.9, "overall": 0.8, "notes": "ok"} for i in (1, 2, 3)]})
    result = tm.sample_article_quality("https://ok.example/", trial={"id": "healthy"})
    assert result["sampled"] == tm.SAMPLE_SIZE
    pw.assert_not_called()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_unit_trial_quality.py::test_quality_gate_playwright_fallback_on_total_httpx_failure -q`
Expected: FAIL — 兜底未实现，`article_texts` 为空 → `sampled == 0`

- [ ] **Step 3: 实现**

在 `sample_article_quality` 中，把现有的：

```python
    if not article_texts:
        return {"sampled": 0, "articles": [], "avg_score": 0.0,
                "error": "Could not extract text from any article",
                "js_only_count": js_only_count,
                "js_only_checked": js_only_checked}
```

改为（在 return 之前插入 Playwright 兜底）：

```python
    if not article_texts:
        # Playwright fallback: httpx reached no body (CF 403 / JS shell). Retry a
        # bounded number of links with a real browser before giving up.
        for url in links[:SAMPLE_SIZE * 2]:
            text = _extract_article_text_playwright(url)
            if text:
                article_texts.append((url, text))
            if len(article_texts) >= SAMPLE_SIZE:
                break

    if not article_texts:
        return {"sampled": 0, "articles": [], "avg_score": 0.0,
                "error": "Could not extract text from any article",
                "js_only_count": js_only_count,
                "js_only_checked": js_only_checked}
```

- [ ] **Step 4: 运行确认通过 + 无回归**

Run: `python3 -m pytest tests/test_unit_trial_quality.py -q && python3 -m pytest tests/ -q`
Expected: PASS；总数 = 545 + 2。

- [ ] **Step 5: 提交**

```bash
git add gmia-trial-manager.py tests/test_unit_trial_quality.py
git commit -m "feat(trial): Playwright body fallback when httpx extracts no article text"
```

---

### Task 4: principal-am httpx content fetcher

**Files:**
- Modify: `fetch_content.py`（新增函数 + `CONTENT_FETCHERS` 注册）
- Test: `tests/test_unit_fetch_content.py`

**Interfaces:**
- Consumes: `requests`、`HEADERS`、`_normalize_html`、`_check_min_content_length`、`_atomic_write`、`CONTENT_DIR`。
- Produces: `_fetch_content_principal_am(article: dict) -> Optional[tuple[Path, str]]`；`CONTENT_FETCHERS["principal-am"]`。

- [ ] **Step 1: 写失败测试**

```python
# 顶部 import 增补 _fetch_content_principal_am
def test_fetch_content_principal_am_saves_body(tmp_path, monkeypatch):
    import fetch_content as fc
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    html = "<html><body><main><p>%s</p></main></body></html>" % ("Principal CRE analysis. " * 30)
    resp = MagicMock(status_code=200, text=html); resp.raise_for_status = lambda: None
    monkeypatch.setattr(fc.requests, "get", lambda *a, **k: resp)
    out = fc._fetch_content_principal_am({"id": "principal-am-001", "url": "https://principalam.com/x"})
    assert out is not None
    path, status = out
    assert status == "ok" and path.exists()
    assert "Principal CRE analysis." in path.read_text()

def test_fetch_content_principal_am_none_on_http_error(tmp_path, monkeypatch):
    import fetch_content as fc
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    def boom(*a, **k): raise fc.requests.RequestException("500")
    monkeypatch.setattr(fc.requests, "get", boom)
    assert fc._fetch_content_principal_am({"id": "principal-am-002", "url": "https://x"}) is None

def test_principal_am_registered():
    from fetch_content import CONTENT_FETCHERS
    assert "principal-am" in CONTENT_FETCHERS
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_unit_fetch_content.py::test_principal_am_registered -q`
Expected: FAIL — `ImportError: cannot import name '_fetch_content_principal_am'`

- [ ] **Step 3: 实现**

在 `fetch_content.py`（其他 `_fetch_content_*` 附近）新增：

```python
def _fetch_content_principal_am(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Principal Asset Management article content via requests (SSR article pages).

    The listing page is a Coveo-driven SPA, but individual article pages are plain
    server-rendered HTML (httpx 200), so no browser is needed for the body.
    """
    url = article["url"]
    log.info("  Principal AM: fetching article page %s", url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Principal AM: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, ".article-body, main article, main p, article p")
    if not _check_min_content_length(text):
        log.warning("  Principal AM: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Principal AM: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")
```

并在 `CONTENT_FETCHERS` 字典末尾加：

```python
    "principal-am": _fetch_content_principal_am,
```

- [ ] **Step 4: 运行确认通过 + 无回归**

Run: `python3 -m pytest tests/test_unit_fetch_content.py -q && python3 -m pytest tests/ -q`
Expected: PASS；总数 = 547 + 3。

- [ ] **Step 5: 提交**

```bash
git add fetch_content.py tests/test_unit_fetch_content.py
git commit -m "feat(content): add httpx content fetcher for principal-am"
```

---

### Task 5: cohen-steers Playwright content fetcher

**Files:**
- Modify: `fetch_content.py`（新增函数 + `CONTENT_FETCHERS` 注册）
- Test: `tests/test_unit_fetch_content.py`

**Interfaces:**
- Consumes: `fetch_articles._get_playwright_page`（局部 import）、`_normalize_html`、`_check_min_content_length`、`_atomic_write`、`CONTENT_DIR`。
- Produces: `_fetch_content_cohen_steers(article: dict) -> Optional[tuple[Path, str]]`；`CONTENT_FETCHERS["cohen-steers"]`。

- [ ] **Step 1: 写失败测试**

```python
def test_fetch_content_cohen_steers_saves_body(tmp_path, monkeypatch):
    import fetch_content as fc
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    html = "<html><body><article><p>%s</p></article></body></html>" % ("Cohen REIT insight. " * 30)
    monkeypatch.setattr("fetch_articles._get_playwright_page", lambda url, **kw: html)
    out = fc._fetch_content_cohen_steers({"id": "cohen-steers-001", "url": "https://cohenandsteers.com/x"})
    assert out is not None
    path, status = out
    assert status == "ok" and path.exists()
    assert "Cohen REIT insight." in path.read_text()

def test_fetch_content_cohen_steers_none_on_browser_error(tmp_path, monkeypatch):
    import fetch_content as fc
    monkeypatch.setattr(fc, "CONTENT_DIR", tmp_path)
    def boom(url, **kw): raise RuntimeError("browser crash")
    monkeypatch.setattr("fetch_articles._get_playwright_page", boom)
    assert fc._fetch_content_cohen_steers({"id": "cohen-steers-002", "url": "https://x"}) is None

def test_cohen_steers_registered():
    from fetch_content import CONTENT_FETCHERS
    assert "cohen-steers" in CONTENT_FETCHERS
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_unit_fetch_content.py::test_cohen_steers_registered -q`
Expected: FAIL — `ImportError: cannot import name '_fetch_content_cohen_steers'`

- [ ] **Step 3: 实现**

```python
def _fetch_content_cohen_steers(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Cohen & Steers article content via Playwright (Cloudflare-gated bodies).

    Article pages sit behind the same CF challenge as the listing, so httpx 403s;
    a real browser renders the body. Reuses fetch_articles._get_playwright_page.
    """
    url = article["url"]
    log.info("  Cohen & Steers: fetching article page %s", url)
    try:
        from fetch_articles import _get_playwright_page
        html = _get_playwright_page(url, wait_until="domcontentloaded", wait_ms=3000)
    except Exception as e:
        log.error("  Cohen & Steers: fetch failed: %s", e)
        return None
    if not html:
        return None

    text = _normalize_html(html, ".article-body, article p, main p")
    if not _check_min_content_length(text):
        log.warning("  Cohen & Steers: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Cohen & Steers: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")
```

并在 `CONTENT_FETCHERS` 加：

```python
    "cohen-steers": _fetch_content_cohen_steers,
```

- [ ] **Step 4: 运行确认通过 + 无回归**

Run: `python3 -m pytest tests/test_unit_fetch_content.py -q && python3 -m pytest tests/ -q`
Expected: PASS；总数 = 550 + 3。

- [ ] **Step 5: 提交**

```bash
git add fetch_content.py tests/test_unit_fetch_content.py
git commit -m "feat(content): add Playwright content fetcher for cohen-steers (CF-gated)"
```

---

### Task 6: 真实实测 + 收尾验证（非 mock）

守门：不打真实站点的单测已全绿，这一步用真实网络确认端到端，并调整 selector（若需要）。

**Files:**
- 可能微调 `fetch_content.py` 的 selector（若实测正文过短）
- 无新测试文件

- [ ] **Step 1: 实测 trial 兜底能过 CF 抓到 cohen 正文**

Run:
```bash
cd ~/hedge-fund-research && ~/stock-env/bin/python3 - <<'PY'
import importlib.util, json
from pathlib import Path
spec=importlib.util.spec_from_file_location("tm","gmia-trial-manager.py")
tm=importlib.util.module_from_spec(spec); spec.loader.exec_module(tm)
from fetch_articles import fetch_cohen_steers
c={c['id']:c for c in json.load(open('config/fund_candidates.json'))}['cohen-steers']
url=fetch_cohen_steers({'url':c['research_url'],'max_articles':3})[0]['url']
print("cohen article:", url)
print("httpx:", "None" if tm._extract_article_text(url) is None else "got")
pw=tm._extract_article_text_playwright(url)
print("playwright:", f"{len(pw)} chars" if pw else "None")
PY
```
Expected: `httpx: None`（CF 403）+ `playwright: <N> chars`（过 CF，正文非空）。

- [ ] **Step 2: 实测两个 content fetcher 端到端存正文**

Run:
```bash
cd ~/hedge-fund-research && ~/stock-env/bin/python3 - <<'PY'
import json, fetch_content as fc
from fetch_articles import fetch_cohen_steers, fetch_principal_am
c={c['id']:c for c in json.load(open('config/fund_candidates.json'))}
for cid,lister,fetcher in [("cohen-steers",fetch_cohen_steers,fc._fetch_content_cohen_steers),
                           ("principal-am",fetch_principal_am,fc._fetch_content_principal_am)]:
    art=lister({'url':c[cid]['research_url'],'max_articles':3})[0]
    art={**art,'id':f'{cid}-verify'}
    out=fetcher(art)
    print(cid, "->", (f"{out[1]}, {out[0].stat().st_size}B" if out else "None(FAIL)"))
PY
```
Expected: 两行都 `ok, <bytes>B`（正文已存）。若 cohen/principal 某个正文过短返回 None → 回 Task 4/5 调 selector，重跑本步。

- [ ] **Step 3: 全套回归**

Run: `python3 -m pytest tests/ -q`
Expected: PASS（≥ 550）。

- [ ] **Step 4: 语法 + 注册自检**

Run: `python3 -c "import fetch_content; assert {'cohen-steers','principal-am'} <= set(fetch_content.CONTENT_FETCHERS)"`
Expected: 无输出、exit 0。

- [ ] **Step 5: 收尾（提交任何 selector 微调；push 与否听用户）**

```bash
git add fetch_content.py   # 仅当有 selector 微调
git commit -m "fix(content): tune cohen/principal body selectors from live verification"
# push 由用户决定（本 repo 之前逐次确认 push）
```

---

## Notes for the executor

- selector（Task 4/5 的 `_normalize_html` 参数）是起始猜测，靠 Task 6 实测收敛——这是既有做法（goehring 也是先写后实测），不是占位符。
- 本 plan 不改 principal-am 的 trial 路径（它 httpx 已能抓正文）；不改数量门/阈值/并发。
- 全程勿 `git add -A`；`config/fund_candidates.json`、`config/trial-state.json` 由调度器并发写。
