# GMIA 入口发现层设计说明

## 目标

在抓取 hedge fund 官网内容时，很多网站不会直接把高价值内容放在固定、明显、稳定的路径下。常见问题包括：

- research 内容入口命名不统一：`research` / `insights` / `publications` / `perspectives` / `thinking` / `market commentary`
- 真正高价值内容可能藏在二级页、专题页、PDF 下载页、RSS 页、年报/季报栏目页
- 官网首页和导航页经常混杂大量营销、品牌、订阅、招募、免责声明内容
- 同一个站点可能同时存在：
  - 高频文章流
  - white paper / annual report / quarterly letter
  - gated 页面
  - 只有标题索引、没有正文的页面

因此，需要把"找对入口"从主抓取器中拆出来，做成单独的前置层。

---

## 核心原则

### 1. AI 负责发现候选入口，不负责最终判定
AI 的职责是：
- 从官网首页、导航、栏目页中识别可能的高价值入口
- 判断这些入口更像：
  - research index
  - market insights index
  - white paper / report hub
  - annual / quarterly report page
  - low-value marketing page

AI 不应直接决定最终抓取列表，更不应直接绕过规则进入生产抓取。

### 2. 规则负责验证和落地
AI 输出后，必须经过确定性规则验证，包括：
- 域名白名单校验
- URL path 关键词校验
- 页面结构信号校验
- gated / marketing / disclaimer 负信号校验

### 3. 入口发现是低频任务，不是高频任务
主抓取流程应该优先使用已经确认的固定入口。
只有在以下情况才触发入口发现：
- 现有入口抓到 0 条
- 文章数大幅异常下降
- 抓到的页面主要是营销文案
- 正文提取失败率突然升高
- 大量链接不再符合 source host/path 预期
- 页面结构明显漂移

### 4. 入口结果必须可持久化
每个 source 的入口发现结果应该写入本地配置，避免每次重跑都依赖 AI。

---

## 实现范围

### Phase 1（本次实现）
- **Layer 1**: 固定入口抓取层 — `entrypoints.json` 配置 + `fetch_articles.py` 集成
- **Layer 2**: 入口巡检层 — 异常检测 + 规则评分引擎 + `validate_entrypoints.py`
- **`discover_entrypoints.py`**: 规则部分（链接提取、域名校验、路径评分、结构信号、gate 检测），AI 分类留 stub

### Phase 2（后续实现）
- **Layer 3**: AI 候选入口发现 — LLM 对候选页做语义分类和排序
- 半自动更新策略
- 自动触发 discover → validate → 写入配置的闭环

---

## 系统分层

### Layer 1: 固定入口抓取层

使用已知入口执行高频抓取。

**输入：**
- `sources.json` 中的 source 基础配置
- `config/entrypoints.json` 中该 source 已确认的入口配置

**输出：**
- 正常文章候选列表
- 抓取质量指标（new_articles, total_found, gated_count, source_mismatch_count）

**行为：**
- 优先使用 `entrypoints.json` 中的固定入口
- 向后兼容：若 `entrypoints.json` 中无该 source，fallback 到 `sources.json` 的 `url` 字段
- 不调用 AI
- 快速、稳定、低成本

---

### Layer 2: 入口巡检层

当固定入口异常时，触发一次巡检。

**异常条件（任一满足即触发）：**
- `new_articles == 0`（连续 2 次运行）
- `total_articles < historical_floor`（低于历史最低值的 50%）
- `valid_body_ratio < 0.3`（正文提取成功率低于 30%）
- `gated_page_ratio > 0.5`（超过一半是 gated 页面）
- `source_mismatch_count > 3`（域名不匹配超过 3 条）

**巡检目标：**
- 判断当前入口是否失效
- 判断站点是否新增更合适入口
- 给出新的候选入口列表（结构化 JSON）

---

### Layer 3: AI 候选入口发现层（Phase 2 — stub only）

对某个 source 官网进行低频扫描，输出结构化候选入口。

**输入：**
- source name
- homepage URL
- allowed domains
- optional: current entrypoints
- optional: historical examples of good article URLs

**AI 应完成：**
- 识别导航、栏目、落地页中的高价值入口
- 区分：article index / report hub / PDF landing page / RSS feed / marketing / gated
- 为每个候选入口给出用途、置信度、原因

**输出格式：**

```json
{
  "candidate_pages": [
    {
      "url": "https://example.com/research",
      "label": "Research Library",
      "content_type": "research_index",
      "confidence": 0.94,
      "why": [
        "contains article cards",
        "contains dates",
        "contains author names",
        "links to PDF and insight pages"
      ]
    }
  ],
  "rejected_pages": [
    {
      "url": "https://example.com/about",
      "reason": "corporate marketing/about page"
    }
  ]
}
```

> Phase 1 只实现规则部分（链接提取 + 四类评分）。AI 分类接口预留但不调用 LLM。

---

## 验证规则设计

### 1. 域名校验（domain score）

候选入口必须满足：
- hostname 在 source 允许域名内
- 或是允许的子域名

允许：`bridgewater.com`, `www.bridgewater.com`, 显式配置的子域名

拒绝：外链媒体站、CDN 下载页以外的陌生域名、社交媒体页

**评分：** 匹配 = 1.0，子域匹配 = 0.8，不匹配 = 0.0（直接拒绝）

### 2. URL 路径信号评分（path score）

**正向关键词（+权重）：**
- research, insight(s), publication(s), commentary, market-commentary
- white-paper, report(s), quarterly, annual, letters, outlook, papers, library

**负向关键词（-权重）：**
- about, careers, contact, team, leadership, events, podcast, video
- subscribe, login, register

**评分：** 正向命中数 / (正向 + 负向命中数)，无命中 = 0.5（中性）

### 3. 页面结构信号评分（structure score）

**高价值信号（+）：**
- 存在文章卡片列表（多个 `<article>` 或重复结构的 `<div>`）
- 存在日期元素
- 存在作者信息
- 存在 PDF 链接
- 存在 "Read more" / "Download report"
- 存在分页或列表容器

**低价值信号（-）：**
- 只有品牌介绍
- 只有 CTA 按钮
- 只有订阅/注册表单
- 只有合规声明或 cookie 弹窗
- 没有日期、没有文章项、没有下载链接

**评分：** 高价值信号计数 / (高价值 + 低价值信号计数)

### 4. gated / disclaimer 检测（gate penalty）

候选页如果包含以下信号，应降权或拒绝：
- "subscribe to read", "register to continue", "log in to read"
- "for clients only"
- "cookie preferences", "privacy policy", "terms of use"
- 过长免责声明且缺少正文结构

**评分：** 每命中一个 gate 关键词扣 0.15，最多扣至 0.0

### 综合评分

```
final_score = domain_score * 0.2 + path_score * 0.3 + structure_score * 0.3 + (1.0 - gate_penalty) * 0.2
```

- `final_score >= 0.6` → 候选入口
- `final_score >= 0.8` → 高置信度候选
- `final_score < 0.4` → 拒绝

---

## 数据模型

### config/entrypoints.json

```json
{
  "version": 1,
  "sources": {
    "bridgewater": {
      "last_verified_at": "2026-04-01T12:00:00Z",
      "verified_by": "manual",
      "entrypoints": [
        {
          "url": "https://www.bridgewater.com/research-and-insights",
          "content_type": "research_index",
          "confidence": 0.95,
          "active": true
        }
      ],
      "rejected_pages": []
    },
    "man-group": {
      "last_verified_at": "2026-04-01T12:00:00Z",
      "verified_by": "manual",
      "entrypoints": [
        {
          "url": "https://www.man.com/insights",
          "content_type": "research_index",
          "confidence": 0.95,
          "active": true
        }
      ],
      "rejected_pages": []
    }
  }
}
```

### config/inspection_state.json（巡检状态）

```json
{
  "bridgewater": {
    "last_inspected_at": "2026-04-01T12:00:00Z",
    "consecutive_zero_count": 0,
    "last_article_count": 3,
    "last_valid_body_ratio": 0.85,
    "last_gated_ratio": 0.0,
    "last_mismatch_count": 0,
    "alert_triggered": false
  }
}
```

---

## 新增脚本

### 1. discover_entrypoints.py

**职责：**
- 从 homepage 开始抓取 1-2 层内部链接
- 提取导航、footer、主要栏目页链接
- 做规则初筛（域名 + 路径关键词）
- 对候选页做结构信号和 gate 检测
- 输出结构化候选入口 JSON
- `--write` 模式下写回 `entrypoints.json`

**CLI：**
```bash
python3 discover_entrypoints.py --source bridgewater           # dry-run (默认)
python3 discover_entrypoints.py --source bridgewater --write   # 写入配置
python3 discover_entrypoints.py --all                          # 扫描所有 source
```

**Phase 1 行为：** 只做规则评分，不调用 LLM。AI 分类接口预留（`_classify_with_ai()` 返回 None）。

### 2. validate_entrypoints.py

**职责：**
- 对 `entrypoints.json` 中的每个入口做确定性评分
- 输出每个入口的 domain_score / path_score / structure_score / gate_penalty / final_score
- 检测失效入口（HTTP 错误、重定向到不同页面、结构变化）
- `--fix` 模式下自动禁用失效入口

**CLI：**
```bash
python3 validate_entrypoints.py                    # 验证所有
python3 validate_entrypoints.py --source gmo       # 验证单个
python3 validate_entrypoints.py --fix              # 自动禁用失效入口
```

---

## fetch_articles.py 集成策略

`fetch_articles.py` 的改动最小化：

1. 启动时加载 `entrypoints.json`
2. 若 source 在 `entrypoints.json` 中有 active 入口 → 使用该入口 URL
3. 若无 → fallback 到 `sources.json` 的 `url`（向后兼容）
4. 每次 fetch 后记录质量指标到 `inspection_state.json`
5. 若触发异常条件 → 输出警告日志（不在主路径中做入口发现）

---

## 触发策略

### 正常模式
- 使用固定入口
- 不调用 AI
- 成本最低

### 修复模式（手动触发或 cron 定期检查）
1. `validate_entrypoints.py` 检测到失效入口
2. 运行 `discover_entrypoints.py --source <id>` 生成候选报告
3. 人工确认后 `--write` 写入配置
4. （Phase 2）高置信度条件下自动更新

---

## 测试计划

必须覆盖的场景：

| 场景 | 脚本 | 预期 |
|------|------|------|
| 正常 research index 识别 | discover | path_score 高，structure_score 高 |
| marketing/about 页拒绝 | discover | path_score 低，final_score < 0.4 |
| gated 页拒绝 | discover | gate_penalty 高，final_score 低 |
| PDF/report hub 识别 | discover | content_type=report_hub |
| 外域链接拒绝 | discover | domain_score=0，直接拒绝 |
| entrypoints.json 向后兼容 | fetch_articles | 无 entrypoints 时用 sources.json url |
| 入口配置回退逻辑 | fetch_articles | active=false 时跳过该入口 |
| 巡检触发条件 | fetch_articles | consecutive_zero >= 2 输出警告 |
| 评分引擎边界 | validate | 空页面、无链接、纯 JS 页面 |
| entrypoints.json 读写 | discover --write | 原子写入，不破坏已有配置 |
