#!/usr/bin/env python3
"""
Candidate Fund Screening — Rule-based page quality checks.

Reads candidates with status="discovered", fetches their research_url,
and applies rule-based checks for public accessibility, login/paywall
detection, and article index detection.

Usage:
  python3 screen_fund_candidates.py
  python3 screen_fund_candidates.py --dry-run
  python3 screen_fund_candidates.py --fund pimco
  python3 screen_fund_candidates.py --fund pimco --dry-run
"""

import argparse
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"
LOG_FILE = BASE_DIR / "logs" / "screen_fund_candidates.log"

TIMEOUT = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

LOGIN_MARKERS = [
    "log in", "sign in", "register", "subscribe to read",
    "create an account", "forgot password", "sign up",
]

# Date pattern: YYYY-MM-DD or Month DD, YYYY or DD Month YYYY etc.
DATE_PATTERN = re.compile(
    r"\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2})"
    r"|(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{1,2},?\s+\d{4})"
    r"|(?:\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+\d{4})\b",
    re.IGNORECASE,
)

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
# Core screening logic
# ---------------------------------------------------------------------------

def screen_page(url: str, status_code: int, html: str) -> dict:
    """Screen a candidate research page using rule-based checks.

    Checks:
    1. Public accessibility (HTTP 200 required)
    2. Login/paywall detection (password inputs + login text markers)
    3. Article index detection (multiple article-like items needed)

    Args:
        url: The URL being screened.
        status_code: HTTP status code from fetching the URL.
        html: Raw HTML content of the page.

    Returns:
        Dict with keys: passed (bool), reason (str), signals (dict).
    """
    signals: dict = {
        "status_code": status_code,
        "has_password_input": False,
        "login_marker_count": 0,
        "article_tags": 0,
        "time_tags": 0,
        "date_patterns": 0,
        "h2_tags": 0,
        "article_signals": 0,
    }

    # Check 1: Public accessibility
    if status_code != 200:
        return {
            "passed": False,
            "reason": f"Not publicly accessible (HTTP {status_code})",
            "signals": signals,
        }

    soup = BeautifulSoup(html, "html.parser")

    # Check 2: Login/paywall detection
    password_inputs = soup.find_all("input", attrs={"type": "password"})
    signals["has_password_input"] = len(password_inputs) > 0

    page_text = soup.get_text(separator=" ", strip=True).lower()
    login_marker_count = sum(1 for marker in LOGIN_MARKERS if marker in page_text)
    signals["login_marker_count"] = login_marker_count

    if signals["has_password_input"] or login_marker_count >= 2:
        return {
            "passed": False,
            "reason": "Login/paywall detected",
            "signals": signals,
        }

    # Check 3: Article index detection
    article_tags = len(soup.find_all("article"))
    time_tags = len(soup.find_all("time"))
    date_patterns = len(DATE_PATTERN.findall(page_text))
    h2_tags = len(soup.find_all("h2"))

    signals["article_tags"] = article_tags
    signals["time_tags"] = time_tags
    signals["date_patterns"] = date_patterns
    signals["h2_tags"] = h2_tags

    # Article signals: count of distinct signal types that suggest an index page
    article_signals = sum([
        article_tags >= 2,
        time_tags >= 2,
        date_patterns >= 2,
    ])
    signals["article_signals"] = article_signals

    if article_signals >= 2 or h2_tags >= 3:
        return {
            "passed": True,
            "reason": "Research index detected",
            "signals": signals,
        }

    return {
        "passed": False,
        "reason": "Insufficient article index signals",
        "signals": signals,
    }


# ---------------------------------------------------------------------------
# Data I/O (reuse pattern from discover_fund_sites)
# ---------------------------------------------------------------------------

def load_candidates() -> list[dict]:
    """Load candidate state from fund_candidates.json."""
    return json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))


def save_candidates(candidates: list[dict]) -> None:
    """Atomically write candidates to fund_candidates.json."""
    import tempfile
    import os

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


# ---------------------------------------------------------------------------
# Screening logic
# ---------------------------------------------------------------------------

def screen_one(candidate: dict) -> dict:
    """Fetch and screen a single candidate's research URL.

    Args:
        candidate: A candidate dict with at least research_url set.

    Returns:
        Dict with screening results from screen_page, plus fetch metadata.
    """
    url = candidate.get("research_url")
    fund_id = candidate["id"]

    if not url:
        return {
            "id": fund_id,
            "passed": False,
            "reason": "No research URL available",
            "signals": {},
            "error": "missing_research_url",
        }

    log.info("Screening %s (%s) ...", candidate["name"], url)

    try:
        with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url)
            status_code = resp.status_code
            html = resp.text
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        return {
            "id": fund_id,
            "passed": False,
            "reason": f"Fetch error: {e}",
            "signals": {},
            "error": str(e),
        }

    result = screen_page(url, status_code, html)
    result["id"] = fund_id
    result["error"] = None
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Candidate Fund Screening — Rule-based page quality checks"
    )
    parser.add_argument("--fund", help="Screen one fund ID only")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print results without updating fund_candidates.json",
    )
    args = parser.parse_args()

    candidates = load_candidates()
    updated_count = 0

    for c in candidates:
        # Filter: only screen candidates with status="discovered"
        if c["status"] != "discovered":
            if args.fund and c["id"] == args.fund:
                log.warning("Fund %s has status '%s', not 'discovered'", c["id"], c["status"])
            continue

        # Filter by fund ID if specified
        if args.fund and c["id"] != args.fund:
            continue

        result = screen_one(c)
        print(json.dumps(result, indent=2, ensure_ascii=False))

        if result.get("error"):
            log.warning("Skipping %s due to error: %s", c["id"], result["error"])
            continue

        now = datetime.now(BJT).isoformat()

        if not args.dry_run:
            c["status"] = "screened"
            c["is_publicly_accessible"] = result["passed"]
            c["has_article_index"] = result["passed"]
            c["last_screened_at"] = now
            c["screening_signals"] = result["signals"]
            c["screening_reason"] = result["reason"]
            updated_count += 1
        else:
            log.info("[DRY RUN] Would update %s: passed=%s, reason=%s",
                     c["id"], result["passed"], result["reason"])

    if not args.dry_run and updated_count > 0:
        save_candidates(candidates)
        log.info("Saved %d screened candidates", updated_count)


if __name__ == "__main__":
    main()
