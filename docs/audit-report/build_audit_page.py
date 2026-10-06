"""Build docs-site/pages/hedge-fund-research-audit.html (static report, 2026-10-02)."""
import html
REPO = "https://github.com/chengli1986/hedge-fund-research"
def c(*hs):  # commit links
    return " ".join(f'<a class="sha" href="{REPO}/commit/{h}">{h}</a>' for h in hs)
def doc(path, label):
    return f'<a href="{REPO}/blob/main/{path}">{html.escape(label)}</a>'
def rows(items):
    return "".join(f"<tr><td class='id'>{i}</td><td>{p}</td><td>{f}</td><td class='st {k}'>{s}</td><td class='cm'>{cm}</td></tr>"
                   for i, p, f, k, s, cm in items)
def table(items):
    return ("<div class='tablewrap'><table><thead><tr><th>编号</th><th>问题（白话）</th><th>怎么处理的</th>"
            "<th>结局</th><th>提交</th></tr></thead><tbody>" + rows(items) + "</tbody></table></div>")
def ul(items): return "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>"
def mistakes(items):
    return ("<div class='tablewrap'><table class='mis'><thead><tr><th>我的错误</th><th>怎么发现、怎么处理</th></tr></thead><tbody>"
            + "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in items) + "</tbody></table></div>")

FIX, KEEP, REJ, PART, REC = "fix", "keep", "rej", "part", "rec"

S1 = [
 ("A1", "第 2 阶段整晚一篇正文都没抓到，流程也照样报“全部成功”", "能返回失败了；判据改为“今晚首次尝试的文章来自 ≥2 个源，且全场 0 成功”——回放 186 次历史运行：0 误报", FIX, "已修", c("3d07d11","90754ee")),
 ("A2", "每周的“入口校验”算出了问题，只打印在终端里，没人看得到", "结果写进文件，进每日健康邮件（发信条件、标题、正文都有）", FIX, "已修", c("995cfa3")),
 ("A3", "写摘要阶段要全部跑完才保存；中途被超时杀掉，当晚已付费的摘要全部丢失", "每 5 篇保存一次", FIX, "已修", c("479e8fd")),
 ("A4", "文章库没有跨进程的锁；手工命令和定时任务撞上会悄悄丢行", "加锁；重写前在锁内重读一遍，把期间新增的行合并进来", FIX, "已修", c("479e8fd")),
 ("B1", "一个源返回的畸形数据，能让整晚抓取崩掉", "每个源独立运行、抓完即保存；有源崩溃时当晚告警", FIX, "已修", c("5fe6c88")),
 ("B2", "同一网址上的新一期连载，只要标题没变就被<b>静默丢弃</b>（实测：troweprice 周报停在 4 月 17 日，另有 gsam、loomis、wellington、kkr 的连载）", "日期前移 ≥5 天就算新一期，不再要求标题也变", FIX, "已修", c("3df7146")),
 ("B3", "“列表页退回旧内容”的守卫只对 1 个源开启，而且有洞", "改为：列表最新一条比库里该源最新文章老 45 天以上，就拒绝整批（实测 42 源正常时位移为 0）", FIX, "已修", c("96efae3")),
 ("B4", "状态文件读坏一次，其余 41 个源的“连续零篇”计数全部清零，真正停更的源告警被推迟", "坏文件另存为证据、继续运行、进健康邮件", FIX, "已修", c("23e4f65")),
 ("B5", "网页上抓来的文字直接进 AI 提示词，可以被“注入指令”——实测 gemini 会照做", "把网页内容放进“只是资料”的围栏；3 个模型 × 3 种注入载荷，9/9 全部挡住", FIX, "已修", c("aea9340")),
 ("B6", "goehring 把网站地图的“修改时间”当成发布日期，还写死了域名", "改读文章页里的真实发布时间；防写死域名的检查补上了裸域名，当场又抓出 2 处", FIX, "已修", c("e94229d")),
 ("B7", "只写到月份的日期被归到月末，22 篇“未来日期”的文章霸占网页顶端", "按“第一次看到它的时间”排序；两处“本周新增”统一口径", FIX, "已修", c("e3da244")),
 ("B8", "往文章库追加写入不是原子的，写到一半被中断会留下残行，连带吞掉下一篇", "统一的读写模块：先补换行、落盘、坏行报行号并计数，坏行数进健康邮件", FIX, "已修", c("fd14ee4")),
 ("B9", "质量指标和文章保存不同步", "B1 改成“每个源抓完即保存”后，随之解决", FIX, "已修", c("5fe6c88")),
 ("B10", "--dry-run 显示的是上一次运行的异常", "只报本次", FIX, "已修", c("560da25")),
 ("B11", "配置里有两个早已不起作用的开关", "删除", FIX, "已修", c("560da25")),
 ("C1", "重复正文的识别只认“一字不差”：janus 一篇重抓回来多了 13 个字，就被当成新文章，网页上出现两次", "同源、同标题下按相似度判断（阈值 0.85，来自实测）；全库回放零误判", FIX, "已修", c("4a2edbe")),
]

S2 = [
 ("A1", "44 个抓正文程序里 40 个失败时不说原因，系统只能靠猜", "让程序报出它知道的原因（被拦截、选择器没匹配等）", FIX, "已修", c("0c55e57")),
 ("A2", "oaktree 用浏览器拿到的网页，“是不是验证页”两套检查都看不到", "浏览器拿到的网页统一走一个入口检查", FIX, "已修", c("f34bc21")),
 ("A3", "9 个特殊抓取程序不走统一流程，统一流程里的修复它们都享受不到", "已补上最关键的检查；彻底统一要先建“网页存档、可回放”的测试工具", PART, "部分缓解", c("f34bc21")),
 ("A4", "一个没有任何地方调用、却配着 5 条测试的函数", "删除", FIX, "已修", c("2e648fc")),
 ("A5", "说明文档写的 AI 模型链三个名字全错；一个没接上的 Claude 备用通道", "文档更正；备用通道按决定删除", FIX, "已修", c("2e648fc","33c7147")),
 ("A6", "Bridgewater 一篇 3 万字的研报，因为正文出现“terms of use”字样被整篇丢弃", "去掉这条误伤普通文章的规则", FIX, "已修", c("0c55e57")),
 ("B1", "“正文至少 100 字”的规则从来没起过作用（最短的正文也有 165 字）", "按决定保持现状", KEEP, "保持现状", "—"),
 ("B2", "真正判断“是不是正文”的是写摘要的 AI，它放过了一批视频简介、活动通知", "试跑新规则（47 篇判断全对），按决定不上线，结果存档", KEEP, "保持现状", doc("docs/evidence/b2-media-trial-2026-10-01.tsv","试跑结果")),
 ("C1", "账本里的“正文路径”字段，1548 条全部等于由编号推出的路径", "当作不变量用测试锁住", FIX, "已修", c("2e648fc")),
 ("C2", "35 个“孤儿正文文件”（有正文、账本里没记录），五个月没人数过", "查清来源（主要是 9-14 去重删了记录没删文件），全部移入备份；健康邮件报告新出现的孤儿", FIX, "已修", c("df4316a")),
 ("C3", "谁都能往正文仓库里写；周审和比对工具用真实编号，漏了防护会<b>悄悄覆盖库里的正文</b>", "统一的“临时目录”工具，三个调用方都用它，都有测试", FIX, "已修", c("df4316a")),
 ("D1", "定时重试从没救回过文章（救回的都是改代码后重新排队的）", "历史数据不全，只记录", REC, "只记录", "—"),
 ("D2", "重试规则没法用历史数据回放验证（台账 9-16 才开始）", "台账会随时间变长，只记录", REC, "只记录", "—"),
 ("D4", "“猜不出原因”时的兜底标签，会让文章在每次改代码后被反复重抓", "改为老实标“原因不明”", FIX, "已修", c("0c55e57")),
 ("D5", "一段生产环境永远走不到的旧代码，配着两条测试它的绿测试", "删除", FIX, "已修", c("2e648fc")),
 ("D6", "“重试次数上限”看起来多余", "模拟后发现它是唯一能拦住“无限重试”的保护——是我审错了", REJ, "驳回", "—"),
 ("E1", "花钱的 AI 调用集中在第 2 阶段没检测能力的源上", "属于 B2/E2 的后果，只记录", REC, "只记录", "—"),
 ("E2", "Lazard 每周专栏抓到的总是同一段栏目介绍", "按决定保持现状", KEEP, "保持现状", "—"),
 ("E3", "ARK 网页被拦时，用“标题+简介”凑一篇正文；AI 29 篇全部拒绝", "按决定取消这条备用路径", FIX, "已修", c("81f6999")),
 ("F1", "一条测试撤掉被测功能后照样通过（假绿）", "换成真能报错的测试", FIX, "已修", c("2e648fc")),
 ("F2", "AI 返回空答案时程序报“下标越界”，看不出原因", "报出具体原因", FIX, "已修", c("68dbdbe")),
 ("G1", "“重复正文”这个会让文章下页的标签，可能被一个巧合的标题抢走", "程序自己写的拒绝理由直接声明标签，不再靠猜", FIX, "已修", c("380ae7d")),
 ("G2", "“防编造核对未通过”这个标签同样可能被抢走，后果是规则修好后也不会重新分析", "同上", FIX, "已修", c("380ae7d")),
 ("G3", "约 16% 的拒绝理由同时命中多条分类规则，靠先后顺序决定", "只影响报表，没有依据说新顺序更对，不改", REC, "有意不修", "—"),
 ("G4", "给“拒绝摘要”加一个“视频页”分类", "实测一篇都拦不到（AI 写的理由里没有“video”）——驳回", REJ, "驳回", "—"),
]

CARDS = [("16 项", "第 1 阶段 · 全部修复"), ("25 项", "第 2 阶段 · 全部有结论"),
         ("1520 → 2078", "测试条数 · 全部通过"), ("19 / 42", "抓列表用声明式模板的源")]

STAGE1 = f"""
<h2>一、总体结论</h2>
<div class="box">
<p>2026-09-16 开始，逐阶段、不跳跃。审查范围是驱动脚本（4 项，A1–A4）和第 1 阶段“抓文章列表”（11 项，B1–B11），修复过程中又发现 1 项（C1），<b>共 16 项，到 09-21 全部修复</b>。期间测试从 1520 条增加到 1726 条。</p>
<p>这一轮反复出现的毛病是：<b>系统其实检测到了问题，但这个信息传不到人</b>——只写进日志、只打印在终端、或者被下游悄悄吞掉。</p>
<p>其中有两项不是“理论风险”而是<b>正在发生</b>：B2（几家机构的连载被静默丢弃了好几个月）和 B7（22 篇“未来日期”文章霸占网页顶端）。</p>
</div>

<h2>二、16 项问题的结局</h2>
{table(S1)}

<h2>三、审查之后做的（第 1 阶段的延续）</h2>
{ul([
 "<b>按页面类型做抓取模板</b>（你提出的思路）：现在 <b>19 / 42</b> 个源用声明式模板抓列表（卡片列表 12、RSS 5、接口 2），42 个源的去留和理由全部写成文档并有测试守着不许漂移。每次切换前用“A/B 闸门”对真实网站逐字段比对 · " + c("d79617c","bbfa88d","d99adcf","65cdf32") + " · " + doc("docs/listing-template-decisions.md","决策记录"),
 "<b>A/B 闸门反过来揪出了手写版的老 bug</b>：wellington 页面同一张卡片重复出现，10 个名额里 4 个是副本，4 篇真文章从未入库 · " + c("680985c") + "；troweprice 的标题存进了不该有的不换行空格 · " + c("dc6aeaf") + "；mfs 的标题里有一个 HTML 实体字符，下次抓取就会原样入库，在入库之前修好 · " + c("d99adcf"),
 "<b>健康探针在测“不跑的代码”</b>：19 个源生产用模板，探针却在测手写版——模板坏了会天天显示健康。已统一成同一套选择逻辑 · " + c("f546927"),
 "<b>维护机制</b>：每周一 A/B 闸门自动复核全部模板源；每月模板普查；告警里写明这个源跑的是哪套代码、怎么一键回滚 · " + c("5632a66","342cb82","5aaa717"),
 "<b>健康面板上线</b>（就是本报告的上一页）· " + c("23debec"),
])}

<h2>四、我自己犯过、已经纠正的错</h2>
{mistakes([
 ("B2 一开始判成“埋雷未爆”", "只看了库里已有的记录；被丢弃的东西按定义不在库里。重跑一次抓取才看见它正在丢"),
 ("注释里写“更正日期通常只挪 1–2 天”", "是我编的。被追问后实测 26 个样本，1–4 天的为 0，已改正注释"),
 ("A1 第一版阈值“待抓 ≥5 篇且 0 成功就算故障”", "没有依据。回放 186 次历史运行会误报 14 次、0 次真故障；改为按源判断后 0 误报"),
 ("A1 第一版修复的测试是假绿", "测试只在源码里找“sys.exit(”，实际代码一运行就报错，测试照样通过。改成真把程序跑起来"),
 ("cambridge 第一版改法会弄坏 4 篇里的 2 篇", "“先测量再提交”拦住了"),
 ("待办“cambridge 有 2 篇走兜底路径”", "没验证就写下的推断，实测全部走主路径，撤回"),
 ("C1：预测“janus 会被判重复、loomis 不会”", "两条都猜反。预测不是证据，事后核对才是"),
 ("B8 的风险是我自己放大的", "B1 把保存从每晚 1 次改成每源 1 次，可能被中断的窗口从 1 个变成最多 42 个；同一会话内修好"),
 ("模板代码里重犯了“跨标签粘词”的老缺陷", "正文抓取器修过同一个问题，A/B 闸门当场揪出"),
 ("A/B 闸门一开始只比标题、网址、日期", "漏了其他保存字段，我 20 分钟前的改动差点让字段静默消失；补上了字段比对"),
])}

<h2>五、遗留</h2>
{ul(["新源仍由程序自动写成手写抓取器，模板覆盖率会慢慢下降。建议过“让新源先试模板”，尚未决定"])}
"""

STAGE2 = f"""
<h2>一、总体结论</h2>
<div class="box">
<p>2026-09-26 取证，09-27 到 10-02 讨论并修复。审查对象是第 2 阶段“抓文章正文”的设计和通用机制，<b>共 25 项，全部有了结论</b>。测试从 1956 条增加到 2078 条（10-06），<b>全部通过</b>——审查开始时有一条测试长期报错，现在也修好了。</p>
<p>审查期间第 2 阶段一直运行正常（九成六以上的文章都抓到了正文），所以这次不是救火。查的是“情况一变就会出错”的地方，以及“看起来在保护、实际没起作用”的机制。</p>
<p>这一轮反复出现的毛病是第 1 阶段那个的变体：<b>知道原因的一方不说，不知道的一方去猜，或者各自设防</b>。</p>
</div>
<div class="cards small">
 <div class="card"><div class="v g">15</div><div class="l">已修好</div></div>
 <div class="card"><div class="v">2</div><div class="l">查证后发现是我审错了，驳回</div></div>
 <div class="card"><div class="v">1</div><div class="l">部分缓解</div></div>
 <div class="card"><div class="v">1</div><div class="l">有意不修</div></div>
 <div class="card"><div class="v y">3</div><div class="l">按你的决定保持现状</div></div>
 <div class="card"><div class="v">3</div><div class="l">只做记录</div></div>
</div>

<h2>二、25 项问题的结局</h2>
{table(S2)}
<p class="muted">每一项的证据、测量数据和讨论过程见 {doc("docs/stage2-audit-findings.md","审计文档")}。9-19 还有过一轮针对第 2 阶段通用机制的检查（F1–F6、D1–D3 的前身），本轮第 ⑥ 节逐条复核了那一轮的修复是否仍然有效，F1 就是在那里发现的假绿。</p>

<h2>三、审查范围以外顺带做的</h2>
{ul([
 "<b>10-06 新发现：同一网址的新一期被当成重复丢掉</b>。GSAM《养老金月报》每期用同一个网址，网站先改了列表日期、几天后才换文章内容；抓到的是上一期正文，被判重复隐藏，之后不再抓。修法：正文和同网址上一期相同时记为“网页还没更新”，次晚再抓、最多等 7 晚；“相同”的标准抽成第 2、3 阶段共用的模块（全库 1651 篇重放，判定 0 差异）。丢失的那一期已找回 · " + c("dacc3bd"),
 "<b>10-06 健康检查报错原因写错</b>：读不了抓回的正文时，邮件会写成“正文太短：0 字”。改为报出真实错误（来自 10-03 整仓自动审查的清单）· " + c("5f3fb74"),
 "<b>之前声明不审的部分</b>：8 个会自己打开浏览器的抓取程序。查完<b>没有发现缺陷</b>：浏览器用完都会关，服务器上没有残留进程；历史上 543 次抓取超时 8 次；整条流程最长 442 秒，时限 1800 秒 · " + c("552e27f"),
 "<b>失败重试偶尔晚一天</b>（68 次里 11 次）：原来按秒比较时间，改为按北京时间的日期判断 · " + c("ca6e987"),
 "<b>测试套件恢复全绿</b>：一条单元测试从 7 月起每次运行都真的打开浏览器、联网访问 example.com；修好它，并补上“测试不许开浏览器”的防护（异常被吞掉也能发现）· " + c("062fb67"),
 "<b>第 3 阶段（写摘要）</b>：一篇真文章因为“就业报告可能……”被误判成 AI 瞎猜，已修复并给一次重写机会 · " + c("9ac726a") + "。防编造规则做了一次盘点（上线以来未抓到过真正的编造、误拦过 2 篇真文章），按你的决定维持现状",
 "<b>对外网页</b>：docs 首页里 GMIA 的模型说明已更正（luna → mini，单一供应商）",
])}

<h2>四、我自己犯过、已经纠正的错</h2>
{mistakes([
 ("把“重试次数上限”当成多余代码（D6）", "动手删之前做了模拟，发现它是交替失败时唯一的退休闸门，驳回"),
 ("孤儿文件的成因写错（归给一个根本不可能造成它的 bug）", "准备补回数据时发现 17 篇其实都还在库里，真因是 9-14 去重；在文档里明确撤回"),
 ("最初提议给拒绝摘要加“视频页”分类（G4）", "AI 的拒绝理由里根本没有“video”这个词，能拦住 0 篇，驳回"),
 ("写了一条 10 月底会自己失败的测试（写死了日期）", "复查时发现，改成用当天日期"),
 ("文档里引用的行号被我后来的改动弄错", "改成引用函数名"),
 ("说“重试总是晚一天”", "全量统计后更正为 68 次里 11 次"),
 ("说“46 个抓取程序”", "实际 42 个，46 是写文件的语句数"),
 ("测试时两次误往正式目录写了文件", "被测试防护拦下，已清理；记下教训：给“防护”类代码做变异测试前，先确认写入目标是临时目录"),
 ("一版改动让 25 条健康探针测试挂掉", "提交前发现，让测试的假模块委托给真函数，而不是另抄一份"),
 ("C1 的守卫测试直接读生产数据文件", "GitHub 上的自动测试环境没有这个文件，从 9-30 我的提交起，到 10-03 修复之前的 23 次推送全部变红；我只看了本机结果没发现。10-03 整仓自动审查发现并修复 · " + c("09e68a6")),
])}

<h2>五、10-03 待确认事项的结果（10-06 核对）</h2>
{ul(["✅ lazard 那篇被误拦的文章：规则修改后自动重新分析，摘要已回来",
     "✅ Apollo 那期播客：10-03 凌晨按新的日期规则准时重试，仍只有简介，按规则放弃",
     "✅ gsam 7 篇：因代码改动重抓一次后照常重新放弃"])}
"""

AUTO = f"""
<h2>一、这是什么</h2>
<div class="box">
<p>10-02 下午到 10-03 凌晨，每日代码审查工具对 GMIA <b>整个仓库</b>做了一次全量审查（82 批，约 3.9 小时），由另一个工作会话执行。它和上面两轮人工分阶段审查是<b>互补</b>的：覆盖面更广，但不深入。</p>
<p>结果：<b>73 条</b>候选问题（22 条标为严重、51 条警告）。逐条核实后：<b>4 条须修</b>、<b>47 条属实但不紧急</b>、<b>22 条是误报</b>（误报率 30%）。</p>
</div>

<h2>二、4 条须修（全部已修）</h2>
<div class='tablewrap'><table class='mis'><thead><tr><th>问题</th><th>处理</th></tr></thead><tbody>
<tr><td>GitHub 自动测试从 9-30 起每次推送都红（原因是我加的 C1 守卫测试读生产数据文件）</td><td>没有数据文件时跳过这条检查 · {c("09e68a6")}</td></tr>
<tr><td>文章库里一行坏数据会让健康面板的 30 天趋势整个清零</td><td>改为逐行读取，坏行跳过 · {c("d0684e9")}</td></tr>
<tr><td>基金资料校验把任何以 /en/a 开头的引用网址都误判为“n/a”</td><td>修正匹配 · {c("73085a8")}</td></tr>
<tr><td>候选基金发现把“需要浏览器才能抓”的结果当成错误跳过</td><td>改为转入相应流程 · {c("86e71d5")}</td></tr>
</tbody></table></div>
<p class="muted">另有一项口径由你决定：资料里带“据报道”“估计”之类不确定性措辞的，一律拦下（删除了一段从未生效的豁免代码）· {c("1401558")}</p>

<h2>三、与抓取流水线有关的项，10-06 逐条复核</h2>
{ul([
 "<b>健康检查读不了正文时把原因写成“正文太短：0 字”</b>：属实，已修 · " + c("5f3fb74"),
 "<b>Bridgewater 正文的段落被压成一整行</b>：属实（20 篇里 14 篇），但写摘要、判重复、每周抽查都不受影响，不修",
 "<b>日期“05/06”会被当成美国格式</b>：实查没有任何源受影响——用日在前格式的 Rothschild 有专门处理，31 篇存下的日期与网址里的年月全部一致；PIMCO、摩根资管、Matthews 都是美国格式。潜在风险，记录",
 "<b>A/B 闸门</b>：某一列有空值时排序会报错；两边都返回空列表时会判为“一致”（零篇抓取有单独告警兜底）。第 1 阶段，次要，记录",
 "<b>模板普查</b>：上一次运行如果被中途杀掉，留下的残行会让下一次读取失败。第 1 阶段，次要，记录",
])}

<h2>四、这次审查的局限</h2>
{ul([
 "18 个大文件只审了前约 25KB（包括第 2 阶段的 fetch_content.py、发布用的 publish.py 等），后半部分没有覆盖——这也是人工分阶段审查仍然必要的原因",
 "30% 的误报说明它的结论必须逐条核实后才能用；本页只收录核实过的",
])}
"""

METHOD = f"""
<h2>审查是怎么做的</h2>
{ul([
 "<b>逐条复核</b>：代理深读代码后列出候选问题，每一条都由我亲自复现或测量后才写进清单。候选里不成立的直接丢弃",
 "<b>先读真实数据再动手</b>：例如 B2 是靠重跑一次真实抓取才发现正在丢文章；C2 的孤儿文件逐个对照了日志和发布页历史",
 "<b>阈值必须有历史依据</b>：凡是门槛，先回放历史算误报和漏报（A1 回放了 186 次运行）",
 "<b>先让测试失败，再修</b>：防护类改动必须先证明“撤掉它测试会红”，否则不算修好",
 "<b>变异测试</b>：故意把修复改回去或改坏，确认测试能抓住；“改动没打进去”不算“改动被抓住”",
 "<b>真实环境验证</b>：涉及抓取的改动，用 dry-run 对 42 个真实网站跑一遍，并核对生产文件前后一字未变",
 "<b>你来做决定的事，写进文档</b>：凡是“改不改”属于取舍的，给出测量数据由你决定，决定和理由都记录在案，以后重新讨论可以直接从数据开始",
])}
<h2>证据在哪里</h2>
{ul([
 doc("docs/stage2-audit-findings.md", "第 2 阶段审计文档") + "：25 项的证据、测量和讨论",
 doc("docs/listing-template-decisions.md", "列表模板决策记录") + "：42 个源的去留与理由",
 doc("docs/content-failure-casebook.md", "正文失败案例集") + "：各种失败标签的真实案例",
 f'<a href="{REPO}/commits/main">提交历史</a>：每个提交说明里写了改了什么、为什么、怎么验证的',
])}
"""

CSS = """
:root{--bg:#0d1117;--surface:#161b22;--surface2:#1c2128;--border:#30363d;
--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--blue:#58a6ff;--yellow:#d29922;
--red:#f85149;--purple:#bc8cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,
BlinkMacSystemFont,"Segoe UI","Noto Sans SC",Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:20px;margin:0 0 4px}
h2{font-size:15px;margin:32px 0 12px;color:var(--muted);font-weight:600}
a{color:var(--blue)}
.sub{color:var(--muted);font-size:12px;margin-bottom:20px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:16px 0}
.cards.small{grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}
.card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px}
.card .v{font-size:24px;font-weight:600}
.card .v.g{color:var(--green)}.card .v.y{color:var(--yellow)}
.card .l{color:var(--muted);font-size:12px;margin-top:2px}
.box{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:4px 16px}
.box p{margin:10px 0}
ul{padding-left:20px}li{margin:6px 0}
.muted{color:var(--muted);font-size:12px}
/* Tabs without script: the page is a static file and stays that way, like the
   health page. Radio inputs drive which panel shows. */
.tabs input{position:absolute;opacity:0;pointer-events:none}
.tabs label{display:inline-block;padding:8px 14px;margin:0 6px 6px 0;border:1px solid var(--border);
border-radius:8px;background:var(--surface);color:var(--muted);cursor:pointer;font-size:13px}
.tabs .panel{display:none}
#t1:checked~.bar label[for=t1],#t2:checked~.bar label[for=t2],#t3:checked~.bar label[for=t3],#t4:checked~.bar label[for=t4]{
border-color:var(--blue);color:var(--text);background:var(--surface2)}
#t1:checked~#p1,#t2:checked~#p2,#t3:checked~#p3,#t4:checked~#p4{display:block}
#t1:focus-visible~.bar label[for=t1],#t2:focus-visible~.bar label[for=t2],
#t3:focus-visible~.bar label[for=t3],#t4:focus-visible~.bar label[for=t4]{outline:2px solid var(--blue)}
.tablewrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:13px;min-width:760px}
table.mis{min-width:560px}
th{text-align:left;color:var(--muted);font-weight:600;padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid #21262d;vertical-align:top}
tr:hover td{background:var(--surface2)}
td.id{font-weight:600;white-space:nowrap}
td.st{white-space:nowrap;font-weight:600}
.st.fix{color:var(--green)}.st.keep{color:var(--yellow)}.st.rej{color:var(--purple)}
.st.part{color:var(--blue)}.st.rec{color:var(--muted)}
td.cm{white-space:nowrap}
a.sha{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;margin-right:4px}
"""

page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GMIA 审查报告</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>GMIA 管线审查报告 · 第 1、2 阶段</h1>
  <div class="sub">整理于 2026-10-02 BJT，10-06 更新 · 静态快照，新一轮审查完成后更新 ·
    <a href="/hedge-fund-research-health.html">← 管线健康</a> ·
    <a href="/hedge-fund-research.html">研报看板</a> ·
    <a href="{REPO}">GitHub 仓库</a></div>
  <div class="cards">{"".join(f'<div class="card"><div class="v">{v}</div><div class="l">{l}</div></div>' for v,l in CARDS)}</div>
  <div class="tabs">
    <input type="radio" name="tab" id="t1" checked>
    <input type="radio" name="tab" id="t2">
    <input type="radio" name="tab" id="t3">
    <input type="radio" name="tab" id="t4">
    <div class="bar">
      <label for="t1">第 1 阶段：抓文章列表</label><label for="t2">第 2 阶段：抓文章正文</label><label for="t4">整仓自动审查（10-03）</label><label for="t3">审查方法与证据</label>
    </div>
    <div class="panel" id="p1">{STAGE1}</div>
    <div class="panel" id="p2">{STAGE2}</div>
    <div class="panel" id="p4">{AUTO}</div>
    <div class="panel" id="p3">{METHOD}</div>
  </div>
</div>
</body>
</html>
"""
import sys
open(sys.argv[1], "w", encoding="utf-8").write(page)
print("written", len(page.encode()), "bytes; stage1 rows", len(S1), "stage2 rows", len(S2))
