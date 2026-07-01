# GMIA 正文抓取补齐：cohen-steers + principal-am（设计）

- 日期：2026-07-01
- 状态：已确认设计，待转 implementation plan
- 关联：`2026-05-14-trial-synthesis-priority.md`（trial 是质量唯一守门员）、fetcher-synthesis 停摆调查（`memory/hedge-fund-research.md`）

## 背景与问题

2026-07-01 的 fetcher-synthesis 诊断跑为 `cohen-steers` 与 `principal-am` 写出了 article/list fetcher，两者状态从 `inaccessible` 进到 `visitable`（提交 `066a6b6` / `2d4f0cb` 已推 origin/main）。按 GMIA pipeline，`visitable` 候选下一步由 `gmia-trial-manager.py` 拾取做 7 天 double-gate trial，PASS → `promoted` → 生产。

但两个基金在拿到列表 fetcher 之后，**正文抓取**这一层仍有缺口，导致它们无法顺利走通 trial → 生产：

- **trial 质量门**（`sample_article_quality`）用通用 httpx 提取器 `_extract_article_text` 抓正文喂 Haiku 评分，**不经过** `CONTENT_FETCHERS`（已用 import 检查确认 trial-manager 不引用 fetch_content）。
- **生产 stage 2**（`fetch_content.py` `main()`）只处理 `source_id in CONTENT_FETCHERS` 的文章（约 line 1502 的过滤），**没有通用兜底**——没有 per-fund content fetcher 的 promoted 源，正文根本不进抓取队列。

## 已验证的事实（实测，非假设）

- `cohen-steers` 文章正文页 httpx GET → **403 Forbidden**（Cloudflare）→ `_extract_article_text` 返回 `None`。
- `principal-am` 文章正文页 httpx GET → **200 OK / 3000 字可提取**（列表页才是 SPA/Coveo，详情页是普通 HTML）。
- 当前 trial queue（`get_trial_queue`）= `['principal-am', 'cohen-steers']`，`active_trials`=1（goehring-rozencwajg），`MAX_CONCURRENT_TRIALS`=3 → 2 个空位，两者下次 trial-manager 运行即入 trial。
- `_is_likely_js_only(url)` 对**非 200**（含 403）直接返回 `False`（见其 docstring 的"HTTP non-200 → return False"）。故"只在 JS-shell 时兜底"的写法救不了 cohen 的 403。
- `fetch_articles._get_playwright_page(url, wait_selector=..., wait_until=..., ...)` 是既有可复用 Playwright helper，返回页面 HTML。
- 常量：`SAMPLE_SIZE=3`、`MIN_DAYS_WITH_ARTICLES=4`；content fetcher 约定返回 `(content_path, status)` 或 `None`。

## 目标

1. `cohen-steers` 能通过 trial 质量门（质量采样能拿到正文喂 Haiku）。
2. `cohen-steers`、`principal-am` 被 promoted 后，生产 stage 2 能抓到各自正文。
3. 不破坏 541 个现有测试；正常源（httpx 能抓）不引入任何 Playwright 开销。

## 非目标（YAGNI）

- 不全局改造 `_extract_article_text`（不给通用提取器无差别加 Playwright fallback）。
- 不改 `principal-am` 的 trial 路径（它 httpx 已能抓正文，trial 质量门无需改动）。
- 不动 trial-manager 的数量门、评分阈值、并发/排队等其他逻辑。
- 不新增跨 `fetch_content.py` ↔ `gmia-trial-manager.py` 的公共模块（两处各自复用既有 `_get_playwright_page` + 本文件内的选择器逻辑即可）。

## 设计

### 改动① — cohen 过 trial 质量门（`gmia-trial-manager.py`）

**触发策略（已选定）：整批 httpx 全失败才 Playwright 兜底。**

- 在 `sample_article_quality` 的 Step 2 之后、现有 `if not article_texts:` 分支处插入兜底：当 httpx 一篇正文都没抓到时，遍历前 `min(SAMPLE_SIZE * 2, len(links))` 篇 link，用 Playwright 逐篇抓正文，累计到 `SAMPLE_SIZE` 篇即停；仍抓不到才返回原有 `"Could not extract text from any article"` 错误。
- 新增 helper `_extract_article_text_playwright(url) -> str | None`：调用 `fetch_articles._get_playwright_page(url)` 拿 HTML，交给共用的选择器提取逻辑。
- 重构 `_extract_article_text`：把"清洗 nav/footer + 按选择器从 soup 提取正文"的部分抽成 `_extract_body_from_soup(soup) -> str | None`，httpx 版与 Playwright 版共用，避免选择器逻辑重复。httpx 版行为对既有调用保持字节级不变（重构不改行为）。

**性能边界**：正常源（httpx 首轮就抓到）永不进入兜底分支 → 零 Playwright 开销。cohen 这类整批失败的源，兜底起 ≤ `SAMPLE_SIZE*2`(=6) 次 Chromium（每次含 CF 挑战 ~10–30s，合计 ~1–2 分钟），落在 trial wrapper `timeout 960s` 内。

### 改动② — cohen 生产抓正文（`fetch_content.py`）

- 新增 `_fetch_content_cohen_steers(article: dict)`：用 Playwright（照 `fetch_content.py` 既有 content fetcher 的 `sync_playwright` 模式，与本文件其他 Playwright fetcher 一致，不跨文件依赖）抓 `article["url"]` 正文页 → 提取正文（≥ 阈值长度）→ 写 content 文件 → 返回 `(content_path, "ok")`；抓不到/太短返回 `None`（由 `main()` 记 `content_status="failed"`）。
- 注册进 `CONTENT_FETCHERS`：`"cohen-steers": _fetch_content_cohen_steers`。
- 照现有 Playwright content fetcher（如 `_fetch_content_goehring_rozencwajg`）的错误处理与存储惯例。

### 改动③ — principal-am 生产抓正文（`fetch_content.py`）

- 新增 `_fetch_content_principal_am(article: dict)`：用 **httpx**（非 Playwright）抓 `article["url"]` → 提取正文 → 写 content 文件 → 返回 `(content_path, "ok")`；失败返回 `None`。
- 注册进 `CONTENT_FETCHERS`：`"principal-am": _fetch_content_principal_am`。
- 照现有 httpx content fetcher（如 `_fetch_content_bridgewater`）模式。

## 测试策略（TDD，先测后码）

- **改动①**
  - `test`：httpx 全失败（mock `_extract_article_text`/httpx 对全部 link 返回 None/403）+ `_get_playwright_page` 返回带正文 HTML → `sample_article_quality` 兜底抓到 ≥1 篇、进入 Haiku 评分路径（mock Haiku）。
  - `test`：正常源 httpx 首轮抓到 → **断言 `_get_playwright_page` 未被调用**（零开销保证）。
  - `test`：`_extract_body_from_soup` 重构等价——对既有样例 HTML，重构前后 httpx 版提取结果一致。
- **改动②③**
  - `test`：content fetcher 成功 → 写出文件、返回 `(path, "ok")`；抓不到 → 返回 `None`。
  - mock 网络层（Playwright/httpx），不打真实站点。
- **回归**：`pytest tests/ -q` 保持 541 passed（新增测试计入总数）。

## 验证标准（Definition of Done）

1. `pytest tests/ -q` 全绿（≥541 + 新增）。
2. 真实实测（非 mock）：
   - cohen：`_extract_article_text_playwright` 对一篇 cohen 文章 URL 返回正文（过 CF）。
   - cohen：`_fetch_content_cohen_steers` 端到端存出正文文件。
   - principal：`_fetch_content_principal_am` 端到端存出正文文件。
3. `python3 -c "import fetch_content; assert {'cohen-steers','principal-am'} <= set(fetch_content.CONTENT_FETCHERS)"` 通过（无语法错误 + 两新键已注册）。
4. 只提交涉及的具体文件（`gmia-trial-manager.py` / `fetch_content.py` / 对应测试），不 `-A`（多会话/cron 并发规矩）。

## 风险与权衡

- **Playwright 兜底拖慢 trial**：已用"整批失败才触发"限定到 CF/JS 全灭的源，正常源零开销；cohen 单源 ~1–2 分钟在 timeout 内。
- **CF 挑战偶发失败**：Playwright 抓 CF 站有瞬时失败可能；兜底遍历前 6 篇取够 3 篇即止，对单篇失败有冗余。若整源某天全失败 → 当天采样 inconclusive（既有行为，非本设计新引入）。
- **重构 `_extract_article_text` 风险**：靠"重构等价"测试守住 httpx 版行为不变。

## 执行顺序

1. 改动①（trial 质量门兜底 + 重构）→ 测试红→绿→重构 → pytest。
2. 改动③（principal httpx content fetcher，简单）→ 红→绿 → pytest。
3. 改动②（cohen Playwright content fetcher）→ 红→绿 → pytest。
4. 真实实测三项 → 提交（具体文件）→ 视用户决定是否 push。
