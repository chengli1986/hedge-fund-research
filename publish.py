#!/usr/bin/env python3
"""
Hedge Fund Research — Stage 4: HTML Dashboard Publisher

Generates a static HTML dashboard from articles.jsonl and sources.json.
Output: /var/www/overview/hedge-fund-research.html (+ .gz)

Dark GitHub-style theme matching docs.sinostor.com.cn.
"""

import html
import json
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
SOURCES_FILE = BASE_DIR / "config" / "sources.json"
OUTPUT_FILE = Path("/var/www/overview/hedge-fund-research.html")

BADGE_COLORS: dict[str, str] = {
    "man-group": "#58a6ff",
    "bridgewater": "#d29922",
    "aqr": "#3fb950",
    "gmo": "#bc8cff",
    "oaktree": "#f85149",
    "ark-invest": "#ff6600",  # ARK orange
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

    # Theme grouping (summarized articles only)
    themes: dict[str, list[dict]] = defaultdict(list)
    for a in sorted_articles:
        if a.get("summarized") and a.get("themes"):
            for t in a["themes"]:
                themes[t].append(a)
    sorted_themes = sorted(themes.items(), key=lambda x: len(x[1]), reverse=True)

    # Fund cards: latest 5 per source
    fund_articles: dict[str, list[dict]] = defaultdict(list)
    for a in sorted_articles:
        sid = a.get("source_id", "")
        if len(fund_articles[sid]) < 5:
            fund_articles[sid].append(a)

    # --- Build HTML ---
    timeline_rows = []
    for i, a in enumerate(sorted_articles):
        sid = a.get("source_id", "unknown")
        color = BADGE_COLORS.get(sid, "#8b949e")
        title = _esc(a.get("title", "Untitled"))
        url = _esc(a.get("url", "#"))
        date = _esc(a.get("date", ""))
        source_name = _esc(a.get("source_name", sid))
        hidden = ' style="display:none" class="timeline-extra"' if i >= INITIAL_VISIBLE else ""

        summary_html = ""
        if a.get("summarized"):
            takeaway_en = _esc(a.get("key_takeaway_en", ""))
            takeaway_zh = _esc(a.get("key_takeaway_zh", ""))
            summary_en = _esc(a.get("summary_en", ""))
            summary_zh = _esc(a.get("summary_zh", ""))
            theme_tags = "".join(
                f'<span class="theme-tag">{_esc(t)}</span>' for t in a.get("themes", [])
            )
            summary_html = f"""
            <details class="summary-block">
              <summary>View analysis</summary>
              <div class="lang-en">
                <p class="takeaway"><strong>Takeaway:</strong> {takeaway_en}</p>
                <p>{summary_en}</p>
              </div>
              <div class="lang-zh" style="display:none">
                <p class="takeaway"><strong>要点:</strong> {takeaway_zh}</p>
                <p>{summary_zh}</p>
              </div>
              <div class="theme-tags">{theme_tags}</div>
            </details>"""
        else:
            summary_html = '<span class="index-only">Index only</span>'

        timeline_rows.append(
            f"""<div class="timeline-item"{hidden}>
  <span class="badge" style="background:{color}">{source_name}</span>
  <span class="date">{date}</span>
  <a href="{url}" target="_blank" rel="noopener">{title}</a>
  {summary_html}
</div>"""
        )

    load_more_btn = ""
    if total > INITIAL_VISIBLE:
        load_more_btn = f'<button class="btn-load-more" onclick="showAll()">Load more ({total - INITIAL_VISIBLE} remaining)</button>'

    timeline_html = "\n".join(timeline_rows)

    # Fund cards
    fund_cards = []
    source_order = ["man-group", "bridgewater", "aqr", "gmo", "oaktree", "ark-invest"]
    for sid in source_order:
        src = sources.get(sid, {})
        color = BADGE_COLORS.get(sid, "#8b949e")
        name = _esc(src.get("name", sid))
        desc = _esc(src.get("description", ""))
        authors = ", ".join(_esc(a) for a in src.get("notable_authors", []))
        arts = fund_articles.get(sid, [])
        art_list = "\n".join(
            f'<li><a href="{_esc(a.get("url", "#"))}" target="_blank" rel="noopener">{_esc(a.get("title", ""))}</a> <span class="date-sm">{_esc(a.get("date", ""))}</span></li>'
            for a in arts
        )
        if not art_list:
            art_list = "<li class=\"muted\">No articles yet</li>"
        fund_cards.append(
            f"""<div class="fund-card" style="border-top:3px solid {color}">
  <h3>{name}</h3>
  <p class="fund-desc">{desc}</p>
  <p class="fund-authors muted">Notable: {authors}</p>
  <ul class="fund-articles">{art_list}</ul>
</div>"""
        )
    fund_grid_html = "\n".join(fund_cards)

    # Theme tracker
    theme_sections = []
    for theme_name, theme_arts in sorted_themes:
        items = "\n".join(
            f'<li><span class="badge" style="background:{BADGE_COLORS.get(a.get("source_id", ""), "#8b949e")}">{_esc(a.get("source_name", ""))}</span> <a href="{_esc(a.get("url", "#"))}" target="_blank" rel="noopener">{_esc(a.get("title", ""))}</a></li>'
            for a in theme_arts
        )
        theme_sections.append(
            f"""<div class="theme-group">
  <h3>{_esc(theme_name)} <span class="count">({len(theme_arts)})</span></h3>
  <ul>{items}</ul>
</div>"""
        )
    themes_html = "\n".join(theme_sections) if theme_sections else '<p class="muted">No themes available yet — articles need analysis first.</p>'

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hedge Fund Research Insights</title>
<style>
:root {{
  --bg: #0d1117; --surface: #161b22; --surface2: #1c2128;
  --border: #30363d; --text: #e6edf3; --text-muted: #8b949e;
  --accent: #58a6ff; --accent2: #3fb950;
}}
*, *::before, *::after {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 0;
  background: var(--bg); color: var(--text);
  font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;
  line-height: 1.6;
}}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}

/* Header */
.header {{
  background: var(--surface); border-bottom: 1px solid var(--border);
  padding: 24px 0;
}}
.header .container {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }}
.header h1 {{ margin: 0; font-size: 1.5rem; }}
.stats {{ color: var(--text-muted); font-size: 0.85rem; }}
.stats span {{ margin-right: 16px; }}
.btn-toggle {{
  background: var(--surface2); color: var(--text); border: 1px solid var(--border);
  padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 0.85rem;
}}
.btn-toggle:hover {{ background: var(--border); }}

/* Sections */
.section {{ margin: 32px 0; }}
.section h2 {{
  font-size: 1.25rem; border-bottom: 1px solid var(--border);
  padding-bottom: 8px; margin-bottom: 16px;
}}

/* Timeline */
.timeline-item {{
  padding: 10px 0; border-bottom: 1px solid var(--border);
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px;
}}
.badge {{
  display: inline-block; padding: 2px 8px; border-radius: 12px;
  font-size: 0.75rem; color: #fff; font-weight: 600; white-space: nowrap;
}}
.date {{ color: var(--text-muted); font-size: 0.85rem; white-space: nowrap; }}
.date-sm {{ color: var(--text-muted); font-size: 0.8rem; }}
.index-only {{
  background: var(--surface2); color: var(--text-muted);
  padding: 2px 8px; border-radius: 4px; font-size: 0.75rem;
}}
.summary-block {{
  width: 100%; margin-top: 6px; padding: 10px;
  background: var(--surface); border-radius: 6px;
}}
.summary-block summary {{ cursor: pointer; color: var(--accent); font-size: 0.85rem; }}
.takeaway {{ color: var(--accent2); }}
.theme-tag {{
  display: inline-block; background: var(--surface2); color: var(--text-muted);
  padding: 2px 6px; border-radius: 4px; font-size: 0.72rem; margin: 2px 2px;
}}
.btn-load-more {{
  display: block; margin: 16px auto; padding: 8px 24px;
  background: var(--surface2); color: var(--accent); border: 1px solid var(--border);
  border-radius: 6px; cursor: pointer; font-size: 0.9rem;
}}
.btn-load-more:hover {{ background: var(--border); }}

/* Fund grid */
.fund-grid {{
  display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px;
}}
.fund-card {{
  background: var(--surface); border-radius: 8px; padding: 16px;
  border: 1px solid var(--border);
}}
.fund-card h3 {{ margin: 0 0 8px 0; font-size: 1.05rem; }}
.fund-desc {{ font-size: 0.85rem; color: var(--text-muted); margin: 4px 0; }}
.fund-authors {{ font-size: 0.8rem; }}
.fund-articles {{ padding-left: 18px; font-size: 0.85rem; }}
.fund-articles li {{ margin: 4px 0; }}

/* Theme tracker */
.theme-group {{ margin-bottom: 20px; }}
.theme-group h3 {{ margin: 0 0 6px 0; font-size: 1rem; }}
.theme-group .count {{ color: var(--text-muted); font-weight: normal; font-size: 0.85rem; }}
.theme-group ul {{ padding-left: 18px; font-size: 0.85rem; }}
.theme-group li {{ margin: 4px 0; }}

.muted {{ color: var(--text-muted); }}

/* Footer */
.footer {{
  margin-top: 48px; padding: 16px 0; border-top: 1px solid var(--border);
  text-align: center; color: var(--text-muted); font-size: 0.8rem;
}}
</style>
</head>
<body>

<div class="header">
  <div class="container">
    <div>
      <h1>Hedge Fund Research Insights</h1>
      <div class="stats">
        <span>{total} articles</span>
        <span>{new_this_week} new this week</span>
        <span>{fund_count} funds tracked</span>
        <span>Updated {now}</span>
      </div>
    </div>
    <button class="btn-toggle" onclick="toggleLang()">CN / EN</button>
  </div>
</div>

<div class="container">

  <div class="section">
    <h2>Latest Research</h2>
    {timeline_html}
    {load_more_btn}
  </div>

  <div class="section">
    <h2>By Fund</h2>
    <div class="fund-grid">
      {fund_grid_html}
    </div>
  </div>

  <div class="section">
    <h2>Theme Tracker</h2>
    {themes_html}
  </div>

</div>

<div class="footer">
  Hedge Fund Research Monitor &middot; Auto-generated dashboard
</div>

<script>
let langZh = false;
function toggleLang() {{
  langZh = !langZh;
  document.querySelectorAll('.lang-en').forEach(el => el.style.display = langZh ? 'none' : 'block');
  document.querySelectorAll('.lang-zh').forEach(el => el.style.display = langZh ? 'block' : 'none');
}}
function showAll() {{
  document.querySelectorAll('.timeline-extra').forEach(el => el.style.display = '');
  const btn = document.querySelector('.btn-load-more');
  if (btn) btn.style.display = 'none';
}}
</script>

</body>
</html>"""

    return page


def main() -> None:
    """Load data, generate HTML, write to output, gzip."""
    articles = load_articles()
    html_content = generate_html(articles)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(html_content, encoding="utf-8")
    print(f"Written {len(html_content)} bytes to {OUTPUT_FILE}")

    subprocess.run(["gzip", "-k", "-f", str(OUTPUT_FILE)], check=True)
    print(f"Gzipped: {OUTPUT_FILE}.gz")


if __name__ == "__main__":
    main()
