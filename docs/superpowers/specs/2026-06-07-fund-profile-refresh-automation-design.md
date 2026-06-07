# 月度基金 Profile 自动审计/刷新 — 设计文档

- **日期**: 2026-06-07（BJT 06-08 凌晨定稿）
- **状态**: 设计已批准（方案 A），待 writing-plans 出实施计划
- **仓库**: `hedge-fund-research`
- **背景会话**: 来源页 `hedge-fund-research.html` 28 基金 profile 人工审计（commit `7c57ecb`）后，用户提出将该审计「自动化 / LLM 化、每月一次」。

---

## 1. 目标与非目标

### 目标
- 每月自动核验 28 个生产基金 profile 的**时效字段**，使页面 AUM / 公司状态保持新鲜、准确。
- 闸门通过的改动**自动应用并发布**；闸门拦下的转**人工告警邮件**。
- 最大化复用已有的 GMIA auto-promote 机制（证据闸门、毕业器、wrapper auth 模式）。

### 非目标（YAGNI）
- **不**重新核验静态事实（创始人、成立年份、历史并购）——这些已一次性人工审过，几乎不变；月月重查浪费 token 且 LLM 易误伤（本次会话 subagent 即把 Bridgewater All-Weather -3.9% 误判为硬错）。
- **不**做整档重生成（会改坏已审措辞 / 抹掉软化）。
- **不**触碰文章内容、fetcher 逻辑、trial/discovery 流水线。

---

## 2. 关键背景（来自代码核实）

- 页面由 `publish.py` 的 `_FUND_PROFILES` dict 生成（**非手写 HTML**）。每只基金字段：`founded / aum / hq / type_en / type_zh / desc_zh / notable_en / notable_zh`。
- 英文短描述 `desc_en` 来自 `config/sources.json` 的 `description` 字段，**部分内嵌 AUM 数字**（9 只）。→ **改 AUM 必同时改这两处，否则 sc-stat 与 sc-desc 矛盾**（本次会话踩过的坑）。
- `publish.py` 发布流程：写 `/var/www/overview/hedge-fund-research.html` → 自动 sync 回 `~/docs-site/pages/` 并 git commit+push（`main()` 无条件执行，dry-run 须走 `import generate_html` 绕开）。
- **已有可复用资产**：
  - `scripts/validate_pending_profile.py::validate_profile()` — 证据闸门：要求 `aum_source`/`founded_source` 含 URL/域名、AUM 量级 $10M–$20T、aum↔desc_zh 货币数字自洽、无不确定词。
  - `scripts/graduate_pending.py` — 把 `pending_profiles/<id>.json` 精确插入 `_FUND_PROFILES`（花括号配平），**但拒绝覆盖已存在条目**（为新基金设计）。
  - `auto-promote/program.md` Phase 4 — agent 必须 WebSearch 核实 AUM+founded 并记录来源 URL。
  - wrapper auth 模式（`wrapper-auto-promote.sh` 等）：`unset ANTHROPIC_API_KEY` → `claude --print` 走 Max Plan OAuth → 跑完恢复；`CLAUDE_BIN=~/.npm-global/bin/claude`（2.x）。
- **`13c51f0` 教训**：Phase 4.5 早期把 LLM 编造的 AUM 直推生产（PineBridge ~$190B、Ares ~$450B 均错），因此加了证据闸门。本 feature 的全自动**必须**建立在闸门之上。

---

## 3. 架构（方案 A：定向刷新 + 复用证据闸门）

```
cron (月度) ─► cron-wrapper.sh ─► scripts/wrapper-profile-refresh.sh
   │  (unset ANTHROPIC_API_KEY / lock / timeout / trap cleanup)
   ▼
echo PROMPT | claude --print   ◄── auto-promote/refresh-program.md（刷新模式指令）
   │  逐只读 _FUND_PROFILES → WebSearch 核 AUM(+source) + 扫并购/退市/改名
   ▼
pending_profiles/<id>.refresh.json   （仅在有改动时产出；含 change_log + sources）
   │
   ▼  scripts/validate_pending_profile.py（复用 + 扩展 change_log 校验）
   ├─ 闸门失败 ─────────────────────────────► 进告警邮件（人工）
   └─ 闸门通过
        │  文本/事件类改动 ─► 对抗复核 agent（claude --print 证伪 + 核来源）
        │       └─ 被驳回 ─────────────────► 进告警邮件（人工）
        ▼ 通过
   scripts/apply_refresh.py（新增：更新现有条目，仅改 change_log 列出字段）
        │  同步 sources.json description 内嵌 AUM（若该字段在 change_log）
        ▼
   python3 publish.py  （重生成 /var/www + sync docs-site 自动 commit/push）
        ▼
   git add publish.py config/sources.json && commit && push  （hedge-fund-research）
        ▼
   scripts/send_refresh_summary.py（HTML 邮件：已自动应用 / 转人工 / 30d 历史）
```

---

## 4. 组件清单（★=新增，其余复用）

| # | 组件 | 说明 |
|---|------|------|
| 1 | ★`scripts/wrapper-profile-refresh.sh` | cron wrapper。抄 GMIA wrapper：`unset ANTHROPIC_API_KEY`→走 Max Plan→恢复；`CLAUDE_BIN` npm-global 2.x；`timeout --kill-after`；lock 文件；`trap cleanup EXIT`（`jobs -p` 杀孙进程 + 恢复 API key）。`set -uo pipefail`（邮件步 `|| true`）。 |
| 2 | ★`auto-promote/refresh-program.md` | agent「刷新模式」指令。逐只读现有 profile → WebSearch 核 AUM 记 `aum_source` + 扫公司事件（并购/退市/改名）→ 仅对**有据可改**字段产出 `<id>.refresh.json` + `change_log`。**最小 diff，禁止重写稳定描述/静态事实**。 |
| 3 | `scripts/validate_pending_profile.py` | 复用证据闸门。**扩展**：校验 `change_log` 存在且每个改动字段带 source；文本字段 diff 比例守卫。 |
| 4 | ★`scripts/apply_refresh.py` | 「更新现有条目」路径（补 `graduate_pending` 的覆盖缺口）。闸门过后只替换 change_log 列出的字段；若改 `aum` 且该基金 sources.json description 内嵌 AUM，则同步替换。幂等、最小 diff。 |
| 5 | ★ 对抗复核 | 文本/事件类改动：另起 `claude --print` 拿 old→new diff「证伪」+核来源；被驳回不应用、转告警。 |
| 6 | ★`scripts/send_refresh_summary.py` | 抄 `send_synthesis_summary.py`。HTML 邮件两栏【已自动应用：旧→新+来源】【转人工：闸门失败/口径模糊/事件需改写】+ 30d 历史。notification only，不影响 EXIT_CODE。 |
| 7 | ★`tests/test_profile_static_facts.py` | 静态事实守卫：断言已审正确 token 不被改回（`Eyk van Otterloo` 在、`Nicholas Otis`/`Hudson River Trading` 永不复现）。 |

---

## 5. 数据 / change_log schema

`pending_profiles/<id>.refresh.json`：

```json
{
  "id": "apollo-global-management",
  "aum": "~$1.03T",
  "aum_source": "https://www.sec.gov/.../agmearningsrelease1q2026.htm",
  "change_log": [
    {"field": "aum", "old": "~$700B", "new": "~$1.03T",
     "reason": "Q1 2026 8-K total AUM $1.026T", "source": "https://sec.gov/..."}
  ]
}
```

- 仅列**实际改动**的字段（最小 diff）。
- 每条 change_log 必须有 `source`（URL/域名），否则闸门硬失败。
- `apply_refresh.py` **只**应用 change_log 中列出的字段；其余字段原样保留 → 静态事实天然不动。

---

## 6. 静态事实保护（红线）

1. 最小 diff：agent 只动有据可改的时效字段。
2. change_log 白名单：`apply_refresh.py` 只改列出的字段。
3. 文本 diff 守卫：`desc/notable` 若无对应公司事件理由却大幅变动 → 拒绝并告警。
4. 静态事实守卫测试（组件 7）：CI/发布前断言关键 token 不被改回。

---

## 7. 错误处理

- wrapper `set -uo pipefail`（非 `-e`）；邮件步 `|| true`。`trap cleanup EXIT` 恢复 `ANTHROPIC_API_KEY` + 杀 `jobs -p`。
- agent exit code 不可信（撞 `--max-turns`=1）→ 按产出文件 reconcile（抄现有 wrapper）。
- `publish.py` 失败 → 不发「成功」邮件。
- git 并发：只 `git add` 具体文件（publish.py / config/sources.json）；提交前重查 status + ahead/behind。
- docs-site：发布前确保仓库干净且 up-to-date（`publish.py` 内部 sync 会 add 具体文件 + push）。

---

## 8. 认证与调度

- **形态**：cron job 启动 headless `claude --print`（动脑）；wrapper（护栏）。
- **计费**：`unset ANTHROPIC_API_KEY` → Claude Code 走 **Max Plan OAuth 订阅（零 API 计费）** → 跑完恢复。逐字沿用 `wrapper-auto-promote.sh:61-69` 模式。
- **binary**：`CLAUDE_BIN=/home/ubuntu/.npm-global/bin/claude`（2.x，支持 `--print`），避开 cron PATH 上的 `/usr/bin/claude` 1.0.65。
- **调度**：每月 1 号 05:00 BJT（= 前一日 21:00 UTC），`cron-wrapper.sh` 包裹，cron name 按 UTC 命名。

---

## 9. 分阶段上线（关键稳妥措施）

1. **Phase 0 — dry-run**：`--dry-run` 只产草稿 + 验证 + 预览 diff，不写 publish.py 不提交。
2. **Phase 1 — 告警模式（头 1–2 个月）**：产草稿 + 发邮件，**不自动应用**。人工核对 agent 质量/口径。
3. **Phase 2 — 自动应用**：确认稳定后翻开开关，闸门通过即自动改+发布。

鉴于全自动 + `13c51f0`（PineBridge/Ares）历史，Phase 1 过渡**必须保留**。

---

## 10. 测试

- 单元：`apply_refresh`（更新现有条目 / 最小 diff / 幂等 / 缺字段拒绝 / sources.json AUM 同步）。
- 扩展 `validate_pending_profile`：change_log 校验、文本 diff 守卫。
- 静态事实守卫测试。
- 邮件渲染测试（抄 synthesis summary 测试）。
- dry-run 端到端（mock claude 输出）。

---

## 11. 待定 / 风险

- **公司事件检测精度**：并购/退市/改名靠开放式 WebSearch，召回/精度不稳 → Phase 1 重点观察；可考虑限定权威来源域名。
- **GSAM 等口径基金**：总 AUS vs 纯资管 vs 另类口径易混 → agent 指令需明确「沿用现有口径」并要求来源页同口径。
- **月度噪声**：上市另类资管 AUM 多在季度 8-K 才更新，月度可能多数无改动 → 可接受（事件随时可发生）；若噪声大可降为季度。
- **对抗复核成本**：每个文本改动一次额外 agent 调用 → 用 Max Plan 无 API 计费，但占 max-turns 预算。
