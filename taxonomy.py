"""The article tag taxonomy: 5 groups, 41 tags (agreed 2026-10-07).

Machine-readable twin of docs/tag-taxonomy.md -- the doc is where a tag is
argued for, this is what the model and the page actually use. Change both.

    GROUPS[group] = (English name, Chinese name, {tag id: (en, zh, definition)})

Tags are filters a reader combines (intersection), not a primary section. The
definitions are written for the model; they were tuned on a 40-article pilot
in three rounds, the last two only on 'ai_tech' / 'infrastructure'.
"""
from __future__ import annotations

GROUPS = {
 "article_type": ("Article type", "文章类型", {
  "annual_outlook": ("Annual outlook", "年度展望", "Annual or mid-year outlooks, 'top themes for next year'."),
  "quarterly": ("Quarterly report", "季度报告", "Quarterly outlooks, quarterly reviews, quarterly allocation views."),
  "periodic": ("Monthly/weekly commentary", "月度/周度点评", "Commentary published on a fixed cycle: monthly/weekly reports, numbered newsletters ('Edition 51'). A fixed-cycle piece that discusses a sudden event is still this."),
  "event": ("Event commentary", "事件快评", "Ad-hoc commentary reacting to a sudden event: war, sell-off, surprise central-bank move, election result, black swan. Not if it belongs to a fixed-cycle series."),
  "research": ("Thematic research", "专题研究", "One-off in-depth analysis of a question, with data and argument."),
  "interview": ("Interview & podcast", "访谈与播客", "Interviews, conversations, podcast transcripts, webinar replays."),
  "education": ("Investor education", "投资者教育", "Primers, explainers, 'how to' guides, saving and planning advice. If it presents original research findings, use 'research'."),
 }),
 "regions": ("Region", "地区", {
  "us": ("United States", "美国", "US economy, US equities/Treasuries, the Fed, US companies. Not for a global piece that only mentions the Fed or the dollar in passing."),
  "europe": ("Europe", "欧洲", "Euro area, UK, Switzerland, Nordics; ECB, Bank of England. Poland/Hungary and other emerging Europe go to 'em'."),
  "china": ("China", "中国", "Mainland China and Hong Kong; PBOC, Chinese equities/bonds/companies. Taiwan goes to 'em'."),
  "japan": ("Japan", "日本", "Japanese economy, equities, JGBs, Bank of Japan, the yen."),
  "em": ("Emerging markets", "新兴市场", "Emerging economies other than China: India, Korea, Taiwan, Southeast Asia, Latin America, emerging Europe, Middle East, Africa; generic 'emerging markets'; pan-Asia ex-Japan (add 'china' if China is the focus)."),
  "global": ("Global", "全球", "Not about one region (global allocation, global macro), or about developed markets outside the five above (Canada, Australia). Never combined with another region."),
 }),
 "assets": ("Asset class", "资产类别", {
  "equities": ("Equities", "股票", "Listed stocks, indices, sectors, single stocks, dividends/buybacks. Energy and mining stocks are equities; add 'commodities' only if the commodity price itself is a focus."),
  "govt_bonds": ("Government bonds", "政府债", "Treasuries and other sovereign/rates bonds, municipal bonds, EM sovereign debt, yield curve, duration."),
  "credit": ("Credit", "信用债", "Investment grade, high yield, leveraged loans, CLOs, ABS/MBS, EM corporate bonds. Unlisted direct lending is 'private_credit'."),
  "private_equity": ("Private equity", "私募股权", "Buyout, venture, growth equity, secondaries, continuation vehicles, PE fundraising and exits."),
  "private_credit": ("Private credit", "私募信贷", "Direct lending, asset-based finance, mezzanine, private debt, NAV loans. Commercial real-estate lending also gets 'real_estate'."),
  "real_estate": ("Real estate", "房地产", "Commercial and residential property, REITs, real-estate debt. Data centres are 'infrastructure' unless discussed as property."),
  "infrastructure": ("Infrastructure", "基础设施", "Data centres, power and grids, utilities, pipelines, transport, towers and fibre; listed and private. When the piece analyses AI or data-centre demand as what drives this infrastructure, also tag 'ai_tech'; infrastructure or energy alone does not get 'ai_tech'."),
  "commodities": ("Commodities", "大宗商品", "Oil, gas, gold, copper and other metals, agriculture; commodity prices and supply/demand."),
  "multi_asset": ("Multi-asset", "多资产", "Allocation across asset classes: stock/bond mix, 60/40, strategic or tactical allocation, diversification. Not for a single-asset piece."),
  "digital_assets": ("Crypto & digital assets", "加密与数字资产", "Cryptocurrencies, bitcoin, stablecoins, tokenisation, blockchain finance, CBDCs."),
  "fx": ("Currencies", "外汇", "Currency views, the dollar, the yen, currency hedging, carry. Not for a macro piece that mentions the dollar in passing."),
 }),
 "topics": ("Topic", "话题", {
  "ai_tech": ("AI & technology", "AI 与科技", "AI, semiconductors, cloud, big tech, the AI boom/bubble, AI's effect on productivity and jobs. Also tag when the piece analyses the electricity, water or data-centre demand created by AI: data-centre power demand counts as AI demand, so an article about whether data centres raise power prices or strain the grid gets this tag. Test: if every sentence about AI, data centres or technology were deleted, would the article's main argument still stand? If yes, do not tag. Tag only when at least one full paragraph analyses AI, data-centre demand or technology. Not for: an article that names AI once or twice; a labour-cost piece that notes AI may lift productivity; a bond or macro commentary that lists tech stocks or AI capex as one item among many; an energy, resources or infrastructure piece that never analyses AI or data-centre demand."),
  "monetary_policy": ("Monetary policy & rates", "货币政策与利率", "Central-bank decisions, rate cuts/hikes, rate path, central-bank leadership and independence, QE/QT."),
  "growth_inflation": ("Growth & inflation", "经济增长与通胀", "Growth, recession risk, labour market, inflation, consumption, the business cycle."),
  "fiscal": ("Fiscal policy & public debt", "财政与政府债务", "Deficits, government borrowing, debt sustainability, budgets, tax cuts or fiscal stimulus at the national level."),
  "geopolitics_trade": ("Geopolitics & trade", "地缘政治与贸易", "War and conflict, sanctions, tariffs, trade wars, supply-chain realignment, great-power rivalry, political effects of elections. A conflict that mainly moves oil also gets 'commodities'."),
  "esg_climate": ("ESG & climate", "ESG 与气候", "Climate risk, energy transition, emissions, sustainable investing, governance, impact investing."),
  "healthcare": ("Healthcare & biotech", "医疗与生物科技", "Pharma, biotech, medical devices, healthcare services, life-sciences investing."),
  "retirement": ("Retirement & pensions", "退休与养老", "Retirement saving, pension plans, 401(k)/IRA, retirement income. A pension fund discussing asset allocation gets this only if the pension angle is specific."),
  "tax_wealth": ("Tax & wealth planning", "税务与财富规划", "Tax-saving strategies, tax-loss harvesting, tax alpha, Roth vs traditional accounts, estate planning, college saving. Municipal bonds only when the tax exemption is discussed. National tax policy is 'fiscal'."),
 }),
 "methods": ("Approach", "投资方法", {
  "factors_style": ("Factors & style", "因子与风格", "Quant factors (value, momentum, quality, low volatility), growth vs value rotation, systematic stock selection, smart beta. Trend following is 'macro_trend'."),
  "risk_hedging": ("Risk management & hedging", "风险管理与对冲", "Volatility, tail risk, drawdown control, hedging with options/futures, stress tests, liquidity risk. Must discuss HOW to manage or hedge risk; a note that 'volatility rose' does not count."),
  "behavioral": ("Behavioural finance & sentiment", "行为金融与情绪", "Investor psychology and biases, herding, sentiment indicators, flows and crowded positioning."),
  "active_passive": ("Active vs passive", "主动与被动", "Active vs index investing, ETFs, the value of active management, fees and performance attribution."),
  "macro_trend": ("Macro & trend strategies", "宏观与趋势策略", "Global macro hedge-fund strategies, CTAs/managed futures, trend following. Macro-economic analysis itself goes under topics."),
  "long_short": ("Equity long/short & market neutral", "股票多空与市场中性", "Equity long/short, market neutral, pairs trading, short selling."),
  "event_rv": ("Event-driven & relative value", "事件驱动与相对价值", "Merger arbitrage, activism, special situations, distressed investing, convertible arbitrage, basis trades, relative value."),
  "multistrat_arp": ("Multi-strategy & alternative risk premia", "多策略与另类风险溢价", "Multi-strategy/multi-manager platforms, alternative risk premia, QIS."),
 }),
}
LIMITS = {"article_type": (1, 1), "regions": (1, 2), "assets": (0, 3), "topics": (0, 3), "methods": (0, 2)}
LIST_GROUPS = ("regions", "assets", "topics", "methods")
ALL_TAGS = {tid: group for group, (_, _, tags) in GROUPS.items() for tid in tags}


def instruction() -> str:
    """The tagging instruction appended to the prompt."""
    lines = ["Tag the article. Tags are filters a reader clicks to find articles; there is no 'main' tag.",
             "Test for every tag: would a reader who clicked this tag feel they found the right article? "
             "Tag only what the article spends real space on; a passing mention does not count.",
             "Use only the ids below, copied exactly.", ""]
    for key, (en, _zh, tags) in GROUPS.items():
        lo, hi = LIMITS[key]
        n = "exactly 1" if lo == hi == 1 else f"{lo} to {hi}"
        lines.append(f"{key} ({en}) - choose {n}:")
        lines += [f'  - "{tid}": {d}' for tid, (_, _, d) in tags.items()]
        lines.append("")
    lines.append('Respond with ONLY a JSON object: {"article_type": "<id>", "regions": [...], '
                 '"assets": [...], "topics": [...], "methods": [...]}')
    return "\n".join(lines)


def validate(d: object) -> list[str]:
    """Every way an answer breaks the rules; empty means usable.

    No fuzzy matching: an id that is not copied exactly is an error, not a
    guess. (The old theme parser matched by prefix and filed "Employment"
    under China/EM and "Airlines" under AI/Tech.)"""
    if not isinstance(d, dict):
        return ["not an object"]
    errs = []
    for key, (lo, hi) in LIMITS.items():
        v = d.get(key)
        if key == "article_type":
            vals = [v] if isinstance(v, str) else None
        else:
            vals = v if isinstance(v, list) and all(isinstance(x, str) for x in v) else None
        if vals is None:
            errs.append(f"{key} missing or malformed")
            continue
        bad = [x for x in vals if x not in GROUPS[key][2]]
        if bad:
            errs.append(f"{key}: unknown {bad}")
        if len(set(vals)) != len(vals):
            errs.append(f"{key}: duplicate tag")
        if not lo <= len(vals) <= hi:
            errs.append(f"{key}: {len(vals)} not in {lo}-{hi}")
    regions = d.get("regions")
    if isinstance(regions, list) and "global" in regions and len(regions) > 1:
        errs.append("global combined with another region")
    return errs


def flatten(d: dict) -> list[str]:
    """A validated answer as the stored list: article type first, then the rest in group order."""
    return [d["article_type"]] + [t for g in LIST_GROUPS for t in d[g]]
