# GMIA Strategy Tags Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 GMIA 候选基金增加标准化策略标签（12 类），并在发现邮件中展示策略覆盖地图和每个基金的标签徽章。

**Architecture:** 在现有 JSON 配置文件中新增 `strategy_tags` 字段（字符串列表），由 Discovery Agent 在分析时自动填写；邮件报告在现有表格顶部加策略覆盖地图，每行加标签徽章。分类逻辑不影响发现算法，纯展示改动。

**Tech Stack:** Python（邮件生成），JSON（配置存储），Bash（wrapper 脚本），Claude Agent（自动打标）

---

## 标准标签集（12 个，全局一致）

```
fixed_income     private_credit   event_driven     macro
quant            private_equity   real_assets      equity
multi_asset      esg_climate      emerging_markets venture_capital
```

---

## 文件清单

| 文件 | 改动类型 |
|------|---------|
| `config/sources.json` | 修改：6 个活跃来源各加 `strategy_tags` |
| `config/fund_seeds.json` | 修改：18 个种子基金各加 `strategy_tags` |
| `config/fund_candidates.json` | 修改：19 个候选各加 `strategy_tags` |
| `candidate-discovery/program.md` | 修改：Phase 2 加打标指令 |
| `scripts/wrapper-candidate-discovery.sh` | 修改：邮件加覆盖地图 + 标签徽章 |

---

## Task 1: 给 6 个活跃来源加 strategy_tags

**Files:**
- Modify: `config/sources.json`

- [ ] **Step 1: 编辑 sources.json，给每个 source 加 strategy_tags**

打开 `config/sources.json`，在每个 source 对象里加 `strategy_tags` 字段：

```json
// man-group
"strategy_tags": ["macro", "quant", "fixed_income", "multi_asset"]

// bridgewater
"strategy_tags": ["macro", "multi_asset"]

// aqr
"strategy_tags": ["quant", "multi_asset", "equity"]

// gmo
"strategy_tags": ["equity", "multi_asset", "emerging_markets"]

// oaktree
"strategy_tags": ["fixed_income", "private_credit", "event_driven"]

// ark-invest
"strategy_tags": ["equity", "venture_capital"]
```

- [ ] **Step 2: 验证 JSON 格式**

```bash
cd ~/hedge-fund-research
python3 -c "import json; data=json.load(open('config/sources.json')); [print(s['id'], s['strategy_tags']) for s in data['sources']]"
```

期望输出：
```
man-group ['macro', 'quant', 'fixed_income', 'multi_asset']
bridgewater ['macro', 'multi_asset']
aqr ['quant', 'multi_asset', 'equity']
gmo ['equity', 'multi_asset', 'emerging_markets']
oaktree ['fixed_income', 'private_credit', 'event_driven']
ark-invest ['equity', 'venture_capital']
```

- [ ] **Step 3: Commit**

```bash
git add config/sources.json
git commit -m "data(gmia): add strategy_tags to 6 active sources"
```

---

## Task 2: 给 fund_seeds.json 加 strategy_tags

**Files:**
- Modify: `config/fund_seeds.json`

- [ ] **Step 1: 编辑 fund_seeds.json，给每个种子加 strategy_tags**

对照下表，给每个对象加 `strategy_tags`：

```
pimco              → ["fixed_income", "macro", "multi_asset"]
blackstone         → ["private_equity", "real_assets", "private_credit", "multi_asset"]
kkr                → ["private_equity", "real_assets", "private_credit", "macro"]
gsam               → ["macro", "fixed_income", "equity", "multi_asset"]
cambridge-associates → ["private_equity", "venture_capital", "private_credit", "multi_asset"]
wellington         → ["equity", "macro", "fixed_income", "multi_asset", "esg_climate"]
amundi             → ["macro", "fixed_income", "emerging_markets", "multi_asset", "esg_climate"]
jpmam              → ["macro", "equity", "fixed_income", "multi_asset"]
troweprice         → ["equity", "macro", "fixed_income"]
fidelity           → ["equity", "multi_asset", "fixed_income"]
blackrock-institute → ["macro", "equity", "fixed_income", "multi_asset", "esg_climate"]
research-affiliates → ["quant", "equity", "multi_asset"]
schroders          → ["esg_climate", "emerging_markets", "multi_asset", "fixed_income"]
carlyle            → ["private_equity", "real_assets", "fixed_income"]
msci-research      → ["quant", "esg_climate", "multi_asset"]
neuberger-berman   → ["multi_asset", "fixed_income", "private_credit"]
pgim               → ["fixed_income", "private_credit", "multi_asset"]
aberdeen           → ["event_driven", "macro", "fixed_income", "multi_asset"]
```

- [ ] **Step 2: 验证 JSON 格式**

```bash
python3 -c "
import json
data = json.load(open('config/fund_seeds.json'))
missing = [f['id'] for f in data if 'strategy_tags' not in f]
print('Missing strategy_tags:', missing or 'none')
print('Total seeds:', len(data))
"
```

期望输出：
```
Missing strategy_tags: none
Total seeds: 18
```

- [ ] **Step 3: Commit**

```bash
git add config/fund_seeds.json
git commit -m "data(gmia): add strategy_tags to 18 fund seeds"
```

---

## Task 3: 给 fund_candidates.json 加 strategy_tags

**Files:**
- Modify: `config/fund_candidates.json`

- [ ] **Step 1: 编辑 fund_candidates.json，给每个候选加 strategy_tags**

对照下表：

```
pimco              → ["fixed_income", "macro", "multi_asset"]
de-shaw            → ["quant"]
blackstone         → ["private_equity", "real_assets", "private_credit", "multi_asset"]
two-sigma          → ["quant"]
kkr                → ["private_equity", "real_assets", "private_credit", "macro"]
cambridge-associates → ["private_equity", "venture_capital", "private_credit", "multi_asset"]
gsam               → ["macro", "fixed_income", "equity", "multi_asset"]
wellington         → ["equity", "macro", "fixed_income", "multi_asset", "esg_climate"]
amundi             → ["macro", "fixed_income", "emerging_markets", "multi_asset", "esg_climate"]
jpmam              → ["macro", "equity", "fixed_income", "multi_asset"]
troweprice         → ["equity", "macro", "fixed_income"]
fidelity           → ["equity", "multi_asset"]
msci-research      → ["quant", "esg_climate", "multi_asset"]
carlyle            → ["private_equity", "real_assets", "fixed_income"]
research-affiliates → ["quant", "equity", "multi_asset"]
blackrock-institute → ["macro", "equity", "fixed_income", "multi_asset", "esg_climate"]
schroders          → ["esg_climate", "emerging_markets", "multi_asset", "fixed_income"]
pgim               → ["fixed_income", "private_credit", "multi_asset"]
aberdeen           → ["event_driven", "macro", "fixed_income", "multi_asset"]
```

- [ ] **Step 2: 验证**

```bash
python3 -c "
import json
data = json.load(open('config/fund_candidates.json'))
missing = [f['id'] for f in data if 'strategy_tags' not in f]
print('Missing strategy_tags:', missing or 'none')
print('Total candidates:', len(data))
"
```

期望输出：
```
Missing strategy_tags: none
Total candidates: 19
```

- [ ] **Step 3: Commit**

```bash
git add config/fund_candidates.json
git commit -m "data(gmia): add strategy_tags to 19 fund candidates"
```

---

## Task 4: 更新 program.md 加打标指令

**Files:**
- Modify: `candidate-discovery/program.md`

- [ ] **Step 1: 在 Phase 2 的第 4 步（Update fund_candidates.json）里加 strategy_tags 指令**

找到 `program.md` 中以下内容：

```markdown
   - Set `topics` field to a short comma-separated list (e.g., "fixed income, macro, credit")
```

在其**后面**插入：

```markdown
   - Set `strategy_tags` to a JSON array using ONLY tags from this fixed set:
     `fixed_income`, `private_credit`, `event_driven`, `macro`, `quant`,
     `private_equity`, `real_assets`, `equity`, `multi_asset`, `esg_climate`,
     `emerging_markets`, `venture_capital`
     Pick all that apply (1–4 tags typical). Example: `["fixed_income", "macro", "multi_asset"]`
```

- [ ] **Step 2: 验证文件可读**

```bash
grep -A 5 "strategy_tags" candidate-discovery/program.md
```

期望输出包含刚加的说明文字。

- [ ] **Step 3: Commit**

```bash
git add candidate-discovery/program.md
git commit -m "feat(gmia): add strategy_tags tagging instruction to discovery agent"
```

---

## Task 5: 更新邮件——加策略覆盖地图和标签徽章

**Files:**
- Modify: `scripts/wrapper-candidate-discovery.sh`

- [ ] **Step 1: 找到邮件 Python 代码中读取 candidates 的部分**

在 wrapper 脚本里，找到这一行：
```python
candidates = json.load(open(os.path.join(repo_dir, "config/fund_candidates.json")))
```

在其后加载 sources：
```python
sources_data = json.load(open(os.path.join(repo_dir, "config/sources.json")))
active_sources = sources_data.get("sources", [])
```

- [ ] **Step 2: 加策略覆盖地图生成函数**

在 Python 代码块里（`candidates = ...` 之后，`rows = ...` 之前）加入：

```python
# 12 个标准标签
ALL_TAGS = [
    "fixed_income", "private_credit", "event_driven", "macro",
    "quant", "private_equity", "real_assets", "equity",
    "multi_asset", "esg_climate", "emerging_markets", "venture_capital"
]
TAG_LABELS = {
    "fixed_income": "Fixed Income", "private_credit": "Private Credit",
    "event_driven": "Event Driven", "macro": "Macro",
    "quant": "Quant", "private_equity": "Private Equity",
    "real_assets": "Real Assets", "equity": "Equity",
    "multi_asset": "Multi Asset", "esg_climate": "ESG/Climate",
    "emerging_markets": "Emerging Mkts", "venture_capital": "Venture Capital"
}

# 统计覆盖：活跃来源 + validated 候选
tag_counts = {t: 0 for t in ALL_TAGS}
for s in active_sources:
    for t in s.get("strategy_tags", []):
        if t in tag_counts:
            tag_counts[t] += 1
for c in candidates:
    if c.get("status") == "validated":
        for t in c.get("strategy_tags", []):
            if t in tag_counts:
                tag_counts[t] += 1

# 生成覆盖地图 HTML（4列布局）
def coverage_bar(count):
    filled = min(count, 5)
    return "█" * filled + "░" * (5 - filled)

map_cells = ""
for i, tag in enumerate(ALL_TAGS):
    count = tag_counts[tag]
    color = "#22863a" if count > 0 else "#cb2431"
    bar = coverage_bar(count)
    label = TAG_LABELS[tag]
    map_cells += (
        f'<td style="padding:4px 8px;width:25%">'
        f'<span style="color:{color};font-family:monospace;font-size:11px">{bar}</span> '
        f'<span style="font-size:12px">{label}</span> '
        f'<span style="color:#586069;font-size:11px">({count})</span>'
        f'</td>'
    )
    if (i + 1) % 4 == 0:
        map_cells += "</tr><tr>"

coverage_map_html = f"""
<div style="margin:0 0 16px">
<div style="font-weight:600;font-size:13px;margin-bottom:6px">策略覆盖地图 — 活跃来源 + 已验证候选</div>
<table style="width:100%;border-collapse:collapse;background:#f6f8fa;border:1px solid #e1e4e8;border-radius:4px">
<tr>{map_cells}</tr>
</table>
</div>
"""
```

- [ ] **Step 3: 在每行基金数据加标签徽章**

找到生成表格行的代码，找到 `topics` 那列：

```python
f'<td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;color:#586069">{topics}</td>'
```

在其**后面**加一列标签徽章：

```python
tags = c.get("strategy_tags", [])
tag_badges = " ".join(
    f'<span style="background:#ddf4ff;color:#0969da;border-radius:3px;padding:1px 5px;font-size:10px;margin-right:2px">{t}</span>'
    for t in tags
)
# 在表格行里加：
f'<td style="padding:4px 6px;border-bottom:1px solid #eee">{tag_badges}</td>'
```

同时在表头 `<tr>` 里加：
```python
'<th style="text-align:left;padding:6px 8px;border-bottom:2px solid #e1e4e8">Tags</th>'
```

- [ ] **Step 4: 把 coverage_map_html 插入邮件 body**

找到 `html = f"""..."""` 里的表格开头，在 `<table ...>` 之前插入：

```python
{coverage_map_html}
```

- [ ] **Step 5: 验证脚本语法**

```bash
bash -n scripts/wrapper-candidate-discovery.sh && echo "syntax OK"
```

期望输出：`syntax OK`

- [ ] **Step 6: 手动触发一次发送测试邮件验证显示效果**

```bash
source ~/.stock-monitor.env
export MAIL_TO SMTP_USER SMTP_PASS
# 单独运行邮件发送部分（wrapper 脚本已有此逻辑，直接运行）
bash scripts/wrapper-candidate-discovery.sh 2>&1 | tail -5
```

检查邮件收到后：
- 顶部有 12 格覆盖地图，有数字和进度条
- 表格每行有蓝色标签徽章
- 无 Python 报错

- [ ] **Step 7: Commit**

```bash
git add scripts/wrapper-candidate-discovery.sh
git commit -m "feat(gmia): add strategy coverage map and tag badges to discovery email"
```

---

## Task 6: 最终验证

- [ ] **Step 1: 检查所有 JSON 文件无格式问题**

```bash
cd ~/hedge-fund-research
python3 -c "
import json
for f in ['config/sources.json','config/fund_seeds.json','config/fund_candidates.json']:
    json.load(open(f))
    print(f, 'OK')
"
```

期望输出：
```
config/sources.json OK
config/fund_seeds.json OK
config/fund_candidates.json OK
```

- [ ] **Step 2: 检查 program.md 包含 strategy_tags 说明**

```bash
grep -c "strategy_tags" candidate-discovery/program.md
```

期望输出：`1`（或更多）

- [ ] **Step 3: 最终 push**

```bash
git push
```

- [ ] **Step 4: 更新 memory 文件**

在 `~/.claude/projects/-home-ubuntu/memory/hedge-fund-research.md` 里记录此次改动：strategy_tags 字段已加入所有配置，12 标签体系，邮件有覆盖地图。
