# 正文获取失败案例库（Content failure casebook）

> 目的：把「文章抓不到正文 / 摘要不该发布」的每一类原因贴上标签，留下真实案例，供以后排查同类问题时对照，并与 `scripts/failure_stats.py` 的统计口径一致。
> 标签的定义在 `failure_labels.py`（运行时由证据自动判定）；本文件记录**标签背后的真实案例**。
> 维护规则：修一个新的抓取问题，就在这里加一条案例，写清「症状 / 根因 / 修法（提交号）/ 为什么当时没被发现」。提交号必须从 `git log` 查出，不要凭记忆写。

## 1. 运行时标签（每篇文章自动判定）

### 第 2 阶段：抓取正文失败 → `content_failure.label`

| 标签 | 含义 | 判定依据 |
|---|---|---|
| `blocked_by_bot_protection` | 网站防护拦截 | 403/429，或页面含 Cloudflare 验证页标记（含 200 返回的验证页） |
| `page_gone` | 页面已删除或下架 | 404/410；同域跳到上级路径（栏目页）；跨域跳到新站首页等浅路径 |
| `media_without_text` | 视频/播客页，没有可读正文 | 正文太短 + 页面有播放器（brightcoveVideoId、libsyn embed、data-video 等；**页脚的 YouTube 频道链接不算**） |
| `selector_miss` | 抓取规则未匹配 | `_normalize_html` 走了 fallback 或整页 |
| `body_too_short` | 规则匹配到了但正文太短 | 以上都不是 |
| `body_rendered_client_side` | 正文由浏览器脚本渲染 | 抓取器显式提示（gsam：HTML 无正文且无 API 简介） |
| `pdf_not_usable` | PDF 读不出或不是这篇 | 抓取器显式提示（标题词重合 <75%、PDF 响应无效） |
| `fetch_error` | 网络/超时/5xx/程序异常 | 5xx、异常、日志中的 timeout/failed 等 |

### 第 3 阶段：AI 拒绝摘要 → `analysis_label`

`title_only` · `duplicate_body` · `grounding_failed` · `wrong_document` · `chart_notes_only` · `navigation_or_login_wall` · `disclaimer_only` · `teaser_only` · `other_declined`（含义见 `failure_labels.ANALYSIS_DECLINE_LABELS`）

## 2. 流水线缺陷标签（让「能救的救不回」的系统问题）

这些不是单篇文章的状态，而是流水线自身的缺陷类型，用于归档案例：

| 标签 | 含义 |
|---|---|
| `pipeline:silent_fallback` | 规则失效后悄悄退回整页/通用容器，照样记成功 |
| `pipeline:wrong_link_choice` | 页面有多个链接/PDF，程序选错了（选了第一个、选了页脚） |
| `pipeline:id_from_url` | 用网址算文章编号，网站改网址/复用网址时出错 |
| `pipeline:field_dropped` | 列表阶段拿到的字段在保存时被丢，下游兜底逻辑变成死代码 |
| `pipeline:never_retried` | 永久失败是终点，代码修好后旧文章不会重试 |
| `pipeline:fix_not_shared` | 修了公共函数，但各源自写的代码没用它 |
| `pipeline:no_decline_path` | AI 没有「拒绝」出口，只能硬写摘要 |

## 3. 案例（2026-09-11 ～ 09-15）

| 日期 | 源 | 症状 | 标签 | 根因 | 修法 | 为什么当时没发现 |
|---|---|---|---|---|---|---|
| 09-11 | 约 35 个源 | 正文里加粗/链接处单词粘连（`by75bpinSeptember`），755/1400 篇 | `pipeline:fix_not_shared`（后续） | `get_text(strip=True)` 无分隔符 | `c149894` | 文字「看起来有内容」，长度检查全过 |
| 09-14 | gsam/robeco/de-shaw/metlife | 同上，公共函数修了但这 4 个没用 | `pipeline:fix_not_shared` | 自写段落循环 | `00a0c55`（+ 结构测试：`get_text` 必须带分隔符） | 只修了公共函数，没排查自写路径 |
| 09-13 | 7 个源 | 存下的「正文」是导航栏、cookie 面板、其他文章列表 | `selector_miss` / `pipeline:silent_fallback` | 网站改版，选择器不匹配，退回 `<main>`/整页 | `203947b`（告警）`0cb6f07` `e906481`（修选择器）`494bd04`（整页不再算成功） | fallback 结果长度 >100 字，被当作成功 |
| 09-13 | acadian-asset | 9/15 篇只有 480 字「其他 Quick Take 标题列表」 | `selector_miss` | Quick Take 用 `short-form__main` 新布局 | `0cb6f07` | 同上 |
| 09-13 | research-affiliates | 改名 Syzygy 后正文全是导航 | `selector_miss` / `body_rendered_client_side` | Next.js 正文在 `self.__next_f.push` 载荷里 | `0cb6f07` | 同上 |
| 09-13 | verdad-capital | 正文只有语言选择列表 | `selector_miss` | Mailchimp 模板把正文放进 `#templateFooter`，被当页脚删掉 | `0cb6f07` | 同上 |
| 09-13 | 全库 | AI 凭标题编摘要（「作者可能…」），metlife 357 字图表注释→完整看多论点 | `grounding_failed` / `pipeline:no_decline_path` | 提示词无拒绝出口；元数据提示词鼓励凭标题分析 | `e10d923` `027aaf0` `d905f85` | 编造摘要可以不带任何猜测词 |
| 09-14 | lazard-am | 26 期 Behind the Headlines 只有介绍+免责声明 | `disclaimer_only`（结果）/ `selector_miss`（根因） | 每周要点在 `<li>`，只取 `<p>` | `06e658e` | AI 正确拒绝，但原因无人看 |
| 09-14 | lazard-am | 15 篇重复存储 | `pipeline:id_from_url` | 列表页间歇输出 AEM 内部路径 `/content/lam/...html` | `06e658e` `5621d8d` | 两条都有摘要，页面只是多一行 |
| 09-14 | 7 个源 | 同一篇文章改网址后再存一遍（metlife/mfs/brookfield/rothschild 改 slug、man `%20`、ares 大小写、apollo 双栏目） | `pipeline:id_from_url` | 编号 = hash(URL) | `8cacc65`（标题+日期去重）`a8d9c6f`（cohen 按受众再发） | 同上 |
| 09-14 | oaktree | 3 篇备忘录存成被引用的旧备忘录 | `wrong_document` / `pipeline:wrong_link_choice` | 已找到 `openPDF` 英文 PDF，又被「页面第一个 .pdf 链接」覆盖 | `61585c6` | 摘要忠实于错的正文，核对拦不住 |
| 09-14 | gmo | 视频页存成员工医保税表 | `wrong_document` / `pipeline:wrong_link_choice` | 取页面第一个 .pdf = 导航菜单链接 | `61585c6` | 同上 |
| 09-14 | 全库 14 组 | 两篇不同文章正文完全相同 | `duplicate_body` | 上两条 + 网站两篇挂同一 PDF | `9e31e60` | 每篇单独看都「正常」 |
| 09-14 | franklin/wellington/gsam/mfs | 新一期被静默丢弃（wellington 月报停在 3 月） | `pipeline:id_from_url` | 专栏页/月报每期复用同一网址 | `30d436b` | 丢弃不产生任何记录 |
| 09-14 | franklin-templeton | 要点在 `<li>`，4 篇只剩引言 | `selector_miss` | 只取 `<p>` | `30d436b` | AI 拒绝，原因无人看 |
| 09-14 | ARK | 38 篇只有标题 | `blocked_by_bot_protection`（28）/ `selector_miss`（10 个视频简介） | Cloudflare 拦截；视频简介在 `.single__content .wysiwyg` | `fa08959`（视频）；拦截部分按用户决定保持现状 | 回退到「仅标题」看起来是正常降级 |
| 09-15 | metlife-im | 10 篇季度报告/养老金报告没有正文 | `body_too_short`（表象）→ 正文在 PDF | 「Download PDF」按钮里才是正文 | `4769f9c`（标题词重合 ≥75%） | 原设计刻意不抓简介，但没去读 PDF |
| 09-15 | matthews-asia | 1 篇观点文章正文在 PDF | 同上 | 「Read Now」PDF | `6e972dd` | 与 13 个视频页一起被判永久失败 |
| 09-15 | apollo | 3 篇白皮书/报告没有正文 | `body_too_short`（表象）→ 正文在 PDF | 下载按钮是自定义元素 `<acl-apollo-button>` | `76bb8c8` | 与节目页一起被判永久失败 |
| 09-15 | de-shaw、oaktree | 链接本身是 PDF，按网页解析得 0 字 / 浏览器报「Download is starting」 | `pdf_not_usable`（表象） | 源抓取器只会处理网页 | `aab1bd6` | 永久失败无原因 |
| 09-15 | gsam、ARK | SPA 页/被拦时的「用简介兜底」从未生效 | `pipeline:field_dropped` | 保存新文章只存固定字段，`gsam_summary`/`summary` 被丢 | `aab1bd6`（+ 结构测试） | 兜底代码存在，看起来有保护 |
| 09-15 | aqr | 选择器已修好，2 篇仍是永久失败 | `pipeline:never_retried` | permafail 是终态 | 本次手工重排；系统化方案待做 | 修复后没人回头看旧失败 |

## 4. 排查同类问题时的检查清单

1. 先跑 `python3 scripts/failure_stats.py`，看是哪一类标签在增加、是否有「本周首次出现」的源/标签组合。
2. `selector_miss` 首次出现 → 大概率网站改版：拿真实页面看正文容器是否换了（`<li>`、自定义元素、脚本载荷、页脚区域）。
3. `body_too_short` 但页面有「Download / Read Now」→ 正文在 PDF：用 `_linked_article_pdf`，必须过标题词重合检查。
4. 摘要内容和标题对不上 → 查是否 `wrong_link_choice`（选了第一个链接/页脚 PDF）或网址复用。
5. 有 Cookie/免责门的站点（metlife），核对链接时**必须带 Cookie**，否则所有链接都会跳到免责页、看起来都有效。
6. 修好一个源后，回头把该源的旧失败文章重新排队（在系统化自动回补做好之前手工做）。
7. 验证修复时，拿「修改前的代码」对比同一批页面，区分「这次改坏了」和「网站把页面删了」。
