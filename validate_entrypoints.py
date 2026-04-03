#!/usr/bin/env python3
"""Validate existing entrypoints: HTTP fetch, structure scoring, detect drift/failures.

Usage:
  python3 validate_entrypoints.py                          # validate all sources
  python3 validate_entrypoints.py --source man-group       # validate one source
  python3 validate_entrypoints.py --fix                    # auto-disable degraded/failed entrypoints
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import requests

from entrypoint_scorer import score_domain, score_path, score_structure, score_gate, score_final

BASE_DIR = Path(__file__).resolve().parent
ENTRYPOINTS_FILE = BASE_DIR / "config" / "entrypoints.json"
SOURCES_FILE = BASE_DIR / "config" / "sources.json"
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def validate_entrypoint(url: str, allowed_domains: list[str]) -> dict:
    """Fetch URL and score it. Returns a result dict.

    Returns:
        {
            "url": str,
            "status": "ok" | "degraded" | "error",
            "scores": {domain, path, structure, gate_penalty, final} or {},
            "error": str or None,
        }
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        log.warning("fetch error for %s: %s", url, e)
        return {"url": url, "status": "error", "scores": {}, "error": str(e)}

    domain = score_domain(url, allowed_domains)
    path = score_path(url)
    structure = score_structure(html)
    gate_penalty = score_gate(html)
    final = score_final(domain, path, structure, gate_penalty)

    status = "ok" if final >= 0.4 else "degraded"

    return {
        "url": url,
        "status": status,
        "scores": {
            "domain": domain,
            "path": path,
            "structure": structure,
            "gate_penalty": gate_penalty,
            "final": final,
        },
        "error": None,
    }


def validate_source(
    source_id: str,
    source_config: dict,
    allowed_domains: list[str],
) -> list[dict]:
    """Validate all active entrypoints for a source.

    Args:
        source_id: source identifier (e.g. "man-group")
        source_config: source entry from entrypoints.json (has "entrypoints" list)
        allowed_domains: list of allowed domain strings

    Returns:
        List of result dicts from validate_entrypoint for each active entrypoint.
    """
    results = []
    for ep in source_config.get("entrypoints", []):
        if not ep.get("active", True):
            log.debug("skipping inactive entrypoint: %s", ep.get("url"))
            continue
        result = validate_entrypoint(ep["url"], allowed_domains)
        results.append(result)
    return results


def _load_allowed_domains(sources_data: dict, source_id: str) -> list[str]:
    """Extract allowed domains from sources.json for a given source_id."""
    for src in sources_data.get("sources", []):
        if src.get("id") == source_id:
            hostname = src.get("expected_hostname")
            if hostname:
                return [hostname]
    return []


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate hedge fund research entrypoints")
    parser.add_argument("--source", metavar="ID", help="Validate one source by ID")
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-disable degraded/failed entrypoints (set active=False, save to entrypoints.json)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as machine-readable JSON to stdout",
    )
    args = parser.parse_args()

    entrypoints_data = json.loads(ENTRYPOINTS_FILE.read_text())
    sources_data = json.loads(SOURCES_FILE.read_text())

    all_sources = entrypoints_data.get("sources", {})
    if args.source:
        if args.source not in all_sources:
            log.error("source '%s' not found in entrypoints.json", args.source)
            sys.exit(1)
        sources_to_validate = {args.source: all_sources[args.source]}
    else:
        sources_to_validate = all_sources

    all_results: dict[str, list[dict]] = {}
    modified = False
    for source_id, source_config in sources_to_validate.items():
        allowed_domains = _load_allowed_domains(sources_data, source_id)
        results = validate_source(source_id, source_config, allowed_domains)
        all_results[source_id] = results

        if not args.json_output:
            for result in results:
                url = result["url"]
                status = result["status"]
                final = result["scores"].get("final", 0.0) if result["scores"] else 0.0

                if status == "ok":
                    tag = "[OK]"
                else:
                    tag = "[FAIL]"

                print(f"{tag} {source_id} | {url} | score={final:.3f} | status={status}")
                if result["error"]:
                    print(f"      error: {result['error']}")

        if args.fix:
            for result in results:
                url = result["url"]
                status = result["status"]
                if status in ("error", "degraded"):
                    for ep in source_config.get("entrypoints", []):
                        if ep["url"] == url:
                            ep["active"] = False
                            modified = True
                            log.info("disabled entrypoint: %s (%s)", url, status)

    if args.json_output:
        print(json.dumps(all_results, indent=2))
        return

    if args.fix and modified:
        ENTRYPOINTS_FILE.write_text(json.dumps(entrypoints_data, indent=4))
        log.info("saved updated entrypoints.json")
        print("entrypoints.json updated — degraded/failed entrypoints disabled.")


if __name__ == "__main__":
    main()
