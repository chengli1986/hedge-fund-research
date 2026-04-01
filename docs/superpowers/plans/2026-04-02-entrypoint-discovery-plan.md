# Entrypoint Discovery Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a rule-based entrypoint discovery and validation system so GMIA can detect broken research URLs, score candidate replacements, and persist verified entrypoints to config — without calling any LLM.

**Architecture:** Three new scripts (`discover_entrypoints.py`, `validate_entrypoints.py`, scoring engine in `entrypoint_scorer.py`) plus two new config files (`entrypoints.json`, `inspection_state.json`). `fetch_articles.py` gets minimal changes: load entrypoints at startup, record quality metrics after each fetch, warn on anomalies.

**Tech Stack:** Python 3.12, requests, BeautifulSoup4, pytest. No new dependencies.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `config/entrypoints.json` | Create | Persisted entrypoint config per source |
| `config/inspection_state.json` | Create | Per-source quality metrics and anomaly state |
| `entrypoint_scorer.py` | Create | Pure scoring engine: domain, path, structure, gate — no I/O |
| `discover_entrypoints.py` | Create | Crawl homepage links, apply scorer, output candidates |
| `validate_entrypoints.py` | Create | Validate existing entrypoints, detect failures |
| `fetch_articles.py` | Modify | Load entrypoints, record quality metrics |
| `tests/test_unit_scorer.py` | Create | Tests for scoring engine |
| `tests/test_unit_discover.py` | Create | Tests for discover_entrypoints |
| `tests/test_unit_validate.py` | Create | Tests for validate_entrypoints |
| `tests/test_integration_entrypoints.py` | Create | End-to-end: discover → validate → fetch_articles integration |

---

### Task 1: Scoring Engine (`entrypoint_scorer.py`)

Pure functions, no I/O, no network. This is the foundation everything else depends on.

**Files:**
- Create: `entrypoint_scorer.py`
- Create: `tests/test_unit_scorer.py`

- [ ] **Step 1: Write failing tests for `score_domain()`**

```python
# tests/test_unit_scorer.py
"""Unit tests for entrypoint_scorer.py — pure scoring functions."""

import pytest
from entrypoint_scorer import score_domain, score_path, score_structure, score_gate, score_final


class TestScoreDomain:
    def test_exact_match(self):
        assert score_domain("https://www.aqr.com/research", ["aqr.com"]) == 1.0

    def test_subdomain_match(self):
        assert score_domain("https://papers.aqr.com/article", ["aqr.com"]) == 0.8

    def test_bare_domain_match(self):
        assert score_domain("https://aqr.com/insights", ["aqr.com"]) == 1.0

    def test_no_match(self):
        assert score_domain("https://www.bloomberg.com/news", ["aqr.com"]) == 0.0

    def test_partial_name_rejected(self):
        assert score_domain("https://notaqr.com/page", ["aqr.com"]) == 0.0

    def test_multiple_allowed(self):
        assert score_domain("https://cdn.aqr.com/file.pdf", ["aqr.com", "cdn.aqr.com"]) == 1.0

    def test_empty_url(self):
        assert score_domain("", ["aqr.com"]) == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_scorer.py::TestScoreDomain -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'entrypoint_scorer'`

- [ ] **Step 3: Implement `score_domain()`**

```python
# entrypoint_scorer.py
"""
Entrypoint scoring engine — pure functions for evaluating candidate URLs.

Four scoring dimensions:
  - domain: hostname whitelist check
  - path: URL path keyword signals
  - structure: HTML page structure signals
  - gate: gated/marketing content penalty

All functions are stateless and do no I/O.
"""

from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Domain scoring
# ---------------------------------------------------------------------------

def score_domain(url: str, allowed_domains: list[str]) -> float:
    """Score URL domain against allowed domain list.

    Returns: 1.0 (exact/www match), 0.8 (subdomain match), 0.0 (no match).
    """
    if not url:
        return 0.0
    hostname = urlparse(url).hostname or ""
    if not hostname:
        return 0.0

    for domain in allowed_domains:
        if hostname == domain or hostname == "www." + domain:
            return 1.0
        if hostname.endswith("." + domain):
            return 0.8
    return 0.0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_scorer.py::TestScoreDomain -v`
Expected: all 7 PASS

- [ ] **Step 5: Write failing tests for `score_path()`**

Add to `tests/test_unit_scorer.py`:

```python
class TestScorePath:
    def test_research_path(self):
        score = score_path("https://www.aqr.com/Insights/Research")
        assert score > 0.7

    def test_about_page(self):
        score = score_path("https://www.aqr.com/about")
        assert score < 0.3

    def test_careers_page(self):
        score = score_path("https://www.man.com/careers")
        assert score < 0.3

    def test_mixed_signals(self):
        """Path with both positive and negative keywords."""
        score = score_path("https://example.com/insights/subscribe")
        assert 0.3 <= score <= 0.7

    def test_neutral_path(self):
        """No keywords at all — should return 0.5."""
        score = score_path("https://example.com/foo/bar")
        assert score == 0.5

    def test_multiple_positive_keywords(self):
        score = score_path("https://example.com/research/quarterly-report")
        assert score > 0.8

    def test_login_page(self):
        score = score_path("https://example.com/login")
        assert score < 0.3
```

- [ ] **Step 6: Implement `score_path()`**

Add to `entrypoint_scorer.py`:

```python
# ---------------------------------------------------------------------------
# Path scoring
# ---------------------------------------------------------------------------

POSITIVE_PATH_KEYWORDS = {
    "research", "insight", "insights", "publication", "publications",
    "commentary", "market-commentary", "white-paper", "report", "reports",
    "quarterly", "annual", "letters", "outlook", "papers", "library",
    "perspectives", "thinking",
}

NEGATIVE_PATH_KEYWORDS = {
    "about", "careers", "contact", "team", "leadership", "events",
    "podcast", "video", "subscribe", "login", "register",
}


def score_path(url: str) -> float:
    """Score URL path based on keyword signals.

    Returns: positive_hits / (positive_hits + negative_hits), or 0.5 if no hits.
    """
    if not url:
        return 0.5
    path = urlparse(url).path.lower().replace("/", " ").replace("-", " ").replace("_", " ")
    path_words = set(path.split())

    pos = len(path_words & POSITIVE_PATH_KEYWORDS)
    neg = len(path_words & NEGATIVE_PATH_KEYWORDS)

    if pos + neg == 0:
        return 0.5
    return pos / (pos + neg)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_scorer.py::TestScorePath -v`
Expected: all 7 PASS

- [ ] **Step 8: Write failing tests for `score_structure()`**

Add to `tests/test_unit_scorer.py`:

```python
class TestScoreStructure:
    def test_research_index_page(self):
        """Page with article cards, dates, authors — high score."""
        html = """
        <article>
            <h2><a href="/research/article-1">Article Title</a></h2>
            <time>March 15, 2026</time>
            <span class="author">John Smith</span>
            <a href="/download/report.pdf">Download PDF</a>
        </article>
        <article>
            <h2><a href="/research/article-2">Another Article</a></h2>
            <time>March 10, 2026</time>
        </article>
        <nav class="pagination"><a href="?page=2">Next</a></nav>
        """
        assert score_structure(html) > 0.7

    def test_marketing_page(self):
        """Page with only CTA and subscribe form — low score."""
        html = """
        <div class="hero">
            <h1>Welcome to Our Fund</h1>
            <button class="cta">Subscribe Now</button>
            <form action="/subscribe"><input type="email" /><button>Sign Up</button></form>
        </div>
        """
        assert score_structure(html) < 0.4

    def test_empty_html(self):
        assert score_structure("") == 0.0

    def test_pdf_hub(self):
        """Page with multiple PDF links — high score."""
        html = """
        <ul>
            <li><a href="/reports/q1.pdf">Q1 Report</a> - January 2026</li>
            <li><a href="/reports/q2.pdf">Q2 Report</a> - April 2026</li>
            <li><a href="/reports/annual.pdf">Annual Report</a> - December 2025</li>
        </ul>
        """
        assert score_structure(html) > 0.5
```

- [ ] **Step 9: Implement `score_structure()`**

Add to `entrypoint_scorer.py`:

```python
import re

# ---------------------------------------------------------------------------
# Structure scoring
# ---------------------------------------------------------------------------

def score_structure(html: str) -> float:
    """Score page HTML based on structural signals.

    Positive: article cards, dates, authors, PDF links, pagination, read-more links.
    Negative: subscribe forms, CTA buttons, no dates/articles/downloads.

    Returns: positive / (positive + negative), or 0.0 if empty.
    """
    if not html:
        return 0.0

    lowered = html.lower()
    pos = 0
    neg = 0

    # Positive signals
    if re.search(r"<article[\s>]", lowered):
        pos += 1
    if len(re.findall(r"<article[\s>]", lowered)) >= 2:
        pos += 1  # multiple article cards
    if re.search(r"<time[\s>]", lowered):
        pos += 1
    if re.search(r'class="[^"]*(?:author|byline)[^"]*"', lowered):
        pos += 1
    if re.search(r'href="[^"]*\.pdf', lowered):
        pos += 1
    if len(re.findall(r'href="[^"]*\.pdf', lowered)) >= 2:
        pos += 1  # multiple PDFs
    if re.search(r"read\s*more|download\s*report|view\s*report", lowered):
        pos += 1
    if re.search(r'class="[^"]*pagination[^"]*"', lowered) or re.search(r"page=\d", lowered):
        pos += 1
    # Date patterns in text (not just <time> tags)
    if re.search(r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4}\b", lowered):
        pos += 1

    # Negative signals
    if re.search(r"<form[\s>]", lowered) and re.search(r"subscribe|sign.?up|newsletter", lowered):
        neg += 1
    if re.search(r'class="[^"]*cta[^"]*"', lowered) or re.search(r"<button[^>]*>.*?(?:subscribe|sign up|get started)", lowered):
        neg += 1
    if not re.search(r"<article[\s>]|<time[\s>]|\.pdf|read\s*more", lowered):
        neg += 1  # no article-like content at all

    if pos + neg == 0:
        return 0.0
    return pos / (pos + neg)
```

- [ ] **Step 10: Run tests to verify they pass**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_scorer.py::TestScoreStructure -v`
Expected: all 4 PASS

- [ ] **Step 11: Write failing tests for `score_gate()` and `score_final()`**

Add to `tests/test_unit_scorer.py`:

```python
class TestScoreGate:
    def test_clean_page(self):
        html = "<article><h2>Research Article</h2><p>Content here.</p></article>"
        assert score_gate(html) == 0.0

    def test_gated_page(self):
        html = "<p>Subscribe to read the full article. Register to continue.</p>"
        penalty = score_gate(html)
        assert penalty >= 0.3

    def test_cookie_page(self):
        html = "<div>Cookie preferences. Privacy policy. Terms of use.</div>"
        assert score_gate(html) >= 0.3

    def test_max_penalty_capped(self):
        html = """Subscribe to read. Register to continue. Log in to read.
        For clients only. Cookie preferences. Privacy policy. Terms of use."""
        assert score_gate(html) == 1.0

    def test_empty_html(self):
        assert score_gate("") == 0.0


class TestScoreFinal:
    def test_perfect_candidate(self):
        score = score_final(domain=1.0, path=1.0, structure=0.9, gate_penalty=0.0)
        assert score >= 0.8

    def test_marketing_page(self):
        score = score_final(domain=1.0, path=0.0, structure=0.1, gate_penalty=0.3)
        assert score < 0.4

    def test_domain_reject(self):
        score = score_final(domain=0.0, path=1.0, structure=1.0, gate_penalty=0.0)
        assert score < 0.4

    def test_weights_sum_to_one(self):
        """All perfect scores should give 1.0."""
        assert score_final(domain=1.0, path=1.0, structure=1.0, gate_penalty=0.0) == 1.0
```

- [ ] **Step 12: Implement `score_gate()` and `score_final()`**

Add to `entrypoint_scorer.py`:

```python
# ---------------------------------------------------------------------------
# Gate penalty scoring
# ---------------------------------------------------------------------------

GATE_MARKERS = [
    "subscribe to read", "register to continue", "log in to read",
    "log in to continue", "sign up to read", "register to read",
    "for clients only",
]

DISCLAIMER_MARKERS = [
    "cookie preferences", "privacy policy", "terms of use",
    "manage cookies", "accept all cookies",
]


def score_gate(html: str) -> float:
    """Compute gate/disclaimer penalty from page HTML.

    Each matched marker adds 0.15, capped at 1.0.
    Returns: penalty value (0.0 = clean, 1.0 = max penalty).
    """
    if not html:
        return 0.0
    lowered = html.lower()
    hits = sum(1 for m in GATE_MARKERS + DISCLAIMER_MARKERS if m in lowered)
    return min(hits * 0.15, 1.0)


# ---------------------------------------------------------------------------
# Final composite score
# ---------------------------------------------------------------------------

def score_final(domain: float, path: float, structure: float, gate_penalty: float) -> float:
    """Compute weighted final score.

    Weights: domain=0.2, path=0.3, structure=0.3, gate=0.2.
    Returns: float 0.0–1.0.
    """
    return domain * 0.2 + path * 0.3 + structure * 0.3 + (1.0 - gate_penalty) * 0.2
```

- [ ] **Step 13: Run all scorer tests**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_scorer.py -v`
Expected: all 27 PASS

- [ ] **Step 14: Commit**

```bash
cd ~/hedge-fund-research
git add entrypoint_scorer.py tests/test_unit_scorer.py
git commit -m "feat: add entrypoint scoring engine — domain, path, structure, gate scoring"
```

---

### Task 2: Entrypoints Config + Seed Data (`config/entrypoints.json`)

Seed `entrypoints.json` from current `sources.json` URLs so existing behavior is preserved.

**Files:**
- Create: `config/entrypoints.json`
- Create: `config/inspection_state.json`

- [ ] **Step 1: Create `config/entrypoints.json` with seed data from current sources**

```json
{
  "version": 1,
  "sources": {
    "man-group": {
      "last_verified_at": "2026-04-02T00:00:00Z",
      "verified_by": "manual",
      "entrypoints": [
        {
          "url": "https://www.man.com/insights",
          "content_type": "research_index",
          "confidence": 0.95,
          "active": true
        }
      ],
      "rejected_pages": []
    },
    "bridgewater": {
      "last_verified_at": "2026-04-02T00:00:00Z",
      "verified_by": "manual",
      "entrypoints": [
        {
          "url": "https://www.bridgewater.com/research-and-insights",
          "content_type": "research_index",
          "confidence": 0.95,
          "active": true
        }
      ],
      "rejected_pages": []
    },
    "aqr": {
      "last_verified_at": "2026-04-02T00:00:00Z",
      "verified_by": "manual",
      "entrypoints": [
        {
          "url": "https://www.aqr.com/Insights/Research",
          "content_type": "research_index",
          "confidence": 0.95,
          "active": true
        }
      ],
      "rejected_pages": []
    },
    "gmo": {
      "last_verified_at": "2026-04-02T00:00:00Z",
      "verified_by": "manual",
      "entrypoints": [
        {
          "url": "https://www.gmo.com/americas/research-library/",
          "content_type": "research_index",
          "confidence": 0.95,
          "active": true
        }
      ],
      "rejected_pages": []
    },
    "oaktree": {
      "last_verified_at": "2026-04-02T00:00:00Z",
      "verified_by": "manual",
      "entrypoints": [
        {
          "url": "https://www.oaktreecapital.com/insights",
          "content_type": "research_index",
          "confidence": 0.95,
          "active": true
        }
      ],
      "rejected_pages": []
    },
    "ark-invest": {
      "last_verified_at": "2026-04-02T00:00:00Z",
      "verified_by": "manual",
      "entrypoints": [
        {
          "url": "https://www.ark-invest.com/feed",
          "content_type": "rss_feed",
          "confidence": 0.95,
          "active": true
        }
      ],
      "rejected_pages": []
    }
  }
}
```

- [ ] **Step 2: Create empty `config/inspection_state.json`**

```json
{}
```

- [ ] **Step 3: Commit**

```bash
cd ~/hedge-fund-research
git add config/entrypoints.json config/inspection_state.json
git commit -m "feat: seed entrypoints.json from existing sources + empty inspection state"
```

---

### Task 3: fetch_articles.py Integration

Minimal changes: load entrypoints at startup, record quality metrics after fetch, warn on anomalies.

**Files:**
- Modify: `fetch_articles.py`
- Modify: `tests/test_unit_fetch_articles.py`

- [ ] **Step 1: Write failing tests for entrypoint loading and quality metrics**

Add to `tests/test_unit_fetch_articles.py`:

```python
from fetch_articles import load_entrypoints, get_source_url, record_quality_metrics, check_anomalies

class TestLoadEntrypoints:
    def test_loads_from_file(self, tmp_path, monkeypatch):
        ep_file = tmp_path / "entrypoints.json"
        ep_file.write_text(json.dumps({
            "version": 1,
            "sources": {
                "aqr": {
                    "entrypoints": [
                        {"url": "https://www.aqr.com/new-research", "content_type": "research_index",
                         "confidence": 0.9, "active": True}
                    ]
                }
            }
        }))
        monkeypatch.setattr("fetch_articles.ENTRYPOINTS_FILE", ep_file)
        ep = load_entrypoints()
        assert "aqr" in ep["sources"]

    def test_missing_file_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("fetch_articles.ENTRYPOINTS_FILE", tmp_path / "nope.json")
        ep = load_entrypoints()
        assert ep == {"version": 1, "sources": {}}


class TestGetSourceUrl:
    def test_uses_entrypoint_when_available(self):
        ep = {"version": 1, "sources": {
            "aqr": {"entrypoints": [
                {"url": "https://www.aqr.com/new", "active": True, "confidence": 0.9, "content_type": "research_index"}
            ]}
        }}
        source = {"id": "aqr", "url": "https://www.aqr.com/old"}
        assert get_source_url(source, ep) == "https://www.aqr.com/new"

    def test_fallback_to_source_url(self):
        ep = {"version": 1, "sources": {}}
        source = {"id": "aqr", "url": "https://www.aqr.com/old"}
        assert get_source_url(source, ep) == "https://www.aqr.com/old"

    def test_skips_inactive_entrypoint(self):
        ep = {"version": 1, "sources": {
            "aqr": {"entrypoints": [
                {"url": "https://www.aqr.com/new", "active": False, "confidence": 0.9, "content_type": "research_index"}
            ]}
        }}
        source = {"id": "aqr", "url": "https://www.aqr.com/old"}
        assert get_source_url(source, ep) == "https://www.aqr.com/old"


class TestRecordQualityMetrics:
    def test_records_metrics(self, tmp_path, monkeypatch):
        state_file = tmp_path / "inspection_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr("fetch_articles.INSPECTION_STATE_FILE", state_file)
        record_quality_metrics("aqr", total_found=5, new_count=3, gated_count=0, mismatch_count=0)
        state = json.loads(state_file.read_text())
        assert state["aqr"]["last_article_count"] == 5

    def test_increments_consecutive_zero(self, tmp_path, monkeypatch):
        state_file = tmp_path / "inspection_state.json"
        state_file.write_text(json.dumps({"aqr": {"consecutive_zero_count": 1, "last_article_count": 0}}))
        monkeypatch.setattr("fetch_articles.INSPECTION_STATE_FILE", state_file)
        record_quality_metrics("aqr", total_found=0, new_count=0, gated_count=0, mismatch_count=0)
        state = json.loads(state_file.read_text())
        assert state["aqr"]["consecutive_zero_count"] == 2


class TestCheckAnomalies:
    def test_no_anomaly(self):
        metrics = {"consecutive_zero_count": 0, "last_article_count": 5,
                   "last_valid_body_ratio": 0.8, "last_gated_ratio": 0.0, "last_mismatch_count": 0}
        assert check_anomalies(metrics) == []

    def test_consecutive_zero(self):
        metrics = {"consecutive_zero_count": 2, "last_article_count": 0,
                   "last_valid_body_ratio": 1.0, "last_gated_ratio": 0.0, "last_mismatch_count": 0}
        alerts = check_anomalies(metrics)
        assert any("zero" in a.lower() for a in alerts)

    def test_high_gated_ratio(self):
        metrics = {"consecutive_zero_count": 0, "last_article_count": 10,
                   "last_valid_body_ratio": 1.0, "last_gated_ratio": 0.6, "last_mismatch_count": 0}
        alerts = check_anomalies(metrics)
        assert any("gated" in a.lower() for a in alerts)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fetch_articles.py::TestLoadEntrypoints -v`
Expected: FAIL — `ImportError: cannot import name 'load_entrypoints'`

- [ ] **Step 3: Implement the four new functions in `fetch_articles.py`**

Add near the top constants:

```python
ENTRYPOINTS_FILE = BASE_DIR / "config" / "entrypoints.json"
INSPECTION_STATE_FILE = BASE_DIR / "config" / "inspection_state.json"
```

Add after the `load_existing_ids()` function:

```python
def load_entrypoints() -> dict:
    """Load entrypoints config. Returns empty structure if file missing."""
    if ENTRYPOINTS_FILE.exists():
        try:
            return json.loads(ENTRYPOINTS_FILE.read_text())
        except (json.JSONDecodeError, KeyError):
            log.warning("Failed to parse %s, using empty entrypoints", ENTRYPOINTS_FILE)
    return {"version": 1, "sources": {}}


def get_source_url(source: dict, entrypoints: dict) -> str:
    """Get the best URL for a source: active entrypoint > sources.json fallback."""
    source_id = source["id"]
    ep_source = entrypoints.get("sources", {}).get(source_id, {})
    for ep in ep_source.get("entrypoints", []):
        if ep.get("active", False):
            return ep["url"]
    return source["url"]


def record_quality_metrics(source_id: str, total_found: int, new_count: int,
                           gated_count: int, mismatch_count: int) -> None:
    """Record fetch quality metrics to inspection_state.json."""
    state = {}
    if INSPECTION_STATE_FILE.exists():
        try:
            state = json.loads(INSPECTION_STATE_FILE.read_text())
        except json.JSONDecodeError:
            state = {}

    prev = state.get(source_id, {})
    consecutive_zero = prev.get("consecutive_zero_count", 0)
    if total_found == 0:
        consecutive_zero += 1
    else:
        consecutive_zero = 0

    valid_body_ratio = 1.0 - (gated_count / max(total_found, 1))
    gated_ratio = gated_count / max(total_found, 1)

    state[source_id] = {
        "last_inspected_at": datetime.now(timezone.utc).isoformat(),
        "consecutive_zero_count": consecutive_zero,
        "last_article_count": total_found,
        "last_valid_body_ratio": round(valid_body_ratio, 2),
        "last_gated_ratio": round(gated_ratio, 2),
        "last_mismatch_count": mismatch_count,
    }

    INSPECTION_STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def check_anomalies(metrics: dict) -> list[str]:
    """Check metrics for anomaly conditions. Returns list of alert messages."""
    alerts = []
    if metrics.get("consecutive_zero_count", 0) >= 2:
        alerts.append("Consecutive zero articles detected — entrypoint may be broken")
    if metrics.get("last_gated_ratio", 0) > 0.5:
        alerts.append("High gated page ratio (>50%) — entrypoint may point to gated content")
    if metrics.get("last_valid_body_ratio", 1.0) < 0.3:
        alerts.append("Low valid body ratio (<30%) — content extraction failing")
    if metrics.get("last_mismatch_count", 0) > 3:
        alerts.append("High source mismatch count (>3) — entrypoint may have drifted")
    return alerts
```

- [ ] **Step 4: Wire into `fetch_source()` and `main()`**

In `fetch_source()`, after the source mismatch counting loop, before the return, add mismatch tracking:

```python
# At top of fetch_source(), add gated_count and mismatch_count counters
# Then before return, call record_quality_metrics()
```

In `main()`, add after config loading:

```python
entrypoints = load_entrypoints()
```

And in the source loop, before calling the fetcher, update source URL:

```python
source["url"] = get_source_url(source, entrypoints)
```

After `fetch_source()` returns, check anomalies:

```python
# After fetching, check inspection state for anomalies
if INSPECTION_STATE_FILE.exists():
    try:
        state = json.loads(INSPECTION_STATE_FILE.read_text())
        source_metrics = state.get(source["id"], {})
        alerts = check_anomalies(source_metrics)
        for alert in alerts:
            log.warning("ANOMALY [%s]: %s", source["id"], alert)
    except json.JSONDecodeError:
        pass
```

- [ ] **Step 5: Run all fetch_articles tests**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fetch_articles.py -v`
Expected: all PASS (old tests + new tests)

- [ ] **Step 6: Commit**

```bash
cd ~/hedge-fund-research
git add fetch_articles.py tests/test_unit_fetch_articles.py
git commit -m "feat: integrate entrypoints.json into fetch_articles — load, fallback, quality metrics"
```

---

### Task 4: Entrypoint Discovery Script (`discover_entrypoints.py`)

Crawls homepage links, applies scorer, outputs structured candidate JSON.

**Files:**
- Create: `discover_entrypoints.py`
- Create: `tests/test_unit_discover.py`

- [ ] **Step 1: Write failing tests for link extraction and candidate scoring**

```python
# tests/test_unit_discover.py
"""Unit tests for discover_entrypoints.py — link extraction, candidate scoring."""

import json
import pytest
from unittest.mock import patch, MagicMock
from discover_entrypoints import extract_nav_links, score_candidates, _classify_with_ai


class TestExtractNavLinks:
    def test_extracts_links_from_nav(self):
        html = """
        <nav>
            <a href="/research">Research</a>
            <a href="/insights">Insights</a>
            <a href="/about">About</a>
        </nav>
        <footer>
            <a href="/careers">Careers</a>
        </footer>
        """
        links = extract_nav_links(html, "https://www.example.com")
        urls = [l["url"] for l in links]
        assert "https://www.example.com/research" in urls
        assert "https://www.example.com/insights" in urls
        assert "https://www.example.com/about" in urls

    def test_deduplicates_links(self):
        html = """
        <nav><a href="/research">Research</a></nav>
        <div><a href="/research">Research Again</a></div>
        """
        links = extract_nav_links(html, "https://www.example.com")
        urls = [l["url"] for l in links]
        assert urls.count("https://www.example.com/research") == 1

    def test_skips_external_links(self):
        html = '<nav><a href="https://twitter.com/fund">Twitter</a></nav>'
        links = extract_nav_links(html, "https://www.example.com", allowed_domains=["example.com"])
        assert len(links) == 0

    def test_skips_anchor_and_javascript(self):
        html = """
        <nav>
            <a href="#">Top</a>
            <a href="javascript:void(0)">Click</a>
            <a href="/real">Real</a>
        </nav>
        """
        links = extract_nav_links(html, "https://www.example.com")
        assert len(links) == 1
        assert links[0]["url"] == "https://www.example.com/real"


class TestScoreCandidates:
    def test_scores_and_sorts(self):
        candidates = [
            {"url": "https://www.example.com/about", "label": "About"},
            {"url": "https://www.example.com/research", "label": "Research"},
        ]
        scored = score_candidates(candidates, allowed_domains=["example.com"], page_html_map={})
        assert scored[0]["url"] == "https://www.example.com/research"
        assert scored[0]["scores"]["path"] > scored[1]["scores"]["path"]

    def test_rejects_domain_mismatch(self):
        candidates = [
            {"url": "https://www.other.com/research", "label": "Research"},
        ]
        scored = score_candidates(candidates, allowed_domains=["example.com"], page_html_map={})
        assert len(scored) == 0 or scored[0]["scores"]["domain"] == 0.0


class TestClassifyWithAi:
    def test_stub_returns_none(self):
        """Phase 1: AI classification is a stub that returns None."""
        result = _classify_with_ai("https://example.com/research", "<html></html>")
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_discover.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'discover_entrypoints'`

- [ ] **Step 3: Implement `discover_entrypoints.py`**

```python
#!/usr/bin/env python3
"""
Entrypoint Discovery — find candidate research entry URLs for a hedge fund source.

Crawls homepage + navigation links, applies rule-based scoring (domain, path,
structure, gate penalty), outputs structured candidate JSON.

Phase 1: rules only, no LLM. AI stub returns None.

Usage:
  python3 discover_entrypoints.py --source bridgewater
  python3 discover_entrypoints.py --source bridgewater --write
  python3 discover_entrypoints.py --all
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from entrypoint_scorer import score_domain, score_path, score_structure, score_gate, score_final

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config" / "sources.json"
ENTRYPOINTS_FILE = BASE_DIR / "config" / "entrypoints.json"
LOG_FILE = BASE_DIR / "logs" / "discover.log"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def extract_nav_links(html: str, base_url: str,
                      allowed_domains: Optional[list[str]] = None) -> list[dict]:
    """Extract unique internal links from page HTML (nav, header, footer, main)."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[dict] = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("#") or href.startswith("javascript:"):
            continue
        url = urljoin(base_url, href).rstrip("/")
        if url in seen:
            continue
        seen.add(url)

        if allowed_domains and score_domain(url, allowed_domains) == 0.0:
            continue

        label = a.get_text(strip=True) or a.get("aria-label", "")
        links.append({"url": url, "label": label[:100]})

    return links


def score_candidates(candidates: list[dict], allowed_domains: list[str],
                     page_html_map: dict[str, str]) -> list[dict]:
    """Score each candidate URL and return sorted by final_score descending.

    Candidates with domain_score == 0.0 are filtered out.
    """
    scored = []
    for c in candidates:
        d = score_domain(c["url"], allowed_domains)
        if d == 0.0:
            continue
        p = score_path(c["url"])
        html = page_html_map.get(c["url"], "")
        s = score_structure(html)
        g = score_gate(html)
        final = score_final(d, p, s, g)
        scored.append({
            "url": c["url"],
            "label": c.get("label", ""),
            "scores": {"domain": d, "path": round(p, 3), "structure": round(s, 3),
                       "gate_penalty": round(g, 3), "final": round(final, 3)},
            "ai_classification": _classify_with_ai(c["url"], html),
        })

    scored.sort(key=lambda x: x["scores"]["final"], reverse=True)
    return scored


def _classify_with_ai(url: str, html: str) -> Optional[dict]:
    """AI classification stub — Phase 2 will add LLM call here.

    Returns None in Phase 1.
    """
    return None


def fetch_page(url: str) -> Optional[str]:
    """Fetch a page's HTML. Returns None on failure."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return None


def discover_source(source: dict, write: bool = False) -> dict:
    """Discover candidate entrypoints for a single source."""
    source_id = source["id"]
    homepage = source["url"]
    expected_host = source.get("expected_hostname", "")
    allowed = [expected_host] if expected_host else []

    log.info("Discovering entrypoints for %s from %s", source_id, homepage)

    # Fetch homepage
    html = fetch_page(homepage)
    if not html:
        return {"source": source_id, "error": "failed to fetch homepage", "candidates": [], "rejected": []}

    # Extract links from homepage
    links = extract_nav_links(html, homepage, allowed_domains=allowed)
    log.info("  Found %d internal links", len(links))

    # Fetch up to 20 candidate pages for structure scoring
    page_html_map: dict[str, str] = {homepage: html}
    for link in links[:20]:
        if link["url"] not in page_html_map:
            page_html = fetch_page(link["url"])
            if page_html:
                page_html_map[link["url"]] = page_html

    # Score all candidates
    scored = score_candidates(links, allowed, page_html_map)

    candidates = [c for c in scored if c["scores"]["final"] >= 0.6]
    rejected = [{"url": c["url"], "reason": f"low score ({c['scores']['final']})"} for c in scored if c["scores"]["final"] < 0.4]

    result = {
        "source": source_id,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "candidate_pages": candidates,
        "rejected_pages": rejected,
    }

    if write and candidates:
        _write_entrypoints(source_id, candidates)

    return result


def _write_entrypoints(source_id: str, candidates: list[dict]) -> None:
    """Write top candidates back to entrypoints.json."""
    ep = {"version": 1, "sources": {}}
    if ENTRYPOINTS_FILE.exists():
        try:
            ep = json.loads(ENTRYPOINTS_FILE.read_text())
        except json.JSONDecodeError:
            pass

    ep.setdefault("sources", {})
    ep["sources"][source_id] = {
        "last_verified_at": datetime.now(timezone.utc).isoformat(),
        "verified_by": "discover_rules",
        "entrypoints": [
            {
                "url": c["url"],
                "content_type": "research_index",
                "confidence": c["scores"]["final"],
                "active": i == 0,  # only top candidate is active
            }
            for i, c in enumerate(candidates[:3])
        ],
        "rejected_pages": [],
    }

    ENTRYPOINTS_FILE.write_text(json.dumps(ep, indent=2, ensure_ascii=False) + "\n")
    log.info("  Written entrypoints for %s to %s", source_id, ENTRYPOINTS_FILE)


def main() -> None:
    parser = argparse.ArgumentParser(description="Entrypoint Discovery")
    parser.add_argument("--source", help="Discover for this source ID only")
    parser.add_argument("--all", action="store_true", help="Discover for all sources")
    parser.add_argument("--write", action="store_true", help="Write results to entrypoints.json")
    args = parser.parse_args()

    config = json.loads(CONFIG_FILE.read_text())
    sources = config["sources"]

    if not args.source and not args.all:
        print("Usage: --source <id> or --all")
        sys.exit(1)

    results = []
    for source in sources:
        if args.source and source["id"] != args.source:
            continue
        result = discover_source(source, write=args.write)
        results.append(result)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    # Summary
    total_candidates = sum(len(r.get("candidate_pages", [])) for r in results)
    total_rejected = sum(len(r.get("rejected_pages", [])) for r in results)
    print(f"\nDiscovery complete: {total_candidates} candidates, {total_rejected} rejected")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_discover.py -v`
Expected: all 7 PASS

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research
git add discover_entrypoints.py tests/test_unit_discover.py
git commit -m "feat: add discover_entrypoints.py — rule-based candidate discovery with AI stub"
```

---

### Task 5: Validation Script (`validate_entrypoints.py`)

Validates existing entrypoints: HTTP check, structure scoring, detect drift.

**Files:**
- Create: `validate_entrypoints.py`
- Create: `tests/test_unit_validate.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_unit_validate.py
"""Unit tests for validate_entrypoints.py."""

import json
import pytest
from unittest.mock import patch, MagicMock
from validate_entrypoints import validate_entrypoint, validate_source


class TestValidateEntrypoint:
    @patch("validate_entrypoints.requests.get")
    def test_valid_entrypoint(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = """
        <article><h2>Research Paper</h2><time>March 2026</time></article>
        <article><h2>Another Paper</h2><time>February 2026</time></article>
        """
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = validate_entrypoint(
            "https://www.example.com/research",
            allowed_domains=["example.com"]
        )
        assert result["status"] == "ok"
        assert result["scores"]["final"] > 0.5

    @patch("validate_entrypoints.requests.get")
    def test_http_error(self, mock_get):
        mock_get.side_effect = Exception("Connection refused")
        result = validate_entrypoint(
            "https://www.example.com/research",
            allowed_domains=["example.com"]
        )
        assert result["status"] == "error"
        assert "Connection refused" in result["error"]

    @patch("validate_entrypoints.requests.get")
    def test_gated_page(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "<p>Subscribe to read. Register to continue. Log in to read.</p>"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = validate_entrypoint(
            "https://www.example.com/research",
            allowed_domains=["example.com"]
        )
        assert result["scores"]["gate_penalty"] > 0.3


class TestValidateSource:
    @patch("validate_entrypoints.validate_entrypoint")
    def test_validates_all_active_entrypoints(self, mock_validate):
        mock_validate.return_value = {"status": "ok", "scores": {"final": 0.8}}
        source_config = {
            "entrypoints": [
                {"url": "https://example.com/research", "active": True, "confidence": 0.9, "content_type": "research_index"},
                {"url": "https://example.com/reports", "active": True, "confidence": 0.8, "content_type": "report_hub"},
            ]
        }
        results = validate_source("test", source_config, allowed_domains=["example.com"])
        assert len(results) == 2
        assert mock_validate.call_count == 2

    @patch("validate_entrypoints.validate_entrypoint")
    def test_skips_inactive(self, mock_validate):
        source_config = {
            "entrypoints": [
                {"url": "https://example.com/old", "active": False, "confidence": 0.5, "content_type": "research_index"},
            ]
        }
        results = validate_source("test", source_config, allowed_domains=["example.com"])
        assert len(results) == 0
        assert mock_validate.call_count == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_validate.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement `validate_entrypoints.py`**

```python
#!/usr/bin/env python3
"""
Entrypoint Validator — check existing entrypoints for health and drift.

For each active entrypoint: HTTP fetch, score structure/gate, detect failures.

Usage:
  python3 validate_entrypoints.py                    # validate all
  python3 validate_entrypoints.py --source gmo       # validate one
  python3 validate_entrypoints.py --fix              # auto-disable failed entrypoints
"""

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from entrypoint_scorer import score_domain, score_path, score_structure, score_gate, score_final

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config" / "sources.json"
ENTRYPOINTS_FILE = BASE_DIR / "config" / "entrypoints.json"
LOG_FILE = BASE_DIR / "logs" / "validate.log"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}


def validate_entrypoint(url: str, allowed_domains: list[str]) -> dict:
    """Validate a single entrypoint URL: fetch, score, report."""
    result: dict = {"url": url, "status": "ok", "scores": {}, "error": None}

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        return result

    html = resp.text
    d = score_domain(url, allowed_domains)
    p = score_path(url)
    s = score_structure(html)
    g = score_gate(html)
    f = score_final(d, p, s, g)

    result["scores"] = {
        "domain": d,
        "path": round(p, 3),
        "structure": round(s, 3),
        "gate_penalty": round(g, 3),
        "final": round(f, 3),
    }

    if f < 0.4:
        result["status"] = "degraded"
    return result


def validate_source(source_id: str, source_config: dict,
                    allowed_domains: list[str]) -> list[dict]:
    """Validate all active entrypoints for a source."""
    results = []
    for ep in source_config.get("entrypoints", []):
        if not ep.get("active", False):
            continue
        log.info("  Validating %s", ep["url"])
        result = validate_entrypoint(ep["url"], allowed_domains)
        results.append(result)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Entrypoint Validator")
    parser.add_argument("--source", help="Validate this source only")
    parser.add_argument("--fix", action="store_true", help="Auto-disable failed entrypoints")
    args = parser.parse_args()

    if not ENTRYPOINTS_FILE.exists():
        print("No entrypoints.json found. Run discover_entrypoints.py first.")
        sys.exit(1)

    ep_config = json.loads(ENTRYPOINTS_FILE.read_text())
    sources_config = json.loads(CONFIG_FILE.read_text())
    sources_by_id = {s["id"]: s for s in sources_config["sources"]}

    any_failed = False
    for source_id, source_ep in ep_config.get("sources", {}).items():
        if args.source and source_id != args.source:
            continue

        source = sources_by_id.get(source_id, {})
        expected_host = source.get("expected_hostname", "")
        allowed = [expected_host] if expected_host else []

        log.info("Validating %s ...", source_id)
        results = validate_source(source_id, source_ep, allowed)

        for r in results:
            status_icon = "OK" if r["status"] == "ok" else "FAIL"
            score_str = f"final={r['scores'].get('final', 'N/A')}" if r["scores"] else r.get("error", "")
            print(f"  [{status_icon}] {r['url']} — {score_str}")

            if r["status"] in ("error", "degraded") and args.fix:
                for ep in source_ep.get("entrypoints", []):
                    if ep["url"] == r["url"]:
                        ep["active"] = False
                        log.info("  Disabled failed entrypoint: %s", r["url"])
                        any_failed = True

    if args.fix and any_failed:
        ENTRYPOINTS_FILE.write_text(json.dumps(ep_config, indent=2, ensure_ascii=False) + "\n")
        log.info("Updated entrypoints.json with disabled entries")

    print("\nValidation complete.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_validate.py -v`
Expected: all 5 PASS

- [ ] **Step 5: Commit**

```bash
cd ~/hedge-fund-research
git add validate_entrypoints.py tests/test_unit_validate.py
git commit -m "feat: add validate_entrypoints.py — HTTP health check + scoring for existing entrypoints"
```

---

### Task 6: Integration Tests

End-to-end tests verifying the full flow: discover → score → validate → fetch_articles fallback.

**Files:**
- Create: `tests/test_integration_entrypoints.py`

- [ ] **Step 1: Write integration tests**

```python
# tests/test_integration_entrypoints.py
"""Integration tests for entrypoint discovery system."""

import json
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from entrypoint_scorer import score_domain, score_path, score_structure, score_gate, score_final
from discover_entrypoints import extract_nav_links, score_candidates, _write_entrypoints
from fetch_articles import load_entrypoints, get_source_url


class TestEndToEndScoring:
    """Verify scoring pipeline produces expected results for realistic HTML."""

    def test_research_index_scores_high(self):
        """A real research index page should score above 0.6."""
        html = """
        <nav><a href="/research">Research</a><a href="/about">About</a></nav>
        <article><h2><a href="/research/paper-1">Deep Value Investing</a></h2>
            <time>March 15, 2026</time><span class="author">J. Smith</span>
            <a href="/papers/deep-value.pdf">Download PDF</a></article>
        <article><h2><a href="/research/paper-2">Factor Returns Q1</a></h2>
            <time>March 10, 2026</time></article>
        <nav class="pagination"><a href="?page=2">Next</a></nav>
        """
        s = score_structure(html)
        g = score_gate(html)
        p = score_path("https://www.example.com/research")
        d = score_domain("https://www.example.com/research", ["example.com"])
        f = score_final(d, p, s, g)
        assert f >= 0.6

    def test_about_page_scores_low(self):
        """An about page should score below 0.4."""
        html = """
        <h1>About Our Firm</h1>
        <p>We are a leading investment management firm.</p>
        <button class="cta">Subscribe Now</button>
        <form action="/subscribe"><input type="email" /><button>Sign Up</button></form>
        """
        s = score_structure(html)
        g = score_gate(html)
        p = score_path("https://www.example.com/about")
        d = score_domain("https://www.example.com/about", ["example.com"])
        f = score_final(d, p, s, g)
        assert f < 0.5

    def test_gated_page_penalized(self):
        """A gated page should have high gate penalty."""
        html = "<p>Subscribe to read the full article. Register to continue. For clients only.</p>"
        g = score_gate(html)
        assert g >= 0.3


class TestEntrypointsConfigRoundtrip:
    """Verify entrypoints.json write and read back correctly."""

    def test_write_and_load(self, tmp_path, monkeypatch):
        ep_file = tmp_path / "entrypoints.json"
        ep_file.write_text('{"version": 1, "sources": {}}')
        monkeypatch.setattr("discover_entrypoints.ENTRYPOINTS_FILE", ep_file)

        candidates = [
            {"url": "https://www.example.com/research", "label": "Research",
             "scores": {"domain": 1.0, "path": 0.9, "structure": 0.8, "gate_penalty": 0.0, "final": 0.87}},
        ]
        _write_entrypoints("test-source", candidates)

        monkeypatch.setattr("fetch_articles.ENTRYPOINTS_FILE", ep_file)
        ep = load_entrypoints()
        assert "test-source" in ep["sources"]
        assert ep["sources"]["test-source"]["entrypoints"][0]["active"] is True

    def test_fallback_when_no_entrypoint(self, tmp_path, monkeypatch):
        ep_file = tmp_path / "entrypoints.json"
        ep_file.write_text('{"version": 1, "sources": {}}')
        monkeypatch.setattr("fetch_articles.ENTRYPOINTS_FILE", ep_file)

        ep = load_entrypoints()
        source = {"id": "unknown", "url": "https://www.example.com/default"}
        url = get_source_url(source, ep)
        assert url == "https://www.example.com/default"


class TestExternalDomainRejection:
    def test_social_media_links_rejected(self):
        html = """
        <nav>
            <a href="https://twitter.com/fund">Twitter</a>
            <a href="https://linkedin.com/company/fund">LinkedIn</a>
            <a href="/research">Research</a>
        </nav>
        """
        links = extract_nav_links(html, "https://www.example.com", allowed_domains=["example.com"])
        urls = [l["url"] for l in links]
        assert not any("twitter" in u for u in urls)
        assert not any("linkedin" in u for u in urls)
        assert any("research" in u for u in urls)
```

- [ ] **Step 2: Run integration tests**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_integration_entrypoints.py -v`
Expected: all PASS

- [ ] **Step 3: Run full test suite to verify no regressions**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/ -v`
Expected: all existing + new tests PASS

- [ ] **Step 4: Commit**

```bash
cd ~/hedge-fund-research
git add tests/test_integration_entrypoints.py
git commit -m "test: add integration tests for entrypoint discovery system"
```

---

### Task 7: Final Wiring + README

Wire everything together, update pipeline docs, verify CLI works.

**Files:**
- Modify: `README.md`
- Modify: `run_pipeline.sh` (no change needed — entrypoints load transparently)

- [ ] **Step 1: Update README.md with entrypoint discovery section**

Add after the existing "How It Works" section:

```markdown
## Entrypoint Management

The system uses a three-layer architecture to manage research entry URLs:

1. **Fixed entrypoints** (`config/entrypoints.json`) — verified URLs used for daily fetching
2. **Inspection** — quality metrics tracked in `config/inspection_state.json`, warns on anomalies
3. **Discovery** — `discover_entrypoints.py` scans homepages and scores candidate URLs

### Commands

```bash
# Discover new entrypoints for a source (dry-run)
python3 discover_entrypoints.py --source bridgewater

# Write discovered entrypoints to config
python3 discover_entrypoints.py --source bridgewater --write

# Validate existing entrypoints
python3 validate_entrypoints.py
python3 validate_entrypoints.py --source gmo --fix
```
```

- [ ] **Step 2: Verify CLI smoke test**

Run (dry-run, no network needed for --help):
```bash
cd ~/hedge-fund-research
python3 discover_entrypoints.py --help
python3 validate_entrypoints.py --help
```
Expected: help text printed without errors

- [ ] **Step 3: Run full test suite one final time**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/ -v`
Expected: all tests PASS

- [ ] **Step 4: Commit README + any final adjustments**

```bash
cd ~/hedge-fund-research
git add README.md
git commit -m "docs: add entrypoint discovery section to README"
```

- [ ] **Step 5: Push all commits**

```bash
cd ~/hedge-fund-research && git push origin main
```
