"""Config sanity checks — catch misclassifications and URL drift at test time.

Two classes of bugs these guard against:

1. **Frequency vs observed cadence mismatch** (Gap 1)
   The 2026-05-11 cambridge-associates + brookfield bug: both declared "weekly"
   but actually publish monthly. The runtime staleness check eventually flagged
   them via fetcher-health WARN, but only after they'd been silently sitting in
   prod with wrong thresholds. A test that compares declared frequency against
   the historical median gap from articles.jsonl catches this at config time.

2. **Candidate URL invariants** (Gap 3)
   The 2026-05-08 ares-management bug: status flipped to "validated" but the
   research_url was 404. A static lint test cannot catch live 404s (that's the
   live URL probe extension to gmia-fetcher-health.py), but it CAN catch
   missing/malformed URLs and host-vs-official-domain drift, which are also
   common manual-edit mistakes.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
SOURCES_FILE = REPO / "config" / "sources.json"
CANDIDATES_FILE = REPO / "config" / "fund_candidates.json"
ARTICLES_FILE = REPO / "data" / "articles.jsonl"

# Mirror gmia-fetcher-health.py's FREQ_TO_STALE_DAYS — keep in sync if it changes.
FREQ_TO_STALE_DAYS = {
    "daily": 14,
    "weekly": 30,
    "biweekly": 45,
    "monthly": 90,
    "quarterly": 240,
    "annual": 540,
    "yearly": 540,
}

# Minimum articles a source needs before we trust its observed cadence
MIN_SAMPLES_FOR_CADENCE_CHECK = 5


def _load_articles_by_source() -> dict[str, list]:
    """Return {source_id: [parsed_date, ...]} from articles.jsonl."""
    by_source: dict[str, list] = defaultdict(list)
    if not ARTICLES_FILE.exists():
        return by_source
    with open(ARTICLES_FILE) as f:
        for line in f:
            try:
                a = json.loads(line)
            except Exception:
                continue
            sid = a.get("source_id")
            raw = a.get("date") or a.get("date_raw")
            if not sid or not raw:
                continue
            try:
                parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
            except Exception:
                continue
            by_source[sid].append(parsed)
    return by_source


def _median_gap_days(dates: list) -> float | None:
    """Median day-gap between consecutive unique dates (descending order)."""
    unique = sorted(set(dates), reverse=True)
    if len(unique) < 2:
        return None
    gaps = [(unique[i] - unique[i + 1]).days for i in range(len(unique) - 1)]
    return statistics.median(gaps)


# ───────────────────────── Gap 1: cadence vs declared frequency ──────────────


def test_declared_frequency_matches_observed_cadence():
    """Each production source's declared frequency should be consistent with
    the median gap between its real articles.

    Rule: median_gap_days must not exceed the staleness threshold for the
    declared frequency. If it does, the source is being throttled to a slower
    cadence than its label admits — either the publisher slowed down (reclassify)
    or the label was wrong from day one.

    This is INTENTIONALLY one-sided: too-fast misclassifications (declared
    monthly, actually weekly) only cause overly-conservative WARN thresholds —
    not a problem. Too-slow misclassifications cause false-positive WARNs.
    """
    sources = json.loads(SOURCES_FILE.read_text())["sources"]
    by_source = _load_articles_by_source()

    failures = []
    for s in sources:
        sid = s["id"]
        freq = (s.get("frequency") or "").strip().lower()
        threshold = FREQ_TO_STALE_DAYS.get(freq)
        if threshold is None:
            continue  # unknown frequency, skip (staleness check uses DEFAULT)
        dates = by_source.get(sid, [])
        if len(set(dates)) < MIN_SAMPLES_FOR_CADENCE_CHECK:
            continue  # not enough data to judge

        gap = _median_gap_days(dates)
        if gap is None:
            continue
        if gap > threshold:
            # Suggest the slowest bucket that would fit
            suggested = next(
                (f for f, t in sorted(FREQ_TO_STALE_DAYS.items(), key=lambda x: x[1])
                 if t >= gap),
                "annual",
            )
            failures.append(
                f"{sid}: declared {freq} (threshold {threshold}d) but median "
                f"article gap is {gap:.0f}d — consider reclassifying to "
                f"{suggested!r}"
            )

    assert not failures, (
        "Frequency misclassifications found:\n  " + "\n  ".join(failures)
    )


# ───────────────────────── Gap 3: candidate URL invariants ───────────────────


def _candidate_records() -> list[dict]:
    return json.loads(CANDIDATES_FILE.read_text())


def _hostname(url: str) -> str:
    return (urlparse(url).hostname or "").lower().lstrip(".")


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def test_validated_candidates_have_research_url():
    """Every candidate marked validated must have a non-empty research_url.

    A validated candidate is one the pipeline thinks is safe to put into a
    trial. Missing URL means the trial will fail Day 1 with no useful signal.
    """
    missing = [
        c["id"]
        for c in _candidate_records()
        if c.get("status") == "validated"
        and not (c.get("research_url") or "").strip()
    ]
    assert not missing, f"validated candidates with empty research_url: {missing}"


def test_validated_candidate_research_url_is_parseable():
    """research_url must parse to a valid http(s) URL with a hostname."""
    bad = []
    for c in _candidate_records():
        if c.get("status") != "validated":
            continue
        url = c.get("research_url", "")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            bad.append(f"{c['id']}: {url!r}")
    assert not bad, "malformed research_url:\n  " + "\n  ".join(bad)


def test_validated_candidate_url_host_matches_official_domain():
    """research_url's hostname must sit within the candidate's official_domain.

    Catches typos / wrong-fund URLs where someone pasted a competitor's link
    or an aggregator URL into a candidate record.
    """
    mismatches = []
    for c in _candidate_records():
        if c.get("status") != "validated":
            continue
        url = c.get("research_url", "")
        official = (c.get("official_domain") or "").lower().strip()
        if not url or not official:
            continue
        host = _strip_www(_hostname(url))
        domain = _strip_www(official)
        if not host.endswith(domain):
            mismatches.append(
                f"{c['id']}: research_url host {host!r} not within "
                f"official_domain {domain!r}"
            )
    assert not mismatches, (
        "research_url / official_domain drift:\n  " + "\n  ".join(mismatches)
    )
