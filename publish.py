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
}

INITIAL_VISIBLE = 20


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


def _article_card(a: dict, show_takeaway: bool = False) -> str:
    """Render a single article as a timeline row."""
    sid = a.get("source_id", "unknown")
    color = BADGE_COLORS.get(sid, "#8b949e")
    title = _esc(a.get("title", "Untitled"))
    url = _esc(a.get("url", "#"))
    date = _esc(a.get("date", "n/a"))
    source_name = _esc(a.get("source_name", sid))

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
        summary_html = f"""<details class="summary-panel">
  <summary><span class="lang-en">Analysis</span><span class="lang-zh" style="display:none">分析</span></summary>
  <div class="summary-copy lang-en">
    <p class="takeaway"><strong>Takeaway:</strong> {takeaway_en}</p>
    <p>{summary_en}</p>
  </div>
  <div class="summary-copy lang-zh" style="display:none">
    <p class="takeaway"><strong>要点:</strong> {takeaway_zh}</p>
    <p>{summary_zh}</p>
  </div>
  <div class="theme-tags">{theme_tags}</div>
</details>"""
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

    return f"""<div class="row-main">
    <span class="badge" style="background:{color}">{source_name}</span>
    <span class="date">{date}</span>
    <a class="headline" href="{url}" target="_blank" rel="noopener">{title}</a>
    <span class="row-spacer"></span>
    {toggle}
  </div>
  {inline_takeaway if show_takeaway else ""}
  {summary_html}"""


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
            # Full cluster card with articles
            article_items = []
            for a in cluster_arts:
                article_items.append(
                    f'<article class="cluster-item">{_article_card(a, show_takeaway=True)}</article>'
                )
            articles_html = "\n".join(article_items)
            cluster_parts.append(
                f"""<section class="cluster" data-cluster="{slug}">
  <div class="cluster-head">
    <div>
      <h2>{_esc(theme_name)} <span class="cluster-count">{len(cluster_arts)}</span> {cross_badge} {new_badge}</h2>
      <div class="cluster-meta">{fund_names}</div>
    </div>
  </div>
  <div class="cluster-articles">{articles_html}</div>
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

    timeline_rows = []
    for i, a in enumerate(sorted_articles):
        sid = a.get("source_id", "unknown")
        extra_class = " timeline-extra" if i >= INITIAL_VISIBLE else ""
        hidden_style = ' style="display:none"' if i >= INITIAL_VISIBLE else ""
        theme_slugs = " ".join(_slugify_theme(t) for t in a.get("themes", [])) if a.get("themes") else "unthemed"
        row_classes = f'timeline-row source-{sid} {"summarized" if a.get("summarized") else "index-only-row"}'
        timeline_rows.append(
            f'<article class="{row_classes}{extra_class}" data-themes="{theme_slugs}"{hidden_style}>'
            f'{_article_card(a)}</article>'
        )

    load_more_btn = ""
    if total > INITIAL_VISIBLE:
        load_more_btn = f'<button class="btn-load-more" onclick="showAll()">Load more ({total - INITIAL_VISIBLE} remaining)</button>'
    timeline_html = "\n".join(timeline_rows)

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
        art_items = []
        for a in arts:
            art_items.append(
                f'<article class="cluster-item">{_article_card(a, show_takeaway=True)}</article>'
            )
        fund_view_parts.append(
            f"""<section class="cluster fund-section" style="--fund-accent:{color}">
  <div class="cluster-head">
    <div>
      <h2><span class="badge" style="background:{color}">{name}</span> <span class="cluster-count">{len(arts)} articles · {analyzed} analyzed</span></h2>
      <div class="cluster-meta"><span class="lang-en">Latest: {latest}</span><span class="lang-zh" style="display:none">最新: {latest}</span></div>
    </div>
  </div>
  <div class="cluster-articles">{"".join(art_items)}</div>
</section>"""
        )
    funds_view_html = "\n".join(fund_view_parts)

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

    fund_names_for_meta = ", ".join(
        sources[sid].get("name", sid) for sid in source_order if sid in sources
    )
    meta_description = _esc(f"Research aggregator: {fund_names_for_meta}.")

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
.timeline-row {{ border-bottom: 1px solid rgba(38,50,71,0.72); padding: 8px 0; }}
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
.cluster-item {{
  border-bottom: 1px solid rgba(38,50,71,0.5); padding: 8px 0;
}}
.cluster-item:last-child {{ border-bottom: none; }}
.inline-takeaway {{
  margin: 4px 0 2px 60px; font-size: 0.82rem; line-height: 1.4;
  color: var(--accent2); font-style: italic;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
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
}}
</style>
</head>
<body>

<div class="header">
  <div class="container">
    <div>
      <a href="/" style="font-size:0.82rem;color:var(--text-muted);text-decoration:none;">&larr; <span class="lang-en">Back to Infrastructure</span><span class="lang-zh" style="display:none">返回基础设施</span></a>
      <h1><span class="lang-en">Hedge Fund Research Insights</span><span class="lang-zh" style="display:none">对冲基金研究洞察</span></h1>
      <div class="deck"><span class="lang-en">Cross-fund research aggregator — scan by theme, timeline, or fund.</span><span class="lang-zh" style="display:none">跨基金研究聚合 — 按主题、时间线或基金浏览。</span></div>
      <div class="stats">
        <span>{total} articles</span>
        <span>{new_this_week} new this week</span>
        <span>{fund_count} funds tracked</span>
        <span>Updated {now}</span>
      </div>
    </div>
    <div class="header-actions">
      <button class="btn-toggle" onclick="toggleLang()">CN / EN</button>
    </div>
  </div>
</div>

<div class="container">
  <div class="view-bar">
    <button class="view-btn active" data-view="themes" onclick="switchView('themes')"><span class="lang-en">Themes</span><span class="lang-zh" style="display:none">主题</span></button>
    <button class="view-btn" data-view="timeline" onclick="switchView('timeline')"><span class="lang-en">Timeline</span><span class="lang-zh" style="display:none">时间线</span></button>
    <button class="view-btn" data-view="funds" onclick="switchView('funds')"><span class="lang-en">Funds</span><span class="lang-zh" style="display:none">基金</span></button>
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
        <div class="timeline-wrap">
          {timeline_html}
          {load_more_btn}
        </div>
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
    <div class="cluster-grid">
      {funds_view_html}
    </div>
  </div>
</div>

<div class="footer">
  <span class="lang-en">Hedge Fund Research Monitor &middot; Auto-generated dashboard</span><span class="lang-zh" style="display:none">对冲基金研究监控 &middot; 自动生成</span>
</div>

<script>
let langZh = false;
const activeThemes = new Set();

/* ── View switching ── */
function switchView(name) {{
  document.querySelectorAll('.view-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
  const panel = document.getElementById('view-' + name);
  if (panel) panel.classList.add('active');
  const btn = document.querySelector('.view-btn[data-view="' + name + '"]');
  if (btn) btn.classList.add('active');
  /* Re-bind toggles for newly-visible panel */
  bindRowToggles();
}}

function toggleLang() {{
  langZh = !langZh;
  document.querySelectorAll('.lang-en').forEach(el => el.style.display = langZh ? 'none' : '');
  document.querySelectorAll('.lang-zh').forEach(el => el.style.display = langZh ? '' : 'none');
}}

/* ── Row toggle (Open/Close) ── */
function bindRowToggles() {{
  document.querySelectorAll('.row-toggle').forEach(btn => {{
    if (btn._bound) return;
    btn._bound = true;
    const parent = btn.closest('.timeline-row') || btn.closest('.cluster-item');
    if (!parent) return;
    const details = parent.querySelector('.summary-panel');
    if (!details) return;
    btn.addEventListener('click', () => {{
      details.open = !details.open;
      btn.textContent = details.open ? 'Close' : 'Open';
    }});
    details.addEventListener('toggle', () => {{
      btn.textContent = details.open ? 'Close' : 'Open';
    }});
  }});
}}
bindRowToggles();

/* ── Timeline filters ── */
function applyThemeFilters() {{
  document.querySelectorAll('.timeline-row').forEach(row => {{
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
  document.querySelectorAll('.timeline-extra').forEach(el => el.style.display = '');
  const btn = document.querySelector('.btn-load-more');
  if (btn) btn.style.display = 'none';
  bindRowToggles();
}}
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


if __name__ == "__main__":
    main()
