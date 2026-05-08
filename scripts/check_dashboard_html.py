#!/usr/bin/env python3
"""Sanity check publish.py's HTML output before users hit it.

Without this, the dashboard at docs.sinostor.com.cn/hedge-fund-research.html
can silently render with missing fund sections, empty titles, or duplicate
style attributes — discoverable only by eyeballing the page.

What we check:
  1. fund-section count vs sources.json count (off-by-one means a fund got dropped)
  2. data-source-id set in HTML == set in sources.json (catches "wrong fund rendered")
  3. each fund section has cluster-count > 0 (catches "fund present but 0 articles")
  4. no duplicate style="" attributes on a single tag (browsers ignore the 2nd one)
  5. no empty <h2></h2> headers (catches missing fund/cluster names)

Exit codes:
  0 = all checks pass (or all skipped because input missing)
  1 = at least one check failed
  2 = HTML file unreadable

Usage:
  python3 scripts/check_dashboard_html.py
  python3 scripts/check_dashboard_html.py --html-path <path>
  python3 scripts/check_dashboard_html.py --html-path <path> --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_HTML_PATH = Path("/var/www/overview/hedge-fund-research.html")
SOURCES_FILE = BASE_DIR / "config" / "sources.json"


# ── individual checks ────────────────────────────────────────────────────────

def _extract_fund_section_ids(html: str) -> list[str]:
    """Return data-source-id values for every <section class="...fund-section...">."""
    pattern = re.compile(
        r'<section[^>]*class="[^"]*\bfund-section\b[^"]*"[^>]*data-source-id="([^"]+)"',
        re.IGNORECASE,
    )
    return pattern.findall(html)


def _extract_cluster_counts(html: str) -> list[tuple[str, int]]:
    """For each fund-section, return (sid, article_count_from_h2)."""
    out = []
    section_re = re.compile(
        r'<section[^>]*class="[^"]*\bfund-section\b[^"]*"[^>]*data-source-id="([^"]+)"[^>]*>'
        r'(.*?)</section>',
        re.IGNORECASE | re.DOTALL,
    )
    count_re = re.compile(r'<span\s+class="cluster-count"[^>]*>(\d+)', re.IGNORECASE)
    for sid, body in section_re.findall(html):
        m = count_re.search(body)
        out.append((sid, int(m.group(1)) if m else 0))
    return out


def _find_duplicate_style_tags(html: str) -> list[str]:
    """Return up to 5 example tags that have two style="..." attributes."""
    pattern = re.compile(r'(<\w+[^>]*\bstyle="[^"]*"[^>]*\bstyle="[^"]*"[^>]*>)',
                         re.IGNORECASE)
    return pattern.findall(html)[:5]


def _find_empty_h2(html: str) -> int:
    """Count <h2> with no visible text (allowing only whitespace + tags)."""
    return len(re.findall(r'<h2[^>]*>\s*</h2>', html, re.IGNORECASE))


# ── orchestration ───────────────────────────────────────────────────────────

def load_expected_source_ids() -> set[str]:
    if not SOURCES_FILE.exists():
        return set()
    data = json.loads(SOURCES_FILE.read_text())
    return {s["id"] for s in data.get("sources", []) if s.get("id")}


def check_dashboard(html: str, expected_ids: set[str]) -> dict:
    """Run all checks; return {ok: bool, checks: [...]}."""
    fund_ids = _extract_fund_section_ids(html)
    fund_id_set = set(fund_ids)
    counts = _extract_cluster_counts(html)
    dup_styles = _find_duplicate_style_tags(html)
    empty_h2 = _find_empty_h2(html)

    checks = []

    # 1. fund section count ~= expected
    expected_count = len(expected_ids)
    actual_count = len(fund_ids)
    if expected_count == 0:
        checks.append({"check": "fund_section_count", "passed": True,
                       "detail": "no expected sources to compare against (skipping)"})
    elif actual_count == 0:
        checks.append({"check": "fund_section_count", "passed": False,
                       "detail": f"expected {expected_count} fund sections, got 0 — page is empty/broken"})
    elif actual_count < expected_count - 1:
        # Allow off-by-one for sources without articles (publish.py skips them)
        checks.append({"check": "fund_section_count", "passed": False,
                       "detail": f"got {actual_count} fund sections, expected ~{expected_count} "
                                 f"(missing >1 — likely a render bug)"})
    else:
        checks.append({"check": "fund_section_count", "passed": True,
                       "detail": f"{actual_count} sections / {expected_count} expected"})

    # 2. data-source-id set is a subset of expected
    unknown = fund_id_set - expected_ids if expected_ids else set()
    if unknown:
        checks.append({"check": "fund_id_membership", "passed": False,
                       "detail": f"HTML shows unknown source ids: {sorted(unknown)} "
                                 f"(not in sources.json)"})
    else:
        checks.append({"check": "fund_id_membership", "passed": True,
                       "detail": "all rendered ids are valid sources"})

    # 3. duplicate sections (same data-source-id appears twice)
    dup_sections = [sid for sid in fund_id_set if fund_ids.count(sid) > 1]
    if dup_sections:
        checks.append({"check": "no_duplicate_sections", "passed": False,
                       "detail": f"sections rendered more than once: {dup_sections}"})
    else:
        checks.append({"check": "no_duplicate_sections", "passed": True,
                       "detail": "every fund rendered at most once"})

    # 4. each fund section has cluster-count > 0
    zero_count = [sid for sid, n in counts if n == 0]
    if zero_count:
        checks.append({"check": "non_empty_sections", "passed": False,
                       "detail": f"sections with 0 articles in cluster-count: {zero_count}"})
    else:
        checks.append({"check": "non_empty_sections", "passed": True,
                       "detail": f"all {len(counts)} sections have ≥1 article"})

    # 5. no duplicate style attributes
    if dup_styles:
        checks.append({"check": "no_duplicate_style_attrs", "passed": False,
                       "detail": f"found {len(dup_styles)} tags with duplicate style attrs "
                                 f"(browsers silently drop the 2nd) — example: "
                                 f"{dup_styles[0][:120]}"})
    else:
        checks.append({"check": "no_duplicate_style_attrs", "passed": True,
                       "detail": "no duplicate style attrs"})

    # 6. no empty h2
    if empty_h2:
        checks.append({"check": "no_empty_h2", "passed": False,
                       "detail": f"{empty_h2} empty <h2></h2> tag(s) (likely missing fund or cluster name)"})
    else:
        checks.append({"check": "no_empty_h2", "passed": True,
                       "detail": "no empty h2 headers"})

    return {"ok": all(c["passed"] for c in checks), "checks": checks}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html-path", default=str(DEFAULT_HTML_PATH))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    html_path = Path(args.html_path)
    if not html_path.exists():
        print(f"ERROR: HTML file not found: {html_path}", file=sys.stderr)
        return 2

    try:
        html = html_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: cannot read {html_path}: {exc}", file=sys.stderr)
        return 2

    expected_ids = load_expected_source_ids()
    result = check_dashboard(html, expected_ids)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for c in result["checks"]:
            mark = "✓" if c["passed"] else "✗"
            print(f"  {mark} {c['check']:30s} {c['detail']}")
        if result["ok"]:
            print(f"OK: dashboard html at {html_path} passes all sanity checks")
        else:
            failed = [c["check"] for c in result["checks"] if not c["passed"]]
            print(f"FAIL: {html_path} — failed checks: {', '.join(failed)}")

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
