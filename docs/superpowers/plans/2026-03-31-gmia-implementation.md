# GMIA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a 4-stage pipeline that fetches hedge fund research, extracts content, generates bilingual LLM summaries, and publishes a dashboard to docs.sinostor.com.cn.

**Architecture:** Independent Python scripts per stage, connected via JSONL data files. Stage 1 (existing) fetches metadata. Stage 2 downloads PDFs/HTML. Stage 3 runs multi-model LLM analysis. Stage 4 generates a static HTML dashboard. Cron runs daily at 03:45 BJT.

**Tech Stack:** Python 3.12, requests, BeautifulSoup4, Playwright, pdfplumber, Gemini/OpenAI/Anthropic APIs, pytest

---

## Task 1: Add hostname validation to Stage 1 (fetch_articles.py)

**Files:**
- Modify: `config/sources.json`
- Modify: `fetch_articles.py:451-494`
- Create: `tests/conftest.py`
- Create: `tests/test_unit_fetch_articles.py`

- [ ] **Step 1: Add `expected_hostname` to sources.json**

```json
{
  "id": "man-group",
  "expected_hostname": "man.com",
  ...
},
{
  "id": "bridgewater",
  "expected_hostname": "bridgewater.com",
  ...
},
{
  "id": "aqr",
  "expected_hostname": "aqr.com",
  ...
},
{
  "id": "gmo",
  "expected_hostname": "gmo.com",
  ...
},
{
  "id": "oaktree",
  "expected_hostname": "oaktreecapital.com",
  ...
}
```

- [ ] **Step 2: Write failing tests for hostname validation**

Create `tests/conftest.py`:
```python
import pytest
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
```

Create `tests/test_unit_fetch_articles.py`:
```python
from fetch_articles import article_id, parse_date, load_existing_ids
from urllib.parse import urlparse


def validate_hostname(url: str, expected_hostname: str) -> bool:
    """Mirrors the validation function we'll add to fetch_articles.py."""
    from fetch_articles import _validate_hostname
    return _validate_hostname(url, expected_hostname)


class TestArticleId:
    def test_deterministic(self):
        a = article_id("aqr", "https://aqr.com/article/1")
        b = article_id("aqr", "https://aqr.com/article/1")
        assert a == b

    def test_unique(self):
        a = article_id("aqr", "https://aqr.com/article/1")
        b = article_id("aqr", "https://aqr.com/article/2")
        assert a != b

    def test_source_matters(self):
        a = article_id("aqr", "https://example.com/article/1")
        b = article_id("gmo", "https://example.com/article/1")
        assert a != b


class TestParseDate:
    def test_month_dd_yyyy(self):
        assert parse_date("March 18, 2026") == "2026-03-18"

    def test_mon_dd_yyyy(self):
        assert parse_date("Mar 18, 2026") == "2026-03-18"

    def test_dd_month_yyyy(self):
        assert parse_date("18 March 2026") == "2026-03-18"

    def test_iso(self):
        assert parse_date("2026-03-18") == "2026-03-18"

    def test_month_yyyy(self):
        assert parse_date("March 2026") == "2026-03-01"

    def test_invalid_returns_none(self):
        assert parse_date("not a date") is None

    def test_empty_returns_none(self):
        assert parse_date("") is None


class TestHostnameValidation:
    def test_exact_match(self):
        from fetch_articles import _validate_hostname
        assert _validate_hostname("https://www.aqr.com/Insights/Research/foo", "aqr.com") is True

    def test_subdomain_match(self):
        from fetch_articles import _validate_hostname
        assert _validate_hostname("https://papers.aqr.com/doc.pdf", "aqr.com") is True

    def test_mismatch_rejected(self):
        from fetch_articles import _validate_hostname
        assert _validate_hostname("https://www.oaktreecapital.com/insights/memo", "aqr.com") is False

    def test_different_tld_rejected(self):
        from fetch_articles import _validate_hostname
        assert _validate_hostname("https://www.aqr.net/article", "aqr.com") is False


class TestLoadExistingIds:
    def test_empty_file(self, tmp_path):
        f = tmp_path / "articles.jsonl"
        f.write_text("")
        import fetch_articles
        original = fetch_articles.DATA_FILE
        fetch_articles.DATA_FILE = f
        try:
            ids = load_existing_ids()
            assert ids == set()
        finally:
            fetch_articles.DATA_FILE = original

    def test_parses_ids(self, tmp_path):
        f = tmp_path / "articles.jsonl"
        f.write_text('{"id":"abc123"}\n{"id":"def456"}\n')
        import fetch_articles
        original = fetch_articles.DATA_FILE
        fetch_articles.DATA_FILE = f
        try:
            ids = load_existing_ids()
            assert ids == {"abc123", "def456"}
        finally:
            fetch_articles.DATA_FILE = original
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fetch_articles.py -v`
Expected: TestHostnameValidation tests FAIL with `cannot import name '_validate_hostname'`

- [ ] **Step 4: Implement hostname validation**

Add to `fetch_articles.py` after the `parse_date` function:

```python
def _validate_hostname(url: str, expected_hostname: str) -> bool:
    """Check that a URL's hostname ends with the expected domain."""
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    return hostname == expected_hostname or hostname.endswith("." + expected_hostname)
```

Then modify `fetch_source()` to filter articles:

```python
def fetch_source(source: dict, existing_ids: set[str], dry_run: bool = False) -> list[dict]:
    source_id = source["id"]
    expected_host = source.get("expected_hostname", "")
    fetcher = FETCHERS.get(source_id)
    if not fetcher:
        log.warning("No fetcher for source: %s", source_id)
        return []

    log.info("Fetching %s (%s) ...", source["name"], source["method"])
    try:
        raw_articles = fetcher(source)
    except Exception as e:
        log.error("Failed to fetch %s: %s", source_id, e)
        return []

    new_articles = []
    now = datetime.now(BJT).isoformat()
    for art in raw_articles:
        # Source identity validation gate
        if expected_host and not _validate_hostname(art["url"], expected_host):
            log.warning("SOURCE_MISMATCH: %s article URL %s does not match expected host %s",
                       source_id, art["url"], expected_host)
            continue

        aid = article_id(source_id, art["url"])
        if aid in existing_ids:
            continue
        new_articles.append({
            "id": aid,
            "source_id": source_id,
            "source_name": source["short_name"],
            "title": art["title"],
            "url": art["url"],
            "date": art.get("date"),
            "date_raw": art.get("date_raw", ""),
            "fetched_at": now,
            "summarized": False,
        })

    log.info("  %s: %d articles found, %d new", source_id, len(raw_articles), len(new_articles))

    if dry_run:
        for a in new_articles:
            log.info("    [NEW] %s — %s", a["date"] or "no date", a["title"])
    return new_articles
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fetch_articles.py -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add config/sources.json fetch_articles.py tests/conftest.py tests/test_unit_fetch_articles.py
git commit -m "feat: add hostname validation gate to Stage 1 fetcher"
```

---

## Task 2: Build Stage 2 — fetch_content.py

**Files:**
- Create: `fetch_content.py`
- Create: `tests/test_unit_fetch_content.py`
- Create: `tests/fixtures/` (sample fixtures)

- [ ] **Step 1: Write failing tests for content fetching logic**

Create `tests/test_unit_fetch_content.py`:
```python
import json
import os
import pytest
from pathlib import Path


class TestContentValidation:
    def test_pdf_content_type_accepted(self):
        from fetch_content import _validate_pdf_response
        assert _validate_pdf_response(200, "application/pdf", 5000) is True

    def test_pdf_html_content_type_rejected(self):
        from fetch_content import _validate_pdf_response
        assert _validate_pdf_response(200, "text/html", 5000) is False

    def test_pdf_too_small_rejected(self):
        from fetch_content import _validate_pdf_response
        assert _validate_pdf_response(200, "application/pdf", 500) is False

    def test_pdf_non_200_rejected(self):
        from fetch_content import _validate_pdf_response
        assert _validate_pdf_response(403, "application/pdf", 5000) is False

    def test_json_api_html_error_detected(self):
        from fetch_content import _validate_json_response
        html_error = "<html><body>Something Went Wrong</body></html>"
        assert _validate_json_response(html_error) is False

    def test_json_api_valid_response(self):
        from fetch_content import _validate_json_response
        valid = '{"listing": [{"Title": "Test"}]}'
        assert _validate_json_response(valid) is True


class TestContentNormalization:
    def test_strips_nav_and_footer(self):
        from fetch_content import _normalize_html
        html = """
        <nav>Navigation</nav>
        <article><p>This is the article body with important content.</p></article>
        <footer>Legal text</footer>
        """
        text = _normalize_html(html, "article p")
        assert "article body" in text
        assert "Navigation" not in text
        assert "Legal" not in text

    def test_preserves_body_text(self):
        from fetch_content import _normalize_html
        html = "<div class='content'><p>Research findings about macro trends.</p></div>"
        text = _normalize_html(html, "div.content p")
        assert "Research findings about macro trends" in text

    def test_min_length_gate(self):
        from fetch_content import _check_min_content_length
        assert _check_min_content_length("Short") is False
        assert _check_min_content_length("A" * 101) is True


class TestAtomicWrite:
    def test_atomic_write_creates_final_file(self, tmp_path):
        from fetch_content import _atomic_write
        target = tmp_path / "test.txt"
        _atomic_write(target, b"hello world")
        assert target.exists()
        assert target.read_bytes() == b"hello world"

    def test_atomic_write_no_tmp_left(self, tmp_path):
        from fetch_content import _atomic_write
        target = tmp_path / "test.txt"
        _atomic_write(target, b"hello world")
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fetch_content.py -v`
Expected: FAIL with `No module named 'fetch_content'`

- [ ] **Step 3: Implement fetch_content.py**

Create `fetch_content.py`:
```python
#!/usr/bin/env python3
"""
Stage 2: Fetch full article content (PDF or HTML body) for unsummarized articles.

For each article without content:
  - GMO: download PDF via direct link from article page
  - Oaktree: download PDF via openPDF() JS link
  - AQR: scrape HTML article body
  - Man Group: scrape HTML article body
  - Bridgewater: skip (index only)

Validation gates:
  - HTTP 2xx required
  - PDF: content-type must be application/pdf, size > 1KB
  - JSON API: must be valid JSON (not HTML error page)
  - Normalized text must be > 100 chars
  - All writes via atomic temp-file + os.replace()
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config" / "sources.json"
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
CONTENT_DIR = BASE_DIR / "content"
LOG_FILE = BASE_DIR / "logs" / "fetch-content.log"

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

MIN_CONTENT_LENGTH = 100


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_pdf_response(status_code: int, content_type: str, content_length: int) -> bool:
    """Validate that a response looks like a real PDF."""
    if status_code < 200 or status_code >= 300:
        return False
    if "application/pdf" not in (content_type or "").lower():
        return False
    if content_length < 1024:
        return False
    return True


def _validate_json_response(text: str) -> bool:
    """Validate that a response is real JSON, not an HTML error page."""
    stripped = text.strip()
    if stripped.startswith("<"):
        return False
    try:
        json.loads(stripped)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _normalize_html(html: str, selector: str) -> str:
    """Extract text from HTML using a CSS selector, strip all tags."""
    soup = BeautifulSoup(html, "html.parser")
    # Remove common non-content elements
    for tag in soup.select("nav, footer, header, script, style, .cookie, .modal, .pagination"):
        tag.decompose()
    elements = soup.select(selector)
    if not elements:
        # Fallback: try article or main
        elements = soup.select("article, main, .content, .article-body")
    texts = [el.get_text(" ", strip=True) for el in elements]
    return "\n\n".join(texts)


def _check_min_content_length(text: str) -> bool:
    """Check that extracted content is substantial enough."""
    return len(text.strip()) > MIN_CONTENT_LENGTH


def _atomic_write(path: Path, data: bytes) -> None:
    """Write data to a temporary file then atomically rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Per-source content fetchers
# ---------------------------------------------------------------------------

def _fetch_content_gmo(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch PDF from GMO article page."""
    resp = requests.get(article["url"], headers=HEADERS,
                        cookies={"GMO_region": "NorthAmerica"}, timeout=30)
    if resp.status_code != 200:
        log.warning("GMO page fetch failed: %d for %s", resp.status_code, article["url"])
        return None

    # Extract PDF link from page
    pdf_match = re.search(r'href="([^"]+\.pdf[^"]*)"', resp.text)
    if not pdf_match:
        log.warning("GMO: no PDF link found on %s", article["url"])
        return None

    pdf_url = urljoin("https://www.gmo.com", pdf_match.group(1))
    pdf_resp = requests.get(pdf_url, headers=HEADERS, timeout=60)

    if not _validate_pdf_response(pdf_resp.status_code,
                                   pdf_resp.headers.get("content-type", ""),
                                   len(pdf_resp.content)):
        log.warning("GMO PDF validation failed: status=%d type=%s size=%d",
                    pdf_resp.status_code, pdf_resp.headers.get("content-type"), len(pdf_resp.content))
        return None

    path = CONTENT_DIR / f"{article['id']}.pdf"
    _atomic_write(path, pdf_resp.content)

    # Extract text from PDF
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
        text = "\n\n".join(text_parts)
    except Exception as e:
        log.warning("GMO PDF text extraction failed: %s", e)
        return None

    if not _check_min_content_length(text):
        log.warning("GMO: extracted text too short (%d chars) for %s", len(text), article["url"])
        return None

    txt_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(txt_path, text.encode("utf-8"))
    return txt_path, "ok"


def _fetch_content_oaktree(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch PDF from Oaktree article page."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(article["url"], wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)
        html = page.content()
        browser.close()

    # Extract PDF URL from openPDF() calls
    pdf_match = re.search(r"openPDF\(['\"]([^'\"]+)['\"]", html)
    if not pdf_match:
        # Try direct PDF link
        pdf_match = re.search(r'href="([^"]+\.pdf[^"]*)"', html)
    if not pdf_match:
        log.warning("Oaktree: no PDF link found on %s", article["url"])
        return None

    pdf_url = urljoin("https://www.oaktreecapital.com", pdf_match.group(1))
    pdf_resp = requests.get(pdf_url, headers=HEADERS, timeout=60)

    if not _validate_pdf_response(pdf_resp.status_code,
                                   pdf_resp.headers.get("content-type", ""),
                                   len(pdf_resp.content)):
        log.warning("Oaktree PDF validation failed for %s", article["url"])
        return None

    path = CONTENT_DIR / f"{article['id']}.pdf"
    _atomic_write(path, pdf_resp.content)

    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text() or ""
                text_parts.append(page_text)
        text = "\n\n".join(text_parts)
    except Exception as e:
        log.warning("Oaktree PDF text extraction failed: %s", e)
        return None

    if not _check_min_content_length(text):
        log.warning("Oaktree: extracted text too short for %s", article["url"])
        return None

    txt_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(txt_path, text.encode("utf-8"))
    return txt_path, "ok"


def _fetch_content_aqr(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch article body from AQR article page (Playwright)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"])
        page.goto(article["url"], wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)
        html = page.content()
        browser.close()

    text = _normalize_html(html, ".article-content p, .article__body p, .research-detail p")

    if not _check_min_content_length(text):
        log.warning("AQR: extracted text too short (%d chars) for %s", len(text), article["url"])
        return None

    path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(path, text.encode("utf-8"))
    return path, "ok"


def _fetch_content_man(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch article body from Man Group article page (SSR)."""
    resp = requests.get(article["url"], headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        log.warning("Man Group page fetch failed: %d for %s", resp.status_code, article["url"])
        return None

    text = _normalize_html(resp.text, ".field--body p, .article-body p, .node__content p")

    if not _check_min_content_length(text):
        log.warning("Man: extracted text too short (%d chars) for %s", len(text), article["url"])
        return None

    path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(path, text.encode("utf-8"))
    return path, "ok"


CONTENT_FETCHERS = {
    "gmo": _fetch_content_gmo,
    "oaktree": _fetch_content_oaktree,
    "aqr": _fetch_content_aqr,
    "man-group": _fetch_content_man,
    # bridgewater: skip (index only)
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_articles() -> list[dict]:
    """Load all articles from JSONL."""
    articles = []
    if DATA_FILE.exists():
        for line in DATA_FILE.read_text().strip().split("\n"):
            if line.strip():
                try:
                    articles.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return articles


def save_articles(articles: list[dict]) -> None:
    """Rewrite entire JSONL file (needed for updating fields)."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for a in articles:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    os.replace(tmp, DATA_FILE)


def main() -> None:
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    articles = load_articles()

    to_fetch = [a for a in articles
                if not a.get("summarized") and
                a.get("content_status") != "ok" and
                a.get("source_id") in CONTENT_FETCHERS]

    log.info("Stage 2: %d articles need content fetching", len(to_fetch))

    fetched = 0
    failed = 0
    for art in to_fetch:
        fetcher = CONTENT_FETCHERS.get(art["source_id"])
        if not fetcher:
            continue

        log.info("  Fetching content: [%s] %s", art["source_id"], art["title"][:60])
        try:
            result = fetcher(art)
            if result:
                path, status = result
                art["content_path"] = str(path)
                art["content_status"] = status
                fetched += 1
            else:
                art["content_status"] = "failed"
                failed += 1
        except Exception as e:
            log.error("  Content fetch error for %s: %s", art["id"], e)
            art["content_status"] = "failed"
            failed += 1

    save_articles(articles)
    log.info("Stage 2 complete: %d fetched, %d failed, %d skipped",
             fetched, failed, len(articles) - len(to_fetch))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_fetch_content.py -v`
Expected: All PASS

- [ ] **Step 5: Install pdfplumber dependency**

```bash
pip3 install pdfplumber --break-system-packages
```

Add to `requirements.txt`: `pdfplumber>=0.10.0`

- [ ] **Step 6: Test Stage 2 with real data (manual smoke test)**

```bash
cd ~/hedge-fund-research && python3 fetch_content.py
```

Expected: Fetches content for ~36 articles (46 total minus Bridgewater), some may fail.

- [ ] **Step 7: Commit**

```bash
git add fetch_content.py tests/test_unit_fetch_content.py requirements.txt
git commit -m "feat: Stage 2 — content fetcher with validation gates and atomic writes"
```

---

## Task 3: Build Stage 3 — analyze_articles.py

**Files:**
- Create: `analyze_articles.py`
- Create: `tests/test_unit_analyze.py`

- [ ] **Step 1: Write failing tests for LLM analysis logic**

Create `tests/test_unit_analyze.py`:
```python
import json
import pytest


class TestArticleFiltering:
    def test_skip_failed_content(self):
        from analyze_articles import _should_analyze
        art = {"summarized": False, "content_status": "failed", "source_id": "gmo"}
        assert _should_analyze(art) is False

    def test_skip_already_summarized(self):
        from analyze_articles import _should_analyze
        art = {"summarized": True, "content_status": "ok", "source_id": "gmo"}
        assert _should_analyze(art) is False

    def test_skip_bridgewater(self):
        from analyze_articles import _should_analyze
        art = {"summarized": False, "source_id": "bridgewater"}
        assert _should_analyze(art) is False

    def test_eligible_article(self):
        from analyze_articles import _should_analyze
        art = {"summarized": False, "content_status": "ok", "source_id": "gmo"}
        assert _should_analyze(art) is True


class TestOutputParsing:
    def test_parse_valid_json_output(self):
        from analyze_articles import _parse_llm_output
        raw = json.dumps({
            "summary_en": "English summary here.",
            "summary_zh": "中文摘要。",
            "themes": ["AI/Tech", "Macro/Rates"],
            "key_takeaway_en": "Key point.",
            "key_takeaway_zh": "关键要点。"
        })
        result = _parse_llm_output(raw)
        assert result is not None
        assert result["summary_en"] == "English summary here."
        assert result["summary_zh"] == "中文摘要。"
        assert "AI/Tech" in result["themes"]

    def test_reject_invalid_theme(self):
        from analyze_articles import _parse_llm_output
        raw = json.dumps({
            "summary_en": "Test",
            "summary_zh": "测试",
            "themes": ["InvalidTheme"],
            "key_takeaway_en": "Test",
            "key_takeaway_zh": "测试"
        })
        result = _parse_llm_output(raw)
        assert result is not None
        assert result["themes"] == []

    def test_parse_json_from_markdown_block(self):
        from analyze_articles import _parse_llm_output
        raw = '```json\n{"summary_en":"Test","summary_zh":"测试","themes":["AI/Tech"],"key_takeaway_en":"K","key_takeaway_zh":"K"}\n```'
        result = _parse_llm_output(raw)
        assert result is not None
        assert result["summary_en"] == "Test"


class TestModelFallback:
    def test_fallback_order(self, monkeypatch):
        from analyze_articles import _analyze_with_fallback
        calls = []

        def mock_gemini(prompt, api_key):
            calls.append("gemini")
            raise Exception("Gemini down")

        def mock_openai(prompt, api_key, model=None):
            calls.append("openai")
            return '{"summary_en":"EN","summary_zh":"ZH","themes":["AI/Tech"],"key_takeaway_en":"K","key_takeaway_zh":"K"}', {"input": 100, "output": 50}, "gpt-4.1-mini"

        import analyze_articles
        monkeypatch.setattr(analyze_articles, "_call_gemini", mock_gemini)
        monkeypatch.setattr(analyze_articles, "_call_openai", mock_openai)

        result = _analyze_with_fallback("test content", {"GEMINI_API_KEY": "k", "OPENAI_API_KEY": "k"})
        assert result is not None
        assert calls == ["gemini", "gemini", "openai"]

    def test_all_models_fail(self, monkeypatch):
        from analyze_articles import _analyze_with_fallback
        import analyze_articles

        def mock_fail(prompt, *args, **kwargs):
            raise Exception("Down")

        monkeypatch.setattr(analyze_articles, "_call_gemini", mock_fail)
        monkeypatch.setattr(analyze_articles, "_call_openai", mock_fail)
        monkeypatch.setattr(analyze_articles, "_call_anthropic", mock_fail)

        result = _analyze_with_fallback("test", {"GEMINI_API_KEY": "k", "OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"})
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_analyze.py -v`
Expected: FAIL with `No module named 'analyze_articles'`

- [ ] **Step 3: Implement analyze_articles.py**

Create `analyze_articles.py`:
```python
#!/usr/bin/env python3
"""
Stage 3: LLM deep analysis of article content.

Generates bilingual summaries (EN/ZH) and theme tags.
Model fallback: Gemini 2.5 Pro → GPT-4.1 Mini → Claude Sonnet.
Only processes articles with content_status="ok" and summarized=False.
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
LOG_FILE = BASE_DIR / "logs" / "analyze.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

VALID_THEMES = {
    "AI/Tech", "Macro/Rates", "Oil/Energy", "Credit/Fixed Income",
    "Equities/Value", "China/EM", "Risk/Volatility", "Geopolitics",
    "ESG/Climate", "Quant/Factor", "Asset Allocation", "Crypto/Digital",
    "Real Estate", "Private Markets", "Behavioral/Sentiment",
}

MODEL_CHAIN = ["gemini-2.5-pro", "gpt-4.1-mini", "claude-sonnet-4-6"]
MAX_ATTEMPTS = 2

ANALYSIS_PROMPT = """You are a senior investment analyst. Analyze the following hedge fund research article and produce a structured JSON response.

Article title: {title}
Source: {source}
Date: {date}

Article content:
{content}

Respond with ONLY a JSON object (no markdown fences, no explanation):
{{
  "summary_en": "2-3 sentence English summary of key thesis and implications",
  "summary_zh": "2-3 sentence Chinese summary (same content, natural Chinese)",
  "themes": ["1-3 theme tags from this list: AI/Tech, Macro/Rates, Oil/Energy, Credit/Fixed Income, Equities/Value, China/EM, Risk/Volatility, Geopolitics, ESG/Climate, Quant/Factor, Asset Allocation, Crypto/Digital, Real Estate, Private Markets, Behavioral/Sentiment"],
  "key_takeaway_en": "One-line English takeaway",
  "key_takeaway_zh": "One-line Chinese takeaway"
}}"""


def _load_api_keys() -> dict[str, str]:
    """Load API keys from environment files."""
    keys: dict[str, str] = {}
    for env_file in [Path.home() / ".stock-monitor.env", Path.home() / ".secrets.env"]:
        if env_file.exists():
            for line in env_file.read_text().split("\n"):
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, val = line.partition("=")
                    key = key.replace("export ", "").strip()
                    keys[key] = val.strip().strip("'\"")
    return keys


def _call_gemini(prompt: str, api_key: str):
    """Call Gemini 2.5 Pro. Returns (text, usage_dict, model_name) or raises."""
    resp = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent",
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4000}},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    um = data.get("usageMetadata", {})
    return text, {"input": um.get("promptTokenCount", 0),
                  "output": um.get("candidatesTokenCount", 0)}, "gemini-2.5-pro"


def _call_openai(prompt: str, api_key: str, model: str = "gpt-4.1-mini"):
    """Call OpenAI API. Returns (text, usage_dict, model_name) or raises."""
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": [{"role": "user", "content": prompt}],
              "temperature": 0.4, "max_tokens": 4000},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return text, {"input": usage.get("prompt_tokens", 0),
                  "output": usage.get("completion_tokens", 0)}, model


def _call_anthropic(prompt: str, api_key: str, model: str = "claude-sonnet-4-6"):
    """Call Anthropic API. Returns (text, usage_dict, model_name) or raises."""
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"Content-Type": "application/json", "x-api-key": api_key,
                 "anthropic-version": "2023-06-01"},
        json={"model": model, "max_tokens": 4000, "temperature": 0.4,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    text = data["content"][0]["text"]
    usage = data.get("usage", {})
    return text, {"input": usage.get("input_tokens", 0),
                  "output": usage.get("output_tokens", 0)}, model


def _should_analyze(article: dict) -> bool:
    """Check if an article should be analyzed."""
    if article.get("summarized"):
        return False
    if article.get("source_id") == "bridgewater":
        return False
    if article.get("content_status") != "ok":
        return False
    return True


def _parse_llm_output(raw: str) -> Optional[dict]:
    """Parse LLM JSON output, handling markdown fences."""
    text = raw.strip()
    # Strip markdown code fences
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    required = ["summary_en", "summary_zh", "themes", "key_takeaway_en", "key_takeaway_zh"]
    if not all(k in data for k in required):
        return None

    # Filter themes to valid set
    data["themes"] = [t for t in data["themes"] if t in VALID_THEMES]
    return data


def _analyze_with_fallback(content: str, api_keys: dict,
                           title: str = "", source: str = "", date: str = "") -> Optional[dict]:
    """Try each model in fallback chain. Returns parsed result or None."""
    prompt = ANALYSIS_PROMPT.format(
        title=title, source=source, date=date or "N/A",
        content=content[:15000]  # Limit to ~15k chars to control tokens
    )

    model_funcs = {
        "gemini-2.5-pro": lambda p: _call_gemini(p, api_keys.get("GEMINI_API_KEY", "")),
        "gpt-4.1-mini": lambda p: _call_openai(p, api_keys.get("OPENAI_API_KEY", "")),
        "claude-sonnet-4-6": lambda p: _call_anthropic(p, api_keys.get("ANTHROPIC_API_KEY", "")),
    }

    for model_name in MODEL_CHAIN:
        func = model_funcs.get(model_name)
        if not func:
            continue
        key_name = {"gemini-2.5-pro": "GEMINI_API_KEY",
                    "gpt-4.1-mini": "OPENAI_API_KEY",
                    "claude-sonnet-4-6": "ANTHROPIC_API_KEY"}[model_name]
        if not api_keys.get(key_name):
            log.info("    Skipping %s (no API key)", model_name)
            continue

        for attempt in range(MAX_ATTEMPTS):
            try:
                raw_text, usage, used_model = func(prompt)
                result = _parse_llm_output(raw_text)
                if result:
                    result["_model"] = used_model
                    result["_usage"] = usage
                    log.info("    Success with %s (attempt %d, in=%d out=%d)",
                             used_model, attempt + 1, usage.get("input", 0), usage.get("output", 0))
                    return result
                log.warning("    %s returned unparseable output (attempt %d)", model_name, attempt + 1)
            except Exception as e:
                log.warning("    %s failed (attempt %d): %s", model_name, attempt + 1, e)

    return None


def load_articles() -> list[dict]:
    articles = []
    if DATA_FILE.exists():
        for line in DATA_FILE.read_text().strip().split("\n"):
            if line.strip():
                try:
                    articles.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return articles


def save_articles(articles: list[dict]) -> None:
    tmp = DATA_FILE.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for a in articles:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    os.replace(tmp, DATA_FILE)


def main() -> None:
    api_keys = _load_api_keys()
    articles = load_articles()

    to_analyze = [a for a in articles if _should_analyze(a)]
    log.info("Stage 3: %d articles to analyze", len(to_analyze))

    analyzed = 0
    failed = 0
    for art in to_analyze:
        content_path = art.get("content_path", "")
        if not content_path or not Path(content_path).exists():
            log.warning("  Missing content file for %s", art["id"])
            continue

        content = Path(content_path).read_text(encoding="utf-8", errors="replace")
        log.info("  Analyzing: [%s] %s (%d chars)", art["source_id"], art["title"][:50], len(content))

        result = _analyze_with_fallback(
            content, api_keys,
            title=art.get("title", ""),
            source=art.get("source_name", ""),
            date=art.get("date", ""),
        )

        if result:
            art["summary_en"] = result["summary_en"]
            art["summary_zh"] = result["summary_zh"]
            art["themes"] = result["themes"]
            art["key_takeaway_en"] = result["key_takeaway_en"]
            art["key_takeaway_zh"] = result["key_takeaway_zh"]
            art["summarized"] = True
            art["analysis_model"] = result.get("_model", "")
            analyzed += 1
        else:
            log.error("  All models failed for %s", art["id"])
            failed += 1

    save_articles(articles)
    log.info("Stage 3 complete: %d analyzed, %d failed, %d skipped",
             analyzed, failed, len(articles) - len(to_analyze))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_analyze.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add analyze_articles.py tests/test_unit_analyze.py
git commit -m "feat: Stage 3 — LLM analysis with multi-model fallback and bilingual output"
```

---

## Task 4: Build Stage 4 — publish.py

**Files:**
- Create: `publish.py`
- Create: `tests/test_unit_publish.py`

- [ ] **Step 1: Write failing tests for HTML generation**

Create `tests/test_unit_publish.py`:
```python
import json
import pytest
from pathlib import Path


SAMPLE_ARTICLES = [
    {
        "id": "abc1", "source_id": "man-group", "source_name": "Man",
        "title": "Fool Me Twice", "url": "https://man.com/insights/1",
        "date": "2026-03-31", "summarized": True, "content_status": "ok",
        "summary_en": "English summary.", "summary_zh": "中文摘要。",
        "themes": ["Macro/Rates", "Oil/Energy"],
        "key_takeaway_en": "Key point.", "key_takeaway_zh": "关键要点。",
    },
    {
        "id": "abc2", "source_id": "bridgewater", "source_name": "Bridgewater",
        "title": "Modern Mercantilism", "url": "https://bridgewater.com/r/1",
        "date": "2026-01-14", "summarized": False,
    },
    {
        "id": "abc3", "source_id": "gmo", "source_name": "GMO",
        "title": "Gains Without Pains", "url": "https://gmo.com/r/1",
        "date": "2026-03-30", "summarized": True, "content_status": "ok",
        "summary_en": "GMO summary.", "summary_zh": "GMO摘要。",
        "themes": ["Asset Allocation"],
        "key_takeaway_en": "GMO key.", "key_takeaway_zh": "GMO要点。",
    },
]


class TestHtmlGeneration:
    def test_valid_html_structure(self):
        from publish import generate_html
        html = generate_html(SAMPLE_ARTICLES)
        assert "<html" in html
        assert "<body" in html
        assert "</html>" in html

    def test_bilingual_content_present(self):
        from publish import generate_html
        html = generate_html(SAMPLE_ARTICLES)
        assert "English summary." in html
        assert "中文摘要" in html

    def test_timeline_sorted_by_date(self):
        from publish import generate_html
        html = generate_html(SAMPLE_ARTICLES)
        # 2026-03-31 should appear before 2026-03-30
        pos_mar31 = html.find("Fool Me Twice")
        pos_mar30 = html.find("Gains Without Pains")
        assert pos_mar31 < pos_mar30

    def test_badge_colors(self):
        from publish import BADGE_COLORS
        assert "man-group" in BADGE_COLORS
        assert "bridgewater" in BADGE_COLORS
        assert "aqr" in BADGE_COLORS
        assert "gmo" in BADGE_COLORS
        assert "oaktree" in BADGE_COLORS

    def test_bridgewater_index_only(self):
        from publish import generate_html
        html = generate_html(SAMPLE_ARTICLES)
        # Find Bridgewater entry and check for "Index only" marker
        assert "Index only" in html or "index-only" in html

    def test_theme_grouping(self):
        from publish import generate_html
        html = generate_html(SAMPLE_ARTICLES)
        assert "Macro/Rates" in html
        assert "Asset Allocation" in html

    def test_empty_articles_graceful(self):
        from publish import generate_html
        html = generate_html([])
        assert "<html" in html
        assert "0" in html  # should show 0 articles
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_publish.py -v`
Expected: FAIL with `No module named 'publish'`

- [ ] **Step 3: Implement publish.py**

Create `publish.py` — this is a large file. Key structure:

```python
#!/usr/bin/env python3
"""
Stage 4: Generate static HTML dashboard for docs.sinostor.com.cn.

Reads articles.jsonl and produces /var/www/overview/hedge-fund-research.html
with three sections: Timeline, By Fund, Theme Tracker.
Dark GitHub-style theme, CN/EN bilingual toggle.
"""

import html as html_mod
import json
import logging
import os
import subprocess
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
CONFIG_FILE = BASE_DIR / "config" / "sources.json"
OUTPUT_FILE = Path("/var/www/overview/hedge-fund-research.html")
LOG_FILE = BASE_DIR / "logs" / "publish.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

BADGE_COLORS = {
    "man-group": "#58a6ff",     # blue
    "bridgewater": "#d29922",   # orange
    "aqr": "#3fb950",           # green
    "gmo": "#bc8cff",           # purple
    "oaktree": "#f85149",       # red
}


def _esc(s: str) -> str:
    return html_mod.escape(str(s), quote=True)


def load_articles() -> list[dict]:
    articles = []
    if DATA_FILE.exists():
        for line in DATA_FILE.read_text().strip().split("\n"):
            if line.strip():
                try:
                    articles.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return articles


def load_sources() -> dict:
    config = json.loads(CONFIG_FILE.read_text())
    return {s["id"]: s for s in config["sources"]}


def generate_html(articles: list[dict]) -> str:
    """Generate the full HTML dashboard page."""
    sources = load_sources() if CONFIG_FILE.exists() else {}
    now = datetime.now(BJT).strftime("%Y-%m-%d %H:%M BJT")

    # Sort by date descending (None dates at end)
    sorted_arts = sorted(articles, key=lambda a: a.get("date") or "0000-00-00", reverse=True)
    summarized = [a for a in articles if a.get("summarized")]
    this_week = [a for a in articles if a.get("date") and a["date"] >= (datetime.now(BJT) - timedelta(days=7)).strftime("%Y-%m-%d")]

    # Build theme index
    themes: dict[str, list[dict]] = defaultdict(list)
    for a in summarized:
        for t in a.get("themes", []):
            themes[t].append(a)

    # --- Timeline Section ---
    timeline_rows = ""
    for i, a in enumerate(sorted_arts):
        color = BADGE_COLORS.get(a.get("source_id", ""), "#8b949e")
        source_name = _esc(a.get("source_name", ""))
        title = _esc(a.get("title", ""))
        date = _esc(a.get("date", "—"))
        url = _esc(a.get("url", "#"))
        is_index_only = not a.get("summarized")
        hidden = ' style="display:none"' if i >= 20 else ""

        summary_en = _esc(a.get("summary_en", ""))
        summary_zh = _esc(a.get("summary_zh", ""))
        takeaway_en = _esc(a.get("key_takeaway_en", ""))
        takeaway_zh = _esc(a.get("key_takeaway_zh", ""))

        index_tag = '<span style="background:#30363d;color:#8b949e;padding:2px 8px;border-radius:4px;font-size:11px;margin-left:8px;">Index only</span>' if is_index_only else ""

        summary_block = ""
        if not is_index_only:
            summary_block = f'''
            <div class="summary-block" style="margin-top:8px;padding:8px 12px;background:var(--surface2);border-radius:6px;font-size:13px;">
              <div class="lang-en"><strong>Takeaway:</strong> {takeaway_en}<br><span style="color:var(--text-muted);">{summary_en}</span></div>
              <div class="lang-zh" style="display:none;"><strong>要点:</strong> {takeaway_zh}<br><span style="color:var(--text-muted);">{summary_zh}</span></div>
            </div>'''

        timeline_rows += f'''
        <div class="timeline-item" data-index="{i}"{hidden}>
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
            <span class="badge" style="background:{color};color:#fff;padding:2px 10px;border-radius:4px;font-size:11px;font-weight:600;">{source_name}</span>
            <span style="color:var(--text-muted);font-size:13px;min-width:80px;">{date}</span>
            <a href="{url}" target="_blank" style="font-weight:600;font-size:14px;">{title}</a>
            {index_tag}
          </div>
          {summary_block}
        </div>'''

    # --- By Fund Section ---
    fund_cards = ""
    for src_id, src in sources.items():
        color = BADGE_COLORS.get(src_id, "#8b949e")
        fund_articles = [a for a in sorted_arts if a.get("source_id") == src_id][:5]
        items = ""
        for a in fund_articles:
            title = _esc(a.get("title", ""))
            date = _esc(a.get("date", "—"))
            url = _esc(a.get("url", "#"))
            preview = _esc((a.get("summary_zh") or a.get("summary_en") or "")[:80])
            items += f'''<div style="padding:8px 0;border-bottom:1px solid var(--border);">
              <a href="{url}" target="_blank" style="font-size:13px;">{title}</a>
              <div style="font-size:12px;color:var(--text-muted);">{date}{(" — " + preview + "...") if preview else ""}</div>
            </div>'''

        authors = ", ".join(src.get("notable_authors", [])[:3])
        fund_cards += f'''
        <div class="fund-card" style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:20px;border-top:3px solid {color};">
          <h3 style="margin:0 0 4px;font-size:16px;">{_esc(src.get("name", ""))}</h3>
          <p style="margin:0 0 8px;font-size:12px;color:var(--text-muted);">{_esc(src.get("description", ""))}</p>
          <p style="margin:0 0 12px;font-size:11px;color:var(--accent);">{_esc(authors)}</p>
          {items or '<p style="color:var(--text-muted);font-size:13px;">No articles yet.</p>'}
        </div>'''

    # --- Theme Tracker Section ---
    theme_sections = ""
    for theme_name in sorted(themes.keys(), key=lambda t: -len(themes[t])):
        theme_arts = themes[theme_name]
        items = ""
        for a in theme_arts[:8]:
            color = BADGE_COLORS.get(a.get("source_id", ""), "#8b949e")
            items += f'''<div style="display:flex;gap:8px;align-items:center;padding:4px 0;">
              <span style="background:{color};color:#fff;padding:1px 6px;border-radius:3px;font-size:10px;">{_esc(a.get("source_name",""))}</span>
              <a href="{_esc(a.get('url','#'))}" target="_blank" style="font-size:13px;">{_esc(a.get("title","")[:60])}</a>
              <span style="color:var(--text-muted);font-size:12px;">{_esc(a.get("date",""))}</span>
            </div>'''
        theme_sections += f'''
        <div style="margin-bottom:20px;">
          <h4 style="margin:0 0 8px;font-size:14px;">{_esc(theme_name)} <span style="color:var(--text-muted);font-weight:normal;">({len(theme_arts)} articles)</span></h4>
          {items}
        </div>'''

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hedge Fund Research Insights</title>
<style>
:root {{
  --bg: #0d1117; --surface: #161b22; --surface2: #1c2128;
  --border: #30363d; --text: #e6edf3; --text-muted: #8b949e;
  --accent: #58a6ff; --accent2: #3fb950;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif; background:var(--bg); color:var(--text); line-height:1.6; font-size:15px; }}
a {{ color:var(--accent); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.container {{ max-width:1000px; margin:0 auto; padding:24px; }}
.header {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:24px; margin-bottom:24px; }}
.section {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:24px; margin-bottom:24px; }}
.section h2 {{ font-size:18px; margin-bottom:16px; padding-bottom:8px; border-bottom:1px solid var(--border); }}
.timeline-item {{ padding:12px 0; border-bottom:1px solid var(--border); }}
.fund-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:16px; }}
.lang-toggle {{ cursor:pointer; padding:4px 12px; border-radius:4px; border:1px solid var(--border); background:var(--surface2); color:var(--text); font-size:12px; }}
.lang-toggle:hover {{ border-color:var(--accent); }}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px;">
      <div>
        <h1 style="font-size:24px;margin-bottom:4px;">Hedge Fund Research Insights</h1>
        <p style="color:var(--text-muted);font-size:13px;">
          {len(articles)} articles | {len(this_week)} new this week | {len(sources)} funds tracked | Updated {now}
        </p>
      </div>
      <button class="lang-toggle" onclick="toggleLang()">中/EN</button>
    </div>
  </div>

  <div class="section">
    <h2>Latest Research</h2>
    {timeline_rows}
    {"<button onclick='showAll()' id='load-more' style='margin-top:12px;padding:8px 20px;background:var(--surface2);border:1px solid var(--border);border-radius:6px;color:var(--accent);cursor:pointer;font-size:13px;'>Load more (" + str(max(0, len(sorted_arts)-20)) + " more)</button>" if len(sorted_arts) > 20 else ""}
  </div>

  <div class="section">
    <h2>By Fund</h2>
    <div class="fund-grid">{fund_cards}</div>
  </div>

  <div class="section">
    <h2 class="lang-en">Theme Tracker</h2>
    <h2 class="lang-zh" style="display:none;">主题追踪</h2>
    {theme_sections if theme_sections else '<p style="color:var(--text-muted);">No themes yet.</p>'}
  </div>

</div>

<script>
let currentLang = 'en';
function toggleLang() {{
  currentLang = currentLang === 'en' ? 'zh' : 'en';
  document.querySelectorAll('.lang-en').forEach(el => el.style.display = currentLang === 'en' ? '' : 'none');
  document.querySelectorAll('.lang-zh').forEach(el => el.style.display = currentLang === 'zh' ? '' : 'none');
}}
function showAll() {{
  document.querySelectorAll('.timeline-item[style*="display:none"]').forEach(el => el.style.display = '');
  document.getElementById('load-more').style.display = 'none';
}}
</script>
</body>
</html>'''


def main() -> None:
    articles = load_articles()
    log.info("Stage 4: generating HTML for %d articles", len(articles))

    html = generate_html(articles)

    # Write to output location
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(html, encoding="utf-8")

    # Gzip for nginx gzip_static
    subprocess.run(["gzip", "-k", "-f", str(OUTPUT_FILE)], check=True)

    log.info("Published to %s (%d bytes)", OUTPUT_FILE, len(html))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_unit_publish.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add publish.py tests/test_unit_publish.py
git commit -m "feat: Stage 4 — HTML dashboard publisher with timeline, fund cards, theme tracker"
```

---

## Task 5: Pipeline wrapper, cron setup, and integration test

**Files:**
- Create: `run_pipeline.sh`
- Modify: crontab
- Modify: `/var/www/overview/index.html` (add sidebar link)

- [ ] **Step 1: Create run_pipeline.sh**

```bash
#!/bin/bash
set -eo pipefail
cd ~/hedge-fund-research

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Pipeline starting"

# Stage 1: fetch metadata (source identity validated internally)
python3 fetch_articles.py

# Stage 2: fetch + validate + normalize content
python3 fetch_content.py

# Stage 3: LLM analysis (only processes content_status="ok" articles)
python3 analyze_articles.py

# Stage 4: publish (always runs — shows whatever data is available)
python3 publish.py

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Pipeline complete"
```

```bash
chmod +x ~/hedge-fund-research/run_pipeline.sh
```

- [ ] **Step 2: Run the full pipeline manually**

```bash
cd ~/hedge-fund-research && bash run_pipeline.sh
```

Expected: All 4 stages execute. Stage 2 fetches content, Stage 3 analyzes with Gemini, Stage 4 generates HTML.

- [ ] **Step 3: Verify the generated page**

```bash
ls -la /var/www/overview/hedge-fund-research.html*
curl -s -u $(cat /etc/nginx/.htpasswd_overview | head -1 | cut -d: -f1):PASSWORD https://docs.sinostor.com.cn/hedge-fund-research.html | head -5
```

- [ ] **Step 4: Add sidebar link to docs index.html**

Add a nav link in `/var/www/overview/index.html` sidebar:
```html
<a href="/hedge-fund-research.html">Hedge Fund Research</a>
```

Then: `gzip -k -f /var/www/overview/index.html`

- [ ] **Step 5: Add cron entries**

```bash
crontab -e
```

Add:
```
# GMIA nightly regression (03:30 BJT = 19:30 UTC)
30 19 * * * ~/cron-wrapper.sh --name gmia-nightly-test --timeout 300 --lock -- python3 -m pytest ~/hedge-fund-research/tests/ -m nightly --tb=short -q >> ~/logs/gmia-nightly.log 2>&1
# GMIA daily pipeline (03:45 BJT = 19:45 UTC)
45 19 * * * ~/cron-wrapper.sh --name gmia-daily --timeout 600 --lock -- bash ~/hedge-fund-research/run_pipeline.sh >> ~/logs/gmia.log 2>&1
```

- [ ] **Step 6: Commit**

```bash
git add run_pipeline.sh
git commit -m "feat: pipeline wrapper and cron integration"
```

---

## Task 6: Functional tests with saved fixtures

**Files:**
- Create: `tests/fixtures/` (multiple fixture files)
- Create: `tests/test_functional_parsers.py`

- [ ] **Step 1: Save current live HTML as fixtures**

```bash
cd ~/hedge-fund-research
mkdir -p tests/fixtures

# Save Man Group
curl -s 'https://www.man.com/insights' -o tests/fixtures/man-group-insights.html

# Save Bridgewater
curl -s 'https://www.bridgewater.com/research-and-insights' -o tests/fixtures/bridgewater-research.html

# Save GMO API response
python3 -c "
import requests, json
from bs4 import BeautifulSoup
resp = requests.get('https://www.gmo.com/americas/research-library/', cookies={'GMO_region':'NorthAmerica'}, timeout=30)
soup = BeautifulSoup(resp.text, 'html.parser')
grid = soup.select_one('section.article-grid[data-endpoint]')
api_url = 'https://www.gmo.com' + grid['data-endpoint'] + '&currentPage=1'
api_resp = requests.get(api_url, cookies={'GMO_region':'NorthAmerica'}, timeout=30)
with open('tests/fixtures/gmo-api-response.json','w') as f:
    json.dump(api_resp.json(), f, indent=2)
print('GMO fixture saved:', len(api_resp.json().get('listing',[])), 'articles')
"
```

- [ ] **Step 2: Write functional parser tests**

Create `tests/test_functional_parsers.py`:
```python
import json
import pytest
from pathlib import Path
from bs4 import BeautifulSoup

FIXTURES = Path(__file__).parent / "fixtures"


class TestManGroupFixture:
    def test_parse_fixture(self):
        from fetch_articles import fetch_man_group
        html = (FIXTURES / "man-group-insights.html").read_text()
        source = {"url": "https://www.man.com/insights", "max_articles": 10}
        # Monkey-patch requests to return fixture
        import unittest.mock as mock
        with mock.patch("fetch_articles.requests.get") as m:
            resp = mock.Mock()
            resp.text = html
            resp.raise_for_status = mock.Mock()
            m.return_value = resp
            articles = fetch_man_group(source)

        assert len(articles) >= 3
        assert all(a["title"] for a in articles)
        assert all(a["date"] for a in articles)
        assert all("man.com" in a["url"] for a in articles)


class TestBridgewaterFixture:
    def test_parse_fixture(self):
        from fetch_articles import fetch_bridgewater
        html = (FIXTURES / "bridgewater-research.html").read_text()
        source = {"url": "https://www.bridgewater.com/research-and-insights", "max_articles": 10}
        import unittest.mock as mock
        with mock.patch("fetch_articles.requests.get") as m:
            resp = mock.Mock()
            resp.text = html
            resp.raise_for_status = mock.Mock()
            m.return_value = resp
            articles = fetch_bridgewater(source)

        assert len(articles) >= 3
        assert all(a["title"] for a in articles)
        assert all("bridgewater.com" in a["url"] for a in articles)


class TestGmoApiFixture:
    def test_parse_fixture(self):
        data = json.loads((FIXTURES / "gmo-api-response.json").read_text())
        assert "listing" in data
        assert len(data["listing"]) >= 5
        for item in data["listing"][:5]:
            assert item.get("Title")
            assert item.get("Date") or item.get("dateData")
            assert item.get("URL")
```

- [ ] **Step 3: Run functional tests**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_functional_parsers.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/ tests/test_functional_parsers.py
git commit -m "test: functional parser tests with saved HTML/JSON fixtures"
```

---

## Task 7: Sanity and nightly regression tests

**Files:**
- Create: `tests/test_sanity.py`
- Create: `tests/test_regression.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Add pytest markers to conftest.py**

Add to `tests/conftest.py`:
```python
def pytest_configure(config):
    config.addinivalue_line("markers", "live: tests that hit live websites")
    config.addinivalue_line("markers", "nightly: nightly regression tests")
```

- [ ] **Step 2: Create sanity tests**

Create `tests/test_sanity.py`:
```python
import json
import pytest
from pathlib import Path

pytestmark = pytest.mark.live


class TestLiveSourceAccess:
    def test_man_group_live(self):
        from fetch_articles import fetch_man_group
        articles = fetch_man_group({"url": "https://www.man.com/insights", "max_articles": 10})
        assert len(articles) >= 1
        assert articles[0]["title"]
        assert articles[0]["date"]

    def test_bridgewater_live(self):
        from fetch_articles import fetch_bridgewater
        articles = fetch_bridgewater({"url": "https://www.bridgewater.com/research-and-insights", "max_articles": 10})
        assert len(articles) >= 1
        assert articles[0]["title"]

    def test_aqr_live(self):
        from fetch_articles import fetch_aqr
        articles = fetch_aqr({"url": "https://www.aqr.com/Insights/Research", "max_articles": 10})
        assert len(articles) >= 1
        assert articles[0]["date"]

    def test_gmo_api_live(self):
        from fetch_articles import fetch_gmo
        articles = fetch_gmo({"url": "https://www.gmo.com/americas/research-library/", "max_articles": 10})
        assert len(articles) >= 1
        assert articles[0]["title"]
        assert articles[0]["date"]

    def test_oaktree_live(self):
        from fetch_articles import fetch_oaktree
        articles = fetch_oaktree({"url": "https://www.oaktreecapital.com/insights", "max_articles": 10})
        assert len(articles) >= 1
        assert articles[0]["title"]
        assert articles[0]["date"]

    def test_config_valid(self):
        config = json.loads((Path(__file__).parent.parent / "config" / "sources.json").read_text())
        from fetch_articles import FETCHERS
        for src in config["sources"]:
            assert src["id"] in FETCHERS, f"No fetcher for {src['id']}"
            assert "expected_hostname" in src, f"Missing expected_hostname for {src['id']}"
```

- [ ] **Step 3: Create regression tests**

Create `tests/test_regression.py`:
```python
import json
import pytest
from pathlib import Path
from urllib.parse import urlparse

pytestmark = pytest.mark.nightly


def _fetch_all_sources():
    """Helper to fetch all sources and return {source_id: articles} dict."""
    config = json.loads((Path(__file__).parent.parent / "config" / "sources.json").read_text())
    from fetch_articles import FETCHERS
    results = {}
    for src in config["sources"]:
        fetcher = FETCHERS.get(src["id"])
        if fetcher:
            try:
                results[src["id"]] = fetcher(src)
            except Exception as e:
                results[src["id"]] = []
    return results, {s["id"]: s for s in config["sources"]}


@pytest.fixture(scope="module")
def all_sources():
    return _fetch_all_sources()


class TestArticleCounts:
    def test_man_group_count(self, all_sources):
        arts, _ = all_sources
        count = len(arts.get("man-group", []))
        assert 3 <= count <= 10, f"Man Group returned {count} articles"

    def test_bridgewater_count(self, all_sources):
        arts, _ = all_sources
        count = len(arts.get("bridgewater", []))
        assert 3 <= count <= 20, f"Bridgewater returned {count} articles"

    def test_aqr_count(self, all_sources):
        arts, _ = all_sources
        count = len(arts.get("aqr", []))
        assert 5 <= count <= 15, f"AQR returned {count} articles"

    def test_gmo_count(self, all_sources):
        arts, _ = all_sources
        count = len(arts.get("gmo", []))
        assert 5 <= count <= 15, f"GMO returned {count} articles"

    def test_oaktree_count(self, all_sources):
        arts, _ = all_sources
        count = len(arts.get("oaktree", []))
        assert 5 <= count <= 20, f"Oaktree returned {count} articles"


class TestDataQuality:
    def test_all_have_titles(self, all_sources):
        arts, _ = all_sources
        for src_id, articles in arts.items():
            for a in articles:
                assert a.get("title"), f"{src_id}: article missing title"

    def test_date_coverage_80pct(self, all_sources):
        arts, _ = all_sources
        total = 0
        with_dates = 0
        for articles in arts.values():
            for a in articles:
                total += 1
                if a.get("date"):
                    with_dates += 1
        assert total > 0
        coverage = with_dates / total
        assert coverage >= 0.8, f"Date coverage {coverage:.0%} < 80%"

    def test_no_cross_source_contamination(self, all_sources):
        arts, sources = all_sources
        for src_id, articles in arts.items():
            expected_host = sources[src_id].get("expected_hostname", "")
            if not expected_host:
                continue
            for a in articles:
                hostname = urlparse(a["url"]).hostname or ""
                assert hostname.endswith(expected_host), \
                    f"{src_id}: URL {a['url']} doesn't match {expected_host}"


class TestApiHealth:
    def test_gmo_api_returns_json(self):
        import requests
        from bs4 import BeautifulSoup
        resp = requests.get("https://www.gmo.com/americas/research-library/",
                           cookies={"GMO_region": "NorthAmerica"}, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
        grid = soup.select_one("section.article-grid[data-endpoint]")
        assert grid, "GMO: article-grid data-endpoint not found"
        api_url = "https://www.gmo.com" + grid["data-endpoint"] + "&currentPage=1"
        api_resp = requests.get(api_url, cookies={"GMO_region": "NorthAmerica"}, timeout=30)
        assert api_resp.status_code == 200
        data = api_resp.json()
        assert "listing" in data
```

- [ ] **Step 4: Run sanity tests**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/test_sanity.py -v`
Expected: All PASS (hits live sites)

- [ ] **Step 5: Run unit + functional tests (default)**

Run: `cd ~/hedge-fund-research && python3 -m pytest tests/ -v --ignore=tests/test_sanity.py --ignore=tests/test_regression.py`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/test_sanity.py tests/test_regression.py
git commit -m "test: sanity and nightly regression tests with live/nightly markers"
```

---

## Task 8: Final integration — run full pipeline and verify dashboard

- [ ] **Step 1: Clean data and run fresh pipeline**

```bash
cd ~/hedge-fund-research
rm -f data/articles.jsonl content/*
bash run_pipeline.sh 2>&1 | tail -20
```

- [ ] **Step 2: Verify dashboard visually**

Open `https://docs.sinostor.com.cn/hedge-fund-research.html` in browser and check:
- Timeline shows articles sorted by date
- Fund cards show 5 sources
- Theme tracker groups articles
- CN/EN toggle works
- "Load more" button works

- [ ] **Step 3: Run all tests**

```bash
cd ~/hedge-fund-research && python3 -m pytest tests/ -v --ignore=tests/test_regression.py
```

Expected: All unit + functional + sanity tests PASS.

- [ ] **Step 4: Update README.md**

Update `README.md` with final architecture, test commands, and cron schedule.

- [ ] **Step 5: Final commit and push**

```bash
git add -A
git commit -m "feat: GMIA v1.0 — complete 4-stage pipeline with tests and dashboard"
git push
```

- [ ] **Step 6: Update project memory**

Update `~/.claude/projects/-home-ubuntu/memory/hedge-fund-research.md` with final state.
