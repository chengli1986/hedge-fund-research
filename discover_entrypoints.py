#!/usr/bin/env python3
"""
Hedge Fund Research — Entrypoint Discovery

Crawls a hedge fund homepage, extracts internal links, scores them using the
entrypoint_scorer module, and outputs structured candidate JSON.

AI classification is a Phase 1 stub that always returns None.

Usage:
  python3 discover_entrypoints.py --source man-group
  python3 discover_entrypoints.py --all
  python3 discover_entrypoints.py --source man-group --write
"""

import argparse
import json
import logging
import tempfile
import os
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from entrypoint_scorer import (
    score_domain,
    score_path,
    score_structure,
    score_gate,
    score_final,
)

BJT = timezone(timedelta(hours=8))
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
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------

def extract_nav_links(
    html: str,
    base_url: str,
    allowed_domains: Optional[list[str]] = None,
) -> list[dict]:
    """Parse HTML, extract all <a href> tags and return deduplicated internal links.

    Args:
        html: Raw HTML string of the page.
        base_url: Used to resolve relative URLs.
        allowed_domains: If provided, filter out URLs where score_domain() == 0.0.

    Returns:
        List of {"url": ..., "label": ...} dicts, deduped by URL (trailing / stripped).
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    links: list[dict] = []

    for tag in soup.find_all("a", href=True):
        href = tag.get("href", "").strip()

        # Skip anchors and javascript: links
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue

        # Resolve relative URLs
        url = urljoin(base_url, href)

        # Normalise: strip trailing slash for dedup
        normalised = url.rstrip("/")
        if normalised in seen:
            continue

        # Domain filter
        if allowed_domains is not None and score_domain(url, allowed_domains) == 0.0:
            continue

        seen.add(normalised)

        # Build label from text content or aria-label, truncated to 100 chars
        label = (tag.get("aria-label") or tag.get_text(separator=" ", strip=True))[:100]

        links.append({"url": url, "label": label})

    return links


# ---------------------------------------------------------------------------
# AI classification
# ---------------------------------------------------------------------------

def _call_llm(prompt: str) -> dict:
    """Call Gemini 2.5 Pro via OpenAI-compatible endpoint and return parsed JSON.

    Args:
        prompt: The user prompt to send.

    Returns:
        Parsed JSON dict from the model response content.

    Raises:
        Exception: On any network, API, or parse error.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set in environment")

    payload = json.dumps({
        "model": "gemini-2.5-pro",
        "messages": [{"role": "user", "content": prompt}],
        "response_format": {"type": "json_object"},
        "max_tokens": 300,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    content = body["choices"][0]["message"]["content"]
    return json.loads(content)


def _classify_with_ai(url: str, html: str) -> Optional[dict]:
    """Use LLM to classify whether a page is a research index.

    Returns {"is_research_index": bool, "confidence": float, "reasoning": str}
    or None on failure (graceful degradation).
    """
    truncated_html = html[:4000]
    prompt = (
        "You are classifying web pages for a hedge fund research aggregator.\n"
        "Determine if the following page is a research index page that lists "
        "research articles, papers, or investment commentary.\n\n"
        f"URL: {url}\n\n"
        f"HTML (first 4000 chars):\n{truncated_html}\n\n"
        "Respond with a JSON object in exactly this format:\n"
        '{"is_research_index": <bool>, "confidence": <float 0.0-1.0>, '
        '"reasoning": "<brief explanation>"}'
    )
    try:
        result = _call_llm(prompt)
        return result
    except Exception as e:
        log.warning("_classify_with_ai(%s) failed: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def fetch_page(url: str) -> Optional[str]:
    """Fetch a URL and return the HTML string, or None on any error."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        log.warning("fetch_page(%s) failed: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_candidates(
    candidates: list[dict],
    allowed_domains: list[str],
    page_html_map: dict[str, str],
) -> list[dict]:
    """Score and rank candidate URLs.

    Args:
        candidates: List of {"url": ..., "label": ...} dicts.
        allowed_domains: Domains to allow; URLs with domain_score == 0.0 are filtered.
        page_html_map: Mapping of url -> fetched HTML (may be empty or partial).

    Returns:
        List of scored dicts sorted by final_score descending.
    """
    results: list[dict] = []

    for candidate in candidates:
        url = candidate["url"]
        label = candidate.get("label", "")

        domain_score = score_domain(url, allowed_domains)
        if domain_score == 0.0:
            continue  # Filter out domain mismatches

        path_score = score_path(url)
        html = page_html_map.get(url, "")
        structure_score = score_structure(html)
        gate_penalty = score_gate(html)
        final = score_final(domain_score, path_score, structure_score, gate_penalty)

        ai_classification = _classify_with_ai(url, html)

        # Apply AI score adjustment
        if ai_classification is not None:
            confidence = float(ai_classification.get("confidence", 0.0))
            if confidence >= 0.8:
                if ai_classification.get("is_research_index"):
                    final += 0.1
                else:
                    final -= 0.1

        results.append({
            "url": url,
            "label": label,
            "domain_score": round(domain_score, 4),
            "path_score": round(path_score, 4),
            "structure_score": round(structure_score, 4),
            "gate_penalty": round(gate_penalty, 4),
            "final_score": round(final, 4),
            "ai_classification": ai_classification,
        })

    results.sort(key=lambda r: r["final_score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Entrypoints persistence
# ---------------------------------------------------------------------------

def _write_entrypoints(source_id: str, candidates: list[dict]) -> None:
    """Write top 3 candidates to entrypoints.json (atomic write).

    Only the first candidate gets active=True.
    """
    if ENTRYPOINTS_FILE.exists():
        try:
            data = json.loads(ENTRYPOINTS_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {"version": 1, "sources": {}}
    else:
        data = {"version": 1, "sources": {}}

    top3 = candidates[:3]
    entrypoints = []
    for i, c in enumerate(top3):
        entrypoints.append({
            "url": c["url"],
            "final_score": c["final_score"],
            "active": i == 0,
            "ai_classification": c.get("ai_classification"),
        })

    data.setdefault("sources", {})[source_id] = {
        "last_discovered_at": datetime.now(BJT).isoformat(),
        "discovered_by": "discover_entrypoints",
        "entrypoints": entrypoints,
        "rejected_pages": [],
    }

    # Atomic write via temp file + rename
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=ENTRYPOINTS_FILE.parent,
        prefix=".entrypoints_tmp_",
        suffix=".json",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, ENTRYPOINTS_FILE)
    except Exception:
        os.unlink(tmp_path)
        raise

    log.info("Wrote %d entrypoints for %s", len(entrypoints), source_id)


# ---------------------------------------------------------------------------
# Main discovery function
# ---------------------------------------------------------------------------

def discover_source(source: dict, write: bool = False) -> dict:
    """Crawl a source homepage, score candidate entrypoints, and return results.

    Args:
        source: Source config dict from sources.json.
        write: If True, write top candidates to entrypoints.json.

    Returns:
        Dict with keys: source, discovered_at, candidate_pages, rejected_pages.
    """
    source_id = source["id"]
    homepage_url = source["url"]
    allowed_domains = [source.get("expected_hostname", urlparse(homepage_url).hostname or "")]

    log.info("Discovering entrypoints for %s (%s) ...", source["name"], homepage_url)

    # Step 1: Fetch homepage
    homepage_html = fetch_page(homepage_url)
    if not homepage_html:
        log.error("Could not fetch homepage for %s", source_id)
        return {
            "source": source_id,
            "discovered_at": datetime.now(BJT).isoformat(),
            "candidate_pages": [],
            "rejected_pages": [],
        }

    # Step 2: Extract nav links (filter to allowed domain)
    nav_links = extract_nav_links(homepage_html, homepage_url, allowed_domains=allowed_domains)
    log.info("  Found %d internal links", len(nav_links))

    # Step 3: Fetch up to 20 candidate pages for structure scoring
    page_html_map: dict[str, str] = {}
    for link in nav_links[:20]:
        url = link["url"]
        html = fetch_page(url)
        if html:
            page_html_map[url] = html

    log.info("  Fetched HTML for %d/%d candidate pages", len(page_html_map), min(len(nav_links), 20))

    # Step 4: Score all candidates
    scored = score_candidates(nav_links, allowed_domains, page_html_map)

    # Step 5: Split into candidates and rejected
    candidates = [c for c in scored if c["final_score"] >= 0.6]
    rejected = [c for c in scored if c["final_score"] < 0.4]

    log.info(
        "  %s: %d candidates (>=0.6), %d rejected (<0.4), %d borderline",
        source_id,
        len(candidates),
        len(rejected),
        len(scored) - len(candidates) - len(rejected),
    )

    # Step 6: Optionally write to entrypoints.json
    if write and candidates:
        _write_entrypoints(source_id, candidates)

    result = {
        "source": source_id,
        "discovered_at": datetime.now(BJT).isoformat(),
        "candidate_pages": candidates,
        "rejected_pages": rejected,
    }

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Hedge Fund Research — Entrypoint Discovery")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source", help="Discover for one source ID")
    group.add_argument("--all", action="store_true", help="Discover for all sources")
    parser.add_argument("--write", action="store_true", help="Write results to entrypoints.json")
    args = parser.parse_args()

    # Early check: GEMINI_API_KEY is required for AI classification
    if not os.environ.get("GEMINI_API_KEY"):
        log.error("GEMINI_API_KEY not set — AI classification will fail. "
                  "Set it with: export GEMINI_API_KEY=your_key")
        raise SystemExit(1)

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    sources = config["sources"]

    targets = sources if args.all else [s for s in sources if s["id"] == args.source]

    if not targets:
        print(f"Source not found: {args.source}")
        return

    all_results = []
    for source in targets:
        result = discover_source(source, write=args.write)
        all_results.append(result)

        # Print JSON result
        print(json.dumps(result, indent=2, ensure_ascii=False))

        # Summary
        candidates = result["candidate_pages"]
        rejected = result["rejected_pages"]
        print(f"\n--- {result['source']} ---")
        print(f"Candidates ({len(candidates)} >= 0.6):")
        for c in candidates[:5]:
            print(f"  [{c['final_score']:.3f}] {c['url']}")
        print(f"Rejected ({len(rejected)} < 0.4):")
        for r in rejected[:3]:
            print(f"  [{r['final_score']:.3f}] {r['url']}")
        print()


if __name__ == "__main__":
    main()
