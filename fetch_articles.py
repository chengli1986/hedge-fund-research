#!/usr/bin/env python3
"""
Hedge Fund Research Fetcher — scrapes research articles from top hedge fund websites.

Sources:
  - Man Group (SSR)
  - Bridgewater (SSR)
  - AQR (Playwright)
  - GMO (Playwright)
  - Oaktree Capital (Playwright)
  - ARK Invest (RSS)

Usage:
  python3 fetch_articles.py                  # fetch all sources
  python3 fetch_articles.py --source man-group  # fetch one source
  python3 fetch_articles.py --dry-run        # show what would be fetched
"""

import argparse
import json
import hashlib
import logging
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests
from bs4 import BeautifulSoup

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config" / "sources.json"
ENTRYPOINTS_FILE = BASE_DIR / "config" / "entrypoints.json"
INSPECTION_STATE_FILE = BASE_DIR / "config" / "inspection_state.json"
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"
LOG_FILE = BASE_DIR / "logs" / "fetch.log"

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
    "Accept-Language": "en-US,en;q=0.9",
}


def article_id(source_id: str, url: str) -> str:
    """Generate a stable article ID from source + URL."""
    return hashlib.sha256(f"{source_id}:{url}".encode()).hexdigest()[:16]


def load_existing_ids() -> set[str]:
    """Load existing article IDs to avoid duplicates."""
    ids: set[str] = set()
    if DATA_FILE.exists():
        for line in DATA_FILE.read_text().strip().split("\n"):
            if line.strip():
                try:
                    ids.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError):
                    continue
    return ids


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
    state: dict = {}
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

    gated_ratio = gated_count / max(total_found, 1)
    valid_body_ratio = 1.0 - gated_ratio

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
    alerts: list[str] = []
    if metrics.get("consecutive_zero_count", 0) >= 2:
        alerts.append("Consecutive zero articles detected — entrypoint may be broken")
    if metrics.get("last_gated_ratio", 0) > 0.5:
        alerts.append("High gated page ratio (>50%) — entrypoint may point to gated content")
    if metrics.get("last_valid_body_ratio", 1.0) < 0.3:
        alerts.append("Low valid body ratio (<30%) — content extraction failing")
    if metrics.get("last_mismatch_count", 0) > 3:
        alerts.append("High source mismatch count (>3) — entrypoint may have drifted")
    return alerts


def parse_date(date_str: str) -> Optional[str]:
    """Try to parse various date formats into ISO format."""
    date_str = date_str.strip()
    for fmt in [
        "%B %d, %Y",      # March 18, 2026
        "%b %d, %Y",      # Mar 18, 2026
        "%d %B %Y",       # 18 March 2026
        "%d %b %Y",       # 18 Mar 2026
        "%Y-%m-%d",        # 2026-03-18
        "%m/%d/%Y",        # 03/18/2026
        "%B %Y",           # March 2026
        "%d/%m/%Y",        # 18/03/2026
    ]:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _validate_hostname(url: str, expected_hostname: str) -> bool:
    """Check that a URL's hostname ends with the expected domain."""
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    return hostname == expected_hostname or hostname.endswith("." + expected_hostname)


# ---------------------------------------------------------------------------
# SSR Fetchers (requests + BeautifulSoup)
# ---------------------------------------------------------------------------

def fetch_man_group(source: dict) -> list[dict]:
    """Fetch articles from Man Group (SSR).

    HTML structure: div.teaser__wrap > a.teaser contains:
      - h2.teaser__title with optional <strong>Series Name</strong><br>Actual Title
      - span.details__date with "March 31, 2026"
      - span.details__category with "Market Views" etc.
      - div.teaser__text > p with summary
    """
    resp = requests.get(source["url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    articles = []
    for card in soup.select("div.teaser__wrap"):
        link = card.select_one("a.teaser")
        if not link:
            continue
        href = link.get("href", "")
        if not href or href == "/insights":
            continue
        url = urljoin(source["url"], href)

        # Parse title: <strong>Series</strong><br>Actual Title
        title_el = card.select_one(".teaser__content .teaser__title")
        if not title_el:
            continue
        strong = title_el.find("strong")
        series = ""
        if strong:
            series = strong.get_text(strip=True)
            for tag in title_el.find_all(["strong", "br"]):
                tag.decompose()
        title_text = title_el.get_text(strip=True)
        if not title_text or len(title_text) < 5:
            continue

        date_el = card.select_one("span.details__date")
        date_str = date_el.get_text(strip=True) if date_el else ""

        category_el = card.select_one("span.details__category")
        category = category_el.get_text(strip=True) if category_el else ""

        summary_el = card.select_one("div.teaser__text p")
        summary = summary_el.get_text(strip=True) if summary_el else ""

        articles.append({
            "title": title_text,
            "series": series,
            "category": category,
            "summary": summary,
            "url": url,
            "date": parse_date(date_str) if date_str else None,
            "date_raw": date_str,
        })

    return articles[:source.get("max_articles", 10)]


def fetch_bridgewater(source: dict) -> list[dict]:
    """Fetch articles from Bridgewater Associates (SSR).

    HTML structure: a.Link[href*=research-and-insights] with
      - aria-label for title
      - sibling div.PromoC-date for date
    """
    resp = requests.get(source["url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    articles = []
    seen_urls: set[str] = set()

    for link in soup.select("a.Link[href*='/research-and-insights/']"):
        href = link.get("href", "")
        if not href or href.rstrip("/") == "https://www.bridgewater.com/research-and-insights":
            continue
        url = urljoin(source["url"], href)
        if url in seen_urls:
            continue
        seen_urls.add(url)

        title = link.get("aria-label", "") or link.get_text(strip=True)
        if not title or len(title) < 5:
            continue
        skip = ("research and insights", "read more", "learn more", "research & insights")
        if title.lower().strip() in skip:
            continue

        # Date is in a sibling or nearby div.PromoC-date
        date_str = ""
        parent = link.parent
        if parent:
            date_el = parent.select_one(".PromoC-date")
            if not date_el and parent.parent:
                date_el = parent.parent.select_one(".PromoC-date")
            if date_el:
                date_str = date_el.get_text(strip=True)

        articles.append({
            "title": title.strip(),
            "url": url,
            "date": parse_date(date_str) if date_str else None,
            "date_raw": date_str,
        })

    return articles[:source.get("max_articles", 10)]


# ---------------------------------------------------------------------------
# Playwright Fetchers (for CSR / JS-rendered sites)
# ---------------------------------------------------------------------------

def _get_playwright_page(url: str, wait_selector: Optional[str] = None, wait_ms: int = 5000):
    """Launch Playwright, navigate, wait for content, return page HTML."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=30000)
        if wait_selector:
            try:
                page.wait_for_selector(wait_selector, timeout=10000)
            except Exception:
                pass
        page.wait_for_timeout(wait_ms)
        html = page.content()
        browser.close()
    return html


def fetch_aqr(source: dict) -> list[dict]:
    """Fetch articles from AQR (Playwright — CSR).

    Structure:
      Featured: h2 > a.insights-featured-article-v2 + p.article-date
      List: div.search-list-v2__item > h2 > a + p.article__date
    """
    html = _get_playwright_page(source["url"], wait_selector="div.search-list-v2__item")
    soup = BeautifulSoup(html, "html.parser")
    articles = []

    # Featured article
    featured_link = soup.select_one("a.insights-featured-article-v2")
    if featured_link:
        title = featured_link.get_text(strip=True)
        href = featured_link.get("href", "")
        date_el = soup.select_one("p.article-date")
        date_str = date_el.get_text(strip=True) if date_el else ""
        topic_el = featured_link.find_parent().find_previous_sibling("span") if featured_link.find_parent() else None
        category = topic_el.get_text(strip=True) if topic_el else ""
        summary_el = soup.select_one("p.text--small-v2")
        summary = summary_el.get_text(strip=True) if summary_el else ""
        if title and href:
            articles.append({
                "title": title,
                "category": category,
                "summary": summary,
                "url": urljoin("https://www.aqr.com", href),
                "date": parse_date(date_str) if date_str else None,
                "date_raw": date_str,
            })

    # List articles
    for item in soup.select("div.search-list-v2__item"):
        link = item.select_one("h2 a")
        if not link:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")
        if not title or not href:
            continue

        date_el = item.select_one("p.article__date")
        date_str = date_el.get_text(strip=True) if date_el else ""

        topic_el = item.select_one("p.eyebrow")
        category = topic_el.get_text(strip=True) if topic_el else ""

        summary_el = item.select_one("p.article__summary")
        summary = summary_el.get_text(strip=True) if summary_el else ""

        articles.append({
            "title": title,
            "category": category,
            "summary": summary,
            "url": urljoin("https://www.aqr.com", href),
            "date": parse_date(date_str) if date_str else None,
            "date_raw": date_str,
        })

    return articles[:source.get("max_articles", 10)]


def fetch_gmo(source: dict) -> list[dict]:
    """Fetch articles from GMO via their JSON API (no Playwright needed).

    API: /api/articles/getArticlesResearchLibrary?uid=...&isGmo=...&currentPage=1
    Requires cookie: GMO_region=NorthAmerica
    Token params extracted from page HTML data-endpoint attribute.
    """
    # First get the page to extract API tokens from data-endpoint
    resp = requests.get(source["url"], headers=HEADERS,
                        cookies={"GMO_region": "NorthAmerica"}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # Extract API endpoint from data-endpoint attribute on section.article-grid
    grid = soup.select_one("section.article-grid[data-endpoint]")
    if not grid:
        log.warning("GMO: could not find article-grid data-endpoint")
        return []
    api_url = "https://www.gmo.com" + grid["data-endpoint"] + "&currentPage=1"

    # Fetch the JSON API
    api_resp = requests.get(api_url, headers=HEADERS,
                            cookies={"GMO_region": "NorthAmerica"}, timeout=30)
    api_resp.raise_for_status()
    data = api_resp.json()

    articles = []
    for item in data.get("listing", []):
        title = item.get("Title", "").strip()
        if not title:
            continue
        url_path = item.get("URL", "")
        url = urljoin("https://www.gmo.com", url_path) if url_path else ""
        date_raw = item.get("Date", "")
        date_data = item.get("dateData", "")  # MM-DD-YYYY format

        parsed_date = None
        if date_data:
            try:
                parsed_date = datetime.strptime(date_data, "%m-%d-%Y").strftime("%Y-%m-%d")
            except ValueError:
                parsed_date = parse_date(date_raw) if date_raw else None
        elif date_raw:
            parsed_date = parse_date(date_raw)

        articles.append({
            "title": title,
            "category": item.get("Type", ""),
            "author": item.get("Author", "").removeprefix("By ").strip(),
            "summary": item.get("Teaser", "").strip(),
            "url": url,
            "date": parsed_date,
            "date_raw": date_raw,
            "gated": item.get("Lock", False),
        })

    return articles[:source.get("max_articles", 10)]


def fetch_oaktree(source: dict) -> list[dict]:
    """Fetch articles from Oaktree Capital (Playwright — CSR).

    Structure: div.insight-item > a containing:
      span.insights-type (category), time.date (date),
      span.title-link (title), span.read-more (badge)
    """
    html = _get_playwright_page(source["url"], wait_selector="div.insight-item")
    soup = BeautifulSoup(html, "html.parser")

    articles = []
    expected_host = source.get("expected_hostname", "oaktreecapital.com")
    for item in soup.select("div.insight-item"):
        link = item.select_one("a[href]")
        if not link:
            continue

        title_el = item.select_one("span.title-link")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue

        href = link.get("href", "")
        # External links use data-link attribute
        if href == "#":
            href = link.get("data-link", "")
        if not href:
            continue
        url = urljoin("https://www.oaktreecapital.com", href)
        if not _validate_hostname(url, expected_host):
            log.warning("Oaktree: skipping external URL %s", url)
            continue

        date_el = item.select_one("time.date")
        date_str = ""
        parsed_date = None
        if date_el:
            # Prefer datetime attribute (ISO format)
            dt_attr = date_el.get("datetime", "")
            if dt_attr:
                try:
                    parsed_date = datetime.fromisoformat(dt_attr.replace("Z", "+00:00")).strftime("%Y-%m-%d")
                except ValueError:
                    pass
            date_str = date_el.get_text(strip=True)
            if not parsed_date:
                parsed_date = parse_date(date_str)

        category_el = item.select_one("span.insights-type")
        category = category_el.get_text(strip=True) if category_el else ""

        badge_el = item.select_one("span.read-more")
        content_type = badge_el.get_text(strip=True) if badge_el else ""

        # Skip "Archived Memos" hub page
        if title.lower() in ("archived memos", "insights"):
            continue

        articles.append({
            "title": title,
            "category": category,
            "content_type": content_type,  # Read/Listen/Watch
            "url": url,
            "date": parsed_date,
            "date_raw": date_str,
        })

    # Deduplicate audio+text versions (keep text "Read" version)
    seen_titles: dict[str, int] = {}
    for i, a in enumerate(articles):
        base_title = a["title"].replace(" (Audio)", "")
        if base_title in seen_titles:
            existing_idx = seen_titles[base_title]
            if articles[existing_idx].get("content_type") != "Read" and a.get("content_type") == "Read":
                seen_titles[base_title] = i
        else:
            seen_titles[base_title] = i

    unique = [articles[i] for i in sorted(seen_titles.values())]
    return unique[:source.get("max_articles", 10)]


def fetch_wellington(source: dict) -> list[dict]:
    """Fetch articles from Wellington Management (Playwright — CSR/AEM).

    Structure: section.insight.article containing:
      a.insight__title (title + href), a.insight__link (href fallback),
      date[datetime] (ISO date attr), div.insight__contentType > span (category)
    """
    base_url = "https://www.wellington.com"
    html = _get_playwright_page(source["url"], wait_selector="section.insight.article")
    soup = BeautifulSoup(html, "html.parser")
    expected_host = source.get("expected_hostname", "wellington.com")

    articles = []
    for item in soup.select("section.insight.article"):
        title_el = item.select_one("a.insight__title")
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue

        link_el = item.select_one("a.insight__link") or title_el
        href = link_el.get("href", "") or title_el.get("href", "")
        if not href:
            continue
        url = urljoin(base_url, href)
        if not _validate_hostname(url, expected_host):
            continue

        date_el = item.select_one("date[datetime]")
        parsed_date = None
        date_raw = ""
        if date_el:
            dt_attr = date_el.get("datetime", "")
            if dt_attr:
                try:
                    parsed_date = datetime.fromisoformat(dt_attr).strftime("%Y-%m-%d")
                except ValueError:
                    parsed_date = parse_date(dt_attr)
            date_raw = date_el.get_text(strip=True)

        category_el = item.select_one("div.insight__contentType span")
        category = category_el.get_text(strip=True) if category_el else ""

        articles.append({
            "title": title,
            "category": category,
            "url": url,
            "date": parsed_date,
            "date_raw": date_raw,
        })

    return articles[:source.get("max_articles", 10)]


_DATE_WORDS: frozenset[str] = frozenset([
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july",
    "august", "september", "october", "november", "december",
    # "may" omitted — identical to its abbreviation, already present above
])


def _is_date_eyebrow(text: str) -> bool:
    """Return True if an eyebrow label looks like a date rather than a category.

    Note: short month names that double as common words ("may", "mar") may
    produce false positives for unusual category labels.
    """
    lower = text.lower()
    return (
        any(re.search(r"\b" + w + r"\b", lower) for w in _DATE_WORDS)
        or bool(re.match(r"\d", text.strip()))
    )


def fetch_troweprice(source: dict) -> list[dict]:
    """Fetch articles from T. Rowe Price (Playwright — CSR/AEM).

    Structure: div.b-grid-item--12-col cards containing:
      a[href*='/insights/'] (link), span.cmp-tile__heading (title),
      span.cmp-tile__eyebrow (date/category — multiple; distinguished by _is_date_eyebrow)
    """
    base_url = "https://www.troweprice.com"
    html = _get_playwright_page(source["url"], wait_selector="div.b-grid-item--12-col")
    soup = BeautifulSoup(html, "html.parser")
    expected_host = source.get("expected_hostname", "troweprice.com")

    articles = []
    for item in soup.select("div.b-grid-item--12-col"):
        link_el = item.select_one("a[href*='/insights/']")
        if not link_el:
            continue
        href = link_el.get("href", "")
        if not href:
            continue
        url = urljoin(base_url, href)
        if not _validate_hostname(url, expected_host):
            continue

        heading_el = item.select_one("span.cmp-tile__heading")
        title = heading_el.get_text(strip=True) if heading_el else link_el.get_text(strip=True)
        if not title:
            continue

        eyebrows = [el.get_text(strip=True) for el in item.select("span.cmp-tile__eyebrow") if el.get_text(strip=True)]
        date_raw = next((e for e in eyebrows if _is_date_eyebrow(e)), "")
        category = next((e for e in eyebrows if not _is_date_eyebrow(e)), "")
        parsed_date = parse_date(date_raw) if date_raw else None

        articles.append({
            "title": title,
            "category": category,
            "url": url,
            "date": parsed_date,
            "date_raw": date_raw,
        })

    return articles[:source.get("max_articles", 10)]


# ---------------------------------------------------------------------------
# RSS Fetchers
# ---------------------------------------------------------------------------

def _strip_html_tags(text: str) -> str:
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text).strip()


def fetch_ark_invest(source: dict) -> list[dict]:
    """Fetch research articles from ARK Invest RSS feed.

    RSS 2.0 XML at /feed (999 items). Filters to high-value categories:
    Analyst Research, Market Commentary, White Papers.
    pubDate is RFC 2822 format parsed via email.utils.parsedate_to_datetime.
    """
    WANTED_CATEGORIES = {"analyst research", "market commentary", "white papers", "market insights"}

    resp = requests.get(source["url"], headers=HEADERS, timeout=30)
    resp.raise_for_status()

    # Handle BOM
    text = resp.text.lstrip("\ufeff")
    root = ET.fromstring(text)
    articles = []

    for item in root.iter("item"):
        categories = [cat.text.strip() for cat in item.findall("category") if cat.text]
        if not any(c.lower() in WANTED_CATEGORIES for c in categories):
            continue

        title_el = item.find("title")
        link_el = item.find("link")
        pub_date_el = item.find("pubDate")
        desc_el = item.find("description")

        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        link = link_el.text.strip() if link_el is not None and link_el.text else ""
        if not title or not link:
            continue

        parsed_date = None
        date_raw = ""
        if pub_date_el is not None and pub_date_el.text:
            date_raw = pub_date_el.text.strip()
            try:
                dt = parsedate_to_datetime(date_raw)
                parsed_date = dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                parsed_date = parse_date(date_raw)

        summary = ""
        if desc_el is not None and desc_el.text:
            summary = _strip_html_tags(desc_el.text)

        category = ", ".join(categories) if categories else ""

        articles.append({
            "title": title,
            "url": link,
            "date": parsed_date,
            "date_raw": date_raw,
            "category": category,
            "summary": summary,
        })

    # Sort by date descending (newest first) before truncating
    articles.sort(key=lambda a: a.get("date") or "0000-00-00", reverse=True)
    return articles[:source.get("max_articles", 10)]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

FETCHERS = {
    "man-group": fetch_man_group,
    "bridgewater": fetch_bridgewater,
    "aqr": fetch_aqr,
    "gmo": fetch_gmo,
    "oaktree": fetch_oaktree,
    "wellington": fetch_wellington,
    "troweprice": fetch_troweprice,
    "ark-invest": fetch_ark_invest,
}


def fetch_source(source: dict, existing_ids: set[str], dry_run: bool = False) -> list[dict]:
    """Fetch articles for a single source, skip duplicates."""
    source_id = source["id"]
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

    expected_host = source.get("expected_hostname", "")

    new_articles = []
    mismatch_count = 0
    gated_count = 0
    now = datetime.now(BJT).isoformat()
    for art in raw_articles:
        if expected_host and not _validate_hostname(art["url"], expected_host):
            log.warning("SOURCE_MISMATCH: %s article URL %s does not match expected host %s",
                       source_id, art["url"], expected_host)
            mismatch_count += 1
            continue
        if art.get("gated"):
            gated_count += 1
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

    log.info(
        "  %s: %d articles found, %d new",
        source_id,
        len(raw_articles),
        len(new_articles),
    )

    # Record quality metrics for inspection
    record_quality_metrics(source_id, len(raw_articles), len(new_articles),
                           gated_count, mismatch_count)

    if dry_run:
        for a in new_articles:
            log.info("    [NEW] %s — %s", a["date"] or "no date", a["title"])
    return new_articles


def save_articles(articles: list[dict]) -> None:
    """Append new articles to JSONL data file."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("a", encoding="utf-8") as f:
        for a in articles:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Hedge Fund Research Fetcher")
    parser.add_argument("--source", help="Fetch only this source ID")
    parser.add_argument("--dry-run", action="store_true", help="Show results without saving")
    parser.add_argument("--list", action="store_true", help="List configured sources")
    args = parser.parse_args()

    config = json.loads(CONFIG_FILE.read_text())
    sources = config["sources"]

    if args.list:
        for s in sources:
            print(f"  {s['id']:20s} {s['method']:12s} {s['name']}")
        return

    existing_ids = load_existing_ids()
    entrypoints = load_entrypoints()
    all_new: list[dict] = []

    for source in sources:
        if args.source and source["id"] != args.source:
            continue
        # Use entrypoint URL if available, fallback to sources.json
        source = dict(source)  # copy to avoid mutating config
        source["url"] = get_source_url(source, entrypoints)

        new = fetch_source(source, existing_ids, dry_run=args.dry_run)
        all_new.extend(new)
        existing_ids.update(a["id"] for a in new)

        # Check for anomalies after fetch
        if INSPECTION_STATE_FILE.exists():
            try:
                state = json.loads(INSPECTION_STATE_FILE.read_text())
                source_metrics = state.get(source["id"], {})
                for alert in check_anomalies(source_metrics):
                    log.warning("ANOMALY [%s]: %s", source["id"], alert)
            except json.JSONDecodeError:
                pass

        # Rate-limit between sources
        if source != sources[-1]:
            time.sleep(2)

    if all_new and not args.dry_run:
        save_articles(all_new)
        log.info("Saved %d new articles to %s", len(all_new), DATA_FILE)
    elif not all_new:
        log.info("No new articles found.")

    # Summary
    print(f"\n{'='*60}")
    print(f"Hedge Fund Research Fetch — {datetime.now(BJT).strftime('%Y-%m-%d %H:%M BJT')}")
    print(f"{'='*60}")
    total_found = len(all_new)
    print(f"New articles: {total_found}")
    for a in all_new:
        print(f"  [{a['source_name']:12s}] {a['date'] or 'n/a':10s}  {a['title'][:70]}")
    print()


if __name__ == "__main__":
    main()
