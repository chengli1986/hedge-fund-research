# Candidate Fund Discovery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an isolated candidate fund discovery pipeline (Phase 1: seed + discovery + screening + report) that finds hedge funds with public research pages without touching production GMIA data.

**Architecture:** Seed list → HTTP discovery (homepage + nav link crawl) → rule-based screening (domain/freshness/content type) → candidate entrypoint scoring (reuse `entrypoint_scorer.py`) → JSON report. All state in `config/fund_seeds.json`, `config/fund_candidates.json`, `config/candidate_entrypoints.json`.

**Tech Stack:** Python 3.12, httpx (async HTTP), entrypoint_scorer.py (reuse), pytest, OpenClaw (optional Tier 1 classification)

---

## File Structure

### New files to create

| File | Responsibility |
|---|---|
| `config/fund_seeds.json` | Phase 1 seed pool (5 funds) |
| `config/fund_candidates.json` | Candidate state tracking (status lifecycle) |
| `config/candidate_entrypoints.json` | Discovered entrypoints for candidates (isolated from production) |
| `discover_fund_sites.py` | Step 2: Crawl seed homepages, extract research page candidates |
| `screen_fund_candidates.py` | Step 3: Rule-based screening (domain, freshness, content type) |
| `discover_candidate_entrypoints.py` | Step 4: Score candidate pages via entrypoint_scorer |
| `tests/test_unit_fund_discovery.py` | Tests for discovery + screening |
| `tests/test_unit_candidate_entrypoints.py` | Tests for candidate entrypoint scoring |

### Existing files to reuse (read-only)

| File | What we reuse |
|---|---|
| `entrypoint_scorer.py` | `score_domain()`, `score_path()`, `score_structure()`, `detect_gate()`, `score_final()` |
| `discover_entrypoints.py` | `extract_nav_links()` pattern (we'll import it) |
| `config/sources.json` | Read production source IDs to exclude from candidate pool |

---

### Task 1: Seed Pool Configuration

**Files:**
- Create: `config/fund_seeds.json`
- Test: `tests/test_unit_fund_discovery.py`

- [ ] **Step 1: Write the seed pool test**

```python
# tests/test_unit_fund_discovery.py
import json
from pathlib import Path

SEEDS_PATH = Path(__file__).parent.parent / "config" / "fund_seeds.json"
SOURCES_PATH = Path(__file__).parent.parent / "config" / "sources.json"


def test_seed_file_is_valid_json():
    seeds = json.loads(SEEDS_PATH.read_text())
    assert isinstance(seeds, list)
    assert len(seeds) >= 1


def test_seeds_have_required_fields():
    seeds = json.loads(SEEDS_PATH.read_text())
    required = {"id", "name", "category", "homepage"}
    for seed in seeds:
        missing = required - set(seed.keys())
        assert not missing, f"Seed {seed.get('id', '?')} missing: {missing}"


def test_seed_ids_are_unique():
    seeds = json.loads(SEEDS_PATH.read_text())
    ids = [s["id"] for s in seeds]
    assert len(ids) == len(set(ids)), f"Duplicate seed IDs: {ids}"


def test_no_overlap_with_production_sources():
    seeds = json.loads(SEEDS_PATH.read_text())
    sources = json.loads(SOURCES_PATH.read_text())
    prod_ids = {s["id"] for s in sources["sources"]}
    seed_ids = {s["id"] for s in seeds}
    overlap = prod_ids & seed_ids
    assert not overlap, f"Seeds overlap with production: {overlap}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fund_discovery.py -v`
Expected: FAIL with `FileNotFoundError` (fund_seeds.json doesn't exist)

- [ ] **Step 3: Create the seed pool**

```json
[
  {
    "id": "pimco",
    "name": "PIMCO",
    "aliases": ["Pacific Investment Management Company"],
    "category": "fixed_income",
    "homepage": "https://www.pimco.com",
    "notes": "Largest fixed income manager, extensive public research"
  },
  {
    "id": "de-shaw",
    "name": "D. E. Shaw",
    "aliases": ["D.E. Shaw & Co."],
    "category": "quant",
    "homepage": "https://www.deshaw.com",
    "notes": "Quant/systematic, has perspectives page"
  },
  {
    "id": "blackstone",
    "name": "Blackstone",
    "aliases": ["Blackstone Inc."],
    "category": "alternatives",
    "homepage": "https://www.blackstone.com",
    "notes": "Largest alternative asset manager, has insights page"
  },
  {
    "id": "two-sigma",
    "name": "Two Sigma",
    "aliases": ["Two Sigma Investments"],
    "category": "quant",
    "homepage": "https://www.twosigma.com",
    "notes": "Quant fund, has blog but mostly technical/recruiting"
  },
  {
    "id": "kkr",
    "name": "KKR",
    "aliases": ["Kohlberg Kravis Roberts"],
    "category": "alternatives",
    "homepage": "https://www.kkr.com",
    "notes": "PE/credit, has insights page"
  }
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fund_discovery.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research && git add config/fund_seeds.json tests/test_unit_fund_discovery.py
git commit -m "feat(candidate): add Phase 1 seed pool (5 funds) + seed validation tests"
```

---

### Task 2: Candidate State Model

**Files:**
- Create: `config/fund_candidates.json`
- Modify: `tests/test_unit_fund_discovery.py` (add tests)

- [ ] **Step 1: Write candidate state tests**

Append to `tests/test_unit_fund_discovery.py`:

```python
CANDIDATES_PATH = Path(__file__).parent.parent / "config" / "fund_candidates.json"

VALID_STATUSES = {"seed", "discovered", "screened", "validated", "watchlist", "rejected", "promoted"}


def test_candidates_file_is_valid_json():
    candidates = json.loads(CANDIDATES_PATH.read_text())
    assert isinstance(candidates, list)


def test_candidates_have_required_fields():
    candidates = json.loads(CANDIDATES_PATH.read_text())
    required = {"id", "name", "status"}
    for c in candidates:
        missing = required - set(c.keys())
        assert not missing, f"Candidate {c.get('id', '?')} missing: {missing}"


def test_candidate_statuses_are_valid():
    candidates = json.loads(CANDIDATES_PATH.read_text())
    for c in candidates:
        assert c["status"] in VALID_STATUSES, f"{c['id']} has invalid status: {c['status']}"


def test_all_seeds_have_candidate_entry():
    seeds = json.loads(SEEDS_PATH.read_text())
    candidates = json.loads(CANDIDATES_PATH.read_text())
    seed_ids = {s["id"] for s in seeds}
    cand_ids = {c["id"] for c in candidates}
    missing = seed_ids - cand_ids
    assert not missing, f"Seeds without candidate entry: {missing}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fund_discovery.py -v`
Expected: FAIL (fund_candidates.json doesn't exist)

- [ ] **Step 3: Create initial candidate state from seeds**

```json
[
  {
    "id": "pimco",
    "name": "PIMCO",
    "status": "seed",
    "homepage_url": null,
    "research_url": null,
    "rss_url": null,
    "official_domain": null,
    "discovery_method": null,
    "last_discovered_at": null,
    "last_screened_at": null,
    "last_validated_at": null,
    "recent_update_at": null,
    "is_publicly_accessible": null,
    "has_article_index": null,
    "fit_score": null,
    "notes": ""
  },
  {
    "id": "de-shaw",
    "name": "D. E. Shaw",
    "status": "seed",
    "homepage_url": null,
    "research_url": null,
    "rss_url": null,
    "official_domain": null,
    "discovery_method": null,
    "last_discovered_at": null,
    "last_screened_at": null,
    "last_validated_at": null,
    "recent_update_at": null,
    "is_publicly_accessible": null,
    "has_article_index": null,
    "fit_score": null,
    "notes": ""
  },
  {
    "id": "blackstone",
    "name": "Blackstone",
    "status": "seed",
    "homepage_url": null,
    "research_url": null,
    "rss_url": null,
    "official_domain": null,
    "discovery_method": null,
    "last_discovered_at": null,
    "last_screened_at": null,
    "last_validated_at": null,
    "recent_update_at": null,
    "is_publicly_accessible": null,
    "has_article_index": null,
    "fit_score": null,
    "notes": ""
  },
  {
    "id": "two-sigma",
    "name": "Two Sigma",
    "status": "seed",
    "homepage_url": null,
    "research_url": null,
    "rss_url": null,
    "official_domain": null,
    "discovery_method": null,
    "last_discovered_at": null,
    "last_screened_at": null,
    "last_validated_at": null,
    "recent_update_at": null,
    "is_publicly_accessible": null,
    "has_article_index": null,
    "fit_score": null,
    "notes": ""
  },
  {
    "id": "kkr",
    "name": "KKR",
    "status": "seed",
    "homepage_url": null,
    "research_url": null,
    "rss_url": null,
    "official_domain": null,
    "discovery_method": null,
    "last_discovered_at": null,
    "last_screened_at": null,
    "last_validated_at": null,
    "recent_update_at": null,
    "is_publicly_accessible": null,
    "has_article_index": null,
    "fit_score": null,
    "notes": ""
  }
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fund_discovery.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research && git add config/fund_candidates.json tests/test_unit_fund_discovery.py
git commit -m "feat(candidate): add candidate state model with lifecycle statuses"
```

---

### Task 3: Site Discovery Script

**Files:**
- Create: `discover_fund_sites.py`
- Modify: `tests/test_unit_fund_discovery.py` (add tests)

- [ ] **Step 1: Write discovery function tests**

Append to `tests/test_unit_fund_discovery.py`:

```python
from unittest.mock import patch, MagicMock
import discover_fund_sites as dfs


def test_extract_research_links_finds_insights():
    html = '''
    <html><body>
    <nav>
      <a href="/insights">Insights</a>
      <a href="/about">About Us</a>
      <a href="/research">Research</a>
      <a href="/careers">Careers</a>
    </nav>
    </body></html>
    '''
    links = dfs.extract_research_links(html, "https://example.com", ["example.com"])
    urls = [l["url"] for l in links]
    assert "https://example.com/insights" in urls
    assert "https://example.com/research" in urls
    assert "https://example.com/careers" not in urls


def test_extract_research_links_filters_negative_paths():
    html = '''
    <html><body>
    <a href="/perspectives">Perspectives</a>
    <a href="/login">Login</a>
    <a href="/subscribe">Subscribe</a>
    <a href="/white-papers">White Papers</a>
    </body></html>
    '''
    links = dfs.extract_research_links(html, "https://example.com", ["example.com"])
    urls = [l["url"] for l in links]
    assert "https://example.com/perspectives" in urls
    assert "https://example.com/white-papers" in urls
    assert "https://example.com/login" not in urls
    assert "https://example.com/subscribe" not in urls


def test_detect_rss_finds_feed_links():
    html = '''
    <html><head>
    <link rel="alternate" type="application/rss+xml" href="/feed.xml" />
    </head><body></body></html>
    '''
    feeds = dfs.detect_rss(html, "https://example.com")
    assert len(feeds) == 1
    assert feeds[0] == "https://example.com/feed.xml"


def test_detect_rss_returns_empty_when_none():
    html = '<html><head></head><body></body></html>'
    feeds = dfs.detect_rss(html, "https://example.com")
    assert feeds == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fund_discovery.py::test_extract_research_links_finds_insights -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement discover_fund_sites.py**

```python
#!/usr/bin/env python3
"""Candidate fund site discovery — find research/insights pages for seed funds.

Reads config/fund_seeds.json, fetches each homepage, extracts candidate
research page links, detects RSS feeds, and updates config/fund_candidates.json.

Usage:
    python3 discover_fund_sites.py [--dry-run] [--fund ID]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

REPO = Path(__file__).parent
SEEDS_PATH = REPO / "config" / "fund_seeds.json"
CANDIDATES_PATH = REPO / "config" / "fund_candidates.json"

TIMEOUT = 30
MAX_LINKS_PER_FUND = 20

# Positive keywords — pages likely to contain research articles
_RESEARCH_KEYWORDS = {
    "research", "insight", "insights", "perspectives", "commentary",
    "white-paper", "white-papers", "publications", "reports", "outlook",
    "thinking", "ideas", "library", "letters", "quarterly", "viewpoints",
}

# Negative keywords — pages to exclude
_NEGATIVE_KEYWORDS = {
    "about", "careers", "career", "contact", "team", "leadership",
    "events", "podcast", "video", "subscribe", "login", "register",
    "privacy", "legal", "terms", "cookie", "press", "media-kit",
    "investor-relations", "ir", "newsroom",
}


def extract_research_links(
    html: str, base_url: str, allowed_domains: list[str]
) -> list[dict]:
    """Extract links from HTML that look like research/insights pages."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    results: list[dict] = []

    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue

        url = urljoin(base_url, href)
        parsed = urlparse(url)

        # Domain filter
        domain = parsed.hostname or ""
        domain_clean = domain.lstrip("www.")
        if not any(domain_clean.endswith(d.lstrip("www.")) for d in allowed_domains):
            continue

        # Normalize
        url_norm = f"{parsed.scheme}://{parsed.hostname}{parsed.path}".rstrip("/")
        if url_norm in seen:
            continue
        seen.add(url_norm)

        # Path keyword scoring
        path_lower = parsed.path.lower()
        path_parts = set(re.split(r"[/\-_]", path_lower)) - {""}

        if path_parts & _NEGATIVE_KEYWORDS:
            continue

        if path_parts & _RESEARCH_KEYWORDS:
            label = tag.get_text(strip=True)[:80]
            results.append({"url": url_norm, "label": label, "path": parsed.path})

    return results[:MAX_LINKS_PER_FUND]


def detect_rss(html: str, base_url: str) -> list[str]:
    """Find RSS/Atom feed links in HTML head."""
    soup = BeautifulSoup(html, "html.parser")
    feeds: list[str] = []
    for link in soup.find_all("link", rel="alternate"):
        link_type = (link.get("type") or "").lower()
        if "rss" in link_type or "atom" in link_type:
            href = link.get("href", "")
            if href:
                feeds.append(urljoin(base_url, href))
    return feeds


def discover_one(seed: dict, dry_run: bool = False) -> dict:
    """Discover research pages for a single seed fund."""
    fund_id = seed["id"]
    homepage = seed["homepage"]
    domain = urlparse(homepage).hostname or ""
    domain_clean = domain.lstrip("www.")
    allowed = [domain_clean]

    result = {
        "id": fund_id,
        "homepage_url": homepage,
        "research_url": None,
        "rss_url": None,
        "official_domain": domain_clean,
        "discovery_method": "homepage_crawl",
        "candidate_links": [],
        "error": None,
    }

    try:
        resp = httpx.get(homepage, timeout=TIMEOUT, follow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        result["error"] = str(e)
        return result

    html = resp.text

    # Extract research links
    links = extract_research_links(html, homepage, allowed)
    result["candidate_links"] = links

    # Best candidate = first match (highest path relevance)
    if links:
        result["research_url"] = links[0]["url"]

    # RSS detection
    feeds = detect_rss(html, homepage)
    if feeds:
        result["rss_url"] = feeds[0]

    return result


def load_seeds(fund_id: str | None = None) -> list[dict]:
    """Load seed funds, optionally filtered by ID."""
    seeds = json.loads(SEEDS_PATH.read_text())
    if fund_id:
        seeds = [s for s in seeds if s["id"] == fund_id]
    return seeds


def load_candidates() -> list[dict]:
    """Load current candidate state."""
    if not CANDIDATES_PATH.exists():
        return []
    return json.loads(CANDIDATES_PATH.read_text())


def save_candidates(candidates: list[dict]) -> None:
    """Write candidate state."""
    CANDIDATES_PATH.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")


def update_candidate(candidates: list[dict], discovery: dict) -> list[dict]:
    """Merge discovery result into candidate state."""
    now = datetime.now(timezone.utc).isoformat()
    for c in candidates:
        if c["id"] == discovery["id"]:
            c["homepage_url"] = discovery["homepage_url"]
            c["research_url"] = discovery["research_url"]
            c["rss_url"] = discovery["rss_url"]
            c["official_domain"] = discovery["official_domain"]
            c["discovery_method"] = discovery["discovery_method"]
            c["last_discovered_at"] = now
            if discovery["research_url"] and not discovery["error"]:
                c["status"] = "discovered"
            if discovery["error"]:
                c["notes"] = f"Discovery error: {discovery['error']}"
            break
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover research pages for candidate funds")
    parser.add_argument("--fund", help="Process single fund by ID")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing")
    args = parser.parse_args()

    seeds = load_seeds(args.fund)
    if not seeds:
        print(f"No seeds found{' for ' + args.fund if args.fund else ''}")
        sys.exit(1)

    candidates = load_candidates()

    for seed in seeds:
        print(f"Discovering {seed['name']} ({seed['homepage']})...")
        result = discover_one(seed, dry_run=args.dry_run)

        if result["error"]:
            print(f"  ERROR: {result['error']}")
        else:
            print(f"  Research URL: {result['research_url'] or 'not found'}")
            print(f"  RSS: {result['rss_url'] or 'not found'}")
            print(f"  Candidate links: {len(result['candidate_links'])}")
            for link in result["candidate_links"][:5]:
                print(f"    - {link['url']} ({link['label'][:40]})")

        if not args.dry_run:
            candidates = update_candidate(candidates, result)

    if not args.dry_run:
        save_candidates(candidates)
        print(f"\nUpdated {CANDIDATES_PATH}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fund_discovery.py -v`
Expected: 12 PASS

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research && git add discover_fund_sites.py tests/test_unit_fund_discovery.py
git commit -m "feat(candidate): add site discovery — homepage crawl + research link extraction"
```

---

### Task 4: Rule-Based Screening Script

**Files:**
- Create: `screen_fund_candidates.py`
- Modify: `tests/test_unit_fund_discovery.py` (add tests)

- [ ] **Step 1: Write screening tests**

Append to `tests/test_unit_fund_discovery.py`:

```python
import screen_fund_candidates as sfc


def test_screen_rejects_non_public_page():
    result = sfc.screen_page("https://example.com/research", status_code=403, html="")
    assert result["passed"] is False
    assert "not publicly accessible" in result["reason"]


def test_screen_rejects_login_page():
    html = '<html><body><form><input type="password" /><button>Log In</button></form></body></html>'
    result = sfc.screen_page("https://example.com/insights", status_code=200, html=html)
    assert result["passed"] is False
    assert "login" in result["reason"].lower()


def test_screen_passes_research_index():
    html = '''<html><body>
    <article><h2>Q1 Outlook</h2><time>2026-03-15</time></article>
    <article><h2>Market Commentary</h2><time>2026-03-01</time></article>
    <article><h2>Investment Perspectives</h2><time>2026-02-15</time></article>
    </body></html>'''
    result = sfc.screen_page("https://example.com/insights", status_code=200, html=html)
    assert result["passed"] is True


def test_screen_rejects_single_article():
    html = '''<html><body>
    <article><h1>Our 2026 Outlook</h1><p>Long article content here...</p></article>
    </body></html>'''
    result = sfc.screen_page("https://example.com/insights/outlook-2026", status_code=200, html=html)
    assert result["passed"] is False
    assert "single article" in result["reason"].lower() or "not an index" in result["reason"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fund_discovery.py::test_screen_rejects_non_public_page -v`
Expected: FAIL (module not found)

- [ ] **Step 3: Implement screen_fund_candidates.py**

```python
#!/usr/bin/env python3
"""Rule-based screening of candidate fund research pages.

Reads candidates from config/fund_candidates.json (status=discovered),
fetches their research_url, and applies rule-based checks:
  - public accessibility (HTTP 200)
  - not a login/paywall page
  - looks like article index (multiple articles, dates)
  - updated within 90 days (if detectable)

Usage:
    python3 screen_fund_candidates.py [--dry-run] [--fund ID]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

REPO = Path(__file__).parent
CANDIDATES_PATH = REPO / "config" / "fund_candidates.json"

TIMEOUT = 30
FRESHNESS_DAYS = 90

_LOGIN_MARKERS = [
    "log in", "login", "sign in", "signin", "register",
    "create account", "subscribe to read", "enter your password",
]

_DATE_PATTERN = re.compile(
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b"
    r"|"
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b",
    re.IGNORECASE,
)


def screen_page(url: str, status_code: int, html: str) -> dict:
    """Apply rule-based screening to a fetched page.

    Returns: {"passed": bool, "reason": str, "signals": dict}
    """
    signals: dict = {}

    # Check 1: Public accessibility
    if status_code != 200:
        return {"passed": False, "reason": f"Not publicly accessible (HTTP {status_code})", "signals": signals}

    soup = BeautifulSoup(html, "html.parser")
    text_lower = soup.get_text(separator=" ").lower()

    # Check 2: Login/paywall detection
    password_inputs = soup.find_all("input", attrs={"type": "password"})
    login_marker_count = sum(1 for m in _LOGIN_MARKERS if m in text_lower)
    signals["login_markers"] = login_marker_count
    signals["password_inputs"] = len(password_inputs)

    if password_inputs or login_marker_count >= 2:
        return {"passed": False, "reason": "Login/paywall detected", "signals": signals}

    # Check 3: Article index detection (multiple articles or list items)
    articles = soup.find_all("article")
    time_tags = soup.find_all("time")
    date_matches = _DATE_PATTERN.findall(soup.get_text())
    h2_tags = soup.find_all("h2")
    list_items = soup.find_all("li")

    signals["articles"] = len(articles)
    signals["time_tags"] = len(time_tags)
    signals["date_matches"] = len(date_matches)
    signals["h2_tags"] = len(h2_tags)

    # Need at least 2 article-like items to qualify as an index
    article_signals = len(articles) + min(len(time_tags), 5) + min(len(date_matches), 5)
    if article_signals < 2 and len(h2_tags) < 3:
        return {"passed": False, "reason": "Not an index page (single article or no article structure)", "signals": signals}

    return {"passed": True, "reason": "Passes all screening rules", "signals": signals}


def screen_one(candidate: dict, dry_run: bool = False) -> dict:
    """Fetch and screen a single candidate's research URL."""
    url = candidate.get("research_url")
    if not url:
        return {"passed": False, "reason": "No research URL discovered", "signals": {}}

    try:
        resp = httpx.get(url, timeout=TIMEOUT, follow_redirects=True)
        return screen_page(url, resp.status_code, resp.text)
    except Exception as e:
        return {"passed": False, "reason": f"Fetch error: {e}", "signals": {}}


def main() -> None:
    parser = argparse.ArgumentParser(description="Screen candidate fund research pages")
    parser.add_argument("--fund", help="Screen single fund by ID")
    parser.add_argument("--dry-run", action="store_true", help="Print results without writing")
    args = parser.parse_args()

    candidates = json.loads(CANDIDATES_PATH.read_text())

    for c in candidates:
        if args.fund and c["id"] != args.fund:
            continue
        if c["status"] not in ("discovered",):
            continue

        print(f"Screening {c['name']} ({c.get('research_url', 'no URL')})...")
        result = screen_one(c, dry_run=args.dry_run)

        if result["passed"]:
            print(f"  PASS: {result['reason']}")
            if not args.dry_run:
                c["status"] = "screened"
                c["last_screened_at"] = datetime.now(timezone.utc).isoformat()
                c["is_publicly_accessible"] = True
                c["has_article_index"] = True
        else:
            print(f"  FAIL: {result['reason']}")
            if not args.dry_run:
                c["notes"] = f"Screening failed: {result['reason']}"

        print(f"  Signals: {result['signals']}")

    if not args.dry_run:
        CANDIDATES_PATH.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
        print(f"\nUpdated {CANDIDATES_PATH}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fund_discovery.py -v`
Expected: 16 PASS

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research && git add screen_fund_candidates.py tests/test_unit_fund_discovery.py
git commit -m "feat(candidate): add rule-based screening — login/paywall/index detection"
```

---

### Task 5: Candidate Entrypoint Discovery

**Files:**
- Create: `discover_candidate_entrypoints.py`
- Create: `config/candidate_entrypoints.json`
- Create: `tests/test_unit_candidate_entrypoints.py`

- [ ] **Step 1: Write candidate entrypoint tests**

```python
# tests/test_unit_candidate_entrypoints.py
import json
from pathlib import Path

CANDIDATE_EP_PATH = Path(__file__).parent.parent / "config" / "candidate_entrypoints.json"
PRODUCTION_EP_PATH = Path(__file__).parent.parent / "config" / "entrypoints.json"


def test_candidate_entrypoints_file_exists_and_valid():
    data = json.loads(CANDIDATE_EP_PATH.read_text())
    assert "version" in data
    assert "sources" in data
    assert isinstance(data["sources"], dict)


def test_candidate_entrypoints_isolated_from_production():
    """Candidate entrypoints must not contain any production source IDs."""
    prod = json.loads(PRODUCTION_EP_PATH.read_text())
    cand = json.loads(CANDIDATE_EP_PATH.read_text())
    prod_ids = set(prod["sources"].keys())
    cand_ids = set(cand["sources"].keys())
    overlap = prod_ids & cand_ids
    assert not overlap, f"Candidate entrypoints overlap with production: {overlap}"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_candidate_entrypoints.py -v`
Expected: FAIL (file not found)

- [ ] **Step 3: Create empty candidate entrypoints file**

```json
{
  "version": 1,
  "sources": {}
}
```

- [ ] **Step 4: Implement discover_candidate_entrypoints.py**

```python
#!/usr/bin/env python3
"""Score and register candidate entrypoints using existing GMIA scorer.

For screened candidates, fetches their research pages, scores them via
entrypoint_scorer.py, and writes results to config/candidate_entrypoints.json.

This script NEVER writes to config/entrypoints.json (production).

Usage:
    python3 discover_candidate_entrypoints.py [--dry-run] [--fund ID]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

import entrypoint_scorer
from discover_entrypoints import extract_nav_links

REPO = Path(__file__).parent
CANDIDATES_PATH = REPO / "config" / "fund_candidates.json"
CANDIDATE_EP_PATH = REPO / "config" / "candidate_entrypoints.json"
SCORER_WEIGHTS_PATH = REPO / "config" / "scorer_weights.json"

TIMEOUT = 30
MAX_ENTRYPOINTS = 3
SCORE_THRESHOLD = 0.5


def load_weights() -> dict:
    if SCORER_WEIGHTS_PATH.exists():
        return json.loads(SCORER_WEIGHTS_PATH.read_text())
    return {"domain": 0.2, "path": 0.3, "structure": 0.3, "gate": 0.2}


def score_candidate_page(url: str, html: str, allowed_domains: list[str], weights: dict) -> dict:
    """Score a single candidate page using entrypoint_scorer."""
    domain_score = entrypoint_scorer.score_domain(url, allowed_domains)
    path_score = entrypoint_scorer.score_path(url)
    structure_score = entrypoint_scorer.score_structure(html)
    gate_penalty = entrypoint_scorer.detect_gate(html)
    final = entrypoint_scorer.score_final(domain_score, path_score, structure_score, gate_penalty)

    return {
        "url": url,
        "scores": {
            "domain": round(domain_score, 3),
            "path": round(path_score, 3),
            "structure": round(structure_score, 3),
            "gate_penalty": round(gate_penalty, 3),
            "final": round(final, 3),
        },
    }


def discover_for_candidate(candidate: dict, dry_run: bool = False) -> list[dict]:
    """Discover and score entrypoints for a single candidate fund."""
    research_url = candidate.get("research_url")
    domain = candidate.get("official_domain", "")
    allowed = [domain] if domain else []
    weights = load_weights()

    if not research_url:
        return []

    scored: list[dict] = []

    # Score the primary research URL
    try:
        resp = httpx.get(research_url, timeout=TIMEOUT, follow_redirects=True)
        if resp.status_code == 200:
            result = score_candidate_page(research_url, resp.text, allowed, weights)
            scored.append(result)

            # Also check nav links from research page for additional entrypoints
            links = extract_nav_links(resp.text, research_url, allowed)
            for link in links[:10]:
                link_url = link["url"]
                if link_url == research_url:
                    continue
                try:
                    link_resp = httpx.get(link_url, timeout=TIMEOUT, follow_redirects=True)
                    if link_resp.status_code == 200:
                        link_result = score_candidate_page(link_url, link_resp.text, allowed, weights)
                        scored.append(link_result)
                except Exception:
                    continue
    except Exception as e:
        print(f"  Fetch error for {research_url}: {e}")

    # Sort by score, take top N
    scored.sort(key=lambda x: x["scores"]["final"], reverse=True)
    return scored[:MAX_ENTRYPOINTS]


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover candidate entrypoints")
    parser.add_argument("--fund", help="Process single fund by ID")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    args = parser.parse_args()

    candidates = json.loads(CANDIDATES_PATH.read_text())
    ep_data = json.loads(CANDIDATE_EP_PATH.read_text())
    now = datetime.now(timezone.utc).isoformat()

    for c in candidates:
        if args.fund and c["id"] != args.fund:
            continue
        if c["status"] not in ("screened",):
            continue

        print(f"Scoring entrypoints for {c['name']}...")
        scored = discover_for_candidate(c)

        if not scored:
            print("  No scoreable pages found")
            continue

        for s in scored:
            status = "ok" if s["scores"]["final"] >= SCORE_THRESHOLD else "weak"
            print(f"  {status}: {s['url']} (score={s['scores']['final']:.3f})")

        if not args.dry_run:
            ep_data["sources"][c["id"]] = {
                "last_verified_at": now,
                "verified_by": "candidate_discovery",
                "entrypoints": [
                    {
                        "url": s["url"],
                        "content_type": "research_index",
                        "confidence": s["scores"]["final"],
                        "active": i == 0,  # only first is active
                        "scores": s["scores"],
                    }
                    for i, s in enumerate(scored)
                    if s["scores"]["final"] >= SCORE_THRESHOLD
                ],
                "rejected_pages": [
                    {"url": s["url"], "score": s["scores"]["final"]}
                    for s in scored
                    if s["scores"]["final"] < SCORE_THRESHOLD
                ],
            }
            c["status"] = "validated"
            c["last_validated_at"] = now
            c["fit_score"] = scored[0]["scores"]["final"] if scored else None

    if not args.dry_run:
        CANDIDATES_PATH.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + "\n")
        CANDIDATE_EP_PATH.write_text(json.dumps(ep_data, indent=2, ensure_ascii=False) + "\n")
        print(f"\nUpdated {CANDIDATES_PATH}")
        print(f"Updated {CANDIDATE_EP_PATH}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_candidate_entrypoints.py tests/test_unit_fund_discovery.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
cd ~/hedge-fund-research && git add discover_candidate_entrypoints.py config/candidate_entrypoints.json tests/test_unit_candidate_entrypoints.py
git commit -m "feat(candidate): add entrypoint scoring — reuses entrypoint_scorer, isolated state"
```

---

### Task 6: End-to-End Integration Test + First Live Run

**Files:**
- Modify: `tests/test_unit_fund_discovery.py` (add integration marker)

- [ ] **Step 1: Write integration test**

Append to `tests/test_unit_fund_discovery.py`:

```python
import subprocess

import pytest


@pytest.mark.live
def test_discover_pimco_dry_run():
    """Integration: discover PIMCO research pages (dry run, no state writes)."""
    result = subprocess.run(
        ["python3", "discover_fund_sites.py", "--fund", "pimco", "--dry-run"],
        capture_output=True, text=True, cwd=str(SEEDS_PATH.parent.parent),
        timeout=60,
    )
    assert result.returncode == 0
    assert "pimco.com" in result.stdout.lower()
```

- [ ] **Step 2: Run the full pipeline manually on PIMCO**

```bash
cd ~/hedge-fund-research
python3 discover_fund_sites.py --fund pimco
python3 screen_fund_candidates.py --fund pimco
python3 discover_candidate_entrypoints.py --fund pimco
```

- [ ] **Step 3: Verify results**

```bash
python3 -c "
import json
c = json.load(open('config/fund_candidates.json'))
pimco = [x for x in c if x['id'] == 'pimco'][0]
print(f'Status: {pimco[\"status\"]}')
print(f'Research URL: {pimco[\"research_url\"]}')
print(f'Fit Score: {pimco[\"fit_score\"]}')

ep = json.load(open('config/candidate_entrypoints.json'))
if 'pimco' in ep['sources']:
    for e in ep['sources']['pimco']['entrypoints']:
        print(f'Entrypoint: {e[\"url\"]} (confidence={e[\"confidence\"]:.3f}, active={e[\"active\"]})')
"
```

Expected: PIMCO status should be `validated`, with at least one entrypoint scored > 0.5.

- [ ] **Step 4: Run all existing tests to verify no regressions**

Run: `cd ~/hedge-fund-research && python3 -m pytest --ignore=tests/test_regression.py -v`
Expected: All existing 164+ tests PASS, plus new tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research && git add -A
git commit -m "feat(candidate): Phase 1 complete — discovery + screening + entrypoint scoring

Candidate fund discovery pipeline (isolated from production):
- 5 seed funds (PIMCO, D.E. Shaw, Blackstone, Two Sigma, KKR)
- Homepage crawl + research link extraction
- Rule-based screening (login/paywall/index detection)
- Entrypoint scoring via existing entrypoint_scorer.py
- All state in config/fund_candidates.json + config/candidate_entrypoints.json
- Zero production file writes"
```

- [ ] **Step 6: Run full discovery for all 5 seeds**

```bash
cd ~/hedge-fund-research
python3 discover_fund_sites.py
python3 screen_fund_candidates.py
python3 discover_candidate_entrypoints.py
git add config/fund_candidates.json config/candidate_entrypoints.json
git commit -m "data(candidate): first full discovery run — 5 seed funds"
git push
```

---

## Self-Review Checklist

1. **Spec coverage**: Seed pool (Task 1) ✓, Discovery (Task 3) ✓, Screening (Task 4) ✓, Candidate entrypoints (Task 5) ✓, Integration test (Task 6) ✓. Isolation verified by tests. Promotion = Phase 2+ (out of scope).

2. **Placeholder scan**: All code blocks are complete. No TBD/TODO. All test assertions are concrete.

3. **Type consistency**: `extract_research_links()` returns `list[dict]` consistently. `screen_page()` returns `{"passed": bool, "reason": str, "signals": dict}` consistently. `score_candidate_page()` returns `{"url": str, "scores": dict}` consistently. Candidate JSON schema fields match between creation (Task 2) and update (Task 3/4/5).

4. **Design doc reference**: `docs/gmia-candidate-fund-discovery-design.md` covers full architecture. Plan implements Phase 1 only (report-only, no production writes).
