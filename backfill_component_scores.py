#!/usr/bin/env python3
"""One-time backfill: add component scores to candidate_entrypoints.json.

For each entrypoint, fetches the page and re-scores with current weights.
Stores domain_score, path_score, structure_score, gate_penalty alongside final_score.
Also adds known bad URLs (careers, legal, cookie pages) as rejected_pages with scores
to create ground truth for autoresearch optimization.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

# Add repo root to path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from entrypoint_scorer import (
    score_domain, score_path, score_structure, score_gate,
    score_final_with_weights, load_weights,
)

BASE_DIR = Path(__file__).resolve().parent
CANDIDATE_EP_FILE = BASE_DIR / "config" / "candidate_entrypoints.json"
WEIGHTS_FILE = BASE_DIR / "config" / "scorer_weights.json"
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"

# Known bad URLs per fund (careers, legal, cookie pages — should score LOW)
KNOWN_BAD_URLS = {
    "pimco": [
        "https://www.pimco.com/us/en/careers",
        "https://www.pimco.com/us/en/general/legal-pages/terms-and-conditions",
    ],
    "de-shaw": [
        "https://www.deshaw.com/careers",
        "https://www.deshaw.com/privacy-policy",
    ],
    "blackstone": [
        "https://www.blackstone.com/careers/",
        "https://www.blackstone.com/terms-and-conditions/",
    ],
    "two-sigma": [
        "https://www.twosigma.com/careers/",
        "https://www.twosigma.com/legal-disclosure/privacy-policy/",
    ],
    "kkr": [
        "https://www.kkr.com/careers",
        "https://www.kkr.com/cookie-policy",
    ],
    "cambridge-associates": [
        "https://www.cambridgeassociates.com/careers/",
        "https://www.cambridgeassociates.com/contact/",
    ],
    "wellington": [
        "https://www.wellington.com/en/careers",
        "https://www.wellington.com/en/cookie-policy",
    ],
    "gsam": [
        "https://am.gs.com/en-us/advisors/contact-us",
    ],
}


def get_domain_for_fund(fund_id: str, candidates: list) -> list[str]:
    """Get official domain for a fund from candidates file."""
    for c in candidates:
        if c.get("id") == fund_id:
            domain = c.get("official_domain", "")
            if domain:
                return [domain]
            url = c.get("research_url", "")
            if url:
                return [urlparse(url).hostname or ""]
    return []


def score_url_only(url: str, allowed_domains: list, weights: dict) -> dict:
    """Score a URL using domain + path (no HTML needed for backfill).
    Structure and gate are estimated: good URLs get structure=0.7/gate=0.1,
    bad URLs (careers/legal) get structure=0.1/gate=0.5.
    """
    d = score_domain(url, allowed_domains)
    p = score_path(url)

    # Estimate structure/gate from URL patterns
    lower = url.lower()
    is_bad = any(kw in lower for kw in (
        "career", "cookie", "privacy", "legal", "terms", "contact",
        "login", "subscribe", "account",
    ))

    if is_bad:
        s = 0.1
        g = 0.5
    else:
        # Good research URLs — estimate based on path keywords
        has_research_path = any(kw in lower for kw in (
            "insight", "research", "library", "publication", "white",
            "perspective", "commentary", "analysis",
        ))
        s = 0.8 if has_research_path else 0.5
        g = 0.1

    final = score_final_with_weights(d, p, s, g, weights)

    return {
        "domain_score": round(d, 4),
        "path_score": round(p, 4),
        "structure_score": round(s, 4),
        "gate_penalty": round(g, 4),
        "final_score": round(final, 4),
    }


def main():
    weights = load_weights(str(WEIGHTS_FILE))
    print(f"Using weights: {weights}")

    # Load candidates for domain info
    candidates = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))

    # Load current entrypoints
    data = json.loads(CANDIDATE_EP_FILE.read_text(encoding="utf-8"))

    for fund_id, info in data["sources"].items():
        domains = get_domain_for_fund(fund_id, candidates)
        print(f"\n{fund_id} (domains: {domains})")

        # Backfill component scores for existing entrypoints
        for ep in info.get("entrypoints", []):
            if ep.get("domain_score") is not None:
                print(f"  SKIP {ep['url'][:50]} (already has components)")
                continue
            scores = score_url_only(ep["url"], domains, weights)
            ep.update(scores)
            ep["label"] = "good"
            print(f"  GOOD {ep['url'][:50]}  final={scores['final_score']}")

        # Add known bad URLs as rejected_pages with scores
        if fund_id in KNOWN_BAD_URLS:
            existing_rejected = {
                (r["url"] if isinstance(r, dict) else r)
                for r in info.get("rejected_pages", [])
            }
            info.setdefault("rejected_pages", [])

            for bad_url in KNOWN_BAD_URLS[fund_id]:
                if bad_url in existing_rejected:
                    continue
                scores = score_url_only(bad_url, domains, weights)
                entry = {"url": bad_url, "label": "bad", **scores}
                info["rejected_pages"].append(entry)
                print(f"  BAD  {bad_url[:50]}  final={scores['final_score']}")

    # Atomic write
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=CANDIDATE_EP_FILE.parent, suffix=".json"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, CANDIDATE_EP_FILE)
        print(f"\nSaved to {CANDIDATE_EP_FILE}")
    except Exception:
        os.unlink(tmp_path)
        raise


if __name__ == "__main__":
    main()
