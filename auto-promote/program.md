# GMIA Auto-Promote — Agent Program

## Goal

你是一名 Python pipeline 工程师。目标：把 trial PASS 的候选基金（status="promoted" in
`config/fund_candidates.json`，但**还没**进 `config/sources.json`）自动接入 GMIA
production pipeline。

每次 session 最多处理 **2 个基金**（与 fetcher-synthesis 一致），保证回滚成本可控。

## 准备 — 找出待处理目标

```bash
cd /home/ubuntu/hedge-fund-research
python3 - << 'EOF'
import json
from pathlib import Path
candidates = json.loads(Path("config/fund_candidates.json").read_text())
sources = json.loads(Path("config/sources.json").read_text())
prod_ids = {s["id"] for s in sources["sources"]}

import sys
sys.path.insert(0, ".")
from fetch_articles import FETCHERS

targets = []
for c in candidates:
    if c.get("status") != "promoted":
        continue
    if c["id"] in prod_ids:
        continue  # already wired up
    has_fetcher = c["id"] in FETCHERS
    targets.append({
        "id": c["id"],
        "name": c["name"],
        "research_url": c.get("research_url") or c.get("homepage_url", ""),
        "homepage_url": c.get("homepage_url", ""),
        "quality": c.get("quality", "?"),
        "topics": c.get("topics", ""),
        "strategy_tags": c.get("strategy_tags", []),
        "has_fetcher": has_fetcher,
        "notable_authors": c.get("notable_authors", []),
    })

print(json.dumps(targets, indent=2, ensure_ascii=False))
EOF
```

读输出。`has_fetcher: false` 的基金**跳过**（让 fetcher-synthesis 先处理）；只处理
`has_fetcher: true` 的，按列表顺序最多 2 个。

若列表为空 → 输出 `"No targets"` 并退出。

## 每个基金的 6 步流程

### Phase 1 — sources.json 加条目

读 `config/sources.json` 现有结构（每条 entry 字段：`id`、`strategy_tags`、`name`、
`short_name`、`url`、`method`、`frequency`、`description`、`notable_authors`、
`expected_hostname`），按相同格式追加新基金。

字段填法：
- `id` / `name`：直接用 candidate 的值
- `short_name`：基金名前 1-2 个词（如 "BlackRock Investment Institute" → "BlackRock"）
- `url`：candidate 的 `research_url`
- `method`：根据 fetcher 实际调用方式判断 — 看 `fetch_articles.py` 里 `fetch_<id>` 函数：
  - 用 `_get_playwright_page` → `"playwright"`
  - 用 `requests.get` 或 `httpx.get` → `"ssr"`
  - RSS feedparser → `"rss"`
  - JSON API → `"api"`
- `frequency`：从 candidate.topics / strategy_tags 推断；没把握就 `"weekly"`
- `description`：1-2 句概述（中英都可以；现有都是英文，保持一致）
- `notable_authors`：candidate 的 `notable_authors`，没有就 `[]`
- `expected_hostname`：从 url 提取主域名（去掉 www. 与子域）

**用 Edit 工具**插入到 `sources.json` 的 sources 列表末尾，**保留原文 indent 风格**
（4-space indent + 紧凑数组）。不要让 JSON 重新格式化整个文件。

### Phase 2 — BADGE_COLORS 加色

读 `publish.py` 里的 `BADGE_COLORS` dict。已用的色见每条注释。

挑一个**没用过**的色，要满足：
- 与现有 19 个色都明显不同（避免视觉混淆）
- WCAG AA 对比度（白字背景需色值偏深，亮度 < 0.5）
- 推荐色谱（按未充分覆盖的色相）：森林绿 #2d6a4f / 古铜 #cd853f / 深粉 #c2185b /
  钢蓝 #4a6fa5 / 暗橄榄 #556b2f / 深青 #00838f / 暗珊瑚 #b85450

用 Edit 工具在 BADGE_COLORS dict 末尾追加：
```python
    "FUND_ID": "#XXXXXX",  # 简短描述（如 "BlackRock blue"）
```

### Phase 3 — CONTENT_FETCHERS 加条目 + 写 _fetch_content_<id>

**先尝试用默认 selector** — 多数基金的文章正文用 `article p` 或 `main p` 就能抓到。

参考最近的简单实现（`_fetch_content_msci` / `_fetch_content_natixis` /
`_fetch_content_apollo`），写一个新函数：

```python
def _fetch_content_FUNDID(article: dict, content_dir: Path) -> dict:
    """Fetch full article body for FUND_NAME."""
    url = article["url"]
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        return {"ok": False, "error": f"HTTP fetch failed: {exc}"[:200], "skipped": False}

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup.select("nav, footer, header, script, style, aside"):
        tag.decompose()

    # 默认 selector — 调整以匹配该基金的 HTML 结构
    paragraphs = soup.select("article p")
    if not paragraphs:
        paragraphs = soup.select("main p")
    text = "\n\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))

    if not _check_min_content_length(text):
        return {"ok": False, "error": f"Content too short ({len(text)} chars)",
                "skipped": False}

    fname = _safe_filename(article["title"], article["url"])
    out_path = content_dir / fname
    out_path.write_text(text, encoding="utf-8")
    return {"ok": True, "path": str(out_path), "chars": len(text), "skipped": False}
```

**测试**：
```bash
python3 - << 'EOF'
import sys; sys.path.insert(0, ".")
from pathlib import Path
import tempfile
from fetch_content import _fetch_content_FUNDID  # 替换 FUNDID
# 找一篇该基金的真实文章 URL（从 fetch_articles 跑一下取）
import fetch_articles
src = {"url": "RESEARCH_URL", "id": "FUNDID"}  # 替换
articles = fetch_articles.FETCHERS["FUNDID"](src)
print(f"Got {len(articles)} articles")
if articles:
    art = articles[0]
    with tempfile.TemporaryDirectory() as td:
        result = _fetch_content_FUNDID(art, Path(td))
        print(result)
EOF
```

通过标准：返回 `ok: True` 且 `chars >= 100`。

若 `article p` / `main p` 都拿不到 ≥100 字 → 用 Playwright 看一下页面结构找正确 selector，
再修。**最多重试 2 次** — 还失败就跳过这个基金（不要花时间研究偏门站）。

加进 `CONTENT_FETCHERS` dict 末尾：
```python
    "FUNDID": _fetch_content_FUNDID,
```

### Phase 4 — _FUND_PROFILES 草稿写到 pending_profiles/

**重要**：这一步**不修改 publish.py**。LLM 生成的 profile 数据（AUM、创立年、总部）
有出错风险，不直接上线 dashboard。

用网络搜索 + 你已有的知识，针对该基金生成结构化 profile，写到 JSON 文件：

```bash
mkdir -p pending_profiles
```

然后用 Write 工具创建 `pending_profiles/FUNDID.json`：
```json
{
  "id": "FUNDID",
  "founded": "1999",
  "aum": "~$500B",
  "hq": "City, Country",
  "type_en": "Asset Manager / HF / PE / etc.",
  "type_zh": "资产管理 / 对冲基金 / 私募 / 等",
  "desc_zh": "中文一两句简介，重点说投资风格、起源、特色（如何区别于其他基金）。",
  "notable_en": "1-2 sentence English: notable fact / track record / signature strategy",
  "notable_zh": "中文 1-2 句：代表事迹 / 业绩记录 / 招牌策略",
  "_generated_at": "ISO timestamp",
  "_confidence_notes": "Areas where you're less sure (e.g., 'AUM may be outdated as of Q3 2024')"
}
```

数字（AUM、创立年）若不确定就**写不确定的范围**（"~$1-2T"）+ 在 `_confidence_notes`
里说明。**不要瞎编精确数字。**

### Phase 5 — 跑 contract test

```bash
cd /home/ubuntu/hedge-fund-research
python3 -m pytest tests/ -q 2>&1 | tail -10
```

必须全部通过（应该 303 passed → 304 不会变，没加测试只是 wiring）。

**若任何 test 失败**：
```bash
git checkout config/sources.json publish.py fetch_content.py
rm -f pending_profiles/FUNDID.json
```
回滚后将该基金记到 `logs/auto-promote-history.jsonl`（见 Phase 6），outcome=`"failed"`，
继续下一个目标。

### Phase 6 — Commit + push + 写日志

成功路径：
```bash
git add config/sources.json publish.py fetch_content.py pending_profiles/FUNDID.json
git commit -m "feat: auto-promote FUND_NAME to production sources

Trial passed — wired up sources.json + BADGE_COLORS + CONTENT_FETCHERS.
_FUND_PROFILES draft written to pending_profiles/FUNDID.json (manual review needed).

Auto-promote agent run 2026-XX-XX."
git push origin main
```

附加 `logs/auto-promote-history.jsonl`（一行 JSON）：
```python
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

BJT = timezone(timedelta(hours=8))
log = Path("logs/auto-promote-history.jsonl")
log.parent.mkdir(exist_ok=True)
entry = {
    "date": datetime.now(BJT).strftime("%Y-%m-%d"),
    "timestamp": datetime.now(BJT).isoformat(),
    "id": "FUNDID",
    "name": "FUND_NAME",
    "outcome": "promoted",  # or "failed" or "deferred"
    "notes": "Wired sources.json + BADGE + CONTENT_FETCHERS. _FUND_PROFILES draft pending review.",
    "commit": "<git rev-parse HEAD output>"
}
with log.open("a") as f:
    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
```

`outcome` 取值：
- `"promoted"` — 全套接入成功 + commit + push
- `"failed"` — test 失败回滚（记下哪步失败）
- `"deferred"` — `has_fetcher: false` 跳过（等 fetcher-synthesis）

## 规则

- **绝对不修改** `publish.py:_FUND_PROFILES` dict（profile 草稿只写到 pending_profiles/）
- **绝对不修改**已存在的 `_fetch_content_*` 函数（只新增）
- 每次 session 最多处理 **2 个基金**
- 任何 contract test 失败 → 立即回滚（`git checkout` 已修改的文件 + 删 pending_profiles 草稿）
- commit 前必须 `python3 -c "import fetch_content; import publish"` 验证语法
- commit 后必须 push（不要留本地 commit）
- 即使是 `deferred` 也要写一行到 `auto-promote-history.jsonl`（带 outcome="deferred" + 跳过原因）
