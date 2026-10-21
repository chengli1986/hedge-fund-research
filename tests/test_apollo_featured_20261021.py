"""Apollo's featured block (2026-10-21).

Between the 2026-07-14 and 2026-08-15 Wayback snapshots apollo.com added a
"bento" block above the card list holding the three newest insights. Both the
template and the hand-written fetcher read only the card list, so each article
was first seen 2-3 weeks late, after it moved down into the list (fetch lag 1
day through 07-14, 13-52 days from 07-22). On 10-21 the 10-16, 10-05 and 09-30
items were missing and the health probe warned "most recent article 34d old".

The markup below copies the page's class names and nesting as served on
2026-10-21 (titles, dates and links are the real ones; saved pages stay out of
git, tests/fixtures/*.html is ignored).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fetch_articles  # noqa: E402
import listing_templates  # noqa: E402

P = "cmp-insight-cardlist-with-images__"
BASE = "/insights-news/insights/"


def _bento(href, eyebrow, title, date, desc):
    return (f'<div class="{P}bento-item"><div class="{P}bento-item-img"><span class="insights-'
            f'article-format-indicator--label">27:07</span></div><div class="{P}bento-item-content">'
            f'<div class="{P}bento-item-info"><a class="{P}bento-item-eyebrow" href="{BASE}{href}">\n'
            f'   {eyebrow}</a><a class="{P}bento-item-title" href="{BASE}{href}">\n   {title}\n  </a>'
            f'<div class="{P}bento-item-release-date"><span class="{P}bento-item-author-date '
            f'{P}bento-item-author-details">\n  {date} </span></div>'
            f'<div class="{P}bento-item-description">{desc}</div></div>'
            f'<div class="{P}bento-item-cta">Listen</div></div>'
            f'<a class="{P}bento-link-preview" href="{BASE}{href}"></a></div>')


def _card(href, tag, title, date):
    return (f'<div class="{P}details"><div class="{P}image"><a href="{BASE}{href}"></a></div>'
            f'<div class="{P}info"><div><a class="{P}tag" href="{BASE}{href}">{tag}</a>'
            f'<a class="text-link-blck-bold" href="{BASE}{href}">{title}</a></div><div>'
            f'<span class="{P}author-date {P}author-details">{date}</span></div>'
            f'<div class="{P}description">x</div></div></div>')


HTML = (f'<div class="cmp-insight-cardlist-with-images"><section class="{P}bento"><div class="{P}bento-grid">'
        + _bento("podcast/the-allocation/2026/10/cycle-structure-selection-private-equity-portfolios-for-todays-market",
                 "Investment Insight | The Allocation",
                 "Cycle, Structure, Selection: Private Equity Portfolios for Today’s Market", "October 16, 2026",
                 "Private equity deal and exit activity have picked up in 2026")
        + _bento("podcast/the-allocation/2026/10/infrastructure-todays-generational-investment-opportunity",
                 "Market Insight | The Allocation",
                 "Infrastructure: Today’s “Generational” Investment Opportunity", "October 05, 2026", "From digital")
        + _bento("podcast/the-allocation/2026/09/torsten-slok-macro-update-higher-for-longer-and-the-road-to-2027",
                 "Market Insight | The Allocation",
                 "Torsten Slok’s Macro Update: Higher for Longer and the Road to 2027", "September 30, 2026", "As 2026")
        + '</div></section><div>'
        + _card("podcast/the-allocation/2026/09/fixed-income-the-ai-issuance-boom-and-what-it-means-for-portfolios",
                "Market Insight | The Allocation",
                "Fixed Income: The AI Issuance Boom and What it Means For Portfolios", "September 17, 2026")
        + _card("2026/09/the-reindustrialization-of-america-is-underway", "Investment Insight",
                "The Reindustrialization of America Is Underway", "September 15, 2026")
        + '</div></div>')
SOURCE = next(s for s in json.loads((ROOT / "config" / "sources.json").read_text())["sources"]
              if s["id"] == "apollo-global-management")
FEATURED = [
    ("2026-10-16", "https://www.apollo.com/insights-news/insights/podcast/the-allocation/2026/10/"
                   "cycle-structure-selection-private-equity-portfolios-for-todays-market"),
    ("2026-10-05", "https://www.apollo.com/insights-news/insights/podcast/the-allocation/2026/10/"
                   "infrastructure-todays-generational-investment-opportunity"),
    ("2026-09-30", "https://www.apollo.com/insights-news/insights/podcast/the-allocation/2026/09/"
                   "torsten-slok-macro-update-higher-for-longer-and-the-road-to-2027"),
]


def _template_rows():
    return listing_templates.parse_cards(HTML, SOURCE, SOURCE["listing_template"])


def _bespoke_rows(monkeypatch):
    class Resp:
        text = HTML

        def raise_for_status(self):
            pass
    monkeypatch.setattr(fetch_articles.requests, "get", lambda url, **kw: Resp())
    return fetch_articles.fetch_apollo_global_management(SOURCE)


def test_the_template_reads_the_featured_block_first():
    rows = _template_rows()
    assert [(r["date"], r["url"]) for r in rows[:3]] == FEATURED
    assert rows[0]["title"] == "Cycle, Structure, Selection: Private Equity Portfolios for Today’s Market"
    # the card list still follows
    assert [r["date"] for r in rows[3:]] == ["2026-09-17", "2026-09-15"]


def test_the_hand_written_fetcher_reads_it_too(monkeypatch):
    rows = _bespoke_rows(monkeypatch)
    assert [(r["date"], r["url"]) for r in rows[:3]] == FEATURED
    assert rows[0]["category"] == "Investment Insight | The Allocation"
    assert rows[0]["summary"].startswith("Private equity deal and exit activity")


def test_template_and_hand_written_agree_as_the_ab_gate_compares(monkeypatch):
    key = lambda r: (r["title"], r["url"], r["date"])   # compare_fetchers.FIELDS
    assert [key(r) for r in _bespoke_rows(monkeypatch)] == [key(r) for r in _template_rows()]
