"""The article tag taxonomy: 5 groups, 41 tags (agreed 2026-10-07).

Machine-readable twin of docs/tag-taxonomy.md -- the doc is where a tag is
argued for, this is what the model and the page actually use. Change both.

    GROUPS[group] = (English name, Chinese name, {tag id: (en, zh, definition)})

Tags are filters a reader combines (intersection), not a primary section. The
definitions are written for the model; they were tuned on a 40-article pilot
in three rounds, the last two only on 'ai_tech' / 'infrastructure'. Round 4
(2026-10-08) followed a failed acceptance check (33/50): background tagged as a
topic, govt_bonds on Fed or stock/bond pieces, passing-mention methods. The
deletion test now applies to every tag, and fewer tags are allowed.
Round 5 (2026-10-08, 36/50): every asset/topic/method tag must quote a
passage that check_evidence() finds in the document, or it is dropped (the
model tagged AI on a data-centre piece with zero AI mentions); a war's market
impact counts as geopolitics (user's call).
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

GROUPS = {
 "article_type": ("Article type", "文章类型", {
  "annual_outlook": ("Annual outlook", "年度展望", "Annual or mid-year outlooks, 'top themes for next year', published once or twice a year. A long-horizon forecast refreshed every month is 'periodic'."),
  "quarterly": ("Quarterly report", "季度报告", "Quarterly outlooks, quarterly reviews, quarterly allocation views."),
  "periodic": ("Monthly/weekly commentary", "月度/周度点评", "Commentary published on a fixed cycle: monthly/weekly reports, numbered newsletters ('Edition 51'), short recurring branded series ('Quick takes', 'Chart of the week', 'Weekly market recap'). A fixed-cycle piece that discusses a sudden event is still this; a piece in such a series is not 'research' just because it makes one argument. A long-horizon forecast refreshed every month (e.g. a 7-year asset-class forecast) is this, and so is a note on a scheduled data release or meeting (monthly CPI or jobs report, FOMC/ECB meeting recap). Podcast episodes and interviews are 'interview' even when numbered."),
  "event": ("Event commentary", "事件快评", "Ad-hoc commentary reacting to a sudden event: war, sell-off, surprise central-bank move, election result, black swan. Not if it belongs to a fixed-cycle series; a scheduled data release or central-bank meeting is not a sudden event ('periodic')."),
  "research": ("Thematic research", "专题研究", "One-off in-depth analysis of a question, with data and argument."),
  "interview": ("Interview & podcast", "访谈与播客", "Interviews, conversations, podcast transcripts, webinar replays, including numbered podcast series and episodes that discuss an outlook."),
  "education": ("Investor education", "投资者教育", "Primers, explainers, 'how to' guides, saving and planning advice. If it presents original research findings, use 'research'."),
 }),
 "regions": ("Region", "地区", {
  "us": ("United States", "美国", "US economy, US equities/Treasuries, the Fed, US companies. Not for a global piece that only mentions the Fed or the dollar in passing."),
  "europe": ("Europe", "欧洲", "Euro area, UK, Switzerland, Nordics; ECB, Bank of England. Poland/Hungary and other emerging Europe go to 'em'."),
  "china": ("China", "中国", "Mainland China and Hong Kong; PBOC, Chinese equities/bonds/companies. Taiwan goes to 'em'."),
  "japan": ("Japan", "日本", "Japanese economy, equities, JGBs, Bank of Japan, the yen."),
  "em": ("Emerging markets", "新兴市场", "Emerging economies other than China: India, Korea, Taiwan, Southeast Asia, Latin America, emerging Europe, Middle East, Africa; generic 'emerging markets'; pan-Asia ex-Japan (add 'china' if China is the focus)."),
  "global": ("Global", "全球", "Not about one region (global allocation, global macro), or about developed markets outside the five above (Canada, Australia). Never combined with another region. If one or two named regions carry most of the analysis, tag those instead."),
 }),
 "assets": ("Asset class", "资产类别", {
  "equities": ("Equities", "股票", "Listed stocks, indices, sectors, single stocks, dividends/buybacks. Energy and mining stocks are equities; add 'commodities' only if the commodity price itself is a focus."),
  "govt_bonds": ("Government bonds", "政府债", "Treasuries and other sovereign/rates bonds, municipal bonds, EM sovereign debt. The piece must analyse the bonds themselves: yields, the yield curve, duration, bond returns or bond positioning. Not for: a central-bank decision with no bond-market analysis (that is 'monetary_policy'); a generic stock/bond mix or 60/40 piece (that is 'multi_asset'); a list of asset views that gives bonds one line."),
  "credit": ("Credit", "信用债", "Investment grade, high yield, leveraged loans, CLOs, ABS/MBS, EM corporate bonds. Unlisted direct lending is 'private_credit'."),
  "private_equity": ("Private equity", "私募股权", "Buyout, venture, growth equity, secondaries, continuation vehicles, PE fundraising and exits; also private-market fund structures and access: closed-end drawdown vs evergreen/semi-liquid funds, GP/LP terms, capital calls, private-markets allocation."),
  "private_credit": ("Private credit", "私募信贷", "Direct lending, asset-based finance, mezzanine, private debt, NAV loans. Commercial real-estate lending also gets 'real_estate'."),
  "real_estate": ("Real estate", "房地产", "Commercial and residential property, REITs, real-estate debt. Data centres are 'infrastructure' unless discussed as property. Not for a multi-asset or credit piece that gives real estate one line."),
  "infrastructure": ("Infrastructure", "基础设施", "Data centres, power and grids, utilities, pipelines, transport, towers and fibre; listed and private. When the piece analyses AI or data-centre demand as what drives this infrastructure, also tag 'ai_tech'; infrastructure or energy alone does not get 'ai_tech'."),
  "commodities": ("Commodities", "大宗商品", "Oil, gas, gold, copper and other metals, agriculture. The piece must analyse the commodity market itself: prices, supply/demand, positioning or commodity investments. Not for an article that names an oil-price move only as the cause of something else (inflation, a bond sell-off)."),
  "multi_asset": ("Multi-asset", "多资产", "Allocation across asset classes: stock/bond mix, 60/40, strategic or tactical allocation, diversification. Not for a single-asset piece."),
  "digital_assets": ("Crypto & digital assets", "加密与数字资产", "Cryptocurrencies, bitcoin, stablecoins, tokenisation, blockchain finance, CBDCs."),
  "fx": ("Currencies", "外汇", "Currency views, the dollar, the yen, currency hedging, carry. Not for a macro piece that mentions the dollar in passing."),
 }),
 "topics": ("Topic", "话题", {
  "ai_tech": ("AI & technology", "AI 与科技", "AI, semiconductors, cloud, big tech, the AI boom/bubble, AI's effect on productivity and jobs. Also tag when the piece analyses the electricity, water or data-centre demand created by AI: data-centre power demand counts as AI demand, so an article about whether data centres raise power prices or strain the grid gets this tag. Test: if every sentence about AI, data centres or technology were deleted, would the article's main argument still stand? If yes, do not tag. Tag only when at least one full paragraph analyses AI, data-centre demand or technology. Not for: an article that names AI once or twice; a labour-cost piece that notes AI may lift productivity; a bond or macro commentary that lists tech stocks or AI capex as one item among many; an energy, resources or infrastructure piece that never analyses AI or data-centre demand."),
  "monetary_policy": ("Monetary policy & rates", "货币政策与利率", "Central-bank decisions, rate cuts/hikes, rate path, central-bank leadership and independence, QE/QT."),
  "growth_inflation": ("Growth & inflation", "经济增长与通胀", "Growth, recession risk, labour market, inflation, consumption, the business cycle. Not for a piece on a market, sector or strategy that cites growth or inflation as one input among others."),
  "fiscal": ("Fiscal policy & public debt", "财政与政府债务", "Deficits, government borrowing, debt sustainability, budgets, tax cuts or fiscal stimulus at the national level. Must have at least a full paragraph on it; one slide or one sentence on public debt does not count."),
  "geopolitics_trade": ("Geopolitics & trade", "地缘政治与贸易", "War and conflict, sanctions, tariffs, trade wars, supply-chain realignment, great-power rivalry, political effects of elections. Includes articles whose subject is how a war or conflict (e.g. the Iran war, Russia-Ukraine) affects markets, oil, sectors or portfolios: a war is geopolitics even when the piece analyses its market impact rather than the conflict itself. Not for an article that names a war or tariffs in a sentence or two as background ('stocks fell on Middle East tensions') and is really about something else."),
  "esg_climate": ("ESG & climate", "ESG 与气候", "Climate risk, energy transition, emissions, sustainable investing, governance, impact investing. Not for a piece that mentions sustainability or energy efficiency in passing."),
  "healthcare": ("Healthcare & biotech", "医疗与生物科技", "Pharma, biotech, medical devices, healthcare services, life-sciences investing."),
  "retirement": ("Retirement & pensions", "退休与养老", "Retirement saving, pension plans, 401(k)/IRA, retirement income. A pension fund discussing asset allocation gets this only if the pension angle is specific."),
  "tax_wealth": ("Tax & wealth planning", "税务与财富规划", "Tax-saving strategies, tax-loss harvesting, tax alpha, Roth vs traditional accounts, estate planning, college saving. Municipal bonds only when the tax exemption is discussed. National tax policy is 'fiscal'."),
 }),
 "methods": ("Approach", "投资方法", {
  "factors_style": ("Factors & style", "因子与风格", "Quant factors (value, momentum, quality, low volatility), growth vs value rotation, systematic stock selection, smart beta. Trend following is 'macro_trend'."),
  "risk_hedging": ("Risk management & hedging", "风险管理与对冲", "Volatility, tail risk, drawdown control, hedging with options/futures, stress tests, liquidity risk. Must discuss HOW to manage or hedge risk; a note that 'volatility rose' does not count."),
  "behavioral": ("Behavioural finance & sentiment", "行为金融与情绪", "Investor psychology and biases, herding, sentiment indicators, flows and crowded positioning, when analysed. Not for a note that 'sentiment improved' or 'investors stay calm'."),
  "active_passive": ("Active vs passive", "主动与被动", "Active vs index investing, ETFs, the value of active management, fees and performance attribution. Not for a piece that simply recommends being selective or mentions one ETF."),
  "macro_trend": ("Macro & trend strategies", "宏观与趋势策略", "Global macro hedge-fund strategies, CTAs/managed futures, trend following, when the strategy is analysed. Macro-economic analysis itself goes under topics; a single mention of trend followers or macro funds does not count."),
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
             "Tag only what the article spends real space on (at least a full paragraph of analysis); "
             "a passing mention does not count.",
             "Deletion test, for every tag: if every sentence about that subject were deleted, would the "
             "article's main argument still stand? If yes, do not tag it.",
             "Background is not a topic: an oil-price move, a Fed decision or a growth number that is named only "
             "in passing as the cause or context of what the article is really about does not get its own tag. "
             "(Exception: an article whose subject is the market impact of a war gets 'geopolitics_trade'.)",
             "Fewer tags are normal. Assets, topics and methods may each be empty, and most articles need "
             "3 to 5 tags in total. Do not fill a group just because it allows more.",
             "Use only the ids below, copied exactly.", ""]
    for key, (en, _zh, tags) in GROUPS.items():
        lo, hi = LIMITS[key]
        n = "exactly 1" if lo == hi == 1 else f"{lo} to {hi}"
        lines.append(f"{key} ({en}) - choose {n}:")
        lines += [f'  - "{tid}": {d}' for tid, (_, _, d) in tags.items()]
        lines.append("")
    lines += ["Evidence: for EVERY tag in assets, topics and methods, copy one passage of 10 to 40 words "
              "from the document, word for word and contiguous (no '...', no paraphrase, no translation), "
              "that shows the article analyses that subject. A tag whose passage is not found in the "
              "document is removed automatically, so if you cannot find such a passage, do not use the tag.",
              "",
              'Respond with ONLY a JSON object: {"article_type": "<id>", "regions": [...], '
              '"assets": [...], "topics": [...], "methods": [...], '
              '"evidence": {"<tag id>": "<passage copied from the document>", ...}}']
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
    ev = d.get("evidence", {})
    if not isinstance(ev, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in ev.items()):
        errs.append("evidence malformed")
    return errs


EVIDENCE_GROUPS = ("assets", "topics", "methods")
MIN_EVIDENCE_WORDS = 6
# Letters and digits a passage must keep after _letters: "_ _ _ _ _ _" counted
# as six words, reduced to "", and "" is in every body (stage-3 health check,
# 2026-10-10). 12 = six two-character CJK words, the shortest real passage.
MIN_EVIDENCE_LETTERS = 12


def _letters(s: str) -> str:
    """Lower-case letters and digits only. Punctuation, curly quotes, dashes,
    line breaks and spaces cannot make a real quote miss -- including words the
    scraper glued together ('Inflation outlookThe fight...', T. Rowe Price).
    NFKC first: PDF text keeps ligatures ('ﬁnancial'), which the model writes
    out as two letters (second stage-3 review R10)."""
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", s).lower())


_CJK = r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]"


def _evidence_words(passage: str) -> int:
    """Words in a passage, two CJK characters counting as one: a Chinese
    sentence has no spaces, so \\w+ read a whole quote as a single word."""
    cjk = len(re.findall(_CJK, passage))
    return len(re.findall(r"\w+", re.sub(_CJK, " ", passage))) + cjk // 2


def check_evidence(d: dict, text: str) -> tuple[dict, list[str]]:
    """Drop every asset/topic/method tag whose evidence passage is not in `text`.

    Returns (answer without those tags, ["tag: reason", ...]). Type and regions
    need no evidence. The model was told unsupported tags are removed, so
    dropping (not retrying) is the intended outcome; 0 tags in a group is legal.

    One passage may support two tags here (a sentence about AI data centres
    is evidence for ai_tech and for infrastructure: 57 kept tags did so on
    2026-10-10). Only a re-quote may not borrow a passage another tag already
    cites (tag_articles._requote, T6): there, reuse is the cheapest way to
    rescue a tag the text does not support."""
    body = _letters(text)
    ev = d.get("evidence") or {}
    kept, dropped = dict(d), []
    for g in EVIDENCE_GROUPS:
        keep = []
        for tid in d[g]:
            passage = ev.get(tid) or ""
            if _evidence_words(passage) < MIN_EVIDENCE_WORDS or len(_letters(passage)) < MIN_EVIDENCE_LETTERS:
                dropped.append(f"{tid}: no passage" if not passage.strip() else f"{tid}: passage too short")
            elif _letters(passage) not in body:
                dropped.append(f"{tid}: passage not in document")
            else:
                keep.append(tid)
        kept[g] = keep
    return kept, dropped


def flatten(d: dict) -> list[str]:
    """A validated answer as the stored list: article type first, then the rest in group order."""
    return [d["article_type"]] + [t for g in LIST_GROUPS for t in d[g]]


SERIES_MIN = 3
SERIES_SHOW = 12
_MONTH = (r"(?:january|february|march|april|may|june|july|august|september|october|november|december"
          r"|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)")
# Directory names that hold a whole site or a subject, not one column.
GENERIC_DIRS = {
    "insights", "insight", "article", "articles", "blog", "news", "library", "archive", "perspectives",
    "perspective", "research", "research-library", "research-and-insights", "blog-post", "paper", "papers",
    "publications", "views-news", "ic-article", "market-insights", "news-and-insights", "investment-insights",
    "investment-research", "equities", "equity", "fixed-income", "multi-asset", "real-estate", "etf",
    "education", "institute", "report-survey", "white-papers", "working-paper", "journal-article",
    "outlooks-and-market-updates", "investment-perspectives", "newsroom", "media", "video-library",
    "practicelab", "series", "verdadcap", "en", "us", "en-us", "en-int",
    # seen on the 2026-10 data: a whole-site path, sub-brands, subjects
    "insights-and-research", "clearbridge-investments", "royce-investment-partners", "western-asset",
    "tax-aware-investing", "real-estate-private-markets", "multi-asset-solutions",
    "memo",  # Oaktree memos: an essay series, each piece is research
}


def _title_prefix(title: str) -> str:
    """Title up to the first ':', ' - ', '|', en or em dash; a leading month (and
    year) dropped, so 'June CPI report' and 'July CPI report' are one series."""
    p = re.split(r"\s*[:\u2013\u2014|]\s*|\s+-\s+", title or "")[0].strip()
    p = re.sub(rf"^{_MONTH}\b\.?\s+(?:\d{{4}}\s+)?", "", p, flags=re.I)
    return " ".join(p.lower().split())


def _url_parts(url: str) -> tuple[str, list[str]]:
    """(last directory, word tokens of the last path segment without numbers or a leading month)."""
    segs = [x for x in urlparse(url or "").path.lower().split("/") if x]
    if not segs:
        return "", []
    toks = [t for t in re.split(r"[-_.]+", segs[-1]) if t and not t.isdigit() and t not in ("html", "htm", "pdf")]
    while toks and re.fullmatch(_MONTH, toks[0]):
        toks = toks[1:]
    d = segs[-2] if len(segs) >= 2 else ""
    return ("" if d in GENERIC_DIRS or re.fullmatch(r"[\d-]+", d) else d), toks


def series_keys(row: dict) -> list[tuple[str, str, str]]:
    """Every way `row` could belong to a column: by title prefix, by the first
    three words of its URL slug ('views-from-the-floor-2026-24-mar', Man; titles
    differ every week), and by a column-named URL directory
    ('/on-the-minds-of-investors/', J.P. Morgan)."""
    src = row.get("source_id") or ""
    d, toks = _url_parts(row.get("url") or "")
    keys = [(src, "title", _title_prefix(row.get("title") or ""))]
    if len(toks) >= 3:
        keys.append((src, "slug", "-".join(toks[:3])))
    if d:
        keys.append((src, "dir", d))
    return [k for k in keys if k[2]]


def series_index(rows: list[dict]) -> dict[tuple[str, str, str], list[str]]:
    """Dates of every column-like group (any series_keys kind) with at least SERIES_MIN articles.

    The model sees one article at a time and cannot tell that a war-focused
    issue belongs to a monthly or weekly column (gate 3, 2026-10-08: 3 of 9
    errors, then 6 of 10 once titles were covered); these dates let it see
    the cadence. Whether a group really is a column is left to the model."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for r in rows:
        if r.get("date"):
            for k in series_keys(r):
                groups.setdefault(k, []).append(str(r["date"])[:10])
    return {k: sorted(v) for k, v in groups.items() if len(v) >= SERIES_MIN}


_SERIES_WHAT = {"title": "titles start with \"{}\"", "slug": "web addresses start with \"{}\"",
                "dir": "web addresses sit under \"/{}/\""}


def series_note(row: dict, index: dict[tuple[str, str, str], list[str]]) -> str:
    """A line for the prompt naming the column's dates, or '' if it is in none.

    Title groups are preferred, then slug, then directory."""
    for key in series_keys(row):
        dates = index.get(key)
        if not dates:
            continue
        shown = dates[-SERIES_SHOW:]
        return (f"Series context: this source has published {len(dates)} articles whose "
                f"{_SERIES_WHAT[key[1]].format(key[2])}, dated {', '.join(shown)}"
                f"{' (latest shown)' if len(dates) > len(shown) else ''}. "
                "This article is an issue of a series if the dates follow a regular cycle, OR if the shared "
                "name is a recurring column or newsletter ('Quick view', 'Chart to watch', 'On the Minds of "
                "Investors', 'Views from the Floor') even when its dates are irregular. For a series use "
                "'periodic' (monthly/weekly or irregular columns), 'quarterly' (a quarterly or half-yearly "
                "review), or 'annual_outlook' (an outlook published once or twice a year) -- even when this "
                "issue is about a sudden event. Ignore this only when the shared name is a broad subject, "
                "brand or division name ('Emerging markets', 'Wealth Management', 'equity') over unrelated "
                "pieces, or when this article is an interview or podcast (keep 'interview').")
    return ""

