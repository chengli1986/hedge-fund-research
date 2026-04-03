#!/usr/bin/env python3
"""
Hedge Fund Research — Article Yield Quality Metric

Computes per-source "article yield": the fraction of fetched articles that are
genuine, summarized market insights (not disclaimers/noise).

Usage:
    python3 evaluate_entrypoints.py           # human-readable table
    python3 evaluate_entrypoints.py --json    # JSON output
"""

import argparse
import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "data" / "articles.jsonl"

# Compiled noise patterns (case-insensitive where applicable)
_NOISE_PATTERNS: list[re.Pattern] = [
    re.compile(r"legal disclaimer", re.IGNORECASE),
    re.compile(r"no substantive.*(content|analysis|investment)", re.IGNORECASE | re.DOTALL),
    re.compile(r"cookie preferences|cookie policy", re.IGNORECASE),
    re.compile(r"terms of use|terms of service", re.IGNORECASE),
]

MIN_TAKEAWAY_CHARS = 20


def is_noise(takeaway: str) -> bool:
    """Return True if the takeaway matches any noise pattern."""
    return any(p.search(takeaway) for p in _NOISE_PATTERNS)


def compute_yield(articles: list[dict]) -> dict[str, dict]:
    """Compute per-source article yield.

    Returns:
        {
            "source_id": {
                "total": N,
                "quality_articles": N,
                "noise_articles": N,
                "yield": float,
            }
        }

    A quality article is: summarized=True AND key_takeaway_en has 20+ chars AND is NOT noise.
    """
    stats: dict[str, dict] = {}

    for article in articles:
        source_id = article.get("source_id", "unknown")
        if source_id not in stats:
            stats[source_id] = {"total": 0, "quality_articles": 0, "noise_articles": 0, "yield": 0.0}

        stats[source_id]["total"] += 1

        takeaway = article.get("key_takeaway_en", "") or ""
        summarized = bool(article.get("summarized", False))
        long_enough = len(takeaway) >= MIN_TAKEAWAY_CHARS
        noisy = is_noise(takeaway)

        if noisy:
            stats[source_id]["noise_articles"] += 1
        elif summarized and long_enough:
            stats[source_id]["quality_articles"] += 1

    for source_id, data in stats.items():
        total = data["total"]
        data["yield"] = data["quality_articles"] / total if total > 0 else 0.0

    return stats


def load_articles() -> list[dict]:
    """Load articles from JSONL file."""
    articles: list[dict] = []
    if not DATA_FILE.exists():
        return articles
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    articles.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return articles


def print_table(results: dict[str, dict]) -> None:
    """Print human-readable per-source yield table."""
    if not results:
        print("No articles found.")
        return

    col_src = max(len(src) for src in results) + 2
    header = (
        f"{'Source':<{col_src}}  {'Total':>7}  {'Quality':>8}  {'Noise':>6}  {'Yield':>7}"
    )
    print(header)
    print("-" * len(header))

    for source_id, data in sorted(results.items()):
        yield_pct = data["yield"] * 100
        print(
            f"{source_id:<{col_src}}  {data['total']:>7}  "
            f"{data['quality_articles']:>8}  {data['noise_articles']:>6}  "
            f"{yield_pct:>6.1f}%"
        )

    # Aggregate totals
    total = sum(d["total"] for d in results.values())
    quality = sum(d["quality_articles"] for d in results.values())
    noise = sum(d["noise_articles"] for d in results.values())
    overall_yield = quality / total * 100 if total > 0 else 0.0
    print("-" * len(header))
    print(
        f"{'TOTAL':<{col_src}}  {total:>7}  {quality:>8}  {noise:>6}  "
        f"{overall_yield:>6.1f}%"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compute article yield (quality metric) per source."
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Output results as JSON instead of a human-readable table.",
    )
    args = parser.parse_args()

    articles = load_articles()
    results = compute_yield(articles)

    if args.json_output:
        print(json.dumps(results, indent=2))
        return

    print_table(results)


if __name__ == "__main__":
    main()
