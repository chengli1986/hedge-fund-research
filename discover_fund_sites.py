#!/usr/bin/env python3
"""
Candidate Fund Discovery — Homepage Crawl + Research Link Extraction

Crawls seed fund homepages, extracts research/insights candidate links,
detects RSS feeds, and updates the candidate state file.

This is part of the candidate fund discovery pipeline (separate from
the production discover_entrypoints.py pipeline).

Usage:
  python3 discover_fund_sites.py
  python3 discover_fund_sites.py --dry-run
  python3 discover_fund_sites.py --fund pimco
  python3 discover_fund_sites.py --fund pimco --dry-run
"""

import argparse
import json
import logging
import tempfile
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"
LOG_FILE = BASE_DIR / "logs" / "discover_fund_sites.log"

TIMEOUT = 30
MAX_LINKS_PER_FUND = 20

POSITIVE_KEYWORDS = {
    "research", "insight", "insights", "perspectives", "commentary",
    "white-paper", "white-papers", "publications", "reports", "outlook",
    "thinking", "ideas", "library", "letters", "quarterly", "viewpoints",
}

NEGATIVE_KEYWORDS = {
    "about", "careers", "career", "contact", "team", "leadership",
    "events", "podcast", "video", "subscribe", "login", "register",
    "privacy", "legal", "terms", "cookie", "press", "media-kit",
    "investor-relations", "ir", "newsroom",
}

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
# Core extraction
# ---------------------------------------------------------------------------

def extract_research_links(
    html: str,
    base_url: str,
    allowed_domains: list[str],
) -> list[dict]:
    """Extract links whose path segments match positive research keywords.

    Filters out links matching negative keywords and links outside allowed domains.

    Args:
        html: Raw HTML string.
        base_url: Used to resolve relative URLs.
        allowed_domains: Only links on these domains are kept.

    Returns:
        List of {"url": ..., "label": ..., "path": ...} dicts, deduped by URL.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[dict] = []

    for tag in soup.find_all("a", href=True):
        href = tag.get("href", "").strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue

        url = urljoin(base_url, href)
        parsed = urlparse(url)

        # Domain filter
        hostname = parsed.hostname or ""
        if not any(hostname == d or hostname.endswith("." + d) for d in allowed_domains):
            continue

        # Normalise for dedup
        normalised = url.rstrip("/")
        if normalised in seen:
            continue

        # Extract path segments for keyword matching
        path = parsed.path.strip("/")
        segments = set(path.lower().split("/")) if path else set()

        # Negative keyword filter — reject if any segment matches
        if segments & NEGATIVE_KEYWORDS:
            continue

        # Positive keyword filter — keep only if at least one segment matches
        if not (segments & POSITIVE_KEYWORDS):
            continue

        seen.add(normalised)
        label = (tag.get("aria-label") or tag.get_text(separator=" ", strip=True))[:100]
        links.append({"url": url, "label": label, "path": path})

    return links[:MAX_LINKS_PER_FUND]


def detect_rss(html: str, base_url: str) -> list[str]:
    """Detect RSS feed URLs from <link rel="alternate" type="application/rss+xml"> tags.

    Args:
        html: Raw HTML string.
        base_url: Used to resolve relative feed URLs.

    Returns:
        List of absolute feed URLs.
    """
    soup = BeautifulSoup(html, "html.parser")
    feeds: list[str] = []

    for link in soup.find_all("link", attrs={"rel": "alternate"}):
        link_type = (link.get("type") or "").lower()
        if "rss+xml" in link_type or "atom+xml" in link_type:
            href = link.get("href", "").strip()
            if href:
                feeds.append(urljoin(base_url, href))

    return feeds


# ---------------------------------------------------------------------------
# Data I/O
# ---------------------------------------------------------------------------

def load_candidates() -> list[dict]:
    """Load candidate state from fund_candidates.json."""
    return json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))


def load_seed_candidates(fund_id: Optional[str] = None) -> list[dict]:
    """Load candidates with status='seed' (not yet discovered)."""
    candidates = load_candidates()
    seeds = [c for c in candidates if c.get("status") == "seed"]
    if fund_id:
        seeds = [s for s in seeds if s["id"] == fund_id]
    return seeds


def save_candidates(candidates: list[dict]) -> None:
    """Atomically write candidates to fund_candidates.json."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=CANDIDATES_FILE.parent,
        prefix=".fund_candidates_tmp_",
        suffix=".json",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(candidates, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, CANDIDATES_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def update_candidate(
    candidates: list[dict],
    fund_id: str,
    *,
    homepage_url: Optional[str] = None,
    research_url: Optional[str] = None,
    rss_url: Optional[str] = None,
    official_domain: Optional[str] = None,
    research_links: Optional[list[dict]] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """Update a single candidate entry in the candidates list (in-place).

    Args:
        candidates: The full candidates list.
        fund_id: The ID of the candidate to update.
        homepage_url: Discovered homepage URL.
        research_url: Best research/insights URL found.
        rss_url: RSS feed URL if detected.
        official_domain: The fund's domain.
        research_links: All research links found.
        status: New status (e.g., "discovered").

    Returns:
        The updated candidates list.
    """
    now = datetime.now(BJT).isoformat()
    for c in candidates:
        if c["id"] != fund_id:
            continue
        if homepage_url is not None:
            c["homepage_url"] = homepage_url
        if research_url is not None:
            c["research_url"] = research_url
        if rss_url is not None:
            c["rss_url"] = rss_url
        if official_domain is not None:
            c["official_domain"] = official_domain
        if research_links is not None:
            c["research_links"] = research_links
        if status is not None:
            c["status"] = status
        c["discovery_method"] = "discover_fund_sites"
        c["last_discovered_at"] = now
        break
    return candidates


# ---------------------------------------------------------------------------
# Discovery logic
# ---------------------------------------------------------------------------

def discover_one(seed: dict) -> dict:
    """Run discovery for a single seed fund.

    Args:
        seed: A seed dict from fund_seeds.json.

    Returns:
        Dict with discovery results: id, homepage_url, research_links, rss_feeds, error.
    """
    fund_id = seed["id"]
    homepage = seed.get("homepage_url") or seed.get("homepage", "")
    parsed = urlparse(homepage)
    domain = parsed.hostname or ""
    allowed_domains = [domain]

    log.info("Discovering %s (%s) ...", seed["name"], homepage)

    result: dict = {
        "id": fund_id,
        "homepage_url": homepage,
        "official_domain": domain,
        "research_links": [],
        "rss_feeds": [],
        "error": None,
    }

    try:
        with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
            resp = client.get(homepage)
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        log.warning("Failed to fetch %s: %s", homepage, e)
        result["error"] = str(e)
        return result

    # Extract research links
    research_links = extract_research_links(html, homepage, allowed_domains)
    result["research_links"] = research_links
    log.info("  Found %d research links for %s", len(research_links), fund_id)

    # Detect RSS feeds
    rss_feeds = detect_rss(html, homepage)
    result["rss_feeds"] = rss_feeds
    if rss_feeds:
        log.info("  Found %d RSS feeds for %s", len(rss_feeds), fund_id)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Candidate Fund Discovery — Homepage Crawl + Research Link Extraction"
    )
    parser.add_argument("--fund", help="Discover for one fund ID only")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print results without updating fund_candidates.json",
    )
    args = parser.parse_args()

    seeds = load_seed_candidates(fund_id=args.fund)
    if not seeds:
        log.info("No seed candidates found%s — all seeds already discovered",
                 f" for fund '{args.fund}'" if args.fund else "")
        return

    candidates = load_candidates()
    updated_count = 0

    for seed in seeds:
        result = discover_one(seed)

        # Print result summary
        print(json.dumps(result, indent=2, ensure_ascii=False))

        if result["error"]:
            log.warning("Skipping %s due to error: %s", seed["id"], result["error"])
            continue

        research_links = result["research_links"]
        rss_feeds = result["rss_feeds"]

        # Determine best research URL (first link found)
        research_url = research_links[0]["url"] if research_links else None
        rss_url = rss_feeds[0] if rss_feeds else None

        # Determine new status
        new_status = "discovered" if research_url else None

        if not args.dry_run:
            update_candidate(
                candidates,
                seed["id"],
                homepage_url=result["homepage_url"],
                research_url=research_url,
                rss_url=rss_url,
                official_domain=result["official_domain"],
                research_links=research_links,
                status=new_status,
            )
            updated_count += 1
        else:
            log.info("[DRY RUN] Would update %s: research_url=%s, rss=%s, status=%s",
                     seed["id"], research_url, rss_url, new_status)

    if not args.dry_run and updated_count > 0:
        save_candidates(candidates)
        log.info("Saved %d updated candidates", updated_count)


if __name__ == "__main__":
    main()
