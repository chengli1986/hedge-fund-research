#!/usr/bin/env python3
"""
Hedge Fund Research — Stage 4: HTML Dashboard Publisher

Generates a static HTML dashboard from articles.jsonl and sources.json.
Output: /var/www/overview/hedge-fund-research.html (+ .gz)

Dark GitHub-style theme matching docs.sinostor.com.cn.
"""

import html
import gzip
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
import argparse
import os

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
SOURCES_FILE = BASE_DIR / "config" / "sources.json"
OUTPUT_FILE = Path("/var/www/overview/hedge-fund-research.html")

BADGE_COLORS: dict[str, str] = {
    "man-group": "#58a6ff",
    "bridgewater": "#d29922",
    "aqr": "#3fb950",
    "gmo": "#9b6be0",
    "oaktree": "#f85149",
    "ark-invest": "#c45000",  # ARK orange (WCAG AA)
    "cambridge-associates": "#2ba397",  # teal
    "wellington": "#0066cc",            # Wellington blue
    "amundi": "#e8601c",                # Amundi orange
    "troweprice": "#00607a",            # T. Rowe Price teal-navy
    "pimco": "#003a70",                 # PIMCO navy
    "aberdeen": "#9d2235",              # Aberdeen burgundy
    "brookfield": "#1e7f3e",            # Brookfield green
    "jpmam": "#7a4cb1",                 # JPMorgan purple
    "verdad-capital": "#7c2d12",        # Verdad terracotta (rust)
    "msci-research": "#1f49e0",         # MSCI cobalt blue
    "natixis-im": "#80276c",            # Natixis purple
    "apollo-global-management": "#5e3a82",  # Apollo plum (avoids existing navy/gold/purple clusters)
    "kkr": "#556b2f",                       # KKR dark olive (unique among 19 existing colors)
    "janus-henderson": "#006d75",           # Janus Henderson teal
    "research-affiliates": "#c2185b",       # Research Affiliates deep magenta (distinct from all 21 existing colors)
    "gsam": "#4a6fa5",                      # Goldman steel blue (muted vs man/wellington/msci, lighter than pimco navy)
    "robeco": "#2d6a4f",                    # Robeco pine/forest green (ESG heritage; distinct from #3fb950 bright lime and #1e7f3e Brookfield mid-green)
    "de-shaw": "#cd853f",                   # D. E. Shaw bronze/peru (warm earth; distinct from #c45000 ARK deep-orange and #e8601c Amundi red-orange)
    "metlife-im": "#00838f",                # MetLife IM deep cyan — inherits the slot freed by pinebridge on 2026-07-27 (MIM acquired PineBridge and the research moved to its site); already vetted as distinct from #006d75 Janus emerald-teal and #2ba397 Cambridge teal, and the crowded corporate-blue cluster (#003a70 PIMCO / #00568c Principal / #0066cc Wellington) leaves no room for a brand-literal MetLife azure
    "ares-management": "#b85450",           # Ares dark coral (distinct from #f85149 Oaktree bright red, #9d2235 Aberdeen burgundy, #7c2d12 Verdad terracotta, and warm-orange ARK/Amundi)
    "matthews-asia": "#3949ab",             # Matthews Asia indigo (fills blue↔violet gap; muted vs #1f49e0 MSCI electric cobalt, deeper/bluer than #7a4cb1 JPMAM violet, WCAG AA ~5.9:1 on white)
    "capital-group": "#37474f",             # Capital Group slate blue-grey (fills the empty neutral-slate niche — only desaturated gray in palette; WCAG AA 9.65:1; avoids the crowded saturated-blue cluster)
    "acadian-asset": "#5d4037",             # Acadian coffee/umber brown (fills the empty muted-brown niche; brand steel-blue avoided as the blue cluster is crowded; distinct from #7c2d12 Verdad terracotta by far-lower saturation; WCAG AA ~8.9:1 on white)
    "lazard-am": "#b07d12",                 # Lazard warm amber-gold (EM/macro heritage; distinct from #d29922 Bridgewater mustard-gold and #cd853f D.E.Shaw bronze; WCAG AA ~4.8:1 on white)
    "rothschild-co-am": "#6a1b4d",          # Rothschild deep claret/Bordeaux-wine (nods to family Bordeaux estates Lafite/Mouton; fills the sparse wine-magenta niche — nearest neighbors #80276c Natixis purple and #c2185b Research-Affiliates magenta are both far in lightness/hue; min perceptual ΔE ~65; WCAG AA 11.2:1 on white)
    "goehring-rozencwajg": "#827717",       # G&R dark yellow-olive "oilfield khaki" (energy/commodities heritage; sits between #b07d12 Lazard amber — lighter, more orange — and #556b2f KKR olive — darker, greener — with RGB dist ≥47 to both; WCAG AA 4.56:1 on white)
    "cohen-steers": "#8d4004",              # Cohen & Steers deep burnt-sienna (real-assets earth tone; much darker than #c45000 ARK orange and #cd853f D.E.Shaw bronze, redder than #7c2d12 Verdad terracotta; WCAG AA ~7.4:1 on white)
    "principal-am": "#00568c",              # Principal deep azure (brand-adjacent corporate blue; sits between #003a70 PIMCO navy — darker — and #0066cc Wellington blue — brighter/lighter — RGB dist ≥40 to both and to #4a6fa5 GSAM steel blue; WCAG AA ~6.9:1 on white)
    "franklin-templeton": "#004d40",        # Franklin Templeton deep pine-teal (the corporate-blue cluster is full, so the brand navy is unusable; this is the darkest, greenest point of the teal family — RGB dist ≥61 to #00607a T.Rowe teal-navy, #006d75 Janus and #00838f MetLife cyan, and 51.6 to its nearest neighbour overall, the highest min-distance of any candidate tested; WCAG AAA 9.8:1 on white)
    "partners-group": "#7b3f00",            # Partners Group dark chocolate-brown (the warm-earth band is the only one with room left: darker and far less red than #8d4004 Cohen & Steers burnt-sienna and #7c2d12 Verdad terracotta, warmer/redder than #5d4037 Acadian umber — min RGB dist 36 to any existing color; WCAG AAA 8.9:1 on white)
    "loomis-sayles": "#a31fa3",             # Loomis Sayles magenta-orchid — the brand navy is unusable (the corporate-blue cluster #003a70/#00568c/#0066cc/#1f49e0/#4a6fa5 is full), and the magenta-violet gap between #9b6be0 GMO lavender (much lighter) and #80276c Natixis plum-purple (much darker/redder) was the widest hole left in the wheel: min ΔE 26.0 (to #7a4cb1 JPMAM violet), min RGB dist 62.5; WCAG AA 6.4:1 on white
    "blue-owl-capital": "#301070",          # Blue Owl midnight indigo-violet ("night owl"; the deep blue-violet slot between the navy and violet clusters was empty — min RGB dist 63.8 to any of the 36 existing colors, the highest of any palette-consistent candidate: 63.8 to #003a70 PIMCO navy, 82 to #3949ab Matthews indigo, 66 to #5e3a82 Apollo plum; WCAG AAA 14.6:1 on white)
}

INITIAL_VISIBLE = 20
RECENT_DAYS = 90  # Articles older than this are folded behind a "Show older" toggle
                  # AND excluded from the inline article-details JSON island.
                  # Tightened 180 → 90 on 2026-05-29 to keep initial JSON parse cost
                  # flat as the production source list grows. Older articles still
                  # render their <article> shell (Show older reveals title + source
                  # link), but their LLM analysis body is omitted from JSON island —
                  # clicking Open shows a "see source above" bilingual notice.

# ── Static fund profile data (displayed in Sources tab) ──
_FUND_PROFILES: dict[str, dict] = {
    "man-group": {
        "founded": "1783", "aum": "~$229B", "hq": "London, UK",
        "type_en": "Listed HF (LSE)", "type_zh": "上市对冲基金 (伦交所)",
        "desc_zh": "全球最大上市对冲基金。AHL 系统化量化与 GLG 主观宏观及信贷多策略并举，伦交所上市。最初为大宗商品经纪商，1980 年代转型资产管理。",
        "notable_en": "AHL Diversified ~15% annualized since 1990s; significant positive returns during 2008 financial crisis, providing genuine equity hedge.",
        "notable_zh": "AHL Diversified 自 1990 年代年化约 15%；2008 年金融危机期间实现显著正回报，提供真实的股票去相关性。",
    },
    "bridgewater": {
        "founded": "1975", "aum": "~$92B", "hq": "Westport, CT",
        "type_en": "Largest Hedge Fund", "type_zh": "全球最大对冲基金",
        "desc_zh": "全球最大对冲基金，Ray Dalio 创立。以「极度透明」文化和系统化宏观方法著称。Pure Alpha 追求全球宏观 Alpha；All Weather 是最早的风险平价策略，旨在穿越任何经济周期。",
        "notable_en": "Pure Alpha +45% in 2010; All Weather's risk-parity design is built to weather any economic regime; Dalio's debt cycle framework widely cited in global macro research.",
        "notable_zh": "Pure Alpha 2010 年 +45%；All Weather 风险平价设计旨在穿越任何经济周期；Dalio 债务周期框架是全球宏观研究的重要参考。",
    },
    "aqr": {
        "founded": "1998", "aum": "~$187B", "hq": "Greenwich, CT",
        "type_en": "Quant Hedge Fund", "type_zh": "量化对冲基金",
        "desc_zh": "Cliff Asness、David Kabiller、Robert Krail、John Liew（均出自高盛量化研究部）共同创立。系统化因子投资先驱：在股票、固收、货币及大宗商品上部署价值、动量、carry 及防御性因子。大量发表于顶级学术期刊。",
        "notable_en": "Multiple papers in Journal of Finance; positive performance in 2022 when global stocks and bonds both fell 15%+; 'Betting Against Beta' paper reshaped factor investing theory.",
        "notable_zh": "多篇论文发表于《Journal of Finance》；2022 年全球股债双杀中录得正回报；'Betting Against Beta' 论文重塑了因子投资理论。",
    },
    "gmo": {
        "founded": "1977", "aum": "~$82B", "hq": "Boston, MA",
        "type_en": "Value Hedge Fund", "type_zh": "价值对冲基金",
        "desc_zh": "Jeremy Grantham、Richard Mayo 和 Eyk van Otterloo 创立（公司名 Grantham, Mayo, Van Otterloo 即源于三位创始人）。深度价值、逆向风格。以季度《7 年资产类别预测》著称——跨全球股票和固定收益市场的机构级预期回报估算。",
        "notable_en": "Accurately called the 1989 Japan bubble, 2000 dot-com crash, and 2007 housing bubble; the quarterly 7-Year Asset Class Forecasts are a long-standing institutional reference for return expectations.",
        "notable_zh": "准确预警 1989 年日本泡沫、2000 年科技泡沫、2007 年美国房产泡沫；季度《7 年资产类别预测》长期是机构衡量预期回报的重要参考。",
    },
    "oaktree": {
        "founded": "1995", "aum": "~$220B", "hq": "Los Angeles, CA",
        "type_en": "Alt. Credit Leader", "type_zh": "另类信贷领军",
        "desc_zh": "全球最大困境债（distressed debt）投资人之一，由 Howard Marks、Bruce Karsh 等前 TCW 固收团队创立。Marks 自 1990 年起撰写的投资备忘录是机构信贷圈的必读材料。2019 年 Brookfield 收购多数股权。",
        "notable_en": "30 consecutive years without a loss in distressed debt; Opportunities Fund VIII returned 30%+ during the 2008-09 crisis; Warren Buffett calls Marks' memos 'must-reads.'",
        "notable_zh": "困境债连续 30 年无亏损年度；Opportunities Fund VIII 2008-09 危机期间回报超 30%；Warren Buffett 称 Marks 备忘录「每次必读」。",
    },
    "ark-invest": {
        "founded": "2014", "aum": "~$30B", "hq": "St. Petersburg, FL",
        "type_en": "Thematic ETF", "type_zh": "主题 ETF",
        "desc_zh": "Cathie Wood 创立，专注颠覆性创新——AI、基因组学、机器人、储能、金融科技和太空。在 ETF 行业首创每日持仓公开透明，打破传统主动管理的不透明模式。",
        "notable_en": "ARKK +152% in 2020 (best-performing active ETF); early Tesla conviction at ~$17 split-adjusted — held through 10x gain; pioneered daily holdings disclosure in the industry.",
        "notable_zh": "ARKK 2020 年 +152%（年度最佳主动 ETF）；特斯拉复权价约 $17 时重仓持有至 10 倍回报；首创每日持仓披露制度。",
    },
    "cambridge-associates": {
        "founded": "1973", "aum": "$600B+ advisory", "hq": "Boston, MA",
        "type_en": "Investment Advisor", "type_zh": "机构投资顾问",
        "desc_zh": "非传统基金管理人，而是全球领先的捐赠基金和基金会投资顾问，哈佛为其首位客户。其 PE/VC 基准指数是全球私募市场业绩衡量的行业标准。",
        "notable_en": "Pioneered institutional private-investment benchmarking; its PE/VC benchmark indices are an industry standard referenced across $3T+ in private market capital globally; long-time adviser to leading university endowments and foundations.",
        "notable_zh": "首创机构级私募投资业绩基准；PE/VC 基准指数是行业标准，被全球超 3 万亿美元私募资本采用；长期服务顶尖大学捐赠基金与基金会。",
    },
    "wellington": {
        "founded": "1928", "aum": "~$1.3T", "hq": "Boston, MA",
        "type_en": "Private Partnership", "type_zh": "私营合伙制",
        "desc_zh": "全球历史最悠久、规模最大的私营投资管理公司之一。为 60+ 国 2,200+ 个机构客户管理资产。非上市合伙制架构，无外部股东压力。以深度基本面研究文化著称，分析师平均任期超 10 年。2026 年 6 月宣布收购 The Hartford 旗下 Hartford Funds（Wellington 此前已为其约 83% 的资产担任投资顾问），交易净现值约 19 亿美元，预计 2027 年一季度交割后并入 Wellington 品牌。",
        "notable_en": "Advisor to Vanguard Wellington Fund (1929, oldest US balanced fund); analysts average 10+ year tenure; serves Harvard endowment and major sovereign wealth funds.",
        "notable_zh": "美国最古老平衡基金 Vanguard Wellington Fund（1929 年）的投资顾问；分析师平均任期逾 10 年；服务哈佛捐赠基金及多家主权财富基金。",
    },
    "amundi": {
        "founded": "2010", "aum": "~€2.4T", "hq": "Paris, France",
        "type_en": "Listed (Euronext)", "type_zh": "上市 (欧交所)",
        "desc_zh": "欧洲最大资产管理公司，2010 年由农业信贷 AM 与兴业 AM 合并成立。Research Center 每周发布宏观及资本市场报告，欧元区政策分析独具优势。欧洲最大 ETF 提供商之一。",
        "notable_en": "Europe's largest asset manager; ESG integration pioneer — among first UN PRI signatories; acquired Lyxor (2021) to dominate European ETF market.",
        "notable_zh": "欧洲最大资产管理公司；ESG 整合先驱，联合国 PRI 首批签署方；2021 年收购 Lyxor 巩固欧洲 ETF 市场地位。",
    },
    "troweprice": {
        "founded": "1937", "aum": "~$1.9T", "hq": "Baltimore, MD",
        "type_en": "Listed (NASDAQ)", "type_zh": "上市 (NASDAQ)",
        "desc_zh": "由 Thomas Rowe Price Jr. 创立，被誉为「成长股投资之父」，1930 年代即提出成长股投资理念并于 1937 年据此创立公司。以基本面研究文化著称，分析师须长期深度覆盖其研究领域。",
        "notable_en": "Founder credited as 'father of growth investing'; 30+ consecutive years without a net annual loss (through 2020); target-date fund series globally top-3 by AUM.",
        "notable_zh": "创始人被誉为「成长股投资之父」；连续 30+ 年无年度净亏损（截至 2020 年）；目标日期基金系列规模全球前三。",
    },
    "pimco": {
        "founded": "1971", "aum": "~$2.33T", "hq": "Newport Beach, CA",
        "type_en": "Fixed-Income Manager", "type_zh": "固收管理人 (Allianz 子公司)",
        "desc_zh": "全球最大固定收益管理人，1971 年由 Bill Gross 等人于 Pacific Mutual Life（1997 年更名 Pacific Life）子公司创立。Total Return Fund 曾长期为全球最大共同基金。Mohamed El-Erian 提出的「新常态」框架影响深远。2000 年被 Allianz 收购后保持独立投资团队，年度「Secular Outlook」是机构固收圈必读。",
        "notable_en": "Total Return Fund was world's largest mutual fund 2008–14; Bill Gross dubbed 'the bond king'; 'new normal' framework reshaped post-2008 macro thinking.",
        "notable_zh": "Total Return Fund 2008–14 年间为全球最大共同基金；Bill Gross 被誉为「债券之王」；「新常态」框架重塑了后金融危机宏观思维。",
    },
    "aberdeen": {
        "founded": "1983", "aum": "~£556B", "hq": "Edinburgh, UK",
        "type_en": "Listed (LSE)", "type_zh": "上市 (伦交所)",
        "desc_zh": "1983 年于苏格兰阿伯丁创立，2017 年与 Standard Life 合并。新兴市场债务和亚洲股票团队最为知名，本地分析师覆盖深度行业领先。2021 年改名「abrdn」后于 2025 年回归「Aberdeen」品牌。",
        "notable_en": "One of the UK's largest independent asset managers; world-leading EM debt franchise; pioneer in Asian equity research with on-the-ground analyst coverage.",
        "notable_zh": "英国领先的独立资产管理公司之一；全球领先新兴市场债务投资人；亚洲股票研究先驱，本地分析师团队深度覆盖。",
    },
    "brookfield": {
        "founded": "1899", "aum": "~$1T+", "hq": "New York, NY",
        "type_en": "Listed Alt Manager (NYSE)", "type_zh": "上市另类资管 (纽交所)",
        "desc_zh": "全球最大基础设施和实物资产投资人之一，1899 年创立于加拿大（前身 Brascan）。可再生能源基础设施先驱。2019 年收购 Oaktree 多数股权后业务扩展至另类信贷。2022 年从母公司 Brookfield Corporation 拆分独立 IPO。",
        "notable_en": "World's largest infrastructure investor; pioneered renewable infrastructure as institutional asset class; majority owner of Oaktree since 2019.",
        "notable_zh": "全球最大基础设施投资人；首创可再生能源基础设施作为机构投资类别；2019 年起为 Oaktree 多数股东。",
    },
    "jpmam": {
        "founded": "1984", "aum": "~$4.0T", "hq": "New York, NY",
        "type_en": "Bank-Affiliated AM", "type_zh": "银行系资产管理",
        "desc_zh": "JPMorgan Chase 资产管理子公司，全球前 5 大资管。季度发布的《Guide to the Markets》是机构投资者最广泛参考的市场研究刊物之一。「On the Minds of Investors」系列覆盖宏观、股权、固收、另类全赛道。Target-date 基金规模全球前 5。",
        "notable_en": "World's top-5 asset manager; Guide to the Markets quarterly publication is industry benchmark; David Kelly's market strategy team globally cited.",
        "notable_zh": "全球前 5 大资产管理公司；《Guide to the Markets》季度报告是机构投资行业基准；David Kelly 市场策略团队全球被广泛引用。",
    },
    "verdad-capital": {
        "founded": "2014", "aum": "~$1B", "hq": "Boston, MA",
        "type_en": "Quantitative Boutique", "type_zh": "量化精品店",
        "desc_zh": "Boston 量化价值投资机构，2014 年由 Dan Rasmussen 创立（前 Bain Capital）。Weekly Research 系列以学术风格的实证研究闻名，主题涵盖私募股权真实回报、杠杆微盘股因子、日本资本效率等小众但深入的领域。研究内容自由开放，不设付费墙。",
        "notable_en": "Founded by Dan Rasmussen (ex-Bain Capital); empirical research dismantling private equity return claims; pioneered leveraged microcap factor research.",
        "notable_zh": "Dan Rasmussen（前 Bain Capital）创立；以拆解私募股权回报神话的实证研究著称；杠杆微盘因子研究先驱。",
    },
    "msci-research": {
        "founded": "1969", "aum": "~$18T+ benchmarked", "hq": "New York, NY",
        "type_en": "Listed Index/Analytics Provider (NYSE)", "type_zh": "上市指数与分析提供商 (纽交所)",
        "desc_zh": "全球领先的指数、ESG/气候分析与因子模型提供商，1969 年由 Capital International 创立，2007 年从 Morgan Stanley 分拆独立上市。核心业务覆盖：ACWI/EAFE/新兴市场基准指数（全球约 $18T 资金跟踪）、Quality/Value/Momentum 等因子指数、ESG 与气候评级、多资产风险模型与指数构建方法论。",
        "notable_en": "MSCI ACWI/EAFE/EM indexes benchmarked by $18T+ globally; pioneered factor index construction (Quality, Value, Momentum); ESG ratings used by 1,700+ institutional investors.",
        "notable_zh": "MSCI ACWI/EAFE/新兴市场指数全球跟踪规模超 $18 万亿；首创因子指数构建方法论（质量、价值、动量）；ESG 评级被 1,700+ 机构投资者采用。",
    },
    "natixis-im": {
        "founded": "2007", "aum": "~$1.5T", "hq": "Paris, France / Boston, MA",
        "type_en": "Multi-Affiliate Asset Manager", "type_zh": "多附属机构资管平台",
        "desc_zh": "法国 Groupe BPCE 旗下多附属机构资管平台，2007 年合并成立。汇集 15+ 家投资附属机构——Loomis Sayles（核心固收/credit）、Harris Associates（价值股）、Mirova（ESG/气候/影响力投资）等——以联邦化模式保留各家投资风格自主性。策略覆盖固定收益、价值股票、ESG/气候、多资产配置，欧洲 + 北美双总部。",
        "notable_en": "Multi-affiliate model preserves boutique autonomy across Loomis Sayles, Harris Associates, Mirova, etc.; Tactical Take podcast is one of the most accessible macro/portfolio strategy series among large managers.",
        "notable_zh": "多附属机构模式在 Loomis Sayles、Harris Associates、Mirova 等品牌下保留精品店自主性；Tactical Take 播客是大型资管中最易获取的宏观与组合策略系列之一。",
    },
    "apollo-global-management": {
        "founded": "1990", "aum": "~$1.03T", "hq": "New York, NY",
        "type_en": "Listed Alt Manager (NYSE)", "type_zh": "上市另类资管 (纽交所)",
        "desc_zh": "美国最大私募信贷/另类资管之一，1990 年由 Leon Black、Marc Rowan、Joshua Harris 创立。核心业务：私募信贷（直接贷款、ABF 资产支持金融开创者）、私募股权（杠杆收购起家）、房地产、保险负债驱动投资。2022 年与 Athene 合并后成为美国最大年金/保险关联资管，永续保险负债成为基金重要资金来源。",
        "notable_en": "Pioneered private credit ABF (asset-backed finance); Athene merger created largest US insurance-linked asset manager; Marc Rowan and Torsten Slok widely cited on macro/private credit.",
        "notable_zh": "私募信贷 ABF（资产支持金融）开创者；与 Athene 合并后成为美国最大保险关联资管；Marc Rowan 与首席经济学家 Torsten Slok 是宏观与私募信贷领域的关键意见领袖。",
    },
    "kkr": {
        "founded": "1976", "aum": "~$758B", "hq": "New York, NY",
        "type_en": "Listed Alternative Asset Manager (NYSE: KKR)", "type_zh": "上市另类资产管理公司 (纽交所: KKR)",
        "desc_zh": "全球最大另类资产管理人之一，1976 年由 Henry Kravis、George Roberts 和 Jerome Kohlberg 创立，开创现代杠杆收购模式。业务涵盖私募股权、基础设施、地产、私募信贷及保险（通过 Global Atlantic 拓展），2010 年纽交所上市。",
        "notable_en": "1988 RJR Nabisco buyout ($25B, the largest LBO at the time, subject of 'Barbarians at the Gate'). KKR Global Macro & Asset Allocation (GMAA) team led by Henry McVey produces widely-cited macro/portfolio research.",
        "notable_zh": "1988 年主导对 RJR Nabisco 的 250 亿美元杠杆收购（当时史上最大 LBO，《门口的野蛮人》原型）。Henry McVey 领衔的 KKR Global Macro & Asset Allocation (GMAA) 团队定期发布广为机构引用的宏观及组合配置研究。",
    },
    "janus-henderson": {
        "founded": "1969", "aum": "~$480B", "hq": "London, UK / Denver, CO",
        "type_en": "Active Asset Manager (private; delisted 2026)", "type_zh": "主动管理型资管 (已私有化，2026 退市)",
        "desc_zh": "2017 年由 Janus Capital（1969，美国）与 Henderson Group（1934，英国）合并成立，纽交所上市。覆盖股票、固收、多资产及另类投资。宏观研究以全球利率周期和地缘政治框架见长；固收团队聚焦投资级信贷与国债；股票侧重科技、医疗健康及成长赛道。2026 年 6 月由 Trian、General Catalyst 与卡塔尔投资局 (QIA) 完成约 74 亿美元私有化，并从纽交所退市。",
        "notable_en": "Nick Sheridan's value equity team; Ben Lofthouse's global equity income franchise; Myron Scholes (Nobel laureate) formerly on advisory board.",
        "notable_zh": "Nick Sheridan 价值股权团队；Ben Lofthouse 全球股息成长策略；宏观洞察系列持续跟踪全球利率周期与地缘政治风险。",
    },
    "research-affiliates": {
        "founded": "2002", "aum": "~$188B", "hq": "Newport Beach, CA",
        "type_en": "Quantitative Research / Index Licensor", "type_zh": "量化研究 / 指数授权机构",
        "desc_zh": "Newport Beach 量化投资研究机构，2002 年由 Rob Arnott 创立。开创 RAFI 基本面指数（Fundamental Indexation）——按销售/现金流/账面/分红等经济体量加权，作为市值加权基准的替代方案。本身不直接管钱，授权 Smart Beta、因子投资、Capital Market Assumptions（CMA）等策略至约 1880 亿美元资产规模。Insights 系列以学术风格的资产配置与长期回报预期分析见长。",
        "notable_en": "Founded by Rob Arnott (2002); pioneered fundamental indexation (RAFI) as alternative to cap-weighted benchmarks; widely-cited Capital Market Assumptions and smart-beta research; licensed strategies on ~$188B of assets.",
        "notable_zh": "Rob Arnott 2002 年创立；首创基本面指数（RAFI）作为市值加权基准的替代方案；Capital Market Assumptions 与 Smart Beta 研究为机构广泛引用；授权策略覆盖约 1880 亿美元资产规模。",
    },
    "gsam": {
        "founded": "1988", "aum": "~$4.0T", "hq": "New York, USA",
        "type_en": "Asset Management Arm of Global IB", "type_zh": "全球投行旗下资产管理子公司",
        "desc_zh": "高盛集团旗下资产管理子公司，全球前十大资管之一，跨公开市场与另类资产管理约 3.3 万亿美元。业务涵盖多资产配置、主动股票、固定收益（含 GIPS 机构平台）、ETF 与流动性管理，以及由 External Investing Group 与并购整合后的另类平台主导的私募股权、私募信贷、地产、基础设施和对冲基金多策略。服务机构、主权基金、保险及顾问渠道。",
        "notable_en": "Manages one of the world's largest fixed-income franchises; following the 2020 absorption of Goldman's Merchant Banking Division and the 2022 NN Investment Partners acquisition, alternatives AUM has scaled to ~$430B (private credit, PE, real assets, hedge funds). Petershill platform takes minority stakes in alternative GPs.",
        "notable_zh": "全球最大固收主动管理平台之一；2020 年整合高盛 Merchant Banking 部门、2022 年完成 NN Investment Partners 收购后，另类资产管理规模达约 4300 亿美元（私募信贷、私募股权、实物资产、对冲基金）。Petershill 平台对外少数股权投资多家另类 GP。",
    },
    "robeco": {
        "founded": "1929", "aum": "~€228B", "hq": "Rotterdam, Netherlands",
        "type_en": "Asset Manager", "type_zh": "资产管理公司",
        "desc_zh": "荷兰资产管理公司（~2000 亿欧元 AUM），1929 年成立于鹿特丹。量化股票投资先驱，1990 年代起将可持续投资 / ESG 全面整合到投资流程。强项：新兴市场股票、全球信贷、因子（价值/动量/质量/低波）策略。",
        "notable_en": "Quant equity pioneer (factor strategies since the 1990s) and one of the earliest mainstream managers to fully integrate sustainability/ESG into core investing; long-standing emerging-markets equity franchise.",
        "notable_zh": "量化股票投资先驱（1990 年代起部署因子策略），最早将可持续 / ESG 全面整合到主流投资流程的资管之一；新兴市场股票特许经营历史悠久。",
    },
    "de-shaw": {
        "founded": "1988", "aum": "~$85B", "hq": "New York, USA",
        "type_en": "Quantitative Hedge Fund / Multi-Strategy", "type_zh": "量化对冲基金 / 多策略",
        "desc_zh": "由计算机科学家 David E. Shaw 于 1988 年创立的量化多策略对冲基金（约 $85B AUM），以系统化/统计套利投资和深度技术能力见长。覆盖全球股票、宏观、信贷、可转债与私募资本；以严格的量化研究流程和强工程化基础设施区别于传统对冲基金。",
        "notable_en": "Pioneer of computational/statistical-arbitrage investing; its alumni network spawned Two Sigma and other quant firms, and Jeff Bezos conceived Amazon while a vice-president at the firm. The Library publishes long-form quant research papers (often only 1-3 per year) with academic-style rigor.",
        "notable_zh": "计算/统计套利量化投资的先驱；前员工创立了 Two Sigma 等量化机构，Jeff Bezos 亦在 D.E. Shaw 任副总裁期间萌生并创立 Amazon。Library 每年只发布 1-3 篇长篇量化研究论文，学术级别的严谨性。",
    },
    "metlife-im": {
        "founded": "1868", "aum": "~$700B", "hq": "Whippany, New Jersey, USA",
        "type_en": "Institutional Asset Manager (MetLife investment arm)", "type_zh": "机构资产管理（MetLife 投资部门）",
        "desc_zh": "MetLife 旗下机构资产管理平台（约 $700B AUM，截至 2026 年 3 月 31 日），总部位于新泽西州 Whippany。2025 年 12 月 30 日完成对 PineBridge Investments 的收购（不含其中国合资公司与私募股权基金业务），PineBridge 研究团队自此改在 MIM 平台发布。业务覆盖公开市场固定收益、私募信贷、不动产、农业金融、基础设施与多资产宏观策略。",
        "notable_en": "Investment arm of MetLife (founded 1868). Completed the $1.2B acquisition of PineBridge Investments on 30 December 2025; article bylines still read 'PineBridge Investments, a MetLife Investment Management company'. Recurring research: Global Risks Update, Relative Value & Tactical Asset Allocation, and the quarterly Leveraged Finance Asset Allocation Insights inherited from PineBridge.",
        "notable_zh": "MetLife（1868 年创立）的投资管理部门。2025 年 12 月 30 日以 12 亿美元完成对 PineBridge Investments 的收购，文章署名仍标注 'PineBridge Investments, a MetLife Investment Management company'。代表研究：Global Risks Update、Relative Value & Tactical Asset Allocation，以及承自 PineBridge 的季度 Leveraged Finance Asset Allocation Insights。",
    },
    "ares-management": {
        "founded": "1997", "aum": "~$644B", "hq": "Los Angeles, USA",
        "type_en": "Listed Alternative Asset Manager (NYSE: ARES)", "type_zh": "上市另类资产管理公司 (NYSE: ARES)",
        "desc_zh": "全球领先另类资产管理公司（约 $620B AUM），1997 年由 Tony Ressler 等人创立，2014 年纽交所上市。核心业务：私募信贷（美国直接借贷与中端市场最大平台之一）、私募股权、不动产/基础设施，以及通过 Aspida 开展的保险解决方案。",
        "notable_en": "Pioneer of US middle-market direct lending; Ares Capital Corporation (NASDAQ: ARCC) is the largest publicly traded BDC. Acquired Landmark Partners (2021), AMP Capital infrastructure (2022) and GLP Capital Partners' international platform (2025), expanding real assets footprint substantially.",
        "notable_zh": "美国中端市场直接借贷开创者；旗下 Ares Capital Corporation（NASDAQ: ARCC）是规模最大的上市 BDC。先后并购 Landmark Partners（2021）、AMP Capital 基础设施业务（2022）与 GLP Capital Partners 国际平台（2025），大幅扩展实物资产规模。",
    },
    "matthews-asia": {
        "founded": "1991", "aum": "~$7.6B", "hq": "San Francisco, CA",
        "type_en": "Asia/EM Equity Specialist", "type_zh": "亚洲/新兴市场股票专家",
        "desc_zh": "专注亚洲与新兴市场股票的资产管理公司，1991 年创立、私人持股，办公室设于旧金山与香港，管理规模约 76 亿美元。坚持主动、自下而上的基本面投资，覆盖中国、印度、日本及更广泛的新兴市场，产品线涵盖全市值成长、股息、小盘与创新主题，并新增主动型 ETF。",
        "notable_en": "One of the longest-established US-based Asia and emerging-markets equity specialists (privately owned since 1991); expanded from mutual funds into active ETFs in 2022, with CIO Sean Taylor leading a San Francisco- and Hong Kong-based investment team.",
        "notable_zh": "美国历史最悠久的专注亚洲及新兴市场股票的投资机构之一（1991 年成立、私人持股）；2022 年从共同基金扩展至主动型 ETF，由首席投资官 Sean Taylor 领导旧金山与香港两地的投研团队。",
    },
    "capital-group": {
        "founded": "1931", "aum": "~$3.3T", "hq": "Los Angeles, California, USA",
        "type_en": "Active Asset Manager", "type_zh": "主动型资产管理公司",
        "desc_zh": "全球最大的主动型资产管理公司之一（约 3.3 万亿美元 AUM），1931 年由 Jonathan Bell Lovelace 创立于洛杉矶，是 American Funds 的母公司。招牌「Capital System」让多位基金经理各自独立管理同一只基金的不同份额，再辅以长期、基本面驱动的主动投资，覆盖全球股票、固定收益与多资产策略。",
        "notable_en": "Home to the American Funds; pioneered 'The Capital System' multi-manager structure where several portfolio managers each run an independent sleeve of a single fund, pursuing consistent long-horizon active returns at massive scale.",
        "notable_zh": "American Funds 的母公司；首创「Capital System」多经理人结构——一只基金由多位经理人各自管理独立份额，在巨大规模下追求稳定的长期主动回报。",
    },
    "acadian-asset": {
        "founded": "1986", "aum": "~$233B", "hq": "Boston, MA",
        "type_en": "Quant Asset Manager", "type_zh": "量化资产管理公司",
        "desc_zh": "全球领先的量化、系统化投资管理公司（管理规模约 ~$233B），1986 年创立，总部位于波士顿。以因子驱动的自下而上选股著称：用模型对全球数千只股票按估值、质量、成长与市场情绪等信号系统化打分，覆盖全球、国际、新兴及前沿市场股票，并将同一套量化流程延伸至系统化信用与多资产策略。",
        "notable_en": "One of the oldest and largest systematic equity managers; its factor-based process spans global, international, emerging- and frontier-market equities and has been extended to systematic fixed income. Publicly traded on the NYSE as AAMI (formerly BrightSphere Investment Group).",
        "notable_zh": "业界历史最悠久、规模最大的系统化股票管理人之一；因子化流程覆盖全球、国际、新兴及前沿市场股票，并已延伸至系统化固定收益。以 NYSE 代码 AAMI 上市（前身为 BrightSphere Investment Group）。",
    },
    "lazard-am": {
        "founded": "1848", "aum": "~$260B", "hq": "New York, NY",
        "type_en": "Independent Active Manager", "type_zh": "独立主动管理资产公司",
        "desc_zh": "全球主动管理资产公司，1848 年创立，总部位于纽约，以新兴市场股票和宏观固定收益研究著称。覆盖股票、固定收益、多资产与实物资产策略，尤其在新兴市场基本面分析和地缘政治宏观研究方面具有深度积累。",
        "notable_en": "EM equity specialist and macro fixed-income manager; 'Lazard Perspective' research is widely cited for geopolitics, EM fundamentals, and global monetary policy analysis; founded by the Lazard brothers in New Orleans, 1848.",
        "notable_zh": "新兴市场股票和宏观固定收益领域专家；《Lazard Perspective》系列研究以地缘政治分析、新兴市场基本面和全球货币政策洞察著称；由 Lazard 兄弟 1848 年于新奥尔良创立。",
    },
    "rothschild-co-am": {
        "founded": "1811", "aum": "~€47B", "hq": "Paris, France",
        "type_en": "Boutique Asset Manager", "type_zh": "精品资产管理公司",
        "desc_zh": "罗斯柴尔德集团旗下精品主动资产管理部门,总部位于巴黎,管理约 €47B 资产,约 170 名员工遍布 9 个国家。秉持高信念、研究驱动的投资理念,覆盖多资产与宏观配置、欧洲及主题股票、信贷与固定收益,并融入财富管理视角,关注地缘政治、另类资产与 ESG。背靠逾两百年罗斯柴尔德金融世家传承。",
        "notable_en": "Active boutique within the 200-year-old Rothschild & Co group; the controlling family also owns the Bordeaux first-growth estates Château Lafite and Mouton Rothschild. The asset-management arm runs conviction-driven European equity, multi-asset and credit strategies for institutional and intermediary clients.",
        "notable_zh": "背靠拥有逾两百年历史的罗斯柴尔德金融世家;控股家族同时拥有波尔多一级名庄拉菲(Château Lafite)与木桐(Mouton Rothschild)。资产管理部门为机构与中介客户运作高信念欧洲股票、多资产及信贷策略。",
    },
    "goehring-rozencwajg": {
        "founded": "2015", "aum": "~$1.5B", "hq": "New York, USA",
        "type_en": "Natural Resources Asset Manager", "type_zh": "自然资源资产管理",
        "desc_zh": "专注自然资源的逆向价值投资精品资管（管理规模 ~$1.5B），2015 年由 Leigh Goehring 与 Adam Rozencwajg 在纽约创立。深耕能源、贵金属、铀、农业与基本金属的基本面研究，以大宗商品资本周期分析框架和集中持仓的长线逆向投资著称。",
        "notable_en": "Quarterly market commentaries read by 10,000+ investment professionals. Leigh Goehring has managed natural-resources portfolios since 1991 (Prudential Jennison, then Chilton's $5B Global Natural Resources Fund).",
        "notable_zh": "季度市场评论拥有超过 1 万名专业投资者读者。Leigh Goehring 自 1991 年起管理自然资源组合（先后执掌 Prudential Jennison 与 Chilton 规模达 $5B 的全球自然资源基金）。",
    },
    "cohen-steers": {
        "founded": "1986", "aum": "~$94B", "hq": "New York, USA",
        "type_en": "Asset Manager (Real Assets Specialist)", "type_zh": "资产管理（实物资产专家）",
        "desc_zh": "实物资产与另类收益专业资管（约 $94B AUM），1986 年由 Martin Cohen 与 Robert Steers 在纽约创立，是美国首家专注上市房地产证券的投资公司。核心领域：REITs、优先证券、上市基础设施与自然资源股票。",
        "notable_en": "Pioneered listed real-estate investing in the US; among the world's largest REIT securities managers (third largest as of 2025). Research spans real estate, preferreds, infrastructure and resource equities.",
        "notable_zh": "美国上市房地产投资先驱，全球第三大 REIT 证券管理机构（2025）。研究覆盖房地产、优先证券、基础设施与自然资源股票。",
    },
    "principal-am": {
        "founded": "1879", "aum": "$577.9B", "hq": "Des Moines, USA",
        "type_en": "Asset Manager", "type_zh": "资产管理",
        "desc_zh": "Principal Financial Group（1879 年创立）旗下全球资管板块（$577.9B AUM），总部位于美国 Des Moines。业务覆盖股票、固定收益、多资产配置与房地产投资，服务 80 多个市场的 1,600 余家机构客户，商业地产投资平台位居美国前列。",
        "notable_en": "Publishes timely macro reaction notes on Fed/ECB decisions and the quarterly Global Market Perspectives outlook; its real-estate research arm covers one of the largest US commercial property portfolios.",
        "notable_zh": "以 Fed/ECB 决议快评与季度《Global Market Perspectives》宏观展望著称；房地产研究依托美国最大商业地产投资组合之一。",
    },
    "franklin-templeton": {
        "founded": "1947", "aum": "~$1.66T", "hq": "San Mateo, California, USA",
        "type_en": "Global Asset Manager (multi-boutique)", "type_zh": "全球资产管理（多精品店模式）",
        "desc_zh": "全球最大的独立资产管理公司之一，管理规模约 1.66 万亿美元。1947 年由 Rupert H. Johnson Sr. 创立于纽约，总部现设加州圣马特奥。采用多精品店模式运营：Templeton 主打新兴市场价值股，Western Asset 与 Brandywine 负责固定收益，Clarion 做地产，另有 Legg Mason、Putnam 等品牌，覆盖股票、债券、新兴市场与多资产策略。",
        "notable_en": "Home of Sir John Templeton, the pioneer of global contrarian value investing whose Templeton Growth Fund set the template for cross-border equity investing. Scaled into a top-tier multi-boutique platform through the Legg Mason (2020) and Putnam (2024) acquisitions.",
        "notable_zh": "旗下 Templeton 系出自全球逆向价值投资先驱约翰·邓普顿爵士，其 Templeton Growth Fund 奠定了跨国股票投资的范式。通过收购 Legg Mason（2020）与 Putnam（2024）扩张为顶级多精品店平台。",
    },
    "partners-group": {
        "founded": "1996", "aum": "~$186B", "hq": "Baar-Zug, Switzerland",
        "type_en": "Private Markets Asset Manager", "type_zh": "私募市场资产管理",
        "desc_zh": "瑞士上市的私募市场专家，1996 年创立于楚格州 Baar，管理规模约 $186B。以「转型式投资」为核心：在私募股权、私募信贷、基础设施与房地产中取得控股权，靠运营改造而非杠杆创造回报，并按主题（去碳化、供应链重构等）自上而下选赛道。区别于同业之处在于很早就把常青半流动基金做成主力产品线，把私募市场开放给财富管理渠道。",
        "notable_en": "A pioneer of evergreen semi-liquid private-markets vehicles, which now sit alongside traditional closed-ended programs and mandates as one of its three AuM pillars. Targets USD 450 billion of AuM by 2033.",
        "notable_zh": "常青型半流动私募基金的开创者之一，该产品线已与传统封闭式基金、专户并列为其三大 AuM 支柱。公司公开目标是 2033 年把管理规模做到 $450B。",
    },
    "blue-owl-capital": {
        "founded": "2021", "aum": "~$319B", "hq": "New York, USA",
        "type_en": "Alternative Asset Manager", "type_zh": "另类资产管理",
        "desc_zh": "美国另类资产管理公司，2021 年由 Owl Rock 与 Dyal Capital 合并而成并登陆纽交所（NYSE: OWL），管理规模约 $319B。三大平台：面向中高端市场赞助商的直接借贷、含数据中心的净租赁实物资产、以及 GP 战略资本——收购其他另类管理人的少数股权，这条赛道由前身 Dyal 开创。逾 $225B 属永久资本，无被迫抛售压力。",
        "notable_en": "Its GP Strategic Capital arm (formerly Dyal) effectively created the GP-stakes niche and set a record with the $12.9B close of GP Stakes V in 2023. The credit platform runs Blue Owl Capital Corporation, the second-largest publicly traded BDC, and the firm has bought its way into new asset classes almost yearly — Oak Street, Wellfleet, Prima, Kuvare, Atalaya, IPI Partners.",
        "notable_zh": "GP 战略资本平台（前身 Dyal）开创了 GP 少数股权这一细分赛道，2023 年 GP Stakes V 以 $12.9B 创下该品类募集纪录。信贷平台旗下的 Blue Owl Capital Corporation 是美国第二大上市 BDC。公司几乎每年靠收购进入新资产类别：Oak Street、Wellfleet、Prima、Kuvare、Atalaya、IPI Partners。",
    },
    "loomis-sayles": {
        "founded": "1926", "aum": "~$435B", "hq": "Boston, USA",
        "type_en": "Asset Manager (Fixed Income)", "type_zh": "资产管理（固定收益）",
        "desc_zh": "1926 年创立于波士顿的老牌固定收益管理人，管理规模 ~$435B。以自下而上的信用研究立身：中央研究团队覆盖企业债、证券化产品、市政债与主权债，之上是一批被称为 Alpha Engine 的自治专才团队，各有独立投资哲学。强项在多部门与全球固定收益、新兴市场债和市政债，另设一支长期集中持股的成长股团队。",
        "notable_en": "Runs an \"Alpha Engine\" model — specialist teams with distinct philosophies drawing on one shared central research bench rather than a single house view, which is why its municipal, global fixed income and EM debt desks publish independent monthly outlooks. Its growth-equity arm was founded and is still led by CIO Aziz Hamzaogullari.",
        "notable_zh": "招牌是 \"Alpha Engine\" 模式：各投资团队保有独立投资哲学，共用同一套中央研究平台，而非统一的公司观点——所以市政债、全球固定收益、新兴市场债三条线各自发布月度展望。成长股团队由 CIO Aziz Hamzaogullari 创立并领导至今。",
    },
}

_STRATEGY_LABELS: dict[str, str] = {
    "macro": "Macro", "quant": "Quant/CTA", "fixed_income": "Fixed Income",
    "multi_asset": "Multi-Asset", "equity": "Equity",
    "emerging_markets": "Emerging Markets", "esg_climate": "ESG/Climate",
    "private_equity": "Private Equity", "venture_capital": "VC",
    "private_credit": "Private Credit", "event_driven": "Event Driven",
    "real_assets": "Real Assets",
}

# ── Country flags (inline SVG, rendered next to each fund's badge) ──
# Self-contained SVGs: no <defs>/id references, so the same flag can be inlined
# many times without DOM id collisions. Country is derived from the profile HQ
# string (which may list two HQs separated by "/", e.g. dual-HQ managers).
_FLAG_SVGS: dict[str, str] = {
    "US": ('<svg class="flag" viewBox="0 0 39 26" role="img" aria-label="United States">'
           '<rect width="39" height="26" fill="#b22234"/>'
           '<g fill="#fff"><rect y="2" width="39" height="2"/><rect y="6" width="39" height="2"/>'
           '<rect y="10" width="39" height="2"/><rect y="14" width="39" height="2"/>'
           '<rect y="18" width="39" height="2"/><rect y="22" width="39" height="2"/></g>'
           '<rect width="16" height="14" fill="#3c3b6e"/></svg>'),
    "GB": ('<svg class="flag" viewBox="0 0 60 30" role="img" aria-label="United Kingdom">'
           '<rect width="60" height="30" fill="#012169"/>'
           '<path d="M0,0 60,30 M60,0 0,30" stroke="#fff" stroke-width="6"/>'
           '<path d="M0,0 60,30 M60,0 0,30" stroke="#c8102e" stroke-width="3.5"/>'
           '<path d="M30,0 V30 M0,15 H60" stroke="#fff" stroke-width="10"/>'
           '<path d="M30,0 V30 M0,15 H60" stroke="#c8102e" stroke-width="6"/></svg>'),
    "FR": ('<svg class="flag" viewBox="0 0 3 2" role="img" aria-label="France">'
           '<rect width="3" height="2" fill="#fff"/><rect width="1" height="2" fill="#002395"/>'
           '<rect x="2" width="1" height="2" fill="#ed2939"/></svg>'),
    "NL": ('<svg class="flag" viewBox="0 0 9 6" role="img" aria-label="Netherlands">'
           '<rect width="9" height="6" fill="#fff"/><rect width="9" height="2" fill="#ae1c28"/>'
           '<rect y="4" width="9" height="2" fill="#21468b"/></svg>'),
}

_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}

# Non-US country keywords → ISO code. Order matters (most specific first).
_HQ_COUNTRY_KEYWORDS = [
    ("netherlands", "NL"), ("france", "FR"),
    ("united kingdom", "GB"), ("uk", "GB"), ("scotland", "GB"), ("england", "GB"),
    ("canada", "CA"), ("germany", "DE"), ("switzerland", "CH"),
    ("japan", "JP"), ("hong kong", "HK"), ("singapore", "SG"), ("australia", "AU"),
]


def _hq_to_flags(hq: str) -> list[str]:
    """Map an HQ string to a list of ISO country codes that have a flag SVG.

    Handles dual-HQ managers (parts split on '/'). US is inferred from a
    trailing 2-letter state code or a 'USA'/'United States' token. Unknown
    countries return no flag rather than a wrong one (safe for future funds)."""
    codes: list[str] = []
    for part in hq.split("/"):
        p = part.strip()
        code = None
        pl = p.lower()
        for kw, c in _HQ_COUNTRY_KEYWORDS:
            if kw in pl:
                code = c
                break
        if code is None:
            last = p.split(",")[-1].strip().upper().rstrip(".")
            if last in {"USA", "US", "UNITED STATES"} or last in _US_STATES:
                code = "US"
        if code and code not in codes:
            codes.append(code)
    return [c for c in codes if c in _FLAG_SVGS]


def _build_sources_view(sources: dict[str, dict]) -> str:
    """Generate fund profile cards for the Sources tab."""
    cards = []
    for sid, src in sources.items():
        profile = _FUND_PROFILES.get(sid, {})
        color = BADGE_COLORS.get(sid, "#8b949e")
        name = html.escape(src.get("name", sid))
        short = html.escape(src.get("short_name", sid))
        url = html.escape(src.get("url", "#"))
        hostname = html.escape(src.get("expected_hostname", url))
        desc_en = html.escape(src.get("description", ""))
        desc_zh = html.escape(profile.get("desc_zh", ""))

        founded = profile.get("founded", "—")
        aum = profile.get("aum", "—")
        hq = profile.get("hq", "—")
        type_en = html.escape(profile.get("type_en", ""))
        type_zh = html.escape(profile.get("type_zh", ""))
        notable_en = html.escape(profile.get("notable_en", ""))
        notable_zh = html.escape(profile.get("notable_zh", ""))

        tags = "".join(
            f'<span class="sc-tag">{html.escape(_STRATEGY_LABELS.get(t, t))}</span>'
            for t in src.get("strategy_tags", [])
        )
        flag_svgs = "".join(_FLAG_SVGS[c] for c in _hq_to_flags(str(hq)))
        flags_html = (
            f'<span class="sc-flags" title="{html.escape(str(hq))}">{flag_svgs}</span>'
            if flag_svgs else ""
        )
        badge_text = "#0b1220" if color in ("#7dd3fc", "#86efac") else "#fff"

        desc_block = f'<p class="sc-desc lang-en">{desc_en}</p>'
        if desc_zh:
            desc_block += f'<p class="sc-desc lang-zh" style="display:none">{desc_zh}</p>'

        cards.append(f"""      <div class="source-card" style="--sc-accent:{color}">
        <div class="sc-head">
          <div class="sc-title">
            {flags_html}<span class="badge" style="background:{color};color:{badge_text}">{short}</span>
            <span class="sc-name">{name}</span>
          </div>
          <span class="sc-founded"><span class="lang-en">Est. {founded}</span><span class="lang-zh" style="display:none">创立 {founded}</span></span>
        </div>
        <div class="sc-stats">
          <span class="sc-stat"><strong>{aum}</strong> AUM</span>
          <span class="sc-stat">{hq}</span>
          <span class="sc-stat lang-en">{type_en}</span><span class="sc-stat lang-zh" style="display:none">{type_zh}</span>
        </div>
        <div class="sc-tags">{tags}</div>
        {desc_block}
        <div class="sc-notable lang-en">{notable_en}</div>
        <div class="sc-notable lang-zh" style="display:none">{notable_zh}</div>
        <div class="sc-footer"><a class="sc-link" href="{url}" target="_blank" rel="noopener">{hostname} →</a></div>
      </div>""")
    return "\n".join(cards)


def load_articles() -> list[dict]:
    """Load articles from JSONL file."""
    articles: list[dict] = []
    if not DATA_FILE.exists():
        return articles
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    articles.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return articles


def _load_sources() -> dict[str, dict]:
    """Load source config keyed by source id."""
    if not SOURCES_FILE.exists():
        return {}
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {s["id"]: s for s in data.get("sources", [])}


def _esc(text: str) -> str:
    """HTML-escape user content."""
    return html.escape(str(text)) if text else ""


def _slugify_theme(theme: str) -> str:
    """Convert a theme label into a stable DOM-safe slug."""
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in theme)
    return "-".join(part for part in slug.split("-") if part)


def _article_card(a: dict, show_takeaway: bool = False) -> tuple[str, dict | None]:
    """Render a single article as a timeline row.

    Returns (html, details_payload). For summarized articles the <details>
    element is emitted as a shell (summary only); the analysis body is moved
    into details_payload so the caller can inject it into a single JSON data
    island and hydrate lazily on first open. This trims ~50% of HTML size and
    cuts initial DOM construction cost from ~6000 to ~3000 nodes.
    """
    sid = a.get("source_id", "unknown")
    color = BADGE_COLORS.get(sid, "#8b949e")
    title = _esc(a.get("title", "Untitled"))
    url = _esc(a.get("url", "#"))
    date = _esc(a.get("date", "n/a"))
    source_name = _esc(a.get("source_name", sid))

    details_payload: dict | None = None
    if a.get("summarized"):
        takeaway_en = _esc(a.get("key_takeaway_en", ""))
        takeaway_zh = _esc(a.get("key_takeaway_zh", ""))
        summary_en = _esc(a.get("summary_en", ""))
        summary_zh = _esc(a.get("summary_zh", ""))
        theme_tags = "".join(
            f'<button class="theme-tag" onclick="filterSingleTheme(\'{_slugify_theme(t)}\')">{_esc(t)}</button>'
            for t in a.get("themes", [])
        )
        toggle = '<button class="row-toggle" type="button">Open</button>'
        # Shell only — body injected by JS hydrateArticleDetails() on first open
        summary_html = (
            '<details class="summary-panel">'
            '<summary><span class="lang-en">Analysis</span>'
            '<span class="lang-zh" style="display:none">分析</span></summary>'
            '</details>'
        )
        details_payload = {
            "tk_en": takeaway_en, "tk_zh": takeaway_zh,
            "bd_en": summary_en, "bd_zh": summary_zh,
            "tags": theme_tags,
        }
        # Inline takeaway for cluster view
        inline_takeaway = ""
        if show_takeaway and takeaway_en:
            inline_takeaway = (
                f'<p class="inline-takeaway lang-en">{takeaway_en}</p>'
                f'<p class="inline-takeaway lang-zh" style="display:none">{takeaway_zh}</p>'
            )
    else:
        toggle = '<span class="index-chip">Index</span>'
        summary_html = ""
        inline_takeaway = ""

    html = f"""<div class="row-main">
    <span class="badge" style="background:{color}">{source_name}</span>
    <span class="date">{date}</span>
    <a class="headline" href="{url}" target="_blank" rel="noopener">{title}</a>
    <span class="row-spacer"></span>
    {toggle}
  </div>
  {inline_takeaway if show_takeaway else ""}
  {summary_html}"""
    return html, details_payload


def generate_html(articles: list[dict]) -> str:
    """Generate the full HTML dashboard string from a list of article dicts."""
    sources = _load_sources()
    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")

    # Sort by date descending
    sorted_articles = sorted(
        articles,
        key=lambda a: a.get("date") or "1970-01-01",
        reverse=True,
    )

    # Stats
    total = len(sorted_articles)
    week_ago = (datetime.now(BJT) - timedelta(days=7)).strftime("%Y-%m-%d")
    new_this_week = sum(1 for a in sorted_articles if (a.get("date") or "") >= week_ago)
    fund_count = len(set(a.get("source_id", "") for a in sorted_articles)) or len(sources) or 5
    production_source_count = len(sources)

    # Recency split: articles older than RECENT_DAYS days are tagged data-age="older"
    # so CSS (body.hide-older …) can fold them by default. Empty/unparseable dates
    # default to "recent" (safer: visible, not silently hidden).
    older_cutoff = (datetime.now(BJT) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    def _age_of(a: dict) -> str:
        d = a.get("date") or ""
        return "older" if d and d < older_cutoff else "recent"
    older_count = sum(1 for a in sorted_articles if _age_of(a) == "older")

    # ── Theme grouping (all articles, for sidebar) ──
    themes: dict[str, list[dict]] = defaultdict(list)
    for a in sorted_articles:
        if a.get("summarized") and a.get("themes"):
            for t in a["themes"]:
                themes[t].append(a)
    sorted_themes = sorted(themes.items(), key=lambda x: len(x[1]), reverse=True)

    # ── Theme clusters: assign each article to ONE primary theme ──
    primary_clusters: dict[str, list[dict]] = defaultdict(list)
    assigned_ids: set[str] = set()
    # First pass: assign themed articles to first theme only
    for a in sorted_articles:
        article_themes = a.get("themes", [])
        if article_themes:
            primary_clusters[article_themes[0]].append(a)
            assigned_ids.add(a.get("id", ""))
    # Second pass: unthemed go to General
    for a in sorted_articles:
        if a.get("id", "") not in assigned_ids:
            primary_clusters["General"].append(a)

    # Sort clusters: by count desc, General always last
    cluster_order = sorted(
        [(k, v) for k, v in primary_clusters.items() if k != "General"],
        key=lambda x: len(x[1]),
        reverse=True,
    )
    if "General" in primary_clusters:
        cluster_order.append(("General", primary_clusters["General"]))

    # ── Build the unified article pool (single source of truth) ──
    # Every article gets rendered EXACTLY ONCE here, carrying data-* attributes
    # so the view-switching JS can move the card into whichever view is active.
    # Note: if sorted_articles contains duplicate article ids (an upstream
    # pipeline dedup issue, not this function's concern), each dup renders as
    # its own <article> in the pool. At runtime document.querySelector('#a-...')
    # returns the first match; the second pool entry is orphaned (no view
    # container ever references it, so it stays hidden in the pool). This is
    # intentional graceful degradation, not a behavior this refactor introduces.
    pool_parts: list[str] = []
    details_by_aid: dict[str, dict] = {}
    for seq, a in enumerate(sorted_articles):
        sid = a.get("source_id", "unknown")
        aid = a.get("id", "")
        theme_slugs = " ".join(
            _slugify_theme(t) for t in a.get("themes", [])
        ) if a.get("themes") else "unthemed"
        card_html, details_payload = _article_card(a, show_takeaway=True)
        # Articles older than RECENT_DAYS are folded behind "Show older" by CSS;
        # their LLM analysis bodies are also excluded from the JSON island to
        # keep initial parse cost flat. Clicking Open on a revealed older article
        # shows a bilingual "see source above" notice (handled in JS hydrate).
        if details_payload is not None and _age_of(a) != "older":
            details_by_aid[f"a-{_esc(aid)}"] = details_payload
        # data-seq = position in the global date-descending order. The timeline
        # view sorts by it because pool DOM order is scrambled after any
        # themes/funds hydration (returnArticlesToPool appends in document order).
        pool_parts.append(
            f'<article id="a-{_esc(aid)}" class="pool-article" '
            f'data-source-id="{_esc(sid)}" '
            f'data-date="{_esc(a.get("date", ""))}" '
            f'data-seq="{seq}" '
            f'data-age="{_age_of(a)}" '
            f'data-themes="{theme_slugs}">'
            f'{card_html}</article>'
        )
    article_pool_html = "\n".join(pool_parts)

    # JSON data island for lazy <details> hydration. Escape </ to <\/ so the
    # HTML parser does not prematurely close the script tag if any analysis
    # body happens to contain a literal '</script>'-like substring.
    details_json = json.dumps(details_by_aid, ensure_ascii=False).replace("</", "<\\/")
    details_island = (
        f'<script type="application/json" id="article-details-data">'
        f'{details_json}</script>'
    )

    # ── Build cluster HTML (Themes view) ──
    cluster_parts = []
    for theme_name, cluster_arts in cluster_order:
        source_set = set(a.get("source_id", "") for a in cluster_arts)
        cross_fund = len(source_set) >= 2
        new_count = sum(1 for a in cluster_arts if (a.get("date") or "") >= week_ago)
        slug = _slugify_theme(theme_name) if theme_name != "General" else "general"
        cross_badge = '<span class="cross-fund-badge">Cross-fund</span>' if cross_fund else ""
        new_badge = f'<span class="new-badge">{new_count} new</span>' if new_count else ""
        fund_names = ", ".join(sorted(
            set(_esc(a.get("source_name", "")) for a in cluster_arts)
        ))

        if theme_name == "General":
            # Compact table for unthemed articles
            table_rows = []
            for a in cluster_arts:
                sid = a.get("source_id", "unknown")
                color = BADGE_COLORS.get(sid, "#8b949e")
                takeaway_en = _esc(a.get("key_takeaway_en", ""))
                takeaway_zh = _esc(a.get("key_takeaway_zh", ""))
                tooltip = f' title="{takeaway_en}"' if takeaway_en else ""
                table_rows.append(
                    f'<tr><td class="ct-date">{_esc(a.get("date", ""))}</td>'
                    f'<td><span class="badge" style="background:{color}">{_esc(a.get("source_name", ""))}</span></td>'
                    f'<td><a href="{_esc(a.get("url", "#"))}" target="_blank" rel="noopener"{tooltip}>{_esc(a.get("title", ""))}</a></td></tr>'
                )
            table_html = "\n".join(table_rows)
            cluster_parts.append(
                f"""<section class="cluster general-cluster" data-cluster="{slug}">
  <div class="cluster-head">
    <h2>{_esc(theme_name)} <span class="cluster-count">{len(cluster_arts)}</span></h2>
    <div class="cluster-meta"><span class="lang-en">Uncategorized articles — hover for takeaway</span><span class="lang-zh" style="display:none">未分类文章 — 悬停查看摘要</span></div>
  </div>
  <table class="compact-table">{table_html}</table>
</section>"""
            )
        else:
            # Full cluster card — articles injected by JS via data-article-ids
            article_ids = " ".join(_esc(a.get("id", "")) for a in cluster_arts)
            cluster_parts.append(
                f"""<section class="cluster" data-cluster="{slug}">
  <div class="cluster-head">
    <div>
      <h2>{_esc(theme_name)} <span class="cluster-count">{len(cluster_arts)}</span> {cross_badge} {new_badge}</h2>
      <div class="cluster-meta">{fund_names}</div>
    </div>
  </div>
  <div class="cluster-articles" data-article-ids="{article_ids}"></div>
</section>"""
            )
    clusters_html = "\n".join(cluster_parts)

    # ── Timeline rows (existing bulletin view) ──
    theme_filters = []
    for theme_name, theme_arts in sorted_themes:
        theme_filters.append(
            f'<button class="filter-pill" data-theme="{_slugify_theme(theme_name)}" onclick="toggleThemeFilter(this)">'
            f'{_esc(theme_name)} <span>{len(theme_arts)}</span></button>'
        )
    unthemed_count = sum(1 for a in sorted_articles if not a.get("themes"))
    if unthemed_count > 0:
        theme_filters.append(
            f'<button class="filter-pill" data-theme="unthemed" onclick="toggleThemeFilter(this)">'
            f'General <span>{unthemed_count}</span></button>'
        )
    theme_filters_html = "".join(theme_filters) if theme_filters else '<span class="muted">Themes appear after analysis.</span>'

    # ── Timeline view: empty wrapper; articles injected by JS on view activation ──
    load_more_btn = ""
    if total > INITIAL_VISIBLE:
        load_more_btn = (
            f'<button class="btn-load-more" onclick="showAll()">'
            f'Load more ({total - INITIAL_VISIBLE} remaining)</button>'
        )
    timeline_html = (
        f'<div class="timeline-wrap" '
        f'data-total="{total}" '
        f'data-initial-visible="{INITIAL_VISIBLE}"></div>'
    )

    # ── Funds view ──
    source_order = list(sources.keys())
    fund_all: dict[str, list[dict]] = defaultdict(list)
    for a in sorted_articles:
        fund_all[a.get("source_id", "")].append(a)

    fund_view_parts = []
    for sid in source_order:
        src = sources.get(sid, {})
        color = BADGE_COLORS.get(sid, "#8b949e")
        name = _esc(src.get("name", sid))
        arts = fund_all.get(sid, [])
        analyzed = sum(1 for a in arts if a.get("summarized"))
        latest = arts[0].get("date", "n/a") if arts else "n/a"
        article_ids = " ".join(_esc(a.get("id", "")) for a in arts)
        fund_view_parts.append(
            f"""<section class="cluster fund-section" data-source-id="{_esc(sid)}" style="--fund-accent:{color}">
  <div class="cluster-head">
    <div>
      <h2><span class="badge" style="background:{color}">{name}</span> <span class="cluster-count">{len(arts)} articles · {analyzed} analyzed</span></h2>
      <div class="cluster-meta"><span class="lang-en">Latest: {latest}</span><span class="lang-zh" style="display:none">最新: {latest}</span></div>
    </div>
  </div>
  <div class="cluster-articles" data-article-ids="{article_ids}"></div>
</section>"""
        )
    funds_view_html = "\n".join(fund_view_parts)

    # ── Fund distribution bar chart (Funds view header) ──
    dist_entries = [(sid, len(fund_all.get(sid, []))) for sid in source_order]
    dist_entries = [(sid, n) for sid, n in dist_entries if n > 0]
    dist_entries.sort(key=lambda x: x[1], reverse=True)
    max_count = dist_entries[0][1] if dist_entries else 0
    total_count = sum(n for _, n in dist_entries)
    dist_rows = []
    for sid, count in dist_entries:
        src = sources.get(sid, {})
        color = BADGE_COLORS.get(sid, "#8b949e")
        name = _esc(src.get("name", sid))
        width_pct = (count / max_count) * 100 if max_count else 0
        dist_rows.append(
            f'<div class="fund-dist-row" data-source-id="{_esc(sid)}">'
            f'<span class="fund-dist-name" style="color:{color}">{name}</span>'
            f'<div class="fund-dist-track">'
            f'<div class="fund-dist-bar" style="width:{width_pct:.1f}%; background:{color}"></div>'
            f'</div>'
            f'<span class="fund-dist-count">{count}</span>'
            f'</div>'
        )
    fund_distribution_html = (
        f'<div class="fund-distribution">'
        f'<h2 class="fund-dist-title">'
        f'<span class="lang-en">Article distribution — {total_count} total across {len(dist_entries)} funds</span>'
        f'<span class="lang-zh" style="display:none">文章分布 — {len(dist_entries)} 个基金共 {total_count} 篇</span>'
        f'</h2>'
        f'<div class="fund-dist-rows">{"".join(dist_rows)}</div>'
        f'</div>'
    ) if dist_entries else ""

    # ── Sidebar fund cards (compact, for Themes/Timeline views) ──
    fund_cards = []
    for sid in source_order:
        src = sources.get(sid, {})
        color = BADGE_COLORS.get(sid, "#8b949e")
        name = _esc(src.get("name", sid))
        arts = fund_all.get(sid, [])[:5]
        latest_date = arts[0].get("date", "n/a") if arts else "n/a"
        analyzed_count = sum(1 for a in arts if a.get("summarized"))
        art_list = "\n".join(
            f'<li><span class="mini-date">{_esc(a.get("date", "n/a"))}</span>'
            f'<a href="{_esc(a.get("url", "#"))}" target="_blank" rel="noopener">{_esc(a.get("title", ""))}</a></li>'
            for a in arts
        )
        if not art_list:
            art_list = '<li class="muted">No articles yet</li>'
        fund_cards.append(
            f"""<section class="fund-panel" style="--fund-accent:{color}">
  <div class="fund-head">
    <h3>{name}</h3>
    <span class="fund-count">{len(arts)} tracked</span>
  </div>
  <div class="fund-meta">
    <span>Latest {latest_date}</span>
    <span>{analyzed_count} analyzed</span>
  </div>
  <ul class="fund-links">{art_list}</ul>
</section>"""
        )
    fund_grid_html = "\n".join(fund_cards)

    # ── Sidebar theme tracker ──
    theme_sections = []
    for theme_name, theme_arts in sorted_themes:
        items = "\n".join(
            f'<li><span class="badge" style="background:{BADGE_COLORS.get(a.get("source_id", ""), "#8b949e")}">{_esc(a.get("source_name", ""))}</span>'
            f' <a href="{_esc(a.get("url", "#"))}" target="_blank" rel="noopener">{_esc(a.get("title", ""))}</a></li>'
            for a in theme_arts
        )
        theme_sections.append(
            f"""<div class="theme-group" data-theme="{_slugify_theme(theme_name)}">
  <h3>{_esc(theme_name)} <span class="count">({len(theme_arts)})</span></h3>
  <ul>{items}</ul>
</div>"""
        )
    themes_html = "\n".join(theme_sections) if theme_sections else '<p class="muted">No themes available yet.</p>'

    # ── Sources view ──
    sources_view_html = _build_sources_view(sources)

    fund_names_for_meta = ", ".join(
        sources[sid].get("name", sid) for sid in source_order if sid in sources
    )
    meta_description = _esc(f"Research aggregator: {fund_names_for_meta}.")

    older_toggle_btn = (
        f'<button id="btn-show-older" class="btn-toggle" onclick="toggleOlder()">'
        f'<span class="lang-en">Show older ({older_count})</span>'
        f'<span class="lang-zh" style="display:none">显示更旧 ({older_count})</span>'
        f'</button>'
    ) if older_count > 0 else ""

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hedge Fund Research Insights</title>
<meta name="description" content="{meta_description}">
<link rel="icon" href="/favicon.ico">
<style>
:root {{
  --bg: #0b1220; --surface: #111827; --surface2: #162033; --surface3: #0f1727;
  --border: #263247; --text: #dbe6f3; --text-muted: #8ea2bb;
  --accent: #7dd3fc; --accent2: #86efac; --accent3: #f9a8d4;
  --pill: #1e293b;
}}
*, *::before, *::after {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 0;
  background: var(--bg); color: var(--text);
  font-family: 'IBM Plex Sans', 'Segoe UI', Helvetica, Arial, sans-serif;
  line-height: 1.45;
}}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.container {{ max-width: 1360px; margin: 0 auto; padding: 18px 22px 28px; }}

/* ── Header ── */
.header {{
  background:
    linear-gradient(135deg, rgba(125, 211, 252, 0.08), transparent 40%),
    linear-gradient(180deg, rgba(17, 24, 39, 0.98), rgba(11, 18, 32, 0.98));
  border-bottom: 1px solid var(--border);
  padding: 18px 0 16px;
}}
.header .container {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }}
.header h1 {{ margin: 0; font-size: 1.6rem; letter-spacing: 0.02em; }}
.deck {{ margin-top: 4px; color: var(--text-muted); font-size: 0.88rem; }}
.stats {{ display: flex; flex-wrap: wrap; gap: 8px; color: var(--text-muted); font-size: 0.8rem; margin-top: 10px; }}
.stats span {{ padding: 4px 8px; border: 1px solid var(--border); background: rgba(15, 23, 39, 0.75); border-radius: 999px; }}
.header-actions {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
.btn-toggle {{
  background: var(--surface2); color: var(--text); border: 1px solid var(--border);
  padding: 7px 12px; border-radius: 999px; cursor: pointer; font-size: 0.78rem;
  text-transform: uppercase; letter-spacing: 0.04em;
}}
.btn-toggle:hover {{ background: var(--border); }}

/* ── Older-article folding: hide articles older than RECENT_DAYS by default ──
   Selector covers the pool itself and every view container that may host pool
   articles (themes/funds/timeline). #btn-show-older toggles body.hide-older. */
body.hide-older article.pool-article[data-age="older"] {{ display: none !important; }}
#btn-show-older.active {{ background: var(--accent); color: #0b1220; border-color: var(--accent); }}

/* ── View switcher ── */
.view-bar {{
  display: flex; gap: 4px; padding: 10px 0 14px;
  border-bottom: 1px solid var(--border); margin-bottom: 16px;
}}
.view-btn {{
  background: transparent; color: var(--text-muted); border: 1px solid transparent;
  padding: 7px 16px; border-radius: 999px; cursor: pointer; font-size: 0.82rem;
  font-weight: 600; letter-spacing: 0.02em; transition: all 0.15s;
}}
.view-btn:hover {{ color: var(--text); background: var(--surface2); }}
.view-btn.active {{
  color: var(--text); background: var(--surface2);
  border-color: var(--accent); box-shadow: 0 0 8px rgba(125,211,252,0.12);
}}
.view-panel {{ display: none; }}
.view-panel.active {{ display: block; }}

/* ── Board (2-col for timeline) ── */
.board {{
  display: grid;
  grid-template-columns: minmax(0, 1.9fr) minmax(320px, 0.95fr);
  gap: 18px; align-items: start;
}}
.board-full {{ display: block; }}

/* ── Shared: rail, badge, row ── */
.rail {{
  background: rgba(17, 24, 39, 0.84);
  border: 1px solid var(--border); border-radius: 18px;
  overflow: hidden; backdrop-filter: blur(10px);
}}
.rail-head {{
  display: flex; justify-content: space-between; align-items: center; gap: 12px;
  padding: 14px 16px; border-bottom: 1px solid var(--border); background: rgba(15, 23, 39, 0.9);
}}
.rail-head h2 {{ margin: 0; font-size: 1rem; letter-spacing: 0.03em; text-transform: uppercase; }}
.rail-copy {{ color: var(--text-muted); font-size: 0.78rem; }}
.timeline-wrap {{ padding: 8px 12px 14px; }}
.filter-bar {{
  display: flex; flex-wrap: wrap; gap: 8px; padding: 0 12px 12px;
  border-bottom: 1px solid var(--border);
}}
.filter-pill, .theme-tag {{
  border: 1px solid var(--border); background: var(--pill); color: var(--text-muted);
  border-radius: 999px; padding: 5px 10px; font-size: 0.75rem; cursor: pointer;
}}
.filter-pill span {{ color: var(--text); margin-left: 5px; }}
.filter-pill.active, .theme-tag:hover, .filter-pill:hover {{
  color: var(--text); border-color: var(--accent); background: rgba(125,211,252,0.1);
}}
.timeline-row, .timeline-wrap > .pool-article {{ border-bottom: 1px solid rgba(38,50,71,0.72); padding: 8px 0; }}
.row-main {{ display: flex; align-items: center; gap: 9px; min-width: 0; }}
.badge {{
  display: inline-block; padding: 2px 8px; border-radius: 999px;
  font-size: 0.69rem; color: #fff; font-weight: 700; white-space: nowrap;
  letter-spacing: 0.02em; flex-shrink: 0;
}}
.date, .mini-date {{ color: var(--text-muted); font-size: 0.73rem; white-space: nowrap; font-variant-numeric: tabular-nums; }}
.headline {{
  color: var(--text); font-size: 0.9rem; line-height: 1.3; min-width: 0;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden; white-space: normal;
}}
.headline:hover {{ color: var(--accent); }}
.row-spacer {{ flex: 1 1 auto; }}
.row-toggle, .index-chip {{
  border: 1px solid var(--border); border-radius: 999px; padding: 4px 9px;
  font-size: 0.7rem; background: transparent; color: var(--text-muted); flex-shrink: 0;
}}
.row-toggle {{ cursor: pointer; }}
.row-toggle:hover {{ color: var(--text); border-color: var(--accent); }}
.summary-panel {{
  margin: 8px 0 2px 60px; padding: 10px 12px;
  background: var(--surface3); border: 1px solid var(--border); border-radius: 12px;
}}
.summary-panel summary {{
  cursor: pointer; list-style: none;
  color: var(--accent); font-size: 0.8rem;
  text-transform: uppercase; letter-spacing: 0.05em;
}}
.summary-panel summary::-webkit-details-marker {{ display: none; }}
.summary-copy p {{ margin: 8px 0 0; font-size: 0.86rem; color: var(--text-muted); }}
.takeaway {{ color: var(--accent2); }}
.theme-tags {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }}
.btn-load-more {{
  display: block; margin: 14px auto 2px; padding: 8px 18px;
  background: var(--surface2); color: var(--accent); border: 1px solid var(--border);
  border-radius: 999px; cursor: pointer; font-size: 0.8rem;
}}
.btn-load-more:hover {{ background: var(--border); }}

/* ── Theme clusters (Themes view) ── */
.cluster-grid {{ display: grid; gap: 16px; }}
.cluster {{
  background: rgba(17,24,39,0.84); border: 1px solid var(--border);
  border-radius: 18px; overflow: hidden; backdrop-filter: blur(10px);
}}
.cluster-head {{
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 16px; border-bottom: 1px solid var(--border);
  background: rgba(15,23,39,0.9);
}}
.cluster-head h2 {{ margin: 0; font-size: 1.05rem; letter-spacing: 0.02em; }}
.cluster-count {{ color: var(--text-muted); font-weight: 400; font-size: 0.8rem; margin-left: 6px; }}
.cluster-meta {{ color: var(--text-muted); font-size: 0.76rem; margin-top: 2px; }}
.cross-fund-badge {{
  display: inline-block; padding: 2px 8px; border-radius: 999px;
  font-size: 0.68rem; font-weight: 700; color: #0b1220;
  background: linear-gradient(135deg, var(--accent), var(--accent2));
  margin-left: 8px; vertical-align: middle;
}}
.new-badge {{
  display: inline-block; padding: 2px 8px; border-radius: 999px;
  font-size: 0.68rem; font-weight: 700; color: #fff;
  background: #f85149; margin-left: 6px; vertical-align: middle;
}}
.cluster-articles {{ padding: 6px 14px 14px; }}
.cluster-item, .cluster-articles > .pool-article {{
  border-bottom: 1px solid rgba(38,50,71,0.5); padding: 8px 0;
}}
.cluster-item:last-child, .cluster-articles > .pool-article:last-child {{ border-bottom: none; }}
.inline-takeaway {{
  margin: 4px 0 2px 60px; font-size: 0.82rem; line-height: 1.4;
  color: var(--accent2); font-style: italic;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}}
/* Hide inline takeaway when article lives inside the Timeline wrapper.
   The same <article> card is shared across views; keeps Timeline dense. */
.timeline-wrap .inline-takeaway {{
  display: none !important;
}}

/* ── Compact table (General cluster) ── */
.compact-table {{
  width: 100%; border-collapse: collapse; font-size: 0.82rem;
  padding: 0; margin: 0;
}}
.compact-table tr {{ border-bottom: 1px solid rgba(38,50,71,0.5); }}
.compact-table tr:last-child {{ border-bottom: none; }}
.compact-table td {{ padding: 6px 8px; vertical-align: middle; }}
.compact-table .ct-date {{ width: 78px; color: var(--text-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }}
.compact-table a {{ color: var(--text); }}
.compact-table a:hover {{ color: var(--accent); }}
.general-cluster .compact-table {{ padding: 4px 14px 10px; }}

/* ── Sidebar ── */
.sidebar {{ display: grid; gap: 18px; }}
.sidebar-section {{ padding: 12px 14px 14px; }}
.section-title {{ margin: 0 0 10px; font-size: 0.96rem; text-transform: uppercase; letter-spacing: 0.04em; }}
.fund-stack, .theme-stack {{ display: grid; gap: 10px; }}
.fund-panel {{
  border: 1px solid var(--border); border-left: 3px solid var(--fund-accent);
  border-radius: 14px; padding: 10px 12px; background: var(--surface3);
}}
.fund-head, .fund-meta {{ display: flex; justify-content: space-between; gap: 10px; align-items: baseline; }}
.fund-head h3 {{ margin: 0; font-size: 0.94rem; }}
.fund-count, .fund-meta {{ color: var(--text-muted); font-size: 0.74rem; }}
.fund-links {{
  list-style: none; padding: 0; margin: 10px 0 0; display: grid; gap: 6px;
}}
.fund-links li {{
  display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 8px;
  align-items: start; font-size: 0.8rem;
}}
.fund-links a {{
  color: var(--text); display: -webkit-box; -webkit-line-clamp: 2;
  -webkit-box-orient: vertical; overflow: hidden; white-space: normal;
}}
.fund-links a:hover {{ color: var(--accent); }}
.theme-group a {{ color: var(--text); }}
.theme-group a:hover {{ color: var(--accent); }}
.theme-group {{
  border: 1px solid var(--border); border-radius: 14px;
  padding: 10px 12px; background: var(--surface3);
}}
.theme-group h3 {{ margin: 0 0 8px 0; font-size: 0.88rem; }}
.theme-group .count {{ color: var(--text-muted); font-weight: normal; font-size: 0.8rem; }}
.theme-group ul {{
  list-style: none; padding: 0; margin: 0; display: grid; gap: 6px; font-size: 0.8rem;
}}
.theme-group li {{ line-height: 1.35; }}

.muted {{ color: var(--text-muted); }}
.hidden-by-filter {{ display: none !important; }}

/* ── Fund distribution chart (Funds view header) ── */
.fund-distribution {{
  margin-bottom: 20px; padding: 14px 16px;
  background: rgba(17,24,39,0.5); border: 1px solid var(--border); border-radius: 12px;
}}
.fund-dist-title {{
  font-size: 0.88rem; margin: 0 0 12px; color: var(--text-muted);
  font-weight: 600; letter-spacing: 0.01em;
}}
.fund-dist-rows {{ display: grid; gap: 7px; }}
.fund-dist-row {{
  display: grid; grid-template-columns: 170px 1fr 44px; gap: 12px;
  align-items: center; font-size: 0.82rem;
}}
.fund-dist-name {{
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  font-weight: 600;
}}
.fund-dist-track {{
  background: rgba(255,255,255,0.06); border-radius: 4px; height: 10px; overflow: hidden;
}}
.fund-dist-bar {{
  height: 100%; border-radius: 4px; min-width: 2px;
  transition: width 0.3s ease;
}}
.fund-dist-count {{
  text-align: right; color: var(--text-muted);
  font-variant-numeric: tabular-nums; font-weight: 600;
}}
@media (max-width: 720px) {{
  .fund-dist-row {{ grid-template-columns: 110px 1fr 36px; gap: 8px; font-size: 0.76rem; }}
}}

/* ── Fund section (Funds view) ── */
.fund-section {{ border-left: 3px solid var(--fund-accent, var(--accent)); }}
.fund-section .cluster-head h2 {{ display: flex; align-items: center; gap: 10px; }}

/* ── Footer ── */
.footer {{
  margin-top: 22px; padding: 16px 0; border-top: 1px solid var(--border);
  text-align: center; color: var(--text-muted); font-size: 0.8rem;
}}
@media (max-width: 980px) {{
  .board {{ grid-template-columns: 1fr; }}
  .summary-panel {{ margin-left: 32px; }}
  .inline-takeaway {{ margin-left: 32px; }}
}}
@media (max-width: 720px) {{
  .container {{ padding: 14px 14px 22px; }}
  .row-main {{ flex-wrap: wrap; }}
  .headline {{ white-space: normal; overflow: visible; }}
  .summary-panel {{ margin-left: 0; }}
  .inline-takeaway {{ margin-left: 0; }}
  .fund-links li {{ grid-template-columns: 1fr; }}
  .view-bar {{ overflow-x: auto; }}
  .sources-grid {{ grid-template-columns: 1fr; }}
}}
/* ── Sources (fund profile) view ── */
.sources-intro {{ color: var(--text-muted); font-size: 0.84rem; margin-bottom: 16px; line-height: 1.5; }}
.sources-grid {{
  display: grid; grid-template-columns: repeat(auto-fill, minmax(430px, 1fr)); gap: 16px;
}}
.source-card {{
  background: rgba(17,24,39,0.84); border: 1px solid var(--border);
  border-left: 3px solid var(--sc-accent, var(--accent)); border-radius: 18px;
  padding: 15px 17px 13px; backdrop-filter: blur(10px);
  display: flex; flex-direction: column;
}}
.sc-head {{ display: flex; justify-content: space-between; align-items: center; gap: 10px; margin-bottom: 10px; }}
.sc-title {{ display: flex; align-items: center; gap: 8px; }}
.sc-flags {{ display: inline-flex; align-items: center; gap: 3px; flex-shrink: 0; }}
.flag {{ height: 13px; width: auto; border-radius: 2px; display: block; box-shadow: 0 0 0 0.5px rgba(0,0,0,0.25); }}
.sc-name {{ font-size: 1rem; font-weight: 700; }}
.sc-founded {{ color: var(--text-muted); font-size: 0.74rem; white-space: nowrap; flex-shrink: 0; }}
.sc-stats {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 9px; }}
.sc-stat {{
  background: var(--surface2); border: 1px solid var(--border);
  border-radius: 999px; padding: 3px 9px; font-size: 0.74rem; color: var(--text-muted);
}}
.sc-stat strong {{ color: var(--text); }}
.sc-tags {{ display: flex; flex-wrap: wrap; gap: 5px; margin-bottom: 10px; }}
.sc-tag {{ background: var(--pill); border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; font-size: 0.69rem; color: var(--text-muted); }}
.sc-desc {{ font-size: 0.84rem; color: var(--text-muted); line-height: 1.5; margin: 0 0 9px; }}
.sc-notable {{
  font-size: 0.78rem; color: var(--accent2); line-height: 1.45;
  padding-top: 9px; border-top: 1px solid rgba(38,50,71,0.6); margin-top: auto;
}}
.sc-notable::before {{ content: '★  '; opacity: 0.7; }}
.sc-footer {{ display: flex; justify-content: flex-end; margin-top: 8px; }}
.sc-link {{
  font-size: 0.73rem; color: var(--accent); opacity: 0.8;
  padding: 3px 9px; border: 1px solid var(--border); border-radius: 999px;
}}
.sc-link:hover {{ opacity: 1; text-decoration: none; background: var(--surface2); }}
.sources-aum-note {{ font-size: 0.73rem; color: var(--text-muted); margin-top: 14px; text-align: right; opacity: 0.7; }}
</style>
<noscript>
<style>
/* No-JS fallback: if JS does not run, the view-switching logic never moves
   articles out of #article-pool into the per-view containers. Expose the pool
   as a flat list so visitors still see every article. */
#article-pool {{ display: block !important; }}
#article-pool .pool-article {{ border-bottom: 1px solid var(--border); padding: 12px 0; }}
.view-bar, .view-panel {{ display: none !important; }}
</style>
</noscript>
</head>
<body class="hide-older">

<div class="header">
  <div class="container">
    <div>
      <a href="/" style="font-size:0.82rem;color:var(--text-muted);text-decoration:none;">&larr; <span class="lang-en">Back to Infrastructure</span><span class="lang-zh" style="display:none">返回基础设施</span></a>
      <h1><span class="lang-en">Hedge Fund Research Insights</span><span class="lang-zh" style="display:none">对冲基金研究洞察</span></h1>
      <div class="deck"><span class="lang-en">Cross-fund research aggregator — scan by theme, timeline, or fund.</span><span class="lang-zh" style="display:none">跨基金研究聚合 — 按主题、时间线或基金浏览。</span></div>
      <div class="stats">
        <span>{total} articles</span>
        <span>{new_this_week} new this week</span>
        <span>{production_source_count} funds tracked</span>
        <span>Updated {now}</span>
      </div>
    </div>
    <div class="header-actions">
      {older_toggle_btn}
      <button class="btn-toggle" onclick="toggleLang()">CN / EN</button>
    </div>
  </div>
</div>

<!-- Hidden article pool: single-copy source of truth. JS moves cards from
     here into whichever view is active, then returns them on view switch. -->
<div id="article-pool" style="display:none">
{article_pool_html}
</div>

<div class="container">
  <div class="view-bar">
    <button class="view-btn active" data-view="themes" onclick="switchView('themes')"><span class="lang-en">Themes</span><span class="lang-zh" style="display:none">主题</span></button>
    <button class="view-btn" data-view="timeline" onclick="switchView('timeline')"><span class="lang-en">Timeline</span><span class="lang-zh" style="display:none">时间线</span></button>
    <button class="view-btn" data-view="funds" onclick="switchView('funds')"><span class="lang-en">Funds</span><span class="lang-zh" style="display:none">基金</span></button>
    <button class="view-btn" data-view="sources" onclick="switchView('sources')"><span class="lang-en">Sources</span><span class="lang-zh" style="display:none">来源介绍</span></button>
  </div>

  <!-- ═══ THEMES VIEW (default) ═══ -->
  <div class="view-panel active" id="view-themes">
    <div class="cluster-grid">
      {clusters_html}
    </div>
  </div>

  <!-- ═══ TIMELINE VIEW ═══ -->
  <div class="view-panel" id="view-timeline">
    <div class="board">
      <section class="rail">
        <div class="rail-head">
          <div>
            <h2><span class="lang-en">Bulletin Feed</span><span class="lang-zh" style="display:none">研究公告</span></h2>
            <div class="rail-copy"><span class="lang-en">Chronological feed — expand rows to inspect.</span><span class="lang-zh" style="display:none">按时间排序 — 展开查看详情。</span></div>
          </div>
        </div>
        <div class="filter-bar">
          {theme_filters_html}
        </div>
        {timeline_html}
        {load_more_btn}
      </section>

      <aside class="sidebar">
        <section class="rail sidebar-section">
          <h2 class="section-title"><span class="lang-en">Funds</span><span class="lang-zh" style="display:none">基金</span></h2>
          <div class="fund-stack">{fund_grid_html}</div>
        </section>
        <section class="rail sidebar-section">
          <h2 class="section-title"><span class="lang-en">Themes</span><span class="lang-zh" style="display:none">主题</span></h2>
          <div class="theme-stack">{themes_html}</div>
        </section>
      </aside>
    </div>
  </div>

  <!-- ═══ FUNDS VIEW ═══ -->
  <div class="view-panel" id="view-funds">
    {fund_distribution_html}
    <div class="cluster-grid">
      {funds_view_html}
    </div>
  </div>

  <!-- ═══ SOURCES VIEW ═══ -->
  <div class="view-panel" id="view-sources">
    <p class="sources-intro lang-en">{production_source_count} production sources — curated for research quality, content accessibility, and institutional relevance.</p>
    <p class="sources-intro lang-zh" style="display:none">{production_source_count} 个生产来源，按研究质量、内容可访问性和机构相关性精选。</p>
    <div class="sources-grid">
{sources_view_html}
    </div>
    <p class="sources-aum-note lang-en">AUM figures as of latest available estimates. Cambridge Associates figure reflects advisory assets, not directly managed AUM.</p>
    <p class="sources-aum-note lang-zh" style="display:none">AUM 数据为最新可得估算值。Cambridge Associates 数字反映受托咨询资产，非直接管理规模。</p>
  </div>
</div>

<div class="footer">
  <span class="lang-en">Hedge Fund Research Monitor &middot; Auto-generated dashboard</span><span class="lang-zh" style="display:none">对冲基金研究监控 &middot; 自动生成</span>
</div>

{details_island}

<script>
let langZh = false;
const activeThemes = new Set();

/* ── View switching ──
 * Each article card lives in #article-pool and is moved into the active view's
 * containers on every switchView call. Moving (not cloning) keeps one DOM node
 * per article; the pool is the resting place between switches.
 */
function returnArticlesToPool() {{
  const pool = document.getElementById('article-pool');
  if (!pool) return;
  document.querySelectorAll('article.pool-article').forEach(a => {{
    if (a.parentElement !== pool) pool.appendChild(a);
  }});
}}

function populateViewFromPool(viewName) {{
  const pool = document.getElementById('article-pool');
  if (!pool) return;
  const panel = document.getElementById('view-' + viewName);
  if (!panel) return;

  if (viewName === 'timeline') {{
    const target = panel.querySelector('.timeline-wrap');
    if (!target) return;
    const initialVisible = parseInt(target.dataset.initialVisible || '20', 10);
    /* Pool DOM order is scrambled after any themes/funds hydration, so sort
       by data-seq (baked-in global date-descending rank) to restore the feed. */
    Array.from(pool.querySelectorAll('article.pool-article'))
      .sort((x, y) => (parseInt(x.dataset.seq, 10) || 0) - (parseInt(y.dataset.seq, 10) || 0))
      .forEach((a, i) => {{
      a.classList.toggle('timeline-extra', i >= initialVisible);
      a.style.display = i >= initialVisible ? 'none' : '';
      target.appendChild(a);
    }});
    updateLoadMoreCount();
  }} else if (viewName === 'themes' || viewName === 'funds') {{
    panel.querySelectorAll('.cluster-articles[data-article-ids]').forEach(target => {{
      const ids = (target.dataset.articleIds || '').split(' ').filter(Boolean);
      ids.forEach(id => {{
        const article = pool.querySelector('#a-' + CSS.escape(id));
        if (article) {{
          article.classList.remove('timeline-extra');
          article.style.display = '';
          target.appendChild(article);
        }}
      }});
    }});
  }}
  /* sources view has no pool articles — it's static fund-profile cards */
}}

function switchView(name) {{
  returnArticlesToPool();
  populateViewFromPool(name);
  document.querySelectorAll('.view-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
  const panel = document.getElementById('view-' + name);
  if (panel) panel.classList.add('active');
  const btn = document.querySelector('.view-btn[data-view="' + name + '"]');
  if (btn) btn.classList.add('active');
  bindRowToggles();
}}

function toggleLang() {{
  langZh = !langZh;
  document.querySelectorAll('.lang-en').forEach(el => el.style.display = langZh ? 'none' : '');
  document.querySelectorAll('.lang-zh').forEach(el => el.style.display = langZh ? '' : 'none');
}}

function toggleOlder() {{
  const body = document.body;
  const btn = document.getElementById('btn-show-older');
  body.classList.toggle('hide-older');
  if (btn) {{
    const showing = !body.classList.contains('hide-older');
    btn.classList.toggle('active', showing);
    /* Update label so it inverts on the second click. We only swap the
       leading verb; the count parenthesis stays intact in both langs. */
    btn.querySelectorAll('.lang-en').forEach(el => {{
      el.textContent = el.textContent.replace(showing ? 'Show older' : 'Hide older',
                                              showing ? 'Hide older' : 'Show older');
    }});
    btn.querySelectorAll('.lang-zh').forEach(el => {{
      el.textContent = el.textContent.replace(showing ? '显示更旧' : '隐藏更旧',
                                              showing ? '隐藏更旧' : '显示更旧');
    }});
  }}
  /* If timeline view is active, refresh the Load-more counter — older rows
     becoming visible changes the "remaining hidden" set. */
  if (typeof updateLoadMoreCount === 'function') updateLoadMoreCount();
}}

/* ── Row toggle (Open/Close) + lazy <details> hydration ──
 * After the unified-pool refactor each article is a .pool-article; legacy
 * .timeline-row / .cluster-item selectors remain as fallback in case future
 * views reintroduce those wrappers.
 *
 * Hydration: <details> shells are emitted with only <summary> inside.
 * Full analysis bodies live in the #article-details-data JSON island and
 * are injected into the matching <details> on first open. Cuts initial DOM
 * construction by ~50% (~6000 nodes → ~3000) since most users never expand
 * more than a handful of articles per session.
 */
const ARTICLE_DETAILS = (() => {{
  const el = document.getElementById('article-details-data');
  if (!el) return {{}};
  try {{ return JSON.parse(el.textContent); }}
  catch (e) {{ console.error('article-details parse failed', e); return {{}}; }}
}})();

function hydrateArticleDetails(article) {{
  const details = article && article.querySelector('.summary-panel');
  if (!details || details.dataset.hydrated === 'true') return;
  const d = ARTICLE_DETAILS[article.id];
  if (!d) {{
    // Older article (>RECENT_DAYS): LLM analysis omitted from JSON island to
    // keep initial parse cost flat. Show a bilingual "see source above" notice
    // so the user knows why the panel is empty.
    details.insertAdjacentHTML('beforeend',
      '<div class="summary-copy lang-en"><em class="older-notice">Older article — see source link above for full content.</em></div>' +
      '<div class="summary-copy lang-zh" style="display:none"><em class="older-notice">较旧文章 — 请查看上方源链接获取完整内容。</em></div>'
    );
    details.dataset.hydrated = 'true';
    if (langZh) {{
      details.querySelectorAll('.lang-en').forEach(el => el.style.display = 'none');
      details.querySelectorAll('.lang-zh').forEach(el => el.style.display = '');
    }}
    return;
  }}
  details.insertAdjacentHTML('beforeend',
    '<div class="summary-copy lang-en">' +
      '<p class="takeaway"><strong>Takeaway:</strong> ' + d.tk_en + '</p>' +
      '<p>' + d.bd_en + '</p>' +
    '</div>' +
    '<div class="summary-copy lang-zh" style="display:none">' +
      '<p class="takeaway"><strong>要点:</strong> ' + d.tk_zh + '</p>' +
      '<p>' + d.bd_zh + '</p>' +
    '</div>' +
    '<div class="theme-tags">' + d.tags + '</div>'
  );
  details.dataset.hydrated = 'true';
  // Apply current lang state so newly-injected lang-en/lang-zh follow the
  // already-toggled UI; lang-zh starts with inline display:none which is
  // correct for default (English) mode.
  if (langZh) {{
    details.querySelectorAll('.lang-en').forEach(el => el.style.display = 'none');
    details.querySelectorAll('.lang-zh').forEach(el => el.style.display = '');
  }}
}}

function bindRowToggles() {{
  document.querySelectorAll('.row-toggle').forEach(btn => {{
    if (btn._bound) return;
    btn._bound = true;
    const parent = btn.closest('.pool-article')
                || btn.closest('.timeline-row')
                || btn.closest('.cluster-item');
    if (!parent) return;
    const details = parent.querySelector('.summary-panel');
    if (!details) return;
    btn.addEventListener('click', () => {{
      if (!details.open) hydrateArticleDetails(parent);
      details.open = !details.open;
      btn.textContent = details.open ? 'Close' : 'Open';
    }});
    details.addEventListener('toggle', () => {{
      if (details.open) hydrateArticleDetails(parent);
      btn.textContent = details.open ? 'Close' : 'Open';
    }});
  }});
}}
bindRowToggles();

/* ── Timeline filters ── */
function applyThemeFilters() {{
  /* Filter pills live inside the Timeline rail, so the selector below is
     intentionally scoped to .timeline-wrap. Themes/Funds views are already
     grouped by cluster; they do not need runtime filtering. */
  document.querySelectorAll('.timeline-wrap article.pool-article').forEach(row => {{
    const rowThemes = (row.dataset.themes || '').split(' ').filter(Boolean);
    const matches = activeThemes.size === 0 || rowThemes.some(theme => activeThemes.has(theme));
    row.classList.toggle('hidden-by-filter', !matches);
  }});
  document.querySelectorAll('.theme-group').forEach(group => {{
    const theme = group.dataset.theme;
    const matches = activeThemes.size === 0 || activeThemes.has(theme);
    group.classList.toggle('hidden-by-filter', !matches);
  }});
  updateLoadMoreCount();
}}

function updateLoadMoreCount() {{
  const btn = document.querySelector('.btn-load-more');
  if (!btn || btn.style.display === 'none') return;
  const hidden = document.querySelectorAll('.timeline-extra:not(.hidden-by-filter)');
  const remaining = Array.from(hidden).filter(el => el.style.display === 'none').length;
  if (remaining > 0) {{
    btn.textContent = 'Load more (' + remaining + ' remaining)';
    btn.style.display = '';
  }} else {{
    btn.style.display = 'none';
  }}
}}

function toggleThemeFilter(button) {{
  const theme = button.dataset.theme;
  if (activeThemes.has(theme)) {{
    activeThemes.delete(theme);
    button.classList.remove('active');
  }} else {{
    activeThemes.add(theme);
    button.classList.add('active');
  }}
  applyThemeFilters();
}}

function clearThemeFilters() {{
  activeThemes.clear();
  document.querySelectorAll('.filter-pill').forEach(b => b.classList.remove('active'));
  applyThemeFilters();
}}

function filterSingleTheme(theme) {{
  clearThemeFilters();
  activeThemes.add(theme);
  document.querySelectorAll('.filter-pill').forEach(b => {{
    if (b.dataset.theme === theme) b.classList.add('active');
  }});
  applyThemeFilters();
}}

function showAll() {{
  document.querySelectorAll('.timeline-wrap article.pool-article').forEach(el => {{
    el.style.display = '';
    el.classList.remove('timeline-extra');
  }});
  const btn = document.querySelector('.btn-load-more');
  if (btn) btn.style.display = 'none';
  bindRowToggles();
}}

/* Populate the default (themes) view on initial page load. */
populateViewFromPool('themes');
bindRowToggles();
</script>

</body>
</html>"""

    return page


def publish_html(output_file: Path, html_content: str) -> Path:
    """Write HTML and gzipped HTML to the configured output path."""
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(html_content, encoding="utf-8")

    gzip_path = output_file.with_suffix(output_file.suffix + ".gz")
    with gzip.open(gzip_path, "wt", encoding="utf-8") as f:
        f.write(html_content)
    return gzip_path


def main() -> None:
    """Load data, generate HTML, and publish to the configured output path."""
    parser = argparse.ArgumentParser(description="Hedge Fund Research — HTML publisher")
    parser.add_argument(
        "--output",
        default=os.environ.get("HEDGE_FUND_RESEARCH_OUTPUT", str(OUTPUT_FILE)),
        help="Output HTML path (default: /var/www/overview/hedge-fund-research.html)",
    )
    args = parser.parse_args()

    articles = load_articles()
    html_content = generate_html(articles)

    output_file = Path(args.output)
    gzip_path = publish_html(output_file, html_content)
    print(f"Written {len(html_content)} bytes to {output_file}")
    print(f"Gzipped: {gzip_path}")

    # Sync generated page back to docs-site repo so docs-sync stays consistent
    docs_page = Path.home() / "docs-site" / "pages" / "hedge-fund-research.html"
    if docs_page.parent.exists():
        try:
            docs_page.write_text(html_content, encoding="utf-8")
            import subprocess
            result = subprocess.run(
                ["git", "-C", str(docs_page.parent.parent), "diff", "--quiet", str(docs_page)],
                capture_output=True,
            )
            if result.returncode != 0:  # file changed
                subprocess.run(
                    ["git", "-C", str(docs_page.parent.parent), "add", str(docs_page)],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["git", "-C", str(docs_page.parent.parent), "commit", "-m",
                     f"sync: hedge-fund-research.html from pipeline ({datetime.now(BJT).strftime('%Y-%m-%d %H:%M BJT')})"],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["git", "-C", str(docs_page.parent.parent), "push"],
                    check=True, capture_output=True,
                )
                print(f"Synced docs-site: {docs_page}")
            else:
                print("docs-site: no change, skipping commit")
        except Exception as e:
            print(f"docs-site sync skipped: {e}")


if __name__ == "__main__":
    main()
