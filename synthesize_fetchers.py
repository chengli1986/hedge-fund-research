#!/usr/bin/env python3
"""Helper for fetcher synthesis pipeline.

Lists inaccessible fund candidates that need a new fetcher, applying skip logic.
Outputs a JSON array to stdout. Used by fetcher-synthesis/program.md agent.

Also auto-rejects candidates whose fetcher-synthesis attempts have failed
MAX_SYNTHESIS_FAILURES times — preventing endless weekly retries on candidates
the agent cannot solve (e.g. IP-layer blocks Playwright cannot bypass).
"""
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from status_util import set_status

BASE_DIR = Path(__file__).parent
CANDIDATES_FILE = BASE_DIR / "config" / "fund_candidates.json"
FETCH_ARTICLES = BASE_DIR / "fetch_articles.py"
HISTORY_FILE = BASE_DIR / "logs" / "fetcher-synthesis-history.jsonl"
SKIP_WINDOW_DAYS = 1
# After this many failed synthesis attempts, the candidate is auto-rejected.
# Why 3: weekly cron → 3 weeks of trying. Tolerates transient site outages
# while still bounding wasted agent time on truly unsolvable candidates.
MAX_SYNTHESIS_FAILURES = 3


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
    """Return candidates that need a synthesis attempt.

    Two sources:
    1. synthesis_priority=True candidates (status="promoted", no fetcher yet) — inserted first.
    2. status="inaccessible" candidates — standard weekly-synthesis queue.
    """
    candidates = load_candidates()
    fetcher_ids = load_fetcher_ids()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=SKIP_WINDOW_DAYS)

    # ── Priority queue: trial-passed, no fetcher yet ──────────────────────────
    priority_targets = []
    for c in candidates:
        if not c.get("synthesis_priority"):
            continue
        if c["id"] in fetcher_ids:
            continue  # fetcher already registered (synthesis succeeded earlier)
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
        # quality guard intentionally omitted: synthesis_priority implies trial-PASS validation
        priority_targets.append({
            "id": c["id"],
            "name": c.get("name", c["id"]),
            "homepage_url": c.get("homepage_url", ""),
            "research_url": c.get("research_url") or c.get("homepage_url", ""),
            "notes": c.get("notes", ""),
            "needs_playwright": bool(c.get("requires_playwright") or c.get("needs_playwright")),
            "quality": c.get("quality", "?"),
        })

    # ── Standard inaccessible queue ───────────────────────────────────────────
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
            "needs_playwright": bool(c.get("needs_playwright")),
            "quality": c.get("quality", "?"),
        })

    quality_order = {"HIGH": 0, "MEDIUM": 1, "?": 2, "LOW": 3}
    targets.sort(key=lambda t: (
        not t["needs_playwright"],
        quality_order.get(t["quality"], 2),
    ))
    # Priority candidates always come first (already sorted by insertion order)
    return priority_targets + targets


def _count_failures(fund_id: str, history_path: Path = HISTORY_FILE) -> int:
    """Count past 'failed' synthesis outcomes for a fund_id in history.jsonl.

    History is the source of truth (not a counter on the candidate) so the
    agent doesn't need to maintain accurate counters — it can write whatever,
    sync_synthesis_history.py reconciles to history, and we count from there.
    """
    if not history_path.exists():
        return 0
    n = 0
    for line in history_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("id") == fund_id and e.get("outcome") == "failed":
            n += 1
    return n


def auto_reject_exhausted_candidates(
    candidates_path: Path = CANDIDATES_FILE,
    history_path: Path = HISTORY_FILE,
    max_failures: int = MAX_SYNTHESIS_FAILURES,
) -> list[str]:
    """Flip inaccessible candidates with >= max_failures synthesis attempts to rejected.

    Returns list of fund IDs that were flipped. Writes back to candidates_path
    only if at least one flip happened (preserves mtime when nothing changes).

    Manual override: to retry a rejected candidate, restore status to "inaccessible"
    AND remove the candidate's failed entries from history.jsonl, otherwise the
    next run will flip it back immediately.
    """
    data = json.loads(candidates_path.read_text())
    candidates = data if isinstance(data, list) else data.get("candidates", [])

    flipped: list[str] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for c in candidates:
        if c.get("status") != "inaccessible":
            continue
        n = _count_failures(c["id"], history_path)
        if n < max_failures:
            continue
        original_notes = c.get("notes", "")
        set_status(c, "rejected")
        c["notes"] = (
            f"Auto-rejected {today}: fetcher-synthesis failed {n} times "
            f"(>= {max_failures} threshold). Original notes: "
            f"{original_notes[:200]}"
        )
        flipped.append(c["id"])

    if flipped:
        out = candidates if isinstance(data, list) else {**data, "candidates": candidates}
        candidates_path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return flipped


def main() -> None:
    flipped = auto_reject_exhausted_candidates()
    if flipped:
        print(
            f"[synthesize_fetchers] auto-rejected {len(flipped)} candidates "
            f"after {MAX_SYNTHESIS_FAILURES} failed attempts: {', '.join(flipped)}",
            file=sys.stderr,
        )
    targets = list_targets()
    json.dump(targets, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
