#!/usr/bin/env python3
"""Helper for fetcher synthesis pipeline.

Lists inaccessible fund candidates that need a new fetcher, applying skip logic.
Outputs a JSON array to stdout. Used by fetcher-synthesis/program.md agent.
"""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).parent
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"
FETCH_ARTICLES = BASE_DIR / "fetch_articles.py"
SKIP_WINDOW_DAYS = 7


def load_candidates() -> list[dict]:
    data = json.loads(CANDIDATES_FILE.read_text())
    return data if isinstance(data, list) else data.get("candidates", [])


def load_fetcher_ids() -> set[str]:
    """Parse FETCHERS dict keys from fetch_articles.py (no import needed)."""
    text = FETCH_ARTICLES.read_text()
    start = text.find("FETCHERS = {")
    if start == -1:
        return set()
    block = text[start:text.find("}", start) + 1]
    return {
        line.split('"')[1]
        for line in block.splitlines()
        if line.strip().startswith('"')
    }


def list_targets() -> list[dict]:
    """Return inaccessible candidates that need a synthesis attempt."""
    candidates = load_candidates()
    fetcher_ids = load_fetcher_ids()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=SKIP_WINDOW_DAYS)

    targets = []
    for c in candidates:
        if c.get("status") != "inaccessible":
            continue
        if c.get("quality") == "LOW":
            continue
        if c["id"] in fetcher_ids:
            continue
        last = c.get("synthesis_attempted_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if last_dt > cutoff:
                    continue
            except ValueError:
                pass
        targets.append({
            "id": c["id"],
            "name": c.get("name", c["id"]),
            "homepage_url": c.get("homepage_url", ""),
            "research_url": c.get("research_url") or c.get("homepage_url", ""),
            "notes": c.get("notes", ""),
        })
    return targets


def main() -> None:
    targets = list_targets()
    json.dump(targets, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
