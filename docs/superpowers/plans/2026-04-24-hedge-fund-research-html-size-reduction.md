# Hedge Fund Research HTML Size Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce `hedge-fund-research.html` page size from ~1.1 MB to ~400 KB by rendering each article card exactly once and populating view containers via JS on demand; enable 5-minute browser caching on HTML pages.

**Architecture:**
Today `publish.py` renders the same 113 articles three times — once in a Themes clusters view (one primary theme per article, but with full inline takeaway + details panel), once in a Timeline view (all articles flat), and once in a Funds view (grouped by source_id, full inline takeaway + details panel). Each `<article>` card carries ~4–5 KB of bilingual content (English + Chinese takeaway + full analysis `<details>`). We'll keep the three visual views but move every article `<article>` node into a single hidden `#article-pool` that's rendered once; each view's container references articles by `data-article-ids`. On view switch, JS moves DOM nodes from the pool into the active view's containers and back again on the next switch. This preserves today's UX exactly (including multi-view behavior and primary-theme cluster grouping) while cutting HTML bytes by ~2/3. A matching nginx Cache-Control tweak lets repeat visitors skip the 300 KB re-download entirely.

**Tech Stack:** Python 3.12 (publish.py), vanilla JS (switchView / ensureArticlesInView), nginx 1.24 (Cache-Control header).

---

## Scope Boundaries

- **In scope:** `~/hedge-fund-research/publish.py`, `~/hedge-fund-research/tests/test_unit_publish.py`, `/etc/nginx/sites-enabled/docs-overview` (HTML location block only).
- **Out of scope:** AJAX split of bilingual content (Option 2), real IntersectionObserver lazy rendering (Option 3), structural changes to docs-site publish flow, any other docs site pages, any Python analysis / fetcher code.
- **Preserved UX:** view buttons (Themes / Timeline / Funds / Sources), language toggle, theme filter pills on Timeline, per-article "Open/Close" summary toggle, primary-theme cluster behavior (article → one cluster), "General" unthemed compact table, sidebar fund cards on Timeline, sidebar theme tracker on Timeline.
- **Minor UX change:** timeline rows keep `inline-takeaway` hidden (same as today) via a dedicated `.timeline-wrap .inline-takeaway { display: none !important; }` rule, since the same `<article>` card is now shared across views.

## File Structure

- **Modify:** `publish.py` — `generate_html()` around lines 267–983 (article rendering + HTML body + `<script>` block).
- **Modify:** `tests/test_unit_publish.py` — add new `TestArticlePool` class with 4+ tests.
- **Modify:** `/etc/nginx/sites-enabled/docs-overview` — single line inside HTML `location /` block.

---

## Task 1: Add failing tests for unified article pool structure

**Files:**
- Modify: `~/hedge-fund-research/tests/test_unit_publish.py` — append new `TestArticlePool` class

- [ ] **Step 1: Write the failing tests**

Append the following after the last existing test class in `tests/test_unit_publish.py`:

```python
class TestArticlePool:
    """After the size-reduction refactor, each article card is rendered exactly once
    in a hidden #article-pool; view containers reference articles by id so JS can
    move/return article DOM nodes on view switch."""

    def test_each_article_rendered_exactly_once(self) -> None:
        """Each article's id appears exactly once as an <article> element."""
        result = generate_html(SAMPLE_ARTICLES)
        for a in SAMPLE_ARTICLES:
            # The article pool uses id="a-<article_id>" per card.
            occurrences = result.count(f'id="a-{a["id"]}"')
            assert occurrences == 1, (
                f"Article {a['id']} rendered {occurrences} times, expected 1"
            )

    def test_pool_is_hidden_by_default(self) -> None:
        """The article pool itself is display:none (articles move out via JS)."""
        result = generate_html(SAMPLE_ARTICLES)
        assert 'id="article-pool"' in result
        # Pool container carries display:none style so articles do not render
        # inline during initial paint; JS ensureArticlesInView moves them out.
        # Tolerate minor variations in quoting/ordering: require both the id
        # and an explicit display:none within the same tag.
        import re
        pool_tag = re.search(r'<div[^>]*id="article-pool"[^>]*>', result)
        assert pool_tag is not None, "article-pool container missing"
        assert 'display:none' in pool_tag.group(0).replace(' ', ''), (
            f"article-pool tag missing display:none — got: {pool_tag.group(0)}"
        )

    def test_pool_articles_carry_filter_data_attributes(self) -> None:
        """Each pool article carries data-source-id, data-date, data-themes
        so view-switching JS can move the right articles into the right views."""
        result = generate_html(SAMPLE_ARTICLES)
        # aaa111 is man-group, 2026-03-28, themes AI/Tech + Equities/Value
        import re
        tag = re.search(r'<article[^>]*id="a-aaa111"[^>]*>', result)
        assert tag is not None, "Pool article aaa111 missing"
        tag_str = tag.group(0)
        assert 'data-source-id="man-group"' in tag_str
        assert 'data-date="2026-03-28"' in tag_str
        # Themes stored as space-separated slugs
        assert 'data-themes="ai-tech equities-value"' in tag_str or \
               'data-themes="equities-value ai-tech"' in tag_str

    def test_theme_clusters_reference_article_ids(self) -> None:
        """Themes view clusters carry data-article-ids referencing pool items
        instead of inlining the full article HTML."""
        result = generate_html(SAMPLE_ARTICLES)
        import re
        # There should be at least one cluster-articles container with data-article-ids
        containers = re.findall(
            r'<div class="cluster-articles"[^>]*data-article-ids="([^"]*)"',
            result,
        )
        assert len(containers) > 0, (
            "Themes view should emit cluster-articles containers with "
            "data-article-ids attributes"
        )
        all_ids = set()
        for c in containers:
            all_ids.update(c.split())
        # Every summarized themed article in SAMPLE_ARTICLES must be referenced
        for a in SAMPLE_ARTICLES:
            if a.get("summarized") and a.get("themes"):
                assert a["id"] in all_ids, (
                    f"Article {a['id']} not referenced by any cluster container"
                )

    def test_funds_view_references_article_ids(self) -> None:
        """Funds view containers also reference pool articles by id."""
        result = generate_html(SAMPLE_ARTICLES)
        import re
        # The funds view panel comes after the themes view; look for fund-section
        # with data-article-ids containers inside it.
        fund_containers = re.findall(
            r'<section class="cluster fund-section"[^>]*data-source-id="([^"]+)"[\s\S]*?'
            r'<div class="cluster-articles"[^>]*data-article-ids="([^"]*)"',
            result,
        )
        assert len(fund_containers) > 0, (
            "Funds view should emit fund-section containers with "
            "data-source-id and data-article-ids"
        )
        seen_sources = {src for src, _ in fund_containers}
        for a in SAMPLE_ARTICLES:
            assert a["source_id"] in seen_sources, (
                f"Fund {a['source_id']} has no fund-section container"
            )

    def test_timeline_wrap_is_empty_container(self) -> None:
        """Timeline view contains an empty .timeline-wrap (articles injected by JS)."""
        result = generate_html(SAMPLE_ARTICLES)
        import re
        # .timeline-wrap should exist but contain no <article> nodes directly.
        m = re.search(
            r'<div class="timeline-wrap"[^>]*>([\s\S]*?)</div>',
            result,
        )
        assert m is not None, "timeline-wrap missing"
        inner = m.group(1)
        assert '<article' not in inner, (
            "Timeline wrap should start empty — articles are moved in by JS on "
            f"view switch. Found article tag inside: {inner[:200]}"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_publish.py::TestArticlePool -v 2>&1 | tail -30
```

Expected: all 6 tests FAIL (article pool does not exist yet in current code).

- [ ] **Step 3: Commit the failing tests**

```bash
cd ~/hedge-fund-research && git add tests/test_unit_publish.py && \
  git commit -m "test(publish): add TestArticlePool covering unified-pool layout (red)"
```

No push yet — we'll push together with the implementation.

---

## Task 2: Refactor `generate_html` to a single article pool

This is the biggest change. We touch the three view-rendering blocks and the HTML template. The `_article_card` helper itself stays unchanged — we still use it to render each card, just once.

**Files:**
- Modify: `~/hedge-fund-research/publish.py` lines ~267–983 (inside `generate_html`)

- [ ] **Step 1: Replace cluster/timeline/funds rendering blocks with pool-driven structure**

Replace the block from `# ── Build cluster HTML (Themes view) ──` down through the end of `funds_view_html = ...` (approximately lines 316–436) with the code below. The `_article_card` function itself is unchanged; only the renderers that *call* it change.

```python
    # ── Build the unified article pool (single source of truth) ──
    # Every article gets rendered EXACTLY ONCE here, carrying data-* attributes
    # so the view-switching JS can move the card into whichever view is active.
    pool_parts: list[str] = []
    for a in sorted_articles:
        sid = a.get("source_id", "unknown")
        aid = a.get("id", "")
        theme_slugs = " ".join(
            _slugify_theme(t) for t in a.get("themes", [])
        ) if a.get("themes") else "unthemed"
        pool_parts.append(
            f'<article id="a-{_esc(aid)}" class="pool-article" '
            f'data-source-id="{_esc(sid)}" '
            f'data-date="{_esc(a.get("date", ""))}" '
            f'data-themes="{theme_slugs}">'
            f'{_article_card(a, show_takeaway=True)}</article>'
        )
    article_pool_html = "\n".join(pool_parts)

    # ── Themes view: cluster headers reference articles by id ──
    cluster_parts = []
    for theme_name, cluster_arts in cluster_order:
        source_set = set(a.get("source_id", "") for a in cluster_arts)
        cross_fund = len(source_set) >= 2
        new_count = sum(
            1 for a in cluster_arts if (a.get("date") or "") >= week_ago
        )
        slug = _slugify_theme(theme_name) if theme_name != "General" else "general"
        cross_badge = (
            '<span class="cross-fund-badge">Cross-fund</span>'
            if cross_fund else ""
        )
        new_badge = (
            f'<span class="new-badge">{new_count} new</span>'
            if new_count else ""
        )
        fund_names = ", ".join(sorted(
            set(_esc(a.get("source_name", "")) for a in cluster_arts)
        ))

        if theme_name == "General":
            # Compact table for unthemed articles — unchanged (small, stays inline)
            table_rows = []
            for a in cluster_arts:
                sid = a.get("source_id", "unknown")
                color = BADGE_COLORS.get(sid, "#8b949e")
                takeaway_en = _esc(a.get("key_takeaway_en", ""))
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

    # ── Timeline filter pills (unchanged) ──
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
    theme_filters_html = "".join(theme_filters) if theme_filters else \
        '<span class="muted">Themes appear after analysis.</span>'

    # ── Timeline view: empty wrapper; articles injected by JS ──
    # We keep the load-more button but it now operates on the JS-injected nodes.
    load_more_btn = ""
    if total > INITIAL_VISIBLE:
        load_more_btn = (
            f'<button class="btn-load-more" onclick="showAll()">'
            f'Load more ({total - INITIAL_VISIBLE} remaining)</button>'
        )
    # Expose the total / initial-visible count to JS via data attributes
    timeline_html = (
        f'<div class="timeline-wrap" '
        f'data-total="{total}" '
        f'data-initial-visible="{INITIAL_VISIBLE}"></div>'
    )

    # ── Funds view: fund-section headers reference articles by id ──
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
```

- [ ] **Step 2: Inject the hidden pool into the HTML template**

Find the line that currently starts the `<div class="container">` section (around line 805) and add a hidden `#article-pool` div right before the container. Locate this block (the current content is shown inside the `</div>` ending the header wrapper):

```python
</div>

<div class="container">
  <div class="view-bar">
```

Replace it with:

```python
</div>

<!-- Hidden article pool: single-copy source of truth. JS moves cards from
     here into whichever view is active, then returns them on view switch. -->
<div id="article-pool" style="display:none">
{article_pool_html}
</div>

<div class="container">
  <div class="view-bar">
```

- [ ] **Step 3: Add the `.timeline-wrap .inline-takeaway` CSS override**

Find the existing `.inline-takeaway` CSS rule (search the `<style>` block for `.inline-takeaway`). After the existing rule, add:

```css
/* Hide inline takeaway when article lives inside the Timeline wrapper.
   The same <article> card is shared across views; this keeps Timeline dense. */
.timeline-wrap .inline-takeaway {
  display: none !important;
}
```

If there is no pre-existing `.inline-takeaway` rule, add the rule anywhere inside the `<style>` block (suggested: near the other view-specific overrides).

- [ ] **Step 4: Rewrite the `<script>` block's `switchView` and add `ensureArticlesInView`**

Replace the existing `function switchView(name) { ... }` block (approximately lines 880–889) with:

```javascript
/* ── View switching ──
 * Each article card lives in #article-pool and is moved into the active view's
 * containers on every switchView call. Moving (not cloning) keeps one DOM node
 * per article; the pool is the resting place between switches.
 */
function returnArticlesToPool() {
  const pool = document.getElementById('article-pool');
  if (!pool) return;
  document.querySelectorAll('article.pool-article').forEach(a => {
    if (a.parentElement !== pool) pool.appendChild(a);
  });
}

function populateViewFromPool(viewName) {
  const pool = document.getElementById('article-pool');
  if (!pool) return;
  const panel = document.getElementById('view-' + viewName);
  if (!panel) return;

  if (viewName === 'timeline') {
    const target = panel.querySelector('.timeline-wrap');
    if (!target) return;
    const initialVisible = parseInt(target.dataset.initialVisible || '20', 10);
    // Pool children are already sorted by date desc (publish.py order).
    Array.from(pool.querySelectorAll('article.pool-article')).forEach((a, i) => {
      a.classList.toggle('timeline-extra', i >= initialVisible);
      a.style.display = i >= initialVisible ? 'none' : '';
      target.appendChild(a);
    });
    updateLoadMoreCount();
  } else if (viewName === 'themes' || viewName === 'funds') {
    panel.querySelectorAll('.cluster-articles[data-article-ids]').forEach(target => {
      const ids = (target.dataset.articleIds || '').split(/\s+/).filter(Boolean);
      ids.forEach(id => {
        const article = pool.querySelector('#a-' + CSS.escape(id));
        if (article) {
          article.classList.remove('timeline-extra');
          article.style.display = '';
          target.appendChild(article);
        }
      });
    });
  }
  /* sources view has no pool articles — it's static fund-profile cards */
}

function switchView(name) {
  returnArticlesToPool();
  populateViewFromPool(name);
  document.querySelectorAll('.view-panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.view-btn').forEach(b => b.classList.remove('active'));
  const panel = document.getElementById('view-' + name);
  if (panel) panel.classList.add('active');
  const btn = document.querySelector('.view-btn[data-view="' + name + '"]');
  if (btn) btn.classList.add('active');
  bindRowToggles();
}

/* Populate the default (themes) view on initial page load. */
populateViewFromPool('themes');
```

- [ ] **Step 5: Update `showAll()` to work on pool articles inside timeline-wrap**

Replace the existing `function showAll()` block (approximately lines 972–977) with:

```javascript
function showAll() {
  document.querySelectorAll('.timeline-wrap article.pool-article').forEach(el => {
    el.style.display = '';
    el.classList.remove('timeline-extra');
  });
  const btn = document.querySelector('.btn-load-more');
  if (btn) btn.style.display = 'none';
  bindRowToggles();
}
```

- [ ] **Step 6: Run the new TestArticlePool tests to verify they pass**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_publish.py::TestArticlePool -v 2>&1 | tail -20
```

Expected: all 6 TestArticlePool tests PASS.

- [ ] **Step 7: Run the full publish.py test suite to catch regressions**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_publish.py -v 2>&1 | tail -30
```

Expected: every existing test still PASSES (bilingual content is now inside the pool, but the text strings are still present in the HTML; timeline sorted-by-date still holds because pool order matches sort order; badge colors test is unaffected).

If any existing test fails, STOP and investigate — do not "fix" the test without understanding why it broke.

- [ ] **Step 8: Run the repo-wide test suite to confirm no cross-module breakage**

```bash
cd ~/hedge-fund-research && python3 -m pytest 2>&1 | tail -5
```

Expected: `270 passed, 15 deselected` (or higher — we added 6 new tests, so `276 passed, 15 deselected`).

- [ ] **Step 9: Commit the refactor**

```bash
cd ~/hedge-fund-research && git add publish.py tests/test_unit_publish.py && \
  git commit -m "refactor(publish): unify article pool across views to cut HTML size ~2/3

Render each article <article> card exactly once inside a hidden #article-pool.
Themes / Funds view containers reference pool articles via data-article-ids;
Timeline view is an empty wrapper populated on view activation. switchView now
moves pool articles into the active view's containers and returns them to the
pool on every switch, so total DOM size stays at one node per article.

Before: 113 articles × ~3 view copies → ~1.1 MB HTML (299 KB gzipped).
After:  113 articles × 1 render      → ~350-400 KB HTML expected.

No UX change: all four views behave identically (primary-theme clustering,
per-source fund grouping, date-sorted timeline, Sources profile cards).
Timeline's initial-visible slice is preserved via pool-article CSS class
rather than pre-rendered hidden rows."
```

---

## Task 3: Regenerate the live page and verify size + behavior

- [ ] **Step 1: Back up the current live HTML for size comparison**

```bash
cp /var/www/overview/hedge-fund-research.html /tmp/hfr-before.html && \
  ls -la /tmp/hfr-before.html /var/www/overview/hedge-fund-research.html.gz
```

- [ ] **Step 2: Run the publish script against real data**

```bash
cd ~/hedge-fund-research && python3 publish.py 2>&1 | tail -5
```

Expected output lines:
```
Written <N> bytes to /var/www/overview/hedge-fund-research.html
Gzipped: /var/www/overview/hedge-fund-research.html.gz
Synced docs-site: /home/ubuntu/docs-site/pages/hedge-fund-research.html
```

(The "Synced docs-site" line commits to docs-site automatically via the existing logic in `main()`.)

- [ ] **Step 3: Compare sizes**

```bash
ls -la /tmp/hfr-before.html /var/www/overview/hedge-fund-research.html && \
  echo "--- gzipped ---" && \
  gzip -c /tmp/hfr-before.html | wc -c && \
  ls -la /var/www/overview/hedge-fund-research.html.gz
```

Expected: uncompressed size dropped from ~1.1 MB to ~350–450 KB; gzipped from ~299 KB to ~100–150 KB. If the drop is <40% something is wrong — re-check that the cluster/fund containers no longer inline article HTML.

- [ ] **Step 4: Smoke-test the live page in a browser**

This page sits behind basic auth, so use `curl` with creds from the ops env:

```bash
source ~/.stock-monitor.env 2>/dev/null; \
  curl -sS --user "$DOCS_OVERVIEW_USER:$DOCS_OVERVIEW_PASS" \
  https://docs.sinostor.com.cn/hedge-fund-research.html | \
  grep -c 'class="pool-article"'
```

Expected: count equals the total article count (113 as of 2026-04-24; run `python3 -c "import json; print(sum(1 for _ in open('/home/ubuntu/hedge-fund-research/data/articles.jsonl')))"` to get the live count).

If Playwright MCP is already in use elsewhere today, a visual check is welcome but not required; the data-attribute count plus the unit tests cover the critical invariants.

- [ ] **Step 5: Check docs-site repo state**

`publish.py` double-writes the HTML into `~/docs-site/pages/hedge-fund-research.html` and auto-commits/pushes when content differs. Verify:

```bash
cd ~/docs-site && git log -1 --oneline -- pages/hedge-fund-research.html && git status
```

Expected: a fresh commit with the regenerated HTML; `git status` shows clean tree (commit already pushed by the publish.py sync step).

---

## Task 4: Update nginx Cache-Control for HTML pages (fix #4)

**Files:**
- Modify: `/etc/nginx/sites-enabled/docs-overview` line 62

- [ ] **Step 1: Change the Cache-Control header**

Replace line 62 inside the `location / { ... }` block.

Before:
```nginx
        # expires 1h; # disabled: use no-cache for instant updates
        add_header Cache-Control "no-cache" always;
```

After:
```nginx
        # expires: 5-minute browser cache + 60s stale-while-revalidate.
        # GMIA/news cron publish at predictable times; 5 min freshness is
        # fine and saves a ~300 KB re-download on every nav.
        add_header Cache-Control "public, max-age=300, stale-while-revalidate=60" always;
```

- [ ] **Step 2: Validate nginx config and reload**

```bash
sudo nginx -t && sudo systemctl reload nginx && echo OK
```

Expected: `nginx: configuration file /etc/nginx/nginx.conf test is successful` followed by `OK`.

- [ ] **Step 3: Verify header in a live request**

```bash
source ~/.stock-monitor.env 2>/dev/null; \
  curl -sI --user "$DOCS_OVERVIEW_USER:$DOCS_OVERVIEW_PASS" \
  https://docs.sinostor.com.cn/hedge-fund-research.html | grep -i cache-control
```

Expected: `cache-control: public, max-age=300, stale-while-revalidate=60`.

---

## Task 5: Push and update memory

- [ ] **Step 1: Push hedge-fund-research**

```bash
cd ~/hedge-fund-research && git push origin main
```

Expected: both commits (failing tests + refactor) pushed. The `publish.py` run in Task 3 already pushed the docs-site sync commit separately.

- [ ] **Step 2: Update README if test count changed**

Check and update the test count in `README.md` from `270` to the new total (expected `276` after the 6 new `TestArticlePool` tests):

```bash
cd ~/hedge-fund-research && grep -n "270 tests\|270 passed" README.md
```

If the number appears, update it, commit, and push:
```bash
cd ~/hedge-fund-research && git add README.md && \
  git commit -m "docs(readme): bump test count after article-pool refactor" && \
  git push origin main
```

- [ ] **Step 3: Update MEMORY.md**

Update `~/.claude/projects/-home-ubuntu/memory/MEMORY.md` GMIA line test count (`270 passed/15 deselected` → `276 passed/15 deselected`) and `hedge-fund-research.md` repo memory file (latest commit hash, test count).

- [ ] **Step 4: Append entry to today's daily log**

Append a new section to `~/.claude/projects/-home-ubuntu/memory/daily/2026-04-24.md`:

```markdown
## Development (~HH:MM BJT, session <id>)

### hedge-fund-research.html size reduction (`<commit-hash>`)

- **Root cause**: each of 113 articles rendered up to 3x across Themes / Timeline / Funds views, with full bilingual takeaway + analysis details panel → 1.1 MB HTML (299 KB gzipped).
- **Fix**: unified `#article-pool` (single `<article>` per item), JS switchView moves nodes into active view's containers; view containers reference articles by `data-article-ids`.
- **Nginx**: `/etc/nginx/sites-enabled/docs-overview` HTML location switched from `Cache-Control: no-cache` to `public, max-age=300, stale-while-revalidate=60`.
- **Size**: before ~1.1 MB / ~299 KB gz → after ~<N> MB / ~<M> KB gz (measured).
- **Tests**: +6 new `TestArticlePool` tests; 270 → 276 passed.
- **UX**: unchanged — all four views still render identically (primary-theme clusters, Timeline initial 20 + load-more, Funds grouping, Sources profile cards).
```

---

## Self-Review

**Spec coverage:**
- Fix #1 (shared DOM across 3 views): Task 2 — article pool + populateViewFromPool. ✓
- Fix #4 (Cache-Control): Task 4. ✓
- Fixes #2 (AJAX analysis) and #3 (real lazy render): intentionally out of scope per user decision.

**Placeholder scan:** No TBDs, no "similar to Task N", every code step contains the actual code.

**Type consistency:**
- `ensureArticlesInView` name mentioned in high-level design was renamed to `populateViewFromPool` + `returnArticlesToPool` for clarity — all task steps use the final names consistently.
- `pool-article` class is used in both publish.py (data pool render) and JS selectors (`article.pool-article`).
- `data-article-ids` attribute is used in both publish.py (cluster/fund containers) and JS (dataset.articleIds parse).

**Risk notes:**
- Primary-theme assignment is pre-existing behavior (lines 293–305); we keep it. Articles with multiple themes still appear in exactly one cluster. Filter pills still work via the Timeline theme-filter mechanism.
- `_article_card` helper is unchanged; bilingual content string still lives in the HTML exactly once per article, so `test_bilingual_content_present` keeps passing.
- `INITIAL_VISIBLE` behavior now lives in JS at view activation rather than in the server-rendered HTML. The semantics are the same (first 20 shown, rest hidden until Load more).
- No-JS fallback: article pool is `display:none`, so users with JS disabled see an empty page. Acceptable — this is a basic-auth-protected internal docs page where JS is always on. Not adding a `<noscript>` notice to keep the change minimal.
