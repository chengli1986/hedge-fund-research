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


def _slugify_theme(theme: str) -> str:
    """Convert a theme label into a stable DOM-safe slug."""
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in theme)
    return "-".join(part for part in slug.split("-") if part)


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

    theme_filters = []
    for theme_name, theme_arts in sorted_themes:
        theme_filters.append(
            f'<button class="filter-pill" data-theme="{_slugify_theme(theme_name)}" onclick="toggleThemeFilter(this)">'
            f'{_esc(theme_name)} <span>{len(theme_arts)}</span></button>'
        )
    theme_filters_html = "".join(theme_filters) if theme_filters else '<span class="muted">Themes appear after analysis.</span>'

    # --- Build HTML ---
    timeline_rows = []
    for i, a in enumerate(sorted_articles):
        sid = a.get("source_id", "unknown")
        color = BADGE_COLORS.get(sid, "#8b949e")
        title = _esc(a.get("title", "Untitled"))
        url = _esc(a.get("url", "#"))
        date = _esc(a.get("date", "n/a"))
        source_name = _esc(a.get("source_name", sid))
        extra_class = " timeline-extra" if i >= INITIAL_VISIBLE else ""
        hidden_style = ' style="display:none"' if i >= INITIAL_VISIBLE else ""
        theme_slugs = " ".join(_slugify_theme(t) for t in a.get("themes", [])) if a.get("themes") else "unthemed"
        row_classes = f'timeline-row source-{sid} {"summarized" if a.get("summarized") else "index-only-row"}'
        insight_toggle = ""

        if a.get("summarized"):
            takeaway_en = _esc(a.get("key_takeaway_en", ""))
            takeaway_zh = _esc(a.get("key_takeaway_zh", ""))
            summary_en = _esc(a.get("summary_en", ""))
            summary_zh = _esc(a.get("summary_zh", ""))
            theme_tags = "".join(
                f'<button class="theme-tag" onclick="filterSingleTheme(\'{_slugify_theme(t)}\')">{_esc(t)}</button>'
                for t in a.get("themes", [])
            )
            insight_toggle = '<button class="row-toggle" type="button">Open</button>'
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
        else:
            insight_toggle = '<span class="index-chip">Index</span>'
            summary_html = ""

        timeline_rows.append(
            f"""<article class="{row_classes}{extra_class}" data-themes="{theme_slugs}"{hidden_style}>
  <div class="row-main">
    <span class="badge" style="background:{color}">{source_name}</span>
    <span class="date">{date}</span>
    <a class="headline" href="{url}" target="_blank" rel="noopener">{title}</a>
    <span class="row-spacer"></span>
    {insight_toggle}
  </div>
  {summary_html}
</article>"""
        )

    load_more_btn = ""
    if total > INITIAL_VISIBLE:
        load_more_btn = f'<button class="btn-load-more" onclick="showAll()">Load more ({total - INITIAL_VISIBLE} remaining)</button>'

    timeline_html = "\n".join(timeline_rows)

    # Fund bulletin
    fund_cards = []
    source_order = ["man-group", "bridgewater", "aqr", "gmo", "oaktree", "ark-invest"]
    for sid in source_order:
        src = sources.get(sid, {})
        color = BADGE_COLORS.get(sid, "#8b949e")
        name = _esc(src.get("name", sid))
        arts = fund_articles.get(sid, [])
        latest_date = arts[0].get("date", "n/a") if arts else "n/a"
        analyzed_count = sum(1 for a in arts if a.get("summarized"))
        art_list = "\n".join(
            f'<li><span class="mini-date">{_esc(a.get("date", "n/a"))}</span>'
            f'<a href="{_esc(a.get("url", "#"))}" target="_blank" rel="noopener">{_esc(a.get("title", ""))}</a></li>'
            for a in arts
        )
        if not art_list:
            art_list = "<li class=\"muted\">No articles yet</li>"
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

    # Theme tracker
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
    themes_html = "\n".join(theme_sections) if theme_sections else '<p class="muted">No themes available yet — articles need analysis first.</p>'

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hedge Fund Research Insights</title>
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

/* Header */
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

.board {{
  display: grid;
  grid-template-columns: minmax(0, 1.9fr) minmax(320px, 0.95fr);
  gap: 18px;
  align-items: start;
}}
.rail {{
  background: rgba(17, 24, 39, 0.84);
  border: 1px solid var(--border);
  border-radius: 18px;
  overflow: hidden;
  backdrop-filter: blur(10px);
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
  color: var(--text); border-color: var(--accent); background: rgba(125, 211, 252, 0.1);
}}
.timeline-row {{
  border-bottom: 1px solid rgba(38, 50, 71, 0.72);
  padding: 8px 0;
}}
.row-main {{
  display: flex; align-items: center; gap: 9px; min-width: 0;
}}
.badge {{
  display: inline-block; padding: 2px 8px; border-radius: 999px;
  font-size: 0.69rem; color: #fff; font-weight: 700; white-space: nowrap;
  letter-spacing: 0.02em;
}}
.date, .mini-date {{ color: var(--text-muted); font-size: 0.73rem; white-space: nowrap; font-variant-numeric: tabular-nums; }}
.headline {{
  color: var(--text); font-size: 0.9rem; line-height: 1.3; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.row-spacer {{ flex: 1 1 auto; }}
.row-toggle, .index-chip {{
  border: 1px solid var(--border); border-radius: 999px; padding: 4px 9px;
  font-size: 0.7rem; background: transparent; color: var(--text-muted);
}}
.row-toggle {{ cursor: pointer; }}
.row-toggle:hover {{ color: var(--text); border-color: var(--accent); }}
.summary-panel {{
  margin: 8px 0 2px 80px; padding: 10px 12px;
  background: var(--surface3); border: 1px solid var(--border); border-radius: 12px;
}}
.summary-panel summary {{
  cursor: pointer; color: var(--accent); font-size: 0.8rem;
  text-transform: uppercase; letter-spacing: 0.05em;
}}
.summary-copy p {{ margin: 8px 0 0; font-size: 0.86rem; color: var(--text-muted); }}
.takeaway {{ color: var(--accent2); }}
.theme-tags {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }}
.btn-load-more {{
  display: block; margin: 14px auto 2px; padding: 8px 18px;
  background: var(--surface2); color: var(--accent); border: 1px solid var(--border);
  border-radius: 999px; cursor: pointer; font-size: 0.8rem;
}}
.btn-load-more:hover {{ background: var(--border); }}

/* Side panels */
.sidebar {{
  display: grid; gap: 18px;
}}
.sidebar-section {{
  padding: 12px 14px 14px;
}}
.section-title {{
  margin: 0 0 10px; font-size: 0.96rem; text-transform: uppercase; letter-spacing: 0.04em;
}}
.fund-stack, .theme-stack {{
  display: grid; gap: 10px;
}}
.fund-panel {{
  border: 1px solid var(--border); border-left: 3px solid var(--fund-accent);
  border-radius: 14px; padding: 10px 12px; background: var(--surface3);
}}
.fund-head, .fund-meta {{
  display: flex; justify-content: space-between; gap: 10px; align-items: baseline;
}}
.fund-head h3 {{ margin: 0; font-size: 0.94rem; }}
.fund-count, .fund-meta {{
  color: var(--text-muted); font-size: 0.74rem;
}}
.fund-links {{
  list-style: none; padding: 0; margin: 10px 0 0;
  display: grid; gap: 6px;
}}
.fund-links li {{
  display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 8px;
  align-items: start; font-size: 0.8rem;
}}
.fund-links a {{
  color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.theme-group {{
  border: 1px solid var(--border); border-radius: 14px; padding: 10px 12px; background: var(--surface3);
}}
.theme-group h3 {{ margin: 0 0 8px 0; font-size: 0.88rem; }}
.theme-group .count {{ color: var(--text-muted); font-weight: normal; font-size: 0.8rem; }}
.theme-group ul {{
  list-style: none; padding: 0; margin: 0;
  display: grid; gap: 6px; font-size: 0.8rem;
}}
.theme-group li {{ line-height: 1.35; }}

.muted {{ color: var(--text-muted); }}
.hidden-by-filter {{ display: none !important; }}

/* Footer */
.footer {{
  margin-top: 22px; padding: 16px 0; border-top: 1px solid var(--border);
  text-align: center; color: var(--text-muted); font-size: 0.8rem;
}}
@media (max-width: 980px) {{
  .board {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 720px) {{
  .container {{ padding: 14px 14px 22px; }}
  .row-main {{ flex-wrap: wrap; }}
  .headline {{ white-space: normal; overflow: visible; }}
  .summary-panel {{ margin-left: 0; }}
  .fund-links li {{ grid-template-columns: 1fr; }}
}}
</style>
</head>
<body>

<div class="header">
  <div class="container">
    <div>
      <a href="/" style="font-size:0.82rem;color:var(--text-muted);text-decoration:none;">&larr; <span class="lang-en">Back to Infrastructure</span><span class="lang-zh" style="display:none">返回基础设施</span></a>
      <h1><span class="lang-en">Hedge Fund Research Insights</span><span class="lang-zh" style="display:none">对冲基金研究洞察</span></h1>
      <div class="deck"><span class="lang-en">Bulletin-style research board for fast scanning across funds, themes, and daily flow.</span><span class="lang-zh" style="display:none">公告板式研究看板，快速扫描基金、主题与每日动态。</span></div>
      <div class="stats">
        <span>{total} articles</span>
        <span>{new_this_week} new this week</span>
        <span>{fund_count} funds tracked</span>
        <span>Updated {now}</span>
      </div>
    </div>
    <div class="header-actions">
      <button class="btn-toggle" onclick="clearThemeFilters()">Clear Filters</button>
      <button class="btn-toggle" onclick="toggleLang()">CN / EN</button>
    </div>
  </div>
</div>

<div class="container">
  <div class="board">
    <section class="rail">
      <div class="rail-head">
        <div>
          <h2><span class="lang-en">Bulletin Feed</span><span class="lang-zh" style="display:none">研究公告</span></h2>
          <div class="rail-copy"><span class="lang-en">Dense one-line rows. Open only the entries you want to inspect.</span><span class="lang-zh" style="display:none">密排单行，展开查看分析详情。</span></div>
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
        <div class="fund-stack">
          {fund_grid_html}
        </div>
      </section>

      <section class="rail sidebar-section">
        <h2 class="section-title"><span class="lang-en">Themes</span><span class="lang-zh" style="display:none">主题</span></h2>
        <div class="theme-stack">
          {themes_html}
        </div>
      </section>
    </aside>
  </div>
</div>

<div class="footer">
  <span class="lang-en">Hedge Fund Research Monitor &middot; Auto-generated dashboard</span><span class="lang-zh" style="display:none">对冲基金研究监控 &middot; 自动生成</span>
</div>

<script>
let langZh = false;
const activeThemes = new Set();

function toggleLang() {{
  langZh = !langZh;
  document.querySelectorAll('.lang-en').forEach(el => el.style.display = langZh ? 'none' : '');
  document.querySelectorAll('.lang-zh').forEach(el => el.style.display = langZh ? '' : 'none');
}}

document.querySelectorAll('.row-toggle').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const details = btn.closest('.timeline-row').querySelector('.summary-panel');
    if (!details) return;
    details.open = !details.open;
    btn.textContent = details.open ? 'Close' : 'Open';
  }});
}});

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
  document.querySelectorAll('.filter-pill').forEach(button => button.classList.remove('active'));
  applyThemeFilters();
}}

function filterSingleTheme(theme) {{
  clearThemeFilters();
  activeThemes.add(theme);
  document.querySelectorAll('.filter-pill').forEach(button => {{
    if (button.dataset.theme === theme) button.classList.add('active');
  }});
  applyThemeFilters();
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
