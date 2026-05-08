#!/usr/bin/env python3
"""Calibrate empirical thresholds against real production source distributions.

The problem: SHELL_HTML_THRESHOLD, FREQ_TO_STALE_DAYS, etc. are picked from one
or two case studies (franklin-templeton 2367 chars, AQR ~50d cadence). Without
data we can't tell whether 5000 chars is conservative or generous, or whether
'monthly → 90d' is even close to actual cadences.

This script doesn't change code. It probes every production source, collects:
  - httpx body size (for SHELL_HTML_THRESHOLD calibration)
  - max consecutive article gap in days (for FREQ_TO_STALE_DAYS calibration)
  - last-article age (for staleness sanity check)
And writes the resulting distribution + recommended thresholds to
logs/threshold-calibration-<date>.json so a human can decide whether to update
constants.

Recommended cadence: monthly (cron 0 5 1 * *), or run on-demand before tuning.

Usage:
  python3 scripts/calibrate_thresholds.py
  python3 scripts/calibrate_thresholds.py --json    # stdout JSON only, no file
  python3 scripts/calibrate_thresholds.py --include-inaccessible  # also probe
                                                                  # candidates
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
SOURCES_FILE = BASE_DIR / "config" / "sources.json"
LOGS_DIR = BASE_DIR / "logs"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}
HTTP_TIMEOUT = 20


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    if f == c:
        return float(sorted_vals[f])
    return float(sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f))


def _summarize(label: str, values: list[float]) -> dict:
    if not values:
        return {"label": label, "n": 0, "summary": None}
    return {
        "label": label,
        "n": len(values),
        "min": min(values),
        "p5": _percentile(values, 0.05),
        "p50": _percentile(values, 0.50),
        "mean": round(statistics.fmean(values), 2),
        "p95": _percentile(values, 0.95),
        "max": max(values),
    }


def _probe_body_size(url: str) -> dict:
    """httpx GET; return {ok, size, status_code} (no parse)."""
    try:
        with httpx.Client(headers=HEADERS, timeout=HTTP_TIMEOUT,
                          follow_redirects=True) as client:
            resp = client.get(url)
            return {"ok": resp.status_code == 200, "size": len(resp.text),
                    "status_code": resp.status_code}
    except Exception as exc:
        return {"ok": False, "size": 0, "status_code": None,
                "error": f"{type(exc).__name__}: {str(exc)[:100]}"}


def sys_path_setup() -> None:
    """Ensure both repo root and scripts/ are importable."""
    for p in (str(BASE_DIR), str(BASE_DIR / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)


def _load_parse_helper():
    """Load _parse_article_date from gmia-fetcher-health.py without re-running its
    module-level code as a side effect (filename has hyphen so we use importlib)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "gfh_for_calib",
        BASE_DIR / "scripts" / "gmia-fetcher-health.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._parse_article_date


def calibrate(sources: list[dict]) -> dict:
    """Run probes against every source. Returns {sources: [...], recommendations: {...}}."""
    parse_date = _load_parse_helper()
    sys_path_setup()
    import fetch_articles

    per_source = []
    body_sizes_ok = []   # httpx 200 OK responses (excludes errors)
    max_gaps_by_freq: dict[str, list[int]] = {}
    last_article_ages = []

    today = datetime.now(timezone.utc).date()

    for s in sources:
        sid = s["id"]
        url = s.get("url") or s.get("research_url") or ""
        freq = s.get("frequency", "unknown").lower()
        record = {"id": sid, "frequency": freq, "url": url}

        # Body size probe
        body = _probe_body_size(url) if url else {"ok": False}
        record["body_size"] = body
        if body.get("ok") and body.get("size"):
            body_sizes_ok.append(body["size"])

        # Article gap probe — call fetch_articles
        fetcher = fetch_articles.FETCHERS.get(sid)
        gaps = []
        last_age = None
        if fetcher:
            try:
                arts = fetcher(s)
                dates = []
                for a in arts:
                    d = parse_date(a.get("date"))
                    if d:
                        dates.append(d.date())
                dates = sorted(set(dates), reverse=True)
                if dates:
                    last_age = (today - dates[0]).days
                    last_article_ages.append(last_age)
                for i in range(len(dates) - 1):
                    gaps.append((dates[i] - dates[i + 1]).days)
            except Exception as exc:
                record["fetcher_error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
        else:
            record["fetcher_error"] = "no fetcher registered"

        record["gaps_days"] = gaps
        record["max_gap_days"] = max(gaps) if gaps else None
        record["last_article_age_days"] = last_age

        if gaps:
            max_gaps_by_freq.setdefault(freq, []).append(max(gaps))

        per_source.append(record)

    # Recommendations
    rec = {
        "shell_html_threshold": _recommend_shell_threshold(body_sizes_ok),
        "stale_thresholds_per_freq": {
            freq: _recommend_stale_threshold(values)
            for freq, values in max_gaps_by_freq.items()
        },
        "global_summaries": {
            "body_sizes": _summarize("httpx body sizes (200 OK)", body_sizes_ok),
            "last_article_ages": _summarize("last article age (days)", last_article_ages),
        },
    }
    return {"calibrated_at": datetime.now(timezone.utc).isoformat(),
            "n_sources": len(sources),
            "per_source": per_source,
            "recommendations": rec}


def _recommend_shell_threshold(sizes: list[int]) -> dict:
    """Recommend SHELL_HTML_THRESHOLD = max(2.5K, p5 / 2). Reasoning: shell pages
    are typically ~2-3K chars (browser chrome). A real index page p5 ÷ 2 keeps
    safety margin without false-flagging genuinely small valid pages."""
    if not sizes:
        return {"current": 5000, "recommended": None,
                "rationale": "no body-size samples"}
    p5 = _percentile(sizes, 0.05)
    recommendation = max(2500, int(p5 / 2)) if p5 else None
    return {
        "current": 5000,
        "recommended": recommendation,
        "p5_observed": int(p5) if p5 else None,
        "rationale": (f"recommend max(2500, p5/2). Observed p5={int(p5) if p5 else 'n/a'}. "
                      f"Rationale: shell HTML is typically <3K, so threshold below the "
                      f"smallest observed real page minus margin."),
    }


def _recommend_stale_threshold(max_gaps: list[int]) -> dict:
    """For sources of a given frequency, recommend stale threshold = p95 of
    observed max consecutive gaps, ×1.5 safety margin."""
    if not max_gaps:
        return {"recommended": None, "rationale": "no observations"}
    p95 = _percentile(max_gaps, 0.95)
    recommendation = int(p95 * 1.5) if p95 else None
    return {
        "n_observed": len(max_gaps),
        "p95_max_gap": int(p95) if p95 else None,
        "recommended": recommendation,
        "rationale": "1.5× p95 of observed max gaps — leaves margin for natural cadence variance",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="print JSON to stdout, do not write file")
    parser.add_argument("--include-inaccessible", action="store_true",
                        help="also probe inaccessible candidates (slower)")
    args = parser.parse_args()

    if not SOURCES_FILE.exists():
        print(f"ERROR: {SOURCES_FILE} missing", file=sys.stderr)
        return 1
    sources = json.loads(SOURCES_FILE.read_text()).get("sources", [])

    print(f"[calibrate] probing {len(sources)} sources...", file=sys.stderr)
    result = calibrate(sources)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        LOGS_DIR.mkdir(exist_ok=True)
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_path = LOGS_DIR / f"threshold-calibration-{date_str}.json"
        out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"[calibrate] wrote {out_path}")

        # Brief summary to stderr
        rec = result["recommendations"]
        shell = rec["shell_html_threshold"]
        print(f"\n=== Recommendations ===", file=sys.stderr)
        print(f"SHELL_HTML_THRESHOLD: current={shell['current']} → "
              f"recommended={shell['recommended']}", file=sys.stderr)
        for freq, r in rec["stale_thresholds_per_freq"].items():
            print(f"FREQ_TO_STALE_DAYS[{freq}]: recommended={r['recommended']} "
                  f"(n={r['n_observed']})", file=sys.stderr)
        print("\nThis is advisory — review the JSON file before changing constants.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
