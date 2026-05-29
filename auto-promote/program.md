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
- `description`：1-2 句**基金本身**的概述（专注市场 / 习惯策略 / 区别于其他基金的特色）。
  ⚠️ **不要描述研究频道/播客本身的内容** —— 那是 notable 字段的事。常见错误：抄了
  "Tactical Take podcast — monthly macro/portfolio strategy series..." 这类
  research-page 的自我介绍，把频道写成了基金。正确风格参考：
  - aqr: "Quantitative investing pioneer. Factor investing, alternatives, tax-aware."
  - oaktree: "Credit/distressed debt specialist. Howard Marks memos are legendary."
  - apollo: "Largest US private credit / alts platform (~$700B). Pioneered ABF
    (asset-backed finance); private equity, real estate, insurance-solutions
    via Athene merger."
  - 错误示例（已被 5-08 commit `1143b99` 修正）：MSCI/Natixis/Apollo 三家初版描述
    全部是研究频道的简介，不是基金本身。
- `notable_authors`：candidate 的 `notable_authors`，没有就 `[]`
- `expected_hostname`：从 url 提取主域名（去掉 www. 与子域）
- `no_publish_dates`（可选）：若 fund_candidates.json 里该字段为 `true`，**原样复制**到 sources.json 条目。这告诉 health probe 跳过 date=None 的 WARN（该源设计上不提供日期）。不存在则不加该字段。

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

**测试（必须强证据，写到 commit message）**：

```bash
python3 - << 'EOF'
import sys; sys.path.insert(0, ".")
from pathlib import Path
import tempfile
from fetch_content import _fetch_content_FUNDID  # 替换 FUNDID
import fetch_articles
src = {"url": "RESEARCH_URL", "id": "FUNDID"}  # 替换
articles = fetch_articles.FETCHERS["FUNDID"](src)
print(f"Got {len(articles)} articles")
if articles:
    art = articles[0]
    with tempfile.TemporaryDirectory() as td:
        result = _fetch_content_FUNDID(art, Path(td))
        print(f"Result: ok={result.get('ok')} chars={result.get('chars', 0)}")
        # 必须把正文前 200 字 preview 打出来，agent 把这段粘到 commit message
        if result.get("ok"):
            text = Path(result["path"]).read_text()
            print(f"--- PREVIEW (first 200 chars) ---\n{text[:200]}\n--- END ---")
EOF
```

**通过标准（4 项硬门，任一失败 → 跳过此基金，不进 Phase 4）**：

1. **抓到 ≥3 文章索引** — `fetch_articles.FETCHERS[id](source)` 返回 ≥3 articles
2. **正文 ≥500 chars**（不是 100）— `_fetch_content_FUNDID` 返回 `ok: True` 且 `chars ≥ 500`
3. **Haiku 质量抽检 relevance ≥ 0.6** — 复用 trial-manager 的质量评估：
   ```bash
   python3 - << 'EOF'
   import sys; sys.path.insert(0, ".")
   import importlib.util
   spec = importlib.util.spec_from_file_location("tm", "gmia-trial-manager.py")
   tm = importlib.util.module_from_spec(spec)
   spec.loader.exec_module(tm)
   url = "RESEARCH_URL"  # 替换
   result = tm.sample_article_quality(url)
   print(f"sampled={result['sampled']} avg={result.get('avg_score',0):.2f}")
   for art in result.get("articles", []):
       print(f"  rel={art['relevance']:.1f} dep={art['depth']:.1f} ext={art['extractable']:.1f} → {art['notes'][:60]}")
   # 通过条件: avg_score >= 0.5 且至少 1 篇 relevance >= 0.6
   EOF
   ```
   Haiku 抽 3 篇，**至少 1 篇 relevance ≥ 0.6** + **avg_score ≥ 0.5**。否则说明抓的不是研究内容（可能是 nav/footer）。
4. **Method 字段 cross-check** — grep 你刚写的 `fetch_<id>` 函数实现，验证与 sources.json 的 `method` 一致：
   ```bash
   if grep -q "_get_playwright_page\|sync_playwright" fetch_articles.py | grep -A 30 "def fetch_FUNDID"; then
       # method 必须是 "playwright"
   elif grep -q "feedparser" ...; then
       # method 必须是 "rss"
   else
       # 默认 method 是 "ssr" (requests/httpx)
   fi
   ```
   实际：读 `fetch_<id>` 函数的源码，确认调用模式与 sources.json `method` 字段一致。**不一致 → 跳过**。

**强证据要求**：commit message 必须包含上述 4 项的实际输出，例如：
```
Live test: 10 articles, content 4823 chars
Haiku quality: avg=0.72 (rel=0.8 dep=0.7 ext=0.7 / rel=0.7 ...)
Method: ssr (requests.get + BeautifulSoup, verified)
Preview: "BlackRock Investment Institute weekly outlook — Markets digest..."
```

⚠️ **自动校验**：agent 退出后 wrapper 会跑 `scripts/validate_promote_commit_msg.py --commit <sha>`
对每条 promoted commit 校验上述 4 项（regex 匹配 + 阈值检查：articles ≥ 3 / chars ≥ 500
/ avg ≥ 0.5 / 至少一篇 rel ≥ 0.6 / preview ≥ 50 chars / method ∈ {playwright, ssr, rss, api}）。
任一项缺失或不达标 → **自动 revert + push + history 写 outcome="failed_validation"**。
不要尝试编造数字应付校验——validator 跟 program 同源，能识别套话；正确做法是真跑测试，
把真实输出粘上来。

若任一硬门未过 → `git checkout fetch_articles.py fetch_content.py config/sources.json publish.py`
回滚，记到 `logs/auto-promote-history.jsonl` outcome=`"failed_phase3"` + reason，继续下一个目标。
不要花时间反复重试调 selector — 偏门站留给 fetcher-synthesis。

加进 `CONTENT_FETCHERS` dict 末尾：
```python
    "FUNDID": _fetch_content_FUNDID,
```

### Phase 4 — _FUND_PROFILES 草稿写到 pending_profiles/

**重要**：这一步**不修改 publish.py**。LLM 生成的 profile 数据（AUM、创立年、总部）
有出错风险，不直接上线 dashboard。

**⚠️ AUM 与 founded 必须用 WebSearch / WebFetch 核实，禁止凭记忆或行业常识填写。**
教训：2026-05-29 auto-graduate 把凭记忆编的 PineBridge `~$190B`（实际 ~$100B）和
Ares `~$450B`（实际 ~$484B）直接上线了。模型参数记忆里的金融数字经常过时或错误。
对每个数字：搜一下 → 找到可信来源（官网 / SEC 文件 / 收购公告 / Wikipedia）→ 把
**来源 URL 写进 `aum_source` / `founded_source` 字段**。validator 现在**硬性要求**这
两个字段含 URL 或域名，没有就不会 auto-graduate（保留转人工 review）。

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
  "desc_zh": "中文一两句简介，重点说投资风格、起源、特色（如何区别于其他基金）。AUM 数字应与 aum 字段一致。",
  "notable_en": "1-2 sentence English: notable fact / track record / signature strategy",
  "notable_zh": "中文 1-2 句：代表事迹 / 业绩记录 / 招牌策略",
  "aum_source": "https://example.com/... (核实 AUM 的来源 URL/域名，必填)",
  "founded_source": "https://example.com/... (核实 founded 的来源 URL/域名，必填)",
  "//desc_zh-rule": "desc_zh 必须描述基金本身（市场覆盖/策略/特色），不能描述研究频道。错误示例：'Tactical Take 月度宏观播客由...主持' — 这是把频道介绍当基金简介。正确示例：'美国最大私募信贷/另类资管之一，1990 年由 Leon Black 等创立。核心业务：私募信贷（ABF 开创者）、私募股权、保险负债驱动投资。' 标志性研究输出（Howard Marks memos / Tactical Take podcast）放 notable_zh，不放 desc_zh。",
  "_generated_at": "ISO timestamp",
  "_confidence_notes": "Areas where you're less sure (e.g., 'AUM as of Q4 2024 per source')"
}
```

规则：
- AUM、founded 必须有来源；`aum` 字段里的金额要和 `desc_zh` 里复述的金额一致（validator 会做 disjoint 检查）。
- 数字若实在核实不到精确值就**写来源给出的范围**（"~$1-2T"）+ `_confidence_notes` 说明；但凭空精确数字 = 严禁。
- 数量级要合理（$10M–$20T 之间），validator 会拦明显离谱的值。

**写完后必须跑 sanity check**：
```bash
python3 scripts/validate_pending_profile.py pending_profiles/FUNDID.json --write-validation
```

退出码：
- `0` → profile 通过所有检查（aum 含货币符号+数字+数量级合理 / founded 4 位数 1700-2026 / hq 含逗号 / desc_zh 50-300 字 / **aum_source + founded_source 含 URL 或域名** / **aum 与 desc_zh 金额一致** / 无 unknown/TBD/reportedly/未知/据传 等高风险标记）
- `1` → 有 issues（验证文件 `<id>.validation.json` 已写出，列出问题）
- `2` → 文件读不了

**判定**：
- 退出码 `0` → 继续 Phase 5 后续
- 退出码 `1` 但**只是 high_risk markers**（unknown/未知/reportedly 等）→ 继续，但 history.jsonl 加 `profile_high_risk: true`（不会 auto-graduate，转人工）
- 退出码 `1` 且**有硬约束违规**（缺 aum_source/founded_source 来源、来源不是 URL/域名、aum 与 desc_zh 金额对不上、aum 数量级离谱、aum 没货币符号、founded 不是 4 位数、missing fields 等）→ **回去补 WebSearch 核实并重写 profile**（最多 1 次重试），仍失败就跳过整个 Phase 5（不写 profile）但继续 Phase 6（仍 commit 代码 wiring，只是没 profile 草稿）

### Phase 4.5 — Auto-graduate profile（条件触发）

⚠️ **仅当 `.validation.json` 同时满足 `ok=true` AND `high_risk=false` 时**触发本步骤；
任一不满足则保留 `pending_profiles/<id>.json` 等人工 review，跳过本步直接进 Phase 5。

```bash
# 读 validation 结果
python3 - << 'EOF'
import json
import subprocess
import sys
from pathlib import Path
val = json.loads(Path("pending_profiles/FUNDID.validation.json").read_text())
if val.get("ok") is True and val.get("high_risk") is False:
    r = subprocess.run(["python3", "scripts/graduate_pending.py", "FUNDID"],
                       capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    sys.exit(r.returncode)
else:
    print(f"[auto-graduate] skipped: ok={val.get('ok')} high_risk={val.get('high_risk')}")
    sys.exit(0)
EOF
```

`graduate_pending.py` 成功后副作用：
- `publish.py` 多了一条 `_FUND_PROFILES["FUNDID"] = {...}` 条目
- `pending_profiles/FUNDID.json` 和 `pending_profiles/FUNDID.validation.json` 被脚本删掉

**判定**：
- 退出码 `0` → auto-graduated；Phase 6 commit 须包含 `publish.py` 改动，history 加 `auto_graduated: true`
- 退出码 `1` (validation 仍 fail) / `2` (已存在) / `3` (pending 文件丢了) / `4` (publish.py 格式异常) → **不重试，不阻塞**；继续 Phase 5（pending 文件保留在原地，等人工 graduate）；history 加 `auto_graduated: false` + `auto_graduate_error: "exit_code=<n>"`
- 跳过（不满足条件）→ history 加 `auto_graduated: false` + `auto_graduate_skip_reason: "validation_ok=false"` 或 `"high_risk=true"`

**Why this gate is conservative**：`graduate_pending.py` 内部只拦 hard_issues，**接受**纯
high_risk markers 通过（设计上是给人工 invoke 留口子，假设人会自己核对）。Auto-mode 下没人核对，
所以本步要在调用前自己加守门——only `ok=true AND high_risk=false` 才放行。

### Phase 5 — 跑 contract test

```bash
cd /home/ubuntu/hedge-fund-research
python3 -m pytest tests/ -q 2>&1 | tail -10
```

必须全部通过（当前套件约 438 passed；wiring 只改 sources.json / publish.py / fetch_content.py，不加测试，数量不会变）。

**若任何 test 失败**：
```bash
git checkout config/sources.json publish.py fetch_content.py
rm -f pending_profiles/FUNDID.json pending_profiles/FUNDID.validation.json
```
回滚后将该基金记到 `logs/auto-promote-history.jsonl`（见 Phase 6），outcome=`"failed"`，
继续下一个目标。`git checkout publish.py` 一次同时撤销 wiring 和 Phase 4.5 的 auto-graduate
插入（如果当时执行了的话）。

### Phase 6 — Commit + push + 写日志

⚠️ **顺序硬约束**：每个基金的 commit + push + history-write **必须是同一原子操作组**，
中间不要插入任何其他工作（不要先 commit/push 两个基金再统一写 history，不要把 history-write
留到 session 末尾）。Wrapper 用 history 文件判断哪些 commit 已成功；如果 agent 在 commit/push
后、history-write 前撞 `--max-turns 120` 被截，wrapper 会把成功工作误判为失败。

正确单基金序列（不可拆）：

```
1. git add … → 2. git commit → 3. git push → 4. 写 history JSONL 一行 → 处理下一个基金
```

成功路径：
```bash
# publish.py 和 sources.json / fetch_content.py 总是要 add。
# pending_profiles/FUNDID.json 仅在未 auto-graduate 时才存在 — 用条件 add。
git add config/sources.json publish.py fetch_content.py
if [ -f pending_profiles/FUNDID.json ]; then
    git add pending_profiles/FUNDID.json pending_profiles/FUNDID.validation.json
fi

# commit message 文案根据是否 auto-graduated 而变：
# - auto_graduated=true → "wired up + _FUND_PROFILES entry graduated automatically"
# - auto_graduated=false → "_FUND_PROFILES draft written to pending_profiles/..."

git commit -m "feat: auto-promote FUND_NAME to production sources

Trial passed — wired up sources.json + BADGE_COLORS + CONTENT_FETCHERS.
[ Phase 4.5 outcome — choose one based on actual result:
  · _FUND_PROFILES entry auto-graduated (validation ok + no high-risk markers).
  · _FUND_PROFILES draft written to pending_profiles/FUNDID.json (manual review needed). ]

Auto-promote agent run 2026-XX-XX."
git push origin main
```

**push 成功后立即**附加 `logs/auto-promote-history.jsonl`（一行 JSON，不可推迟）：
```python
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

BJT = timezone(timedelta(hours=8))
log = Path("logs/auto-promote-history.jsonl")
log.parent.mkdir(exist_ok=True)

# Read profile validation if it still exists. Note: when Phase 4.5 auto-graduates,
# `graduate_pending.py` deletes both pending_profiles/<id>.json and the .validation.json,
# so val_path.exists() will be False — keep profile_val_ok/high_risk as None in that case.
profile_val_ok = None
profile_high_risk = None
val_path = Path(f"pending_profiles/FUNDID.validation.json")
if val_path.exists():
    val = json.loads(val_path.read_text())
    profile_val_ok = val.get("ok")
    profile_high_risk = val.get("high_risk")

# Auto-graduate outcome: True only if Phase 4.5 actually ran graduate_pending.py
# successfully; False otherwise (skipped due to gate, or graduate exit non-zero).
# Set this based on what Phase 4.5 actually did in this run.
auto_graduated = True   # or False — set explicitly per Phase 4.5 outcome
auto_graduate_skip_reason = None  # "validation_ok=false" | "high_risk=true" | "exit_code=N" | None

entry = {
    "date": datetime.now(BJT).strftime("%Y-%m-%d"),
    "timestamp": datetime.now(BJT).isoformat(),
    "id": "FUNDID",
    "name": "FUND_NAME",
    "outcome": "promoted",  # or other (see below)
    "notes": "Wired sources.json + BADGE + CONTENT_FETCHERS. _FUND_PROFILES entry auto-graduated.",
    "commit": "<git rev-parse HEAD output>",
    "live_test": {"articles": 10, "content_chars": 4823, "avg_quality": 0.72},
    "profile_validation_ok": profile_val_ok,
    "profile_high_risk": profile_high_risk,
    "auto_graduated": auto_graduated,
    "auto_graduate_skip_reason": auto_graduate_skip_reason,
}
with log.open("a") as f:
    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
```

`outcome` 取值：
- `"promoted"` — 全套接入成功 + commit + push（Phase 3 4 项硬门通过）
- `"failed_phase3"` — live test / Haiku 质量 / method cross-check 任一失败，回滚
- `"failed_pytest"` — Phase 5 contract test 失败，回滚
- `"failed_validation"` — wrapper 的 commit-msg 校验失败（Phase 6.5）自动 revert
- `"deferred"` — `has_fetcher: false` 跳过（等 fetcher-synthesis）
- `"auto_reverted"` — wrapper 的 post-commit health probe 检测异常自动 revert（见 Phase 7）

## Phase 7 — Wrapper post-commit health probe（自动）

这一步**不是 agent 做**，是 `scripts/wrapper-auto-promote.sh` 在 agent 退出后自动执行：

1. 扫今天 `logs/auto-promote-history.jsonl` 里 outcome=`"promoted"` 的 entries
2. 对每家跑 `python3 scripts/gmia-fetcher-health.py --source <id> --dry-run`
3. 退出码非 0（FAIL/issues）→ `git revert <commit_sha> --no-edit` + push + 邮件告警 + history 写一条 `outcome="auto_reverted"`

这把 health-check 的 3 天延迟压到 30 分钟内（agent 跑完立即验证）。Agent **不需要**自己管这一步。

## 规则

- **绝对不修改** `publish.py:_FUND_PROFILES` dict（profile 草稿只写到 pending_profiles/）
- **绝对不修改**已存在的 `_fetch_content_*` 函数（只新增）
- 每次 session 最多处理 **2 个基金**
- 任何 contract test 失败 → 立即回滚（`git checkout` 已修改的文件 + 删 pending_profiles 草稿）
- commit 前必须 `python3 -c "import fetch_content; import publish"` 验证语法
- commit 后必须 push（不要留本地 commit）
- 即使是 `deferred` 也要写一行到 `auto-promote-history.jsonl`（带 outcome="deferred" + 跳过原因）
