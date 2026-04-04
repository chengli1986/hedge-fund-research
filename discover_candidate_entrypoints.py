#!/usr/bin/env python3
"""
Candidate Fund Entrypoint Discovery — Score research pages for new fund candidates.

Reads candidates with status="screened" from fund_candidates.json, fetches their
research_url, scores pages using entrypoint_scorer, checks nav links for additional
entrypoints, and writes top-scoring pages to config/candidate_entrypoints.json.

Never writes to the production entrypoints.json.

Usage:
  python3 discover_candidate_entrypoints.py
  python3 discover_candidate_entrypoints.py --dry-run
  python3 discover_candidate_entrypoints.py --fund pimco
  python3 discover_candidate_entrypoints.py --fund pimco --dry-run
"""

import argparse
import json
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx

from entrypoint_scorer import (
    load_weights,
    score_domain,
    score_path,
    score_structure,
    score_gate,
    score_final_with_weights,
)
from discover_entrypoints import extract_nav_links

BJT = timezone(timedelta(hours=8))
BASE_DIR = Path(__file__).resolve().parent
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"
CANDIDATE_EP_FILE = BASE_DIR / "config" / "candidate_entrypoints.json"
WEIGHTS_FILE = BASE_DIR / "config" / "scorer_weights.json"
LOG_FILE = BASE_DIR / "logs" / "discover_candidate_entrypoints.log"

MAX_ENTRYPOINTS = 3
SCORE_THRESHOLD = 0.5
TIMEOUT = 30
MAX_NAV_LINKS = 10

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
# Scoring helpers (pure, no I/O)
# ---------------------------------------------------------------------------

def score_candidate_page(
    url: str,
    html: str,
    allowed_domains: list[str],
    weights: dict | None = None,
) -> dict:
    """Score a single candidate page using entrypoint_scorer functions.

    Args:
        url: The page URL.
        html: Raw HTML content of the page.
        allowed_domains: Domains to score against.
        weights: Scorer weights dict. Uses defaults if None.

    Returns:
        Dict with url, component scores, and final_score.
    """
    if weights is None:
        weights = load_weights(str(WEIGHTS_FILE))

    d = score_domain(url, allowed_domains)
    p = score_path(url)
    s = score_structure(html)
    g = score_gate(html)
    final = score_final_with_weights(d, p, s, g, weights)

    return {
        "url": url,
        "domain_score": round(d, 4),
        "path_score": round(p, 4),
        "structure_score": round(s, 4),
        "gate_penalty": round(g, 4),
        "final_score": round(final, 4),
    }


def pick_top_entrypoints(scored_pages: list[dict]) -> list[dict]:
    """Select top entrypoints from scored pages.

    Filters by SCORE_THRESHOLD, sorts descending, keeps top MAX_ENTRYPOINTS.
    First entry gets active=True, rest get active=False.

    Args:
        scored_pages: List of dicts with at least 'url' and 'final_score'.

    Returns:
        List of entrypoint dicts with 'active' flag set.
    """
    above = [p for p in scored_pages if p["final_score"] >= SCORE_THRESHOLD]
    above.sort(key=lambda x: x["final_score"], reverse=True)
    top = above[:MAX_ENTRYPOINTS]

    result = []
    for i, page in enumerate(top):
        result.append({
            **page,
            "active": i == 0,
        })
    return result


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> str | None:
    """Fetch a URL and return HTML, or None on error."""
    try:
        with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return resp.text
    except Exception as e:
        log.warning("fetch_page(%s) failed: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Data I/O
# ---------------------------------------------------------------------------

def load_candidates() -> list[dict]:
    """Load candidate state from fund_candidates.json."""
    return json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))


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


def load_candidate_entrypoints() -> dict:
    """Load candidate entrypoints state."""
    if CANDIDATE_EP_FILE.exists():
        try:
            return json.loads(CANDIDATE_EP_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"version": 1, "sources": {}}


def save_candidate_entrypoints(data: dict) -> None:
    """Atomically write candidate entrypoints."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=CANDIDATE_EP_FILE.parent,
        prefix=".candidate_ep_tmp_",
        suffix=".json",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, CANDIDATE_EP_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


# ---------------------------------------------------------------------------
# Main validation logic
# ---------------------------------------------------------------------------

def validate_candidate(candidate: dict, weights: dict, dry_run: bool = False) -> dict:
    """Validate a single screened candidate by scoring its entrypoints.

    Steps:
    1. Fetch research_url and score it.
    2. Extract nav links from the research page, fetch up to MAX_NAV_LINKS.
    3. Score all fetched pages.
    4. Pick top entrypoints (>= threshold).
    5. Return results.

    Args:
        candidate: A candidate dict with research_url and official_domain.
        weights: Scorer weights dict.
        dry_run: If True, don't persist anything.

    Returns:
        Dict with entrypoints, fit_score, and metadata.
    """
    fund_id = candidate["id"]
    research_url = candidate.get("research_url")
    domain = candidate.get("official_domain")

    if not research_url:
        log.warning("Candidate %s has no research_url, skipping", fund_id)
        return {"id": fund_id, "entrypoints": [], "fit_score": 0.0, "error": "no_research_url"}

    allowed_domains = [domain] if domain else [urlparse(research_url).hostname or ""]

    log.info("Validating %s (%s) ...", candidate["name"], research_url)

    # Step 1: Fetch and score the main research page
    scored_pages: list[dict] = []
    main_html = fetch_page(research_url)

    if main_html:
        main_scored = score_candidate_page(research_url, main_html, allowed_domains, weights)
        scored_pages.append(main_scored)
        log.info("  Main page score: %.3f", main_scored["final_score"])

        # Step 2: Extract and score nav links
        nav_links = extract_nav_links(main_html, research_url, allowed_domains)
        log.info("  Found %d nav links, checking up to %d", len(nav_links), MAX_NAV_LINKS)

        for link in nav_links[:MAX_NAV_LINKS]:
            link_url = link["url"]
            # Skip if same as main research URL
            if link_url.rstrip("/") == research_url.rstrip("/"):
                continue
            link_html = fetch_page(link_url)
            if link_html:
                link_scored = score_candidate_page(link_url, link_html, allowed_domains, weights)
                scored_pages.append(link_scored)
    else:
        log.warning("  Could not fetch main research page for %s", fund_id)

    # Step 3: Pick top entrypoints
    top = pick_top_entrypoints(scored_pages)
    fit_score = top[0]["final_score"] if top else 0.0

    log.info("  %s: %d entrypoints above threshold (fit_score=%.3f)",
             fund_id, len(top), fit_score)

    return {
        "id": fund_id,
        "entrypoints": top,
        "fit_score": round(fit_score, 4),
        "all_scored": scored_pages,
        "error": None,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Candidate Fund Entrypoint Discovery — Score research pages"
    )
    parser.add_argument("--fund", help="Validate one fund ID only")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print results without updating state files",
    )
    args = parser.parse_args()

    weights = load_weights(str(WEIGHTS_FILE))
    candidates = load_candidates()
    ep_data = load_candidate_entrypoints()
    updated_count = 0

    for c in candidates:
        # Filter: only process candidates with status="screened"
        if c["status"] != "screened":
            if args.fund and c["id"] == args.fund:
                log.warning("Fund %s has status '%s', not 'screened'", c["id"], c["status"])
            continue

        # Filter by fund ID if specified
        if args.fund and c["id"] != args.fund:
            continue

        result = validate_candidate(c, weights, dry_run=args.dry_run)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))

        if result.get("error"):
            log.warning("Skipping %s due to error: %s", c["id"], result["error"])
            continue

        now = datetime.now(BJT).isoformat()

        if not args.dry_run:
            # Update candidate state
            c["status"] = "validated"
            c["fit_score"] = result["fit_score"]
            c["last_validated_at"] = now

            # Write to candidate_entrypoints.json (NEVER production)
            if result["entrypoints"]:
                ep_data["sources"][c["id"]] = {
                    "last_discovered_at": now,
                    "discovered_by": "discover_candidate_entrypoints",
                    "entrypoints": [
                        {
                            "url": ep["url"],
                            "final_score": ep["final_score"],
                            "active": ep["active"],
                        }
                        for ep in result["entrypoints"]
                    ],
                    "rejected_pages": [],
                }

            updated_count += 1
        else:
            log.info("[DRY RUN] Would validate %s: fit_score=%.3f, entrypoints=%d",
                     c["id"], result["fit_score"], len(result["entrypoints"]))

    if not args.dry_run and updated_count > 0:
        save_candidates(candidates)
        save_candidate_entrypoints(ep_data)
        log.info("Saved %d validated candidates", updated_count)


if __name__ == "__main__":
    main()
