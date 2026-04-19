# GMIA Fetcher Synthesis — Agent Program

## Goal

你是一名 Python 爬虫工程师。目标：对 GMIA 候选列表中每个 `inaccessible` 基金，
尝试多种抓取策略，写出可运行的 `fetch_<id>()` 函数，注入 `fetch_articles.py`，
并将该基金状态更新为 `validated`。若所有策略均失败，记录尝试并继续。

## 准备

```bash
cd /home/ubuntu/hedge-fund-research
python3 synthesize_fetchers.py
```

读取 JSON 输出，这是**目标列表**。每次最多处理 **2 个基金**（优先选 HIGH quality）。
若列表为空，输出 "No targets" 并退出。

## 每个基金的工作流程

### Phase 1 — 检查页面

```bash
cd /home/ubuntu/hedge-fund-research
python3 - << 'EOF'
from playwright.sync_api import sync_playwright

url = "REPLACE_WITH_RESEARCH_URL"
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    )
    page.goto(url, timeout=30000)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(3000)
    content = page.content()
    print(f"Content length: {len(content)}")
    print(content[:6000])
    browser.close()
EOF
```

分析 HTML，寻找：
- 文章卡片容器（`article`, `div[class*=card]`, `li[class*=item]`, `a[class*=insight]`）
- 标题元素（`h2`, `h3`, `h4`, `a`）
- 日期元素（`time[datetime]`, `span[class*=date]`, `p[class*=date]`）

若 HTML 内容极少（<500 字）→ 极度 JS 渲染，尝试延长等待时间（page.wait_for_timeout(6000)）。

### Phase 2 — 写 fetcher 函数

根据页面结构，参考 `fetch_blackstone`（fetch_articles.py 第 764 行）写 `fetch_<fund_id>` 函数。

所有函数必须：
- 参数：`source: dict`，返回：`list[dict]`
- 每个 dict 包含：`title`（str）、`url`（str）、`date`（str|None）、`date_raw`（str）
- JS 渲染页面用 `_get_playwright_page(source["url"], wait_selector="<selector>")`
- 静态页面用 `requests.get(url, headers=HEADERS, timeout=20)`
- 日期解析用 `parse_date()`
- URL 验证用 `_validate_hostname(url, expected_host)`
- 返回 `articles[:source.get("max_articles", 10)]`

### Phase 3 — 测试 fetcher

```bash
cd /home/ubuntu/hedge-fund-research
python3 - << 'EOF'
import sys
sys.path.insert(0, "/home/ubuntu/hedge-fund-research")
from fetch_articles import _get_playwright_page, parse_date, _validate_hostname, HEADERS
from bs4 import BeautifulSoup
from urllib.parse import urljoin

def fetch_FUNDID(source):
    # 粘贴你的实现
    pass

source = {"url": "RESEARCH_URL", "max_articles": 10}
results = fetch_FUNDID(source)
print(f"Found {len(results)} articles")
for a in results[:3]:
    print(f"  {a.get('date') or 'n/a':10s}  {a['title'][:70]}")
EOF
```

**通过标准**：返回 ≥ 3 篇文章。若未达到，尝试不同选择器或策略。

备用策略（按顺序尝试）：
1. **RSS 发现**：`curl -s "URL/feed" | head -100` 或 `URL/rss`
2. **JSON API 嗅探**：用 Playwright 截取 network requests 找 API endpoint

若所有策略返回 0 → 跳过此基金（标记为已尝试，处理下一个）。

### Phase 4 — 注入 `fetch_articles.py`

仅当 Phase 3 返回 ≥ 3 篇文章时执行。

**Step A**：将函数体插入到这一行**之前**（用 Edit 工具）：
```
# FETCHER_SYNTHESIS_INSERTION_POINT — auto-generated fetchers inserted above this line
```

**Step B**：在 FETCHERS 字典末尾加一行（找到 `"pimco": fetch_pimco,` 后插入）：
```python
    "FUNDID": fetch_FUNDID,
```

**Step C**：验证注入没有破坏文件：
```bash
cd /home/ubuntu/hedge-fund-research
python3 -c "import fetch_articles; print('OK —', list(fetch_articles.FETCHERS.keys()))"
```

**Step D**：运行单元测试：
```bash
python3 -m pytest tests/ -q --timeout=30 2>&1 | tail -5
```
若测试失败 → `git checkout fetch_articles.py` 回滚，将基金标记为 failed。

### Phase 5 — 更新 `fund_candidates.json`

```python
import json
from datetime import datetime, timezone
from pathlib import Path

f = Path("config/fund_candidates.json")
data = json.loads(f.read_text())
candidates = data if isinstance(data, list) else data.get("candidates", [])
now = datetime.now(timezone.utc).isoformat()

for c in candidates:
    if c["id"] == "FUNDID":
        # 成功路径：
        c["status"] = "validated"
        c["synthesis_attempted_at"] = now
        c["synthesis_outcome"] = "success"
        # 失败路径（注释掉成功路径，用这段）：
        # c["synthesis_attempted_at"] = now
        # c["synthesis_outcome"] = "failed"
        break

out = candidates if isinstance(data, list) else {**data, "candidates": candidates}
f.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
print(f"Updated FUNDID")
```

### Phase 6 — 提交

```bash
cd /home/ubuntu/hedge-fund-research
git add fetch_articles.py config/fund_candidates.json
git commit -m "feat(fetcher): auto-synthesize fetcher for FUNDID"
git push
```

## 规则

- **绝对不修改** `config/sources.json` 或 `config/entrypoints.json`
- **绝对不修改**已有的任何 `fetch_*` 函数（只新增）
- 每次 session 最多处理 2 个基金
- 注入后若 pytest 失败 → 立即 `git checkout fetch_articles.py` 回滚
- 提交前必须运行 `python3 -c "import fetch_articles"` 验证无语法错误
