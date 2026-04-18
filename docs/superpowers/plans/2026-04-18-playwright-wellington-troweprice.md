# Playwright Fetchers: Wellington + T. Rowe Price Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Wellington 和 T. Rowe Price 添加 Playwright 抓取支持，将其状态从 `inaccessible` 升级为可正式抓取的研究来源。

**Architecture:** 复用现有 `_get_playwright_page()` helper（已支持 AQR/Oaktree），在 `fetch_articles.py` 中新增 `fetch_wellington` 和 `fetch_troweprice` 函数，注册到 `FETCHERS` dict，并在 `sources.json` 和 `fund_candidates.json` 中更新元数据。测试使用与 Oaktree 相同的 mock 模式（`patch("fetch_articles._get_playwright_page", ...)`）。

**Tech Stack:** Python, Playwright (sync API), BeautifulSoup, pytest

**PIMCO 说明：** PIMCO 被 Akamai CDN 封锁（即使 Playwright 也返回 "Access Denied"），暂缓实施，等待 stealth 方案。

---

## 已确认的 HTML 选择器（勿改动）

### Wellington (`https://www.wellington.com/en/insights`)
- 等待选择器：`section.insight.article`
- 容器：`section.insight.article`（每篇文章一个 `<section>`）
- 标题：`a.insight__title`（get_text）
- 链接：`a.insight__link`（href 属性）
- 日期：`date[datetime]`（datetime 属性，格式 YYYY-MM-DD）
- 分类：`div.insight__contentType span`（第一个 span 的文本，如 "Article"）
- Base URL：`https://www.wellington.com`

### T. Rowe Price (`https://www.troweprice.com/personal-investing/resources/insights.html`)
- 等待选择器：`div.b-grid-item--12-col`
- 容器：`div.b-grid-item--12-col`（过滤条件：必须包含 `a[href*='/insights/']`）
- 标题：`span.cmp-tile__heading`（get_text；若不存在则用链接文本）
- 链接：`a[href*='/insights/']`（href 属性）
- 日期：`span.cmp-tile__eyebrow`（取第一个匹配日期格式的，即含月份词或数字的）
- 分类：`span.cmp-tile__eyebrow`（取第一个不含日期的文本，如 "Markets & Economy"）
- Base URL：`https://www.troweprice.com`

---

## 文件清单

| 文件 | 改动类型 |
|------|---------|
| `fetch_articles.py` | 修改：新增 `fetch_wellington`、`fetch_troweprice`，注册到 `FETCHERS` |
| `config/sources.json` | 修改：新增 wellington 和 troweprice 两个 source 对象 |
| `config/fund_candidates.json` | 修改：wellington 和 troweprice 的 status 从 `inaccessible` 改为 `validated` |
| `tests/test_unit_fetch_articles.py` | 修改：新增两个 fetcher 的 mock 单元测试 |

---

## Task 1: 实现 `fetch_wellington`

**Files:**
- Modify: `fetch_articles.py`
- Modify: `tests/test_unit_fetch_articles.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_unit_fetch_articles.py` 的导入行加 `fetch_wellington`：

```python
from fetch_articles import (
    article_id, parse_date, _validate_hostname, load_existing_ids,
    fetch_oaktree, fetch_wellington, DATA_FILE, FETCHERS
)
```

在文件末尾加测试类：

```python
class TestFetchWellington:
    def test_parses_articles(self):
        html = """
        <html><body>
        <section class="insight article has-image">
          <div class="insight__content">
            <div class="insight__head">
              <div class="insight__contentType"><span>Article</span></div>
              <div class="insight__date">
                <date datetime="2026-04-08"><span>April 2026</span></date>
              </div>
            </div>
            <a class="insight__title" href="/en/insights/quarterly-outlook-q2-2026">
              Quarterly Asset Allocation Outlook Q2 2026
            </a>
            <a class="insight__link" href="/en/insights/quarterly-outlook-q2-2026">Read more</a>
          </div>
        </section>
        <section class="insight article has-image">
          <div class="insight__content">
            <div class="insight__head">
              <div class="insight__contentType"><span>Whitepaper</span></div>
              <div class="insight__date">
                <date datetime="2026-03-15"><span>March 2026</span></date>
              </div>
            </div>
            <a class="insight__title" href="/en/insights/credit-outlook-2026">
              Credit Outlook 2026
            </a>
            <a class="insight__link" href="/en/insights/credit-outlook-2026">Read more</a>
          </div>
        </section>
        </body></html>
        """
        source = {
            "id": "wellington",
            "url": "https://www.wellington.com/en/insights",
            "max_articles": 10,
            "expected_hostname": "wellington.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_wellington(source)

        assert len(articles) == 2
        assert articles[0]["title"] == "Quarterly Asset Allocation Outlook Q2 2026"
        assert articles[0]["url"] == "https://www.wellington.com/en/insights/quarterly-outlook-q2-2026"
        assert articles[0]["date"] == "2026-04-08"
        assert articles[0]["category"] == "Article"
        assert articles[1]["title"] == "Credit Outlook 2026"
        assert articles[1]["date"] == "2026-03-15"

    def test_respects_max_articles(self):
        # 3 articles in HTML, max_articles=2 → only 2 returned
        html = """
        <html><body>
        <section class="insight article">
          <a class="insight__title" href="/en/insights/a1">Article One</a>
          <a class="insight__link" href="/en/insights/a1">Read</a>
          <div class="insight__date"><date datetime="2026-04-01"></date></div>
          <div class="insight__contentType"><span>Article</span></div>
        </section>
        <section class="insight article">
          <a class="insight__title" href="/en/insights/a2">Article Two</a>
          <a class="insight__link" href="/en/insights/a2">Read</a>
          <div class="insight__date"><date datetime="2026-03-01"></date></div>
          <div class="insight__contentType"><span>Article</span></div>
        </section>
        <section class="insight article">
          <a class="insight__title" href="/en/insights/a3">Article Three</a>
          <a class="insight__link" href="/en/insights/a3">Read</a>
          <div class="insight__date"><date datetime="2026-02-01"></date></div>
          <div class="insight__contentType"><span>Article</span></div>
        </section>
        </body></html>
        """
        source = {
            "id": "wellington",
            "url": "https://www.wellington.com/en/insights",
            "max_articles": 2,
            "expected_hostname": "wellington.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_wellington(source)
        assert len(articles) == 2

    def test_skips_external_urls(self):
        html = """
        <html><body>
        <section class="insight article">
          <a class="insight__title" href="https://other-site.com/article">External</a>
          <a class="insight__link" href="https://other-site.com/article">Read</a>
          <div class="insight__date"><date datetime="2026-04-01"></date></div>
          <div class="insight__contentType"><span>Article</span></div>
        </section>
        <section class="insight article">
          <a class="insight__title" href="/en/insights/valid">Valid Article</a>
          <a class="insight__link" href="/en/insights/valid">Read</a>
          <div class="insight__date"><date datetime="2026-04-02"></date></div>
          <div class="insight__contentType"><span>Article</span></div>
        </section>
        </body></html>
        """
        source = {
            "id": "wellington",
            "url": "https://www.wellington.com/en/insights",
            "max_articles": 10,
            "expected_hostname": "wellington.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_wellington(source)
        assert len(articles) == 1
        assert "wellington.com" in articles[0]["url"]
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd ~/hedge-fund-research
python3 -m pytest tests/test_unit_fetch_articles.py::TestFetchWellington -v
```

期望：`ImportError: cannot import name 'fetch_wellington'`

- [ ] **Step 3: 实现 `fetch_wellington`**

在 `fetch_articles.py` 中，`fetch_oaktree` 函数之后（第 519 行之后）插入：

```python
def fetch_wellington(source: dict) -> list[dict]:
    """Fetch articles from Wellington Management (Playwright — CSR/AEM).

    Structure: section.insight.article containing:
      a.insight__title (title + href), a.insight__link (href fallback),
      date[datetime] (ISO date attr), div.insight__contentType > span (category)
    """
    base_url = "https://www.wellington.com"
    html = _get_playwright_page(source["url"], wait_selector="section.insight.article")
    soup = BeautifulSoup(html, "html.parser")
    expected_host = source.get("expected_hostname", "wellington.com")

    articles = []
    for item in soup.select("section.insight.article"):
        title_el = item.select_one("a.insight__title")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue

        # Prefer insight__link for href, fall back to insight__title href
        link_el = item.select_one("a.insight__link") or title_el
        href = link_el.get("href", "") or title_el.get("href", "")
        if not href:
            continue
        url = urljoin(base_url, href)
        if not _validate_hostname(url, expected_host):
            continue

        date_el = item.select_one("date[datetime]")
        parsed_date = None
        date_raw = ""
        if date_el:
            dt_attr = date_el.get("datetime", "")
            if dt_attr:
                try:
                    parsed_date = datetime.fromisoformat(dt_attr).strftime("%Y-%m-%d")
                except ValueError:
                    parsed_date = parse_date(dt_attr)
            date_raw = date_el.get_text(strip=True)

        category_el = item.select_one("div.insight__contentType span")
        category = category_el.get_text(strip=True) if category_el else ""

        articles.append({
            "title": title,
            "category": category,
            "url": url,
            "date": parsed_date,
            "date_raw": date_raw,
        })

    return articles[:source.get("max_articles", 10)]
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python3 -m pytest tests/test_unit_fetch_articles.py::TestFetchWellington -v
```

期望：3 个测试全部 PASS

- [ ] **Step 5: 运行全量测试确认无回归**

```bash
python3 -m pytest tests/ -q
```

期望：全部通过（新增 3 个，原有数量不减少）

- [ ] **Step 6: Commit**

```bash
git add fetch_articles.py tests/test_unit_fetch_articles.py
git commit -m "feat(gmia): add Wellington Playwright fetcher with unit tests"
```

---

## Task 2: 实现 `fetch_troweprice`

**Files:**
- Modify: `fetch_articles.py`
- Modify: `tests/test_unit_fetch_articles.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_unit_fetch_articles.py` 导入行加 `fetch_troweprice`：

```python
from fetch_articles import (
    article_id, parse_date, _validate_hostname, load_existing_ids,
    fetch_oaktree, fetch_wellington, fetch_troweprice, DATA_FILE, FETCHERS
)
```

在文件末尾加测试类：

```python
class TestFetchTrowePrice:
    def test_parses_articles(self):
        html = """
        <html><body>
        <div class="b-grid-item--12-col b-md:6-col b-lg:4-col">
          <span class="cmp-tile__eyebrow">April 17, 2026</span>
          <span class="cmp-tile__eyebrow">Markets &amp; Economy</span>
          <span class="cmp-tile__heading">Global markets weekly update</span>
          <a href="/personal-investing/resources/insights/global-markets-weekly-update.html">
            Global markets weekly update
          </a>
        </div>
        <div class="b-grid-item--12-col b-md:6-col b-lg:4-col">
          <span class="cmp-tile__eyebrow">Mar 2026</span>
          <span class="cmp-tile__eyebrow">Monthly Market Playbook</span>
          <span class="cmp-tile__heading">Has the AI arms race changed mega-cap tech?</span>
          <a href="/personal-investing/resources/insights/ai-arms-race.html">
            Has the AI arms race changed mega-cap tech?
          </a>
        </div>
        </body></html>
        """
        source = {
            "id": "troweprice",
            "url": "https://www.troweprice.com/personal-investing/resources/insights.html",
            "max_articles": 10,
            "expected_hostname": "troweprice.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_troweprice(source)

        assert len(articles) == 2
        assert articles[0]["title"] == "Global markets weekly update"
        assert articles[0]["url"] == "https://www.troweprice.com/personal-investing/resources/insights/global-markets-weekly-update.html"
        assert articles[0]["date"] == "2026-04-17"
        assert articles[0]["category"] == "Markets & Economy"
        assert articles[1]["title"] == "Has the AI arms race changed mega-cap tech?"

    def test_skips_cards_without_insights_link(self):
        # Navigation cards that don't link to /insights/ should be skipped
        html = """
        <html><body>
        <div class="b-grid-item--12-col">
          <a href="/personal-investing/accounts/index.html">Explore accounts</a>
        </div>
        <div class="b-grid-item--12-col">
          <span class="cmp-tile__eyebrow">April 17, 2026</span>
          <span class="cmp-tile__heading">Valid article</span>
          <a href="/personal-investing/resources/insights/valid-article.html">Valid article</a>
        </div>
        </body></html>
        """
        source = {
            "id": "troweprice",
            "url": "https://www.troweprice.com/personal-investing/resources/insights.html",
            "max_articles": 10,
            "expected_hostname": "troweprice.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_troweprice(source)
        assert len(articles) == 1
        assert "valid-article" in articles[0]["url"]

    def test_respects_max_articles(self):
        cards = ""
        for i in range(5):
            cards += f"""
            <div class="b-grid-item--12-col">
              <span class="cmp-tile__eyebrow">Apr 2026</span>
              <span class="cmp-tile__heading">Article {i}</span>
              <a href="/personal-investing/resources/insights/article-{i}.html">Article {i}</a>
            </div>"""
        html = f"<html><body>{cards}</body></html>"
        source = {
            "id": "troweprice",
            "url": "https://www.troweprice.com/personal-investing/resources/insights.html",
            "max_articles": 3,
            "expected_hostname": "troweprice.com",
        }
        with patch("fetch_articles._get_playwright_page", return_value=html):
            articles = fetch_troweprice(source)
        assert len(articles) == 3
```

- [ ] **Step 2: 运行测试确认失败**

```bash
python3 -m pytest tests/test_unit_fetch_articles.py::TestFetchTrowePrice -v
```

期望：`ImportError: cannot import name 'fetch_troweprice'`

- [ ] **Step 3: 实现 `fetch_troweprice`**

在 `fetch_articles.py` 中，`fetch_wellington` 函数之后插入：

```python
_DATE_WORDS = frozenset([
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
    "january", "february", "march", "april", "june",
    "july", "august", "september", "october", "november", "december",
])


def _is_date_eyebrow(text: str) -> bool:
    """Return True if the eyebrow span contains a date (not a category)."""
    lower = text.lower()
    # Contains a month word, or starts with a digit (e.g., "2026-04-17")
    return any(w in lower for w in _DATE_WORDS) or bool(re.match(r"\d", text.strip()))


def fetch_troweprice(source: dict) -> list[dict]:
    """Fetch articles from T. Rowe Price personal-investing insights (Playwright — CSR/AEM).

    Structure: div.b-grid-item--12-col containing:
      a[href*='/insights/'] (link + fallback title),
      span.cmp-tile__heading (title, preferred),
      span.cmp-tile__eyebrow (multiple: first date-like = date, first non-date = category)
    """
    base_url = "https://www.troweprice.com"
    html = _get_playwright_page(source["url"], wait_selector="div.b-grid-item--12-col")
    soup = BeautifulSoup(html, "html.parser")
    expected_host = source.get("expected_hostname", "troweprice.com")

    articles = []
    for item in soup.select("div.b-grid-item--12-col"):
        link_el = item.select_one("a[href*='/insights/']")
        if not link_el:
            continue

        href = link_el.get("href", "")
        if not href:
            continue
        url = urljoin(base_url, href)
        if not _validate_hostname(url, expected_host):
            continue

        heading_el = item.select_one("span.cmp-tile__heading")
        title = heading_el.get_text(strip=True) if heading_el else link_el.get_text(strip=True)
        if not title:
            continue

        eyebrows = [el.get_text(strip=True) for el in item.select("span.cmp-tile__eyebrow") if el.get_text(strip=True)]
        date_raw = next((e for e in eyebrows if _is_date_eyebrow(e)), "")
        category = next((e for e in eyebrows if not _is_date_eyebrow(e)), "")
        parsed_date = parse_date(date_raw) if date_raw else None

        articles.append({
            "title": title,
            "category": category,
            "url": url,
            "date": parsed_date,
            "date_raw": date_raw,
        })

    return articles[:source.get("max_articles", 10)]
```

- [ ] **Step 4: 运行测试确认通过**

```bash
python3 -m pytest tests/test_unit_fetch_articles.py::TestFetchTrowePrice -v
```

期望：3 个测试全部 PASS

- [ ] **Step 5: 运行全量测试确认无回归**

```bash
python3 -m pytest tests/ -q
```

期望：全部通过

- [ ] **Step 6: Commit**

```bash
git add fetch_articles.py tests/test_unit_fetch_articles.py
git commit -m "feat(gmia): add T. Rowe Price Playwright fetcher with unit tests"
```

---

## Task 3: 注册到 FETCHERS + 更新 sources.json + fund_candidates.json

**Files:**
- Modify: `fetch_articles.py`（FETCHERS dict）
- Modify: `config/sources.json`
- Modify: `config/fund_candidates.json`

- [ ] **Step 1: 注册到 FETCHERS**

在 `fetch_articles.py` 的 `FETCHERS` dict（约第 597 行）中添加：

```python
FETCHERS = {
    "man-group": fetch_man_group,
    "bridgewater": fetch_bridgewater,
    "aqr": fetch_aqr,
    "gmo": fetch_gmo,
    "oaktree": fetch_oaktree,
    "ark-invest": fetch_ark_invest,
    "wellington": fetch_wellington,
    "troweprice": fetch_troweprice,
}
```

- [ ] **Step 2: 更新 sources.json**

在 `config/sources.json` 的 `sources` 数组末尾（`ark-invest` 之后）添加两个新 source：

```json
{
  "id": "wellington",
  "name": "Wellington Management",
  "method": "playwright",
  "url": "https://www.wellington.com/en/insights",
  "expected_hostname": "wellington.com",
  "max_articles": 10,
  "strategy_tags": ["equity", "macro", "fixed_income", "multi_asset", "esg_climate"]
},
{
  "id": "troweprice",
  "name": "T. Rowe Price",
  "method": "playwright",
  "url": "https://www.troweprice.com/personal-investing/resources/insights.html",
  "expected_hostname": "troweprice.com",
  "max_articles": 10,
  "strategy_tags": ["equity", "macro", "fixed_income"]
}
```

- [ ] **Step 3: 更新 fund_candidates.json**

找到 `wellington` 和 `troweprice` 对象，将 `"status": "inaccessible"` 改为 `"status": "validated"`：

```json
{ "id": "wellington", "status": "validated", ... }
{ "id": "troweprice", "status": "validated", ... }
```

- [ ] **Step 4: 验证 JSON 格式**

```bash
cd ~/hedge-fund-research
python3 -c "
import json
for f in ['config/sources.json', 'config/fund_candidates.json']:
    data = json.load(open(f))
    print(f, 'OK')
python3 -c "import json; data=json.load(open('config/sources.json')); [print(s['id'], s['method']) for s in data['sources']]"
```

期望：
```
config/sources.json OK
config/fund_candidates.json OK
man-group ssr
bridgewater ssr
aqr playwright
gmo api
oaktree playwright
ark-invest rss
wellington playwright
troweprice playwright
```

- [ ] **Step 5: 验证 FETCHERS 注册**

```bash
python3 -c "from fetch_articles import FETCHERS; print(list(FETCHERS.keys()))"
```

期望：`['man-group', 'bridgewater', 'aqr', 'gmo', 'oaktree', 'ark-invest', 'wellington', 'troweprice']`

- [ ] **Step 6: 运行全量测试**

```bash
python3 -m pytest tests/ -q
```

期望：全部通过

- [ ] **Step 7: Commit**

```bash
git add fetch_articles.py config/sources.json config/fund_candidates.json
git commit -m "feat(gmia): register wellington+troweprice fetchers, promote to validated"
```

---

## Task 4: Live 冒烟测试（可选，需网络）

**Files:**（无文件改动，仅验证）

- [ ] **Step 1: 测试 Wellington live 抓取**

```bash
cd ~/hedge-fund-research
python3 -c "
import json
from fetch_articles import fetch_wellington
source = {
    'id': 'wellington',
    'url': 'https://www.wellington.com/en/insights',
    'max_articles': 5,
    'expected_hostname': 'wellington.com',
}
articles = fetch_wellington(source)
print(f'Articles fetched: {len(articles)}')
for a in articles:
    print(f'  [{a[\"date\"]}] {a[\"title\"][:60]}')
"
```

期望：≥ 3 篇文章，有标题和日期

- [ ] **Step 2: 测试 T. Rowe Price live 抓取**

```bash
python3 -c "
import json
from fetch_articles import fetch_troweprice
source = {
    'id': 'troweprice',
    'url': 'https://www.troweprice.com/personal-investing/resources/insights.html',
    'max_articles': 5,
    'expected_hostname': 'troweprice.com',
}
articles = fetch_troweprice(source)
print(f'Articles fetched: {len(articles)}')
for a in articles:
    print(f'  [{a[\"date\"]}] {a[\"title\"][:60]}')
"
```

期望：≥ 3 篇文章，有标题和日期

- [ ] **Step 3: 最终 push**

```bash
git push
```

---

## 计划后注记

**PIMCO 状态：** Akamai CDN 在 EC2 IP 段封锁所有爬虫（curl 和 Playwright 均返回 Access Denied），暂保持 `inaccessible`。可能的解法：playwright-stealth + 住宅代理，但成本和维护复杂度不合算，留待日后专项处理。
