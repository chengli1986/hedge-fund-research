#!/usr/bin/env python3
"""
Hedge Fund Research — Entrypoint Ranking Quality Metric

Re-scores all candidate entrypoints using current weights from
config/scorer_weights.json, then measures ranking precision:
what fraction of "good" URLs rank above all "bad" URLs for each fund?

Usage:
    python3 evaluate_entrypoints.py           # human-readable table
    python3 evaluate_entrypoints.py --json    # JSON output

The metric printed on the last line is:
    overall_precision: <float>
This is the value autoresearch optimizes.
"""

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from entrypoint_scorer import score_final_with_weights, load_weights

CANDIDATE_EP_FILE = BASE_DIR / "config" / "candidate_entrypoints.json"
WEIGHTS_FILE = BASE_DIR / "config" / "scorer_weights.json"

# An entrypoint is "correctly ranked" if its re-scored final_score >= THRESHOLD
# and all bad URLs for the same fund score below THRESHOLD.
THRESHOLD = 0.5


def rescore_entry(entry: dict, weights: dict) -> float:
    """Re-compute final_score from stored component scores + current weights."""
    d = entry.get("domain_score")
    p = entry.get("path_score")
    s = entry.get("structure_score")
    g = entry.get("gate_penalty")
    if any(v is None for v in (d, p, s, g)):
        # Fallback: return stored final_score if components missing
        return entry.get("final_score", 0.0)
    return round(score_final_with_weights(d, p, s, g, weights), 4)


def compute_ranking_precision(data: dict, weights: dict) -> dict:
    """Compute per-fund and overall ranking precision.

    For each fund:
    - Re-score all good entrypoints and bad rejected pages with current weights
    - Precision = (good URLs above threshold) / (total good URLs)
    - Separation = avg(good scores) - avg(bad scores)

    Returns dict with per-fund stats and overall precision.
    """
    results = {}
    total_good_correct = 0
    total_good = 0
    total_bad_correct = 0
    total_bad = 0

    for fund_id, info in data.get("sources", {}).items():
        good_entries = info.get("entrypoints", [])
        bad_entries = [r for r in info.get("rejected_pages", []) if isinstance(r, dict)]

        good_scores = [rescore_entry(e, weights) for e in good_entries]
        bad_scores = [rescore_entry(e, weights) for e in bad_entries]

        # Good URLs above threshold
        good_above = sum(1 for s in good_scores if s >= THRESHOLD)
        # Bad URLs below threshold
        bad_below = sum(1 for s in bad_scores if s < THRESHOLD)

        precision = good_above / len(good_scores) if good_scores else 1.0
        bad_reject_rate = bad_below / len(bad_scores) if bad_scores else 1.0

        avg_good = sum(good_scores) / len(good_scores) if good_scores else 0.0
        avg_bad = sum(bad_scores) / len(bad_scores) if bad_scores else 0.0
        separation = avg_good - avg_bad if bad_scores else avg_good

        results[fund_id] = {
            "good_count": len(good_scores),
            "good_above_threshold": good_above,
            "precision": round(precision, 4),
            "bad_count": len(bad_scores),
            "bad_below_threshold": bad_below,
            "bad_reject_rate": round(bad_reject_rate, 4),
            "avg_good_score": round(avg_good, 4),
            "avg_bad_score": round(avg_bad, 4),
            "separation": round(separation, 4),
        }

        total_good_correct += good_above
        total_good += len(good_scores)
        total_bad_correct += bad_below
        total_bad += len(bad_scores)

    # Overall metric: weighted combination of precision + rejection rate
    overall_precision = total_good_correct / total_good if total_good else 0.0
    overall_reject_rate = total_bad_correct / total_bad if total_bad else 0.0
    # Combined metric: 60% good-precision + 40% bad-rejection
    overall = 0.6 * overall_precision + 0.4 * overall_reject_rate

    return {
        "per_fund": results,
        "overall_precision": round(overall_precision, 4),
        "overall_reject_rate": round(overall_reject_rate, 4),
        "overall": round(overall, 4),
        "total_good": total_good,
        "total_bad": total_bad,
    }


def print_table(metrics: dict) -> None:
    """Print human-readable ranking quality table."""
    per_fund = metrics["per_fund"]
    if not per_fund:
        print("No entrypoint data found.")
        return

    col_fund = max(len(f) for f in per_fund) + 2
    header = (
        f"{'Fund':<{col_fund}}  {'Good':>5}  {'Above':>6}  {'Prec':>6}  "
        f"{'Bad':>4}  {'Below':>6}  {'Rej%':>6}  {'Sep':>6}"
    )
    print(header)
    print("-" * len(header))

    for fund_id, data in sorted(per_fund.items()):
        print(
            f"{fund_id:<{col_fund}}  {data['good_count']:>5}  "
            f"{data['good_above_threshold']:>6}  {data['precision']*100:>5.1f}%  "
            f"{data['bad_count']:>4}  {data['bad_below_threshold']:>6}  "
            f"{data['bad_reject_rate']*100:>5.1f}%  {data['separation']:>6.3f}"
        )

    print("-" * len(header))
    print(
        f"{'TOTAL':<{col_fund}}  {metrics['total_good']:>5}  "
        f"{'':>6}  {metrics['overall_precision']*100:>5.1f}%  "
        f"{metrics['total_bad']:>4}  {'':>6}  "
        f"{metrics['overall_reject_rate']*100:>5.1f}%"
    )
    # This line is what autoresearch reads
    print(f"\noverall_precision: {metrics['overall']:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate entrypoint ranking quality with current scorer weights."
    )
    parser.add_argument(
        "--json", dest="json_output", action="store_true",
        help="Output results as JSON.",
    )
    args = parser.parse_args()

    if not CANDIDATE_EP_FILE.exists():
        print("ERROR: candidate_entrypoints.json not found", file=sys.stderr)
        sys.exit(1)

    data = json.loads(CANDIDATE_EP_FILE.read_text(encoding="utf-8"))
    weights = load_weights(str(WEIGHTS_FILE))

    metrics = compute_ranking_precision(data, weights)

    if args.json_output:
        print(json.dumps(metrics, indent=2))
        return

    print_table(metrics)


if __name__ == "__main__":
    main()
