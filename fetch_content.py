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
import time
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

# Content-fetch dead-letter cap: an article that fails content fetch this many
# times is retired to the terminal "permafail" status and stops being retried
# every day. Without this, the ~20 permanently-gated articles (WAF/JS bodies that
# never resolve — AQR/MSCI/Bridgewater/…) are retried forever, wasting work and
# pinning the daily "failed" count to a constant floor that drowns out any real
# new failure. Per-article, NOT per-source: a source that recovers still fetches
# its new articles normally.
MAX_CONTENT_ATTEMPTS = 5
# content_status values that mean "done, never pending again".
TERMINAL_CONTENT_STATUSES = {"ok", "metadata_only", "permafail"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Mirrors fetch_articles.METLIFE_COOKIES (kept local for the same reason HEADERS
# is duplicated — fetch_content does not import fetch_articles). MetLife IM
# enforces a country+role disclaimer interstitial server-side on this cookie
# pair; without it every /investments/ URL 302s to the disclaimer page.
METLIFE_COOKIES = {"culture": "en-us", "disclaimer": "en-us/Institution"}

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

    for tag in soup.select(
        "nav, footer, header, script, style, .cookie, .modal, .pagination, "
        ".disclaimer, .subscribe, form, "
        # Post-2026-04 redesign emits non-article content as sibling
        # RichTextBody divs (legal disclaimer / cookie banner / browser
        # compatibility notice). Strip them so the .RichTextBody selector
        # below cleanly captures only the article body, and so the
        # gate-detection heuristic does not false-trigger on footer text.
        ".Disclaimer-text, .CookieBanner-text, .BrowserCompatibility-text"
    ):
        tag.decompose()

    selectors = [
        # Current Bridgewater site (post-2026-04 redesign): article body lives
        # in <div class="RichTextBody"> with sibling RichTextExpandable-* wrappers.
        # The plain ".RichTextBody" selector matches both p-nested and
        # raw-text variants observed across 6 sampled articles (3.9-24.5K chars);
        # the "p" variant misses articles where paragraphs aren't <p>-wrapped.
        ".RichTextBody",
        ".RichTextBody p",
        ".RichTextExpandable-shortDescription",
        ".RichTextExpandable",
        # Legacy selectors retained as fallback in case site rolls back.
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


def _check_min_content_length(text: str, min_length: int = MIN_CONTENT_LENGTH) -> bool:
    """Check that extracted text meets minimum length threshold."""
    return len(text) > min_length


# Per-source minimum content thresholds. Overrides the global MIN_CONTENT_LENGTH
# for funds whose typical article body is meaningfully longer than 100 chars
# and where short pieces are non-research (video/podcast preview blurbs).
APOLLO_MIN_CONTENT = 1500  # Filter "In this episode..." video/podcast preview cards
MATTHEWS_MIN_CONTENT = 500  # Filter video/teaser pages whose only text is the ~300-char header lead



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

    html = ""
    for attempt in range(1, 3):
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
            break
        except Exception as e:
            log.warning("  T.Rowe Price: Playwright attempt %d failed: %s", attempt, e)
            if attempt == 2:
                log.error("  T.Rowe Price: all attempts failed, giving up")
                return None
            time.sleep(5)

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


def _fetch_content_pimco(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch PIMCO article content via Playwright (CSR — Coveo-backed site).

    Coveo's analytics/tracking beacons keep firing, so the page never reaches
    networkidle (same root cause as the fetch_pimco listing fetcher fix,
    2026-07-19); wait_until="domcontentloaded" is required here too.
    """
    from playwright.sync_api import sync_playwright

    url = article["url"]
    log.info("  PIMCO: fetching article page %s", url)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            html = page.content()
            browser.close()
    except Exception as e:
        log.error("  PIMCO: Playwright fetch failed: %s", e)
        return None

    text = _normalize_html(html, "article p")

    if not _check_min_content_length(text):
        log.warning("  PIMCO: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  PIMCO: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_aberdeen(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Aberdeen Investments article content via Playwright (Next.js CSR)."""
    from playwright.sync_api import sync_playwright

    url = article["url"]
    log.info("  Aberdeen: fetching article page %s", url)

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
        log.error("  Aberdeen: Playwright fetch failed: %s", e)
        return None

    # RichText component holds article body. Exclude RichText_spacing-tight
    # variant which renders the legal disclosure footer, not article content.
    text = _normalize_html(
        html,
        "div[class*='RichText_sc-rich-text']:not([class*='spacing-tight']) p, "
        "div[class*='RichText_sc-rich-text']:not([class*='spacing-tight']) li",
    )

    if not _check_min_content_length(text):
        log.warning("  Aberdeen: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Aberdeen: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_pgim(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch PGIM article content via requests (SSR — AEM .cmp-text components)."""
    url = article["url"]
    log.info("  PGIM: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  PGIM: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, ".cmp-text p")

    if not _check_min_content_length(text):
        log.warning("  PGIM: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  PGIM: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_brookfield(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Brookfield Insights article content via requests (SSR — Drupal)."""
    url = article["url"]
    log.info("  Brookfield: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Brookfield: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, "article p")

    if not _check_min_content_length(text):
        log.warning("  Brookfield: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Brookfield: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_verdad(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Verdad Weekly Research content from its Mailchimp campaign page.

    Verdad froze its Squarespace archive after 2026-05-04 and now publishes
    only via the newsletter, so articles land on mailchi.mp. Those pages are
    table-based email HTML with no <p> tags — the body lives in
    <td class="mcnTextContent"> blocks.
    """
    url = article["url"]
    log.info("  Verdad: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Verdad: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, ".mcnTextContent")

    if not _check_min_content_length(text):
        log.warning("  Verdad: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Verdad: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_jpmam(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch J.P. Morgan AM article content via requests (SSR — AEM editorial-rich-text)."""
    url = article["url"]
    log.info("  JPMAM: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  JPMAM: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, ".jpm-am-editorial-rich-text-field p")

    if not _check_min_content_length(text):
        log.warning("  JPMAM: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  JPMAM: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_apollo(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Apollo Global Management article content via requests (SSR — AEM .cmp-text)."""
    url = article["url"]
    log.info("  Apollo: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Apollo: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, ".cmp-text p")

    if not _check_min_content_length(text, APOLLO_MIN_CONTENT):
        log.warning("  Apollo: extracted text too short (%d chars, min %d filters video/podcast previews)",
                    len(text), APOLLO_MIN_CONTENT)
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Apollo: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_natixis(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Natixis Investment Managers article content via requests (SSR)."""
    url = article["url"]
    log.info("  Natixis IM: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Natixis IM: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, "main p")

    if not _check_min_content_length(text):
        log.warning("  Natixis IM: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Natixis IM: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_kkr(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch KKR Insights article content via requests (SSR — AEM .cmp-text).

    Listing page is Next.js CSR (fetch_articles uses Playwright), but
    article body is SSR-rendered in initial HTML under AEM standard
    `.cmp-text p` containers — same pattern as Apollo / JPMAM.
    """
    url = article["url"]
    log.info("  KKR: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  KKR: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, ".cmp-text p")

    if not _check_min_content_length(text):
        log.warning("  KKR: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  KKR: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_msci(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch MSCI Research & Insights article content via requests.

    MSCI's article pages are SSR-rendered (despite the listing page being
    Next.js CSR — listing requires Playwright but article body is in initial
    HTML). Article body lives in <main><article><p> with Tailwind layout
    div.ms-flex.ms-flex-col.ms-gap-4 wrapping content paragraphs.
    """
    url = article["url"]
    log.info("  MSCI: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  MSCI: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, "main article p")

    if not _check_min_content_length(text):
        log.warning("  MSCI: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  MSCI: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_janus_henderson(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Janus Henderson article content via requests (SSR — WordPress).

    Article body is SSR-rendered in initial HTML; `main p` captures the full
    body paragraphs, stripping navigation and boilerplate sidebars.
    """
    url = article["url"]
    log.info("  Janus Henderson: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Janus Henderson: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, "main p")

    if not _check_min_content_length(text):
        log.warning("  Janus Henderson: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Janus Henderson: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_researchaffiliates(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Research Affiliates article content via requests (SSR).

    Listing page is Next.js CSR (fetch_articles uses Playwright), but article
    body is SSR-rendered into div.rendered-html (also tagged
    ra-mathjax-content / article-html-lightbox). No <main>/<article> tags,
    so the generic "main p" / "article p" selectors yield zero — must use the
    rendered-html wrapper directly.
    """
    url = article["url"]
    log.info("  Research Affiliates: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Research Affiliates: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, "div.rendered-html p")

    if not _check_min_content_length(text):
        log.warning("  Research Affiliates: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Research Affiliates: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_gsam(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Goldman Sachs Asset Management article content via requests (SSR).

    GSAM migrated to a SPA — the article body is no longer SSR'd. The page
    HTML only contains footer disclaimers inside <main>. Fallback: use the
    summaryDescription + summaryTeaserText fields passed through from the
    search API in fetch_articles.fetch_gsam (stored as article['gsam_summary']).
    """
    url = article["url"]
    log.info("  GSAM: fetching article page %s", url)

    text = ""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup.select("nav, footer, header, script, style, aside, "
                               ".footer-disclosure-text"):
            tag.decompose()
        paragraphs = soup.select("main p")
        text = "\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))
    except Exception as e:
        log.error("  GSAM: fetch failed: %s", e)

    if not _check_min_content_length(text):
        summary = article.get("gsam_summary", "")
        if _check_min_content_length(summary):
            log.info("  GSAM: HTML body empty (SPA), using API summary (%d chars)", len(summary))
            text = summary
        else:
            log.warning("  GSAM: extracted text too short (%d chars, summary %d chars)",
                        len(text), len(summary))
            return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  GSAM: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_robeco(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Robeco Insights article content via requests (SSR).

    Listing page is Nuxt.js SPA (fetch_articles uses Playwright), but article
    body is SSR-rendered in the initial HTML; `main p` captures the article
    body after stripping nav/footer/aside boilerplate.
    """
    url = article["url"]
    log.info("  Robeco: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Robeco: fetch failed: %s", e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup.select("nav, footer, header, script, style, aside"):
        tag.decompose()

    paragraphs = soup.select("main p")
    text = "\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))

    if not _check_min_content_length(text):
        log.warning("  Robeco: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Robeco: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_de_shaw(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch D. E. Shaw Library article content via requests (SSR — Next.js).

    Listing page is Next.js CSR (fetch_articles uses Playwright), but article
    body is SSR-rendered in the initial HTML. Paragraphs live inside
    `div[class*='Blogs_']` (CSS-modules hashed prefix) containers; the trailing
    legal disclosure block uses `p.Blogs_MILegal__*` and must be excluded —
    otherwise ~12% of the saved text is copyright/jurisdiction boilerplate.
    """
    url = article["url"]
    log.info("  D. E. Shaw: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  D. E. Shaw: fetch failed: %s", e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup.select("nav, footer, header, script, style, aside"):
        tag.decompose()

    paragraphs = soup.select("div[class*='Blogs_'] p:not([class*='MILegal'])")
    text = "\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))

    if not _check_min_content_length(text):
        log.warning("  D. E. Shaw: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  D. E. Shaw: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_metlife_im(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch MetLife Investment Management article content via requests (SSR).

    Replaced `_fetch_content_pinebridge` on 2026-07-27 when the source moved to
    MetLife IM (see fetch_articles.fetch_metlife_im). Every article also
    carries a ~11k-char `terms-of-use-legalText` block plus a
    `terms-of-use-staticText` block — identical boilerplate on all pages, so
    they are stripped before extraction to keep the summariser from drowning
    in disclaimer text.

    Updated 2026-08-03 for the move to www.metlife.com/investments/en-us/:
    pages are now behind the disclaimer cookie gate (see METLIFE_COOKIES), and
    the body container narrowed to `div.read-more-section.richtext` — the bare
    `div.richtext` selector also matches the page's empty search widget, whose
    "Sorry, we couldn't find any results matching" placeholder would otherwise
    lead the extracted text.

    Deliberately no `main p` fallback: the pages that lack a read-more section
    are PDF-teaser stubs (e.g. the monthly Pension Funding Status), where
    `main p` yields a 260-400 char blurb — over MIN_CONTENT_LENGTH, so it would
    be stored and summarised as if it were the research itself. Returning None
    for those is the correct outcome; the real content is in a linked PDF.
    """
    url = article["url"]
    log.info("  MetLife IM: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, cookies=METLIFE_COOKIES, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  MetLife IM: fetch failed: %s", e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup.select("nav, footer, header, script, style, aside, "
                            "div[class*='terms-of-use'], div[class*='disclaimer']"):
        tag.decompose()

    paragraphs = soup.select("div.read-more-section.richtext p")
    kept = []
    for p in paragraphs:
        para = p.get_text(strip=True)
        if not para:
            continue
        # Same disclaimer, sometimes inlined in the body under a "Disclosure"
        # heading run together with the text (9.3k of 30.5k chars on the
        # 2026-07-20 Global Risks piece).
        if para.lower().startswith("disclosure"):
            continue
        kept.append(para)
    text = "\n".join(kept)

    if not _check_min_content_length(text):
        log.warning("  MetLife IM: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  MetLife IM: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_ares(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Ares Management Perspectives article content via requests (SSR — AEM .cmp-text).

    Same AEM-standard `.cmp-text p` pattern as Apollo / KKR / JPMAM — body
    paragraphs are SSR-rendered in initial HTML. The /perspectives listing
    itself is gated by a country+role attestation modal (fetch_articles
    bypasses via sitemap.xml), but individual article pages serve their
    body unconditionally.
    """
    url = article["url"]
    log.info("  Ares: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Ares: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, ".cmp-text p")

    if not _check_min_content_length(text):
        log.warning("  Ares: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Ares: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


_MATTHEWS_DISCLAIMER = "The views and information discussed in this report"


def _fetch_content_matthews_asia(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Matthews Asia Insights article content via requests (SSR).

    Article bodies are SSR-rendered inside <main>, but the template varies:
    long-form pieces use div.article_body-content, transcript pages use a
    section.module_spacing block. Plain "main p" therefore captures the body
    across templates — but also two boilerplate blocks that live in <main>:
      - the fixed ~1.3K-char legal disclaimer (a section.module_spacing whose
        text begins "The views and information discussed in this report"), and
      - "related insights" card teasers in a.item.
    Both are stripped here. The disclaimer strip is text-scoped (only the
    module_spacing section that holds it) so the transcript template's own
    module_spacing body survives. Video/teaser pages carry only the ~300-char
    header lead; they fall below MATTHEWS_MIN_CONTENT and are skipped (same
    intent as the Apollo podcast-preview filter).
    """
    url = article["url"]
    log.info("  Matthews Asia: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Matthews Asia: fetch failed: %s", e)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup.select("nav, footer, header, script, style, a.item"):
        tag.decompose()
    for sec in soup.select("section.module_spacing"):
        if _MATTHEWS_DISCLAIMER in sec.get_text(" ", strip=True):
            sec.decompose()

    paragraphs = soup.select("main p")
    text = "\n".join(
        p.get_text(" ", strip=True) for p in paragraphs if p.get_text(strip=True)
    )

    if not _check_min_content_length(text, MATTHEWS_MIN_CONTENT):
        log.warning("  Matthews Asia: extracted text too short (%d chars, min %d "
                    "filters video/teaser pages)", len(text), MATTHEWS_MIN_CONTENT)
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Matthews Asia: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_capital_group(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Capital Group ('Capital Ideas') article content via Playwright.

    Article pages are CSR (the requests-only HTML is a ~7KB shell — same
    DataDog/mPulse/Evidon beacon stack as the listing). Body renders after
    domcontentloaded; AEM standard `.cmp-text p` containers hold the article
    text (same selector family as Apollo/KKR/JPMAM, but those serve SSR while
    Capital Group needs a browser to hydrate).
    """
    from playwright.sync_api import sync_playwright

    url = article["url"]
    log.info("  Capital Group: fetching article page %s", url)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(4000)
            html = page.content()
            browser.close()
    except Exception as e:
        log.error("  Capital Group: Playwright fetch failed: %s", e)
        return None

    text = _normalize_html(html, ".cmp-text p")

    if not _check_min_content_length(text):
        log.warning("  Capital Group: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Capital Group: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_acadian_asset(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Acadian Asset Management article content via requests (SSR).

    The insights listing is client-rendered (fetch_articles uses Playwright),
    but the article body itself is SSR in the initial HTML — same split as
    KKR / MSCI. Body lives in a single `div.long-form__main` (rich-text)
    container. Selecting `main p` would double-count text because html.parser
    nests the article's paragraphs inside one malformed outer <p>; selecting
    the container once yields the deduplicated body.
    """
    url = article["url"]
    log.info("  Acadian: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Acadian: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, "div.long-form__main")

    if not _check_min_content_length(text):
        log.warning("  Acadian: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Acadian: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_lazard_am(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Lazard Asset Management article content via requests (SSR — AEM .cmp-text).

    Individual article pages are server-rendered using the same AEM .cmp-text
    pattern as Ares / Apollo. A plain requests.get suffices; no Playwright needed.
    """
    url = article["url"]
    log.info("  Lazard: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Lazard: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, ".cmp-text p")

    if not _check_min_content_length(text):
        log.warning("  Lazard: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Lazard: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_rothschild_co_am(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Rothschild & Co Asset Management article content via requests (SSR).

    Although the candidate notes flagged EC2-403 on article pages, plain
    requests.get now returns HTTP 200 for the insight bodies. The article body
    lives in <article> with <p> paragraphs (verified 1.9K–50K chars across the
    sampled insights). _normalize_html falls back to article/main/.content if
    the primary "article p" selector yields nothing.
    """
    url = article["url"]
    log.info("  Rothschild: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Rothschild: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, "article p")

    if not _check_min_content_length(text):
        log.warning("  Rothschild: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Rothschild: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_goehring_rozencwajg(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Goehring & Rozencwajg article content via requests (SSR — HubSpot CMS).

    Article body renders server-side inside <main p>; confirmed ~47k chars typical.
    """
    url = article["url"]
    log.info("  G&R: fetching article page %s", url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  G&R: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, "main p")

    if not _check_min_content_length(text):
        log.warning("  G&R: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  G&R: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_principal_am(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Principal Asset Management article content via requests (SSR article pages).

    The listing page is a Coveo-driven SPA, but individual article pages are plain
    server-rendered HTML (httpx 200), so no browser is needed for the body.
    """
    url = article["url"]
    log.info("  Principal AM: fetching article page %s", url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Principal AM: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, ".article-body, main article, main p, article p")
    if not _check_min_content_length(text):
        log.warning("  Principal AM: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Principal AM: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_cohen_steers(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Cohen & Steers article content via Playwright (Cloudflare-gated bodies).

    Article pages sit behind the same CF challenge as the listing, so httpx 403s;
    a real browser renders the body. Reuses fetch_articles._get_playwright_page.
    """
    url = article["url"]
    log.info("  Cohen & Steers: fetching article page %s", url)
    try:
        from fetch_articles import _get_playwright_page
        html = _get_playwright_page(url, wait_until="domcontentloaded", wait_ms=3000)
    except Exception as e:
        log.error("  Cohen & Steers: fetch failed: %s", e)
        return None
    if not html:
        return None

    text = _normalize_html(html, ".article-body, article p, main p")
    if not _check_min_content_length(text):
        log.warning("  Cohen & Steers: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Cohen & Steers: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_franklin_templeton(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Franklin Templeton article content via Playwright (Angular JS-SPA).

    Like the listing, article pages are client-rendered: `requests` returns the same
    ~2.7KB Angular shell (HTTP 200, zero paragraphs), so a headless browser is
    required for the body. There is no WAF and the app never reaches networkidle, so
    wait_until='domcontentloaded' plus a short settle is enough. The body renders
    into `main` (no `<article>` element exists on these pages), hence the `main p`
    selector. Verified live 2026-07-28: 11.8K chars from a research post.
    """
    url = article["url"]
    log.info("  Franklin Templeton: fetching article page %s", url)
    try:
        from fetch_articles import _get_playwright_page
        html = _get_playwright_page(url, wait_until="domcontentloaded", wait_ms=4000)
    except Exception as e:
        log.error("  Franklin Templeton: fetch failed: %s", e)
        return None
    if not html:
        return None

    text = _normalize_html(html, "main p")
    if not _check_min_content_length(text):
        log.warning("  Franklin Templeton: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Franklin Templeton: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_partners_group(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Partners Group Perspectives article content via requests (SSR).

    Article pages are plain server-rendered HTML (same as the listing), so no
    browser is needed. There is no `<article>` element; the body lives in
    `div.rte` blocks. `.rte p` rather than `main p` matters: `main` also carries
    the boilerplate legal/risk disclaimer that trails every piece (~5K chars of
    "principal loss is possible; leverage risk; ..."), which would dilute the LLM
    summaries. A comma-joined fallback would not help — `_normalize_html` unions
    both selectors and would pull the disclaimer back in — so we rely on its
    generic article/main fallback if a page ever skips the rich-text component.
    Verified live 2026-08-01: 13.7K chars from the 2026 Mid-Year Outlook.
    """
    url = article["url"]
    log.info("  Partners Group: fetching article page %s", url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Partners Group: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, ".rte p")
    if not _check_min_content_length(text):
        log.warning("  Partners Group: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Partners Group: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_blue_owl_capital(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Blue Owl Capital Insights article content via requests (SSR).

    Insight pages are plain server-rendered HTML. The body lives in
    `.insights-content-wysiwyg` paragraph blocks; `.article__content p` /
    `article p` would also match, but those wrappers additionally carry the
    boilerplate legal disclaimer that trails every piece (~6K chars of "not a
    recommendation or solicitation ... Copyright(c) Blue Owl Capital Inc.")
    plus the "July 14, 2026|1min read" byline row, which would dilute the LLM
    summaries. `_normalize_html`'s generic article/main fallback covers any
    page that skips the wysiwyg component.
    Verified live 2026-08-06: 10.1K chars from the continuation-vehicles piece.
    """
    url = article["url"]
    log.info("  Blue Owl: fetching article page %s", url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Blue Owl: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, ".insights-content-wysiwyg p")
    if not _check_min_content_length(text):
        log.warning("  Blue Owl: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Blue Owl: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_loomis_sayles(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Loomis Sayles Insights article content via requests (SSR WordPress).

    Insight pages are server-rendered WordPress; the body is the standard
    `div.entry-content`. `main p` also works but additionally picks up the
    trailing "Subscribe to our 'In Short' research notes" CTA row, so we scope
    to the post container. The compliance disclaimer and the numeric tracking
    code that end every piece do live inside `.entry-content`, but they run a
    few hundred chars at most against multi-thousand-char bodies, so they are
    not worth a second pass. `_normalize_html`'s generic article/main fallback
    covers any page that skips the WordPress theme wrapper.
    Verified live 2026-08-08: 3.3K-8.1K chars across 4 recent insights.
    """
    url = article["url"]
    log.info("  Loomis Sayles: fetching article page %s", url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Loomis Sayles: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, ".entry-content p")
    if not _check_min_content_length(text):
        log.warning("  Loomis Sayles: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Loomis Sayles: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_resonanz_capital(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Resonanz Capital Insights article content via requests (SSR HubSpot).

    Insight pages are server-rendered HubSpot blog posts; the body lives in the
    `#hs_cos_wrapper_post_body` rich-text span. `main p` also works but adds the
    listing summary line and the "5 min read |Jul 23, 2026" meta row above the
    body, so we scope to the post-body wrapper. `_normalize_html`'s generic
    article/main fallback covers any page that skips the HubSpot wrapper.
    Verified live 2026-08-09: 4.6K-6.8K chars across 4 recent insights.
    """
    url = article["url"]
    log.info("  Resonanz: fetching article page %s", url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Resonanz: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, "#hs_cos_wrapper_post_body p")
    if not _check_min_content_length(text):
        log.warning("  Resonanz: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Resonanz: saved %d chars to %s", len(text), content_path.name)
    return (content_path, "ok")


def _fetch_content_baillie_gifford(article: dict) -> Optional[tuple[Path, str]]:
    """Fetch Baillie Gifford insights article content via requests (SSR — Next.js).

    ic-article pages are server-rendered; the body paragraphs live under `main`
    inside CSS-module-hashed wrappers (`ArticleBody_body__XXXXX`), so any
    class-based selector would break on the next site rebuild — `main p` is the
    only stable handle. It picks up the standard "your capital is at risk"
    lead-in and the FinSA/regulatory disclaimer tail alongside the body, which
    is acceptable noise relative to the 5-10K chars of actual commentary.
    Verified live 2026-08-13: 9.3K chars on the Q3 "Charting the trends beyond
    AI" piece.
    """
    url = article["url"]
    log.info("  Baillie Gifford: fetching article page %s", url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        log.error("  Baillie Gifford: fetch failed: %s", e)
        return None

    text = _normalize_html(resp.text, "main p")
    if not _check_min_content_length(text):
        log.warning("  Baillie Gifford: extracted text too short (%d chars)", len(text))
        return None

    content_path = CONTENT_DIR / f"{article['id']}.txt"
    _atomic_write(content_path, text.encode("utf-8"))
    log.info("  Baillie Gifford: saved %d chars to %s", len(text), content_path.name)
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
    "pimco": _fetch_content_pimco,
    "aberdeen": _fetch_content_aberdeen,
    "brookfield": _fetch_content_brookfield,
    "jpmam": _fetch_content_jpmam,
    "verdad-capital": _fetch_content_verdad,
    "msci-research": _fetch_content_msci,
    "natixis-im": _fetch_content_natixis,
    "apollo-global-management": _fetch_content_apollo,
    "kkr": _fetch_content_kkr,
    "janus-henderson": _fetch_content_janus_henderson,
    "research-affiliates": _fetch_content_researchaffiliates,
    "gsam": _fetch_content_gsam,
    "robeco": _fetch_content_robeco,
    "de-shaw": _fetch_content_de_shaw,
    "metlife-im": _fetch_content_metlife_im,
    "ares-management": _fetch_content_ares,
    "matthews-asia": _fetch_content_matthews_asia,
    "capital-group": _fetch_content_capital_group,
    "acadian-asset": _fetch_content_acadian_asset,
    "lazard-am": _fetch_content_lazard_am,
    "rothschild-co-am": _fetch_content_rothschild_co_am,
    "goehring-rozencwajg": _fetch_content_goehring_rozencwajg,
    "principal-am": _fetch_content_principal_am,
    "cohen-steers": _fetch_content_cohen_steers,
    "franklin-templeton": _fetch_content_franklin_templeton,
    "partners-group": _fetch_content_partners_group,
    "blue-owl-capital": _fetch_content_blue_owl_capital,
    "loomis-sayles": _fetch_content_loomis_sayles,
    "resonanz-capital": _fetch_content_resonanz_capital,
    "baillie-gifford": _fetch_content_baillie_gifford,
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


def is_content_pending(article: dict, source_filter: Optional[str] = None) -> bool:
    """True if an article still needs a content fetch this run.

    Excludes already-summarized articles, terminal content_status values
    (ok / metadata_only / permafail — see MAX_CONTENT_ATTEMPTS), and sources with
    no registered content fetcher."""
    return (
        not article.get("summarized")
        and article.get("content_status") not in TERMINAL_CONTENT_STATUSES
        and article.get("source_id") in CONTENT_FETCHERS
        and (not source_filter or article.get("source_id") == source_filter)
    )


def mark_content_failure(article: dict, max_attempts: int = MAX_CONTENT_ATTEMPTS) -> str:
    """Record one content-fetch failure and return the new content_status.

    Increments content_attempts; once it reaches max_attempts the article is
    retired to the terminal "permafail" status (stamped with when) so it drops
    out of the pending set permanently instead of being retried every day."""
    attempts = int(article.get("content_attempts", 0)) + 1
    article["content_attempts"] = attempts
    if attempts >= max_attempts:
        article["content_status"] = "permafail"
        article["content_permafailed_at"] = datetime.now(BJT).isoformat()
    else:
        article["content_status"] = "failed"
    return article["content_status"]


def main() -> None:
    parser = argparse.ArgumentParser(description="Hedge Fund Research — Content Fetcher")
    parser.add_argument("--source", help="Fetch content for this source ID only")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be fetched")
    args = parser.parse_args()

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)

    articles = load_articles()
    # Skip articles already fetched, classified terminal, or retired to permafail.
    pending = [a for a in articles if is_content_pending(a, args.source)]

    log.info("Found %d articles pending content fetch (of %d total)", len(pending), len(articles))

    if args.dry_run:
        for a in pending:
            log.info("  [PENDING] %s — %s — %s", a["source_id"], a.get("date", "n/a"), a["title"])
        return

    success_count = 0
    fail_count = 0
    permafail_count = 0  # articles retired to permafail THIS run

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
            a.pop("content_attempts", None)  # clear the failure counter on success
            success_count += 1
        else:
            status = mark_content_failure(a)
            fail_count += 1
            if status == "permafail":
                permafail_count += 1
                log.warning("  Retired to permafail after %d attempts: %s (%s)",
                            a.get("content_attempts"), a["id"], a["title"])

    # Save all articles back (full rewrite)
    save_articles(articles)
    log.info("Content fetch complete: %d ok, %d failed (%d newly retired to permafail)",
             success_count, fail_count, permafail_count)

    # Summary
    print(f"\n{'='*60}")
    print(f"Content Fetch — {datetime.now(BJT).strftime('%Y-%m-%d %H:%M BJT')}")
    print(f"{'='*60}")
    print(f"Pending: {len(pending)} | Success: {success_count} | Failed: {fail_count}"
          f" | Retired(permafail): {permafail_count}")
    print()


if __name__ == "__main__":
    main()
