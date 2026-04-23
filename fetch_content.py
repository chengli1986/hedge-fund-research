#!/usr/bin/env python3
"""
Hedge Fund Research — Stage 2: Content Fetcher

Downloads full article content (PDF or HTML body text) for LLM analysis.

Sources:
  - GMO: PDF download via requests (cookie: GMO_region=NorthAmerica)
  - Oaktree: PDF download via Playwright (openPDF() JS extraction)
  - AQR: HTML article body via Playwright
  - Man Group: HTML article body via requests (SSR)
  - Bridgewater: Skipped (gated content)

Usage:
  python3 fetch_content.py                     # fetch all pending content
  python3 fetch_content.py --source gmo        # fetch one source only
  python3 fetch_content.py --dry-run           # show what would be fetched
"""

import argparse
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config" / "sources.json"
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
CONTENT_DIR = BASE_DIR / "content"
LOG_FILE = BASE_DIR / "logs" / "fetch_content.log"

MIN_CONTENT_LENGTH = 100

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

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


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_pdf_response(status_code: int, content_type: str, content_length: int) -> bool:
    """Check that a response looks like a valid PDF: 2xx + application/pdf + >1024 bytes."""
    if not (200 <= status_code < 300):
        return False
    if "application/pdf" not in (content_type or "").lower():
        return False
    if content_length <= 1024:
        return False
    return True


def _validate_json_response(text: str) -> bool:
    """Reject if text starts with '<' (HTML error page), else try json.loads."""
    if text.strip().startswith("<"):
        return False
    try:
        json.loads(text)
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def _normalize_html(html: str, selector: str) -> str:
    """Extract article text from HTML using CSS selectors, stripping boilerplate.

    Args:
        html: Raw HTML string.
        selector: CSS selector(s) for article content paragraphs.

    Returns:
        Cleaned text string. Falls back to broad selectors if primary yields nothing.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove boilerplate elements
    for tag in soup.select("nav, footer, header, script, style, .cookie, .modal, .pagination"):
        tag.decompose()

    # Try primary selector
    elements = soup.select(selector)
    if not elements:
        # Fallback selectors
        for fallback in ["article", "main", ".content", ".article-body"]:
            elements = soup.select(fallback)
            if elements:
                break

    if elements:
        text = "\n".join(el.get_text(strip=True) for el in elements)
    else:
        text = soup.get_text(strip=True)

    return text


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _looks_like_bridgewater_gate(text: str) -> bool:
    """Detect common bridge/gate/disclaimer pages so we do not store them as正文."""
    lowered = _normalize_whitespace(text).lower()
    gate_markers = (
        "subscribe to read",
        "sign up to read",
        "register to continue",
        "register to read",
        "log in to continue",
        "log in to read",
        "accept all cookies",
        "cookie preferences",
        "manage cookies",
        "manage preferences",
        "privacy policy",
        "terms of use",
    )
    disclaimer_markers = (
        "this content is available",
        "disclaimer",
    )
    return any(marker in lowered for marker in gate_markers + disclaimer_markers)


def _extract_bridgewater_text(html: str) -> Optional[str]:
    """Extract Bridgewater article text from article-like containers only."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select("nav, footer, header, script, style, .cookie, .modal, .pagination, .disclaimer, .subscribe, form"):
        tag.decompose()

    selectors = [
        "article",
        "article p",
        "main article",
        "main article p",
        ".article-body",
        ".article-body p",
        ".rich-text",
        ".rich-text p",
        ".content-body",
        ".content-body p",
        ".body-content",
        ".body-content p",
        ".content",
        ".content p",
    ]

    for selector in selectors:
        elements = soup.select(selector)
        if not elements:
            continue
        text = "\n".join(_normalize_whitespace(el.get_text(" ", strip=True)) for el in elements)
        text = _normalize_whitespace(text)
        if len(text) < MIN_CONTENT_LENGTH:
            continue
        if _looks_like_bridgewater_gate(text):
            continue
        return text

    return None


def _check_min_content_length(text: str) -> bool:
    """Check that extracted text meets minimum length threshold."""
    return len(text) > MIN_CONTENT_LENGTH


def _atomic_write(path: Path, data: bytes) -> None:
    """Write data to path atomically via a temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp_path.write_bytes(data)
        os.replace(str(tmp_path), str(path))
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


# ---------------------------------------------------------------------------
# Per-source content fetchers
# ---------------------------------------------------------------------------

def _fetch_content_gmo(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch GMO article content: download PDF, extract text with pdfplumber."""
    import pdfplumber

    url = article["url"]
    log.info("  GMO: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS,
                            cookies={"GMO_region": "NorthAmerica"}, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  GMO: failed to fetch article page: %s", e)
        return None

    # Extract PDF href from the page
    pdf_match = re.search(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', resp.text)
    if not pdf_match:
        log.warning("  GMO: no PDF link found on page %s", url)
        return None

    pdf_url = pdf_match.group(1)
    if not pdf_url.startswith("http"):
        pdf_url = "https://www.gmo.com" + pdf_url

    log.info("  GMO: downloading PDF %s", pdf_url)
    try:
        pdf_resp = requests.get(pdf_url, headers=HEADERS,
                                cookies={"GMO_region": "NorthAmerica"}, timeout=60)
    except Exception as e:
        log.error("  GMO: failed to download PDF: %s", e)
        return None

    if not _validate_pdf_response(pdf_resp.status_code,
                                   pdf_resp.headers.get("Content-Type", ""),
                                   len(pdf_resp.content)):
        log.warning("  GMO: invalid PDF response (status=%d, type=%s, size=%d)",
                     pdf_resp.status_code,
                     pdf_resp.headers.get("Content-Type", ""),
                     len(pdf_resp.content))
        return None

    # Extract text with pdfplumber
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
            tmp.write(pdf_resp.content)
            tmp.flush()
            with pdfplumber.open(tmp.name) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        log.error("  GMO: pdfplumber extraction failed: %s", e)
        return None

    if not _check_min_content_length(text):
        log.warning("  GMO: extracted text too short (%d chars)", len(text))
        return None

    # Save content
    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  GMO: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_oaktree(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Oaktree article content: Playwright page -> find PDF URL -> download -> extract."""
    import pdfplumber
    from playwright.sync_api import sync_playwright

    url = article["url"]
    log.info("  Oaktree: fetching article page %s", url)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
    except Exception as e:
        log.error("  Oaktree: Playwright fetch failed: %s", e)
        return None

    # Look for PDF URL via openPDF() call or direct .pdf href
    pdf_url = None
    # openPDF('title','url') — capture the second argument (URL)
    # Find all PDF URLs, prefer English (no _JPN/_KRN/_SC/_TC suffix)
    pdf_matches = re.findall(r"openPDF\(['\"][^'\"]+['\"],\s*['\"]([^'\"]+\.pdf[^'\"]*)['\"]", html)
    for match in pdf_matches:
        if not re.search(r"_(jpn|krn|sc|tc)\.", match, re.IGNORECASE):
            pdf_url = match
            break
    if not pdf_url and pdf_matches:
        pdf_url = pdf_matches[0]  # fallback to first match
    else:
        pdf_match = re.search(r'href=["\']([^"\']*\.pdf[^"\']*)["\']', html)
        if pdf_match:
            pdf_url = pdf_match.group(1)

    if not pdf_url:
        log.warning("  Oaktree: no PDF link found on page %s", url)
        return None

    if not pdf_url.startswith("http"):
        pdf_url = "https://www.oaktreecapital.com" + pdf_url

    log.info("  Oaktree: downloading PDF %s", pdf_url)
    try:
        pdf_resp = requests.get(pdf_url, headers=HEADERS, timeout=60)
    except Exception as e:
        log.error("  Oaktree: failed to download PDF: %s", e)
        return None

    if not _validate_pdf_response(pdf_resp.status_code,
                                   pdf_resp.headers.get("Content-Type", ""),
                                   len(pdf_resp.content)):
        log.warning("  Oaktree: invalid PDF response (status=%d, type=%s, size=%d)",
                     pdf_resp.status_code,
                     pdf_resp.headers.get("Content-Type", ""),
                     len(pdf_resp.content))
        return None

    # Extract text with pdfplumber
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
            tmp.write(pdf_resp.content)
            tmp.flush()
            with pdfplumber.open(tmp.name) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    except Exception as e:
        log.error("  Oaktree: pdfplumber extraction failed: %s", e)
        return None

    if not _check_min_content_length(text):
        log.warning("  Oaktree: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Oaktree: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_aqr(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch AQR article content: Playwright -> extract HTML article body."""
    from playwright.sync_api import sync_playwright

    url = article["url"]
    log.info("  AQR: fetching article page %s", url)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
    except Exception as e:
        log.error("  AQR: Playwright fetch failed: %s", e)
        return None

    text = _normalize_html(html, ".article-content p, .article__body p, .research-detail p")

    if not _check_min_content_length(text):
        log.warning("  AQR: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  AQR: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_man(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Man Group article content: requests (SSR) -> extract HTML article body."""
    url = article["url"]
    log.info("  Man: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Man: failed to fetch article page: %s", e)
        return None

    text = _normalize_html(resp.text, ".field--body p, .article-body p, .node__content p")

    if not _check_min_content_length(text):
        log.warning("  Man: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Man: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _fetch_content_ark(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch ARK Invest article content with 403 fallback to metadata-only.

    Primary: fetch HTML article body from ark-invest.com.
    Fallback: if 403 (Cloudflare IP block), save RSS summary as metadata-only
    content so the analysis pipeline can still process it with reduced confidence.
    """
    url = article["url"]
    log.info("  ARK: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 403:
            # Cloudflare IP block — fall back to RSS summary as metadata-only
            return _ark_metadata_fallback(article)
        log.warning("  ARK: HTTP error fetching article: %s", e)
        return None
    except Exception as e:
        log.warning("  ARK: failed to fetch article page: %s", e)
        return None

    text = _normalize_html(resp.text, "article p, .post-content p, .entry-content p, .wp-block-paragraph")

    if not _check_min_content_length(text):
        log.warning("  ARK: extracted text too short (%d chars)", len(text))
        return _ark_metadata_fallback(article)

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  ARK: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _ark_metadata_fallback(article: dict) -> Optional[tuple[Path, str]]:
    """Save RSS summary as metadata-only content for ARK articles.

    Returns ("metadata_only") status so the analysis pipeline knows to use
    a lighter prompt and lower confidence scoring.
    """
    summary = article.get("summary", "").strip()
    title = article.get("title", "").strip()
    category = article.get("category", "").strip()

    if not summary and not title:
        log.warning("  ARK: no metadata available for fallback on %s", article.get("id"))
        return None

    # Build metadata-only content from RSS fields
    parts = []
    if title:
        parts.append(f"Title: {title}")
    if category:
        parts.append(f"Category: {category}")
    if summary:
        parts.append(f"Summary: {summary}")
    text = "\n".join(parts)

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  ARK: saved metadata-only (%d chars) to %s (source restricted)", len(text), content_path.name)
    return (content_path, "metadata_only")


def _fetch_content_bridgewater(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Bridgewater article content: requests (SSR) -> extract HTML article body.

    Most articles are publicly accessible despite the disclaimer on some pages.
    If a page returns a gate/registration form, the min content length check will reject it.
    """
    url = article["url"]
    log.info("  Bridgewater: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Bridgewater: failed to fetch article page: %s", e)
        return None

    text = _extract_bridgewater_text(resp.text)
    if text is None:
        log.warning("  Bridgewater: no article body found or page looks gated")
        return None

    if not _check_min_content_length(text):
        log.warning("  Bridgewater: extracted text too short (%d chars) — may be gated", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Bridgewater: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_cambridge(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Cambridge Associates article content: Playwright -> <main> paragraphs."""
    from playwright.sync_api import sync_playwright

    url = article["url"]
    log.info("  Cambridge: fetching article page %s", url)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
            html = page.content()
            browser.close()
    except Exception as e:
        log.error("  Cambridge: Playwright fetch failed: %s", e)
        return None

    text = _normalize_html(html, "main p, article p")

    if not _check_min_content_length(text):
        log.warning("  Cambridge: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Cambridge: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_wellington(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Wellington Management article content via Playwright (AEM site)."""
    from playwright.sync_api import sync_playwright

    url = article["url"]
    log.info("  Wellington: fetching article page %s", url)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            # AEM pages often have long-running analytics requests that never reach
            # networkidle; SSR content is fully present at load event
            page.goto(url, wait_until="load", timeout=30000)
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()
    except Exception as e:
        log.error("  Wellington: Playwright fetch failed: %s", e)
        return None

    # AEM .cmp-text components hold the real article body
    text = _normalize_html(html, ".cmp-text p, [itemprop='articleBody'] p, .rich-text p")

    if not _check_min_content_length(text):
        log.warning("  Wellington: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Wellington: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_amundi(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Amundi Research Center article content via requests (public SSR site)."""
    url = article["url"]
    log.info("  Amundi: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Amundi: fetch failed: %s", e)
        return None

    text = _normalize_html(
        resp.text,
        ".article__body p, .article-body p, .content-body p, main p, article p",
    )

    if not _check_min_content_length(text):
        log.warning("  Amundi: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Amundi: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_troweprice(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch T. Rowe Price article content via Playwright (CSR site)."""
    from playwright.sync_api import sync_playwright

    url = article["url"]
    log.info("  T.Rowe Price: fetching article page %s", url)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()
    except Exception as e:
        log.error("  T.Rowe Price: Playwright fetch failed: %s", e)
        return None

    text = _normalize_html(
        html,
        ".beacon-article-body p, .article-content p, [itemprop='articleBody'] p, main p",
    )

    if not _check_min_content_length(text):
        log.warning("  T.Rowe Price: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  T.Rowe Price: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


CONTENT_FETCHERS = {
    "gmo": _fetch_content_gmo,
    "oaktree": _fetch_content_oaktree,
    "aqr": _fetch_content_aqr,
    "man-group": _fetch_content_man,
    "ark-invest": _fetch_content_ark,
    "bridgewater": _fetch_content_bridgewater,
    "cambridge-associates": _fetch_content_cambridge,
    "wellington": _fetch_content_wellington,
    "amundi": _fetch_content_amundi,
    "troweprice": _fetch_content_troweprice,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_articles() -> list[dict]:
    """Load all articles from the JSONL data file."""
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
    """Rewrite all articles to JSONL data file atomically."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(json.dumps(a, ensure_ascii=False) for a in articles) + "\n"
    tmp_path = DATA_FILE.with_suffix(".jsonl.tmp")
    try:
        tmp_path.write_text(data, encoding="utf-8")
        os.replace(str(tmp_path), str(DATA_FILE))
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Hedge Fund Research — Content Fetcher")
    parser.add_argument("--source", help="Fetch content for this source ID only")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched")
    args = parser.parse_args()

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    articles = load_articles()
    # Skip articles that already have content or have been classified as metadata-only/restricted
    terminal_statuses = {"ok", "metadata_only"}
    pending = [
        a for a in articles
        if not a.get("summarized")
        and a.get("content_status") not in terminal_statuses
        and a.get("source_id") in CONTENT_FETCHERS
        and (not args.source or a.get("source_id") == args.source)
    ]

    log.info("Found %d articles pending content fetch (of %d total)", len(pending), len(articles))

    if args.dry_run:
        for a in pending:
            log.info("  [PENDING] %s — %s — %s", a["source_id"], a.get("date", "n/a"), a["title"])
        return

    success_count = 0
    fail_count = 0

    for a in pending:
        fetcher = CONTENT_FETCHERS[a["source_id"]]
        try:
            result = fetcher(a)
        except Exception as e:
            log.error("Unexpected error fetching %s (%s): %s", a["id"], a["title"], e)
            result = None

        if result is not None:
            content_path, status = result
            a["content_path"] = str(content_path.relative_to(BASE_DIR))
            a["content_status"] = status
            success_count += 1
        else:
            a["content_status"] = "failed"
            fail_count += 1

    # Save all articles back (full rewrite)
    save_articles(articles)
    log.info("Content fetch complete: %d ok, %d failed", success_count, fail_count)

    # Summary
    print(f"\n{'='*60}")
    print(f"Content Fetch — {datetime.now(BJT).strftime('%Y-%m-%d %H:%M BJT')}")
    print(f"{'='*60}")
    print(f"Pending: {len(pending)} | Success: {success_count} | Failed: {fail_count}")
    print()


if __name__ == "__main__":
    main()
