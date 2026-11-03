#!/usr/bin/env python3
"""Run a source's hand-written fetcher and its listing_template side by side.

A source is switched to the template only when this says the two agree, on the
live site, field by field. "It should be the same" is not evidence; this is.

    python3 scripts/compare_fetchers.py --source cambridge-associates
    python3 scripts/compare_fetchers.py --all          # every source that has a template
    python3 scripts/compare_fetchers.py --all --json   # machine-readable

Exit code 0 when every compared source agrees, 1 otherwise -- so it can gate a
switch, and be re-run later to catch a site that drifted away from its
template.

The two fetches run one after the other against the live site, so a listing
that publishes between them disagrees by one row at each end of the window
(seen on wellington, 2026-09-25: the template run picked up an article the
bespoke run had not seen, which pushed the oldest row off the max_articles
cap). A single-row difference where the extra template row is the newest on
the page is that race, not a defect -- re-run before concluding anything.

A source whose site answers 502/503/504 (after the --recheck retry) is "site
temporarily down, not compared" for one run: listed, recorded under "upstream"
in the snapshot, not in the exit code. Down on the previous run too
("site_down" in its snapshot) = a failure as before, every run until it is back. Same rule as the health probe's UPSTREAM grace
(gmia-fetcher-health.py, 2026-10-22); added 2026-11-04 after MFS's own search
API answered 503 to three gate runs in a row while probes between them got 200.

msci-research reports "order differs" on roughly half its runs: the site
serves its cards in a different order each load. Its listing renders exactly
max_articles cards (10 of 10, counted 2026-09-26), so the set cannot change
and nothing downstream reads fetch order. Re-run; it flips back.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

# One line per --all run. The gate mails only on failure, so a green week
# used to leave no trace and the stage-1 health page had nothing to show.
HISTORY = BASE_DIR / "data" / "ab-gate.jsonl"

import fetch_articles as fa          # noqa: E402
import listing_templates as lt       # noqa: E402

FIELDS = ("title", "url", "date")

# As in gmia-fetcher-health.py: the site is down or in maintenance. 500 is the
# site's code failing, often on our request, and gets no grace.
UPSTREAM_DOWN_CODES = (502, 503, 504)
_UPSTREAM_DOWN_MSG = re.compile(r"\b(502|503|504) Server Error\b")


def _upstream_down_status(exc: Exception) -> int | None:
    """The 5xx code if this failure means the site is down, else None."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if code in UPSTREAM_DOWN_CODES:
        return code
    m = _UPSTREAM_DOWN_MSG.search(str(exc))
    return int(m.group(1)) if m else None


def has_hand_written(source_id: str) -> bool:
    """Whether there is a hand-written fetcher to A/B the template against."""
    return source_id in fa.FETCHERS


def _key(row: dict) -> tuple:
    return tuple(row.get(f) for f in FIELDS)


def compare_source(source: dict) -> dict:
    """{agree, bespoke, template, only_bespoke, only_template, order_differs, error, upstream_down}"""
    out = {"source_id": source["id"], "agree": False, "error": None, "upstream_down": None,
           "bespoke": 0, "template": 0, "only_bespoke": [], "only_template": [],
           "order_differs": False, "dropped_fields": []}
    try:
        url = fa.get_source_url(dict(source), fa.load_entrypoints())
        bespoke = fa.FETCHERS[source["id"]](dict(source, url=url))
        template = lt.fetch(dict(source, url=url))
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        out["upstream_down"] = _upstream_down_status(exc)
        return out
    b = [_key(r) for r in bespoke]
    t = [_key(r) for r in template]
    out["bespoke"], out["template"] = len(b), len(t)
    out["only_bespoke"] = [list(k) for k in b if k not in t]
    out["only_template"] = [list(k) for k in t if k not in b]
    out["order_differs"] = b != t and not out["only_bespoke"] and not out["only_template"]
    # Agreeing on (title, url, date) is not the whole story: fetch_articles
    # also stores LISTING_FIELDS_KEPT, and a template that does not emit one
    # of them stops it being stored from the switch onward without changing a
    # single compared value.  gsam_summary is read by fetch_content, so this
    # can be a real loss, not only dead data.
    declared = set(source.get("listing_template_drops", []))
    out["dropped_fields"] = sorted(
        f for f in fa.LISTING_FIELDS_KEPT
        if any(r.get(f) for r in bespoke) and not any(r.get(f) for r in template)
    )
    # Same rows in a different order is the same ingestion outcome: ids come
    # from the URL, the listing-head guard sorts the dates it checks, and
    # publish orders by date. The exception is a source that declares
    # date_sorted, where fetch_source refuses every article if the order
    # breaks (DATE_ORDER_BROKEN) -- there the order IS the contract.
    same_rows = b == t or (not source.get("date_sorted") and sorted(b) == sorted(t))
    out["agree"] = same_rows and not (set(out["dropped_fields"]) - declared)
    return out


def _passes(r: dict) -> bool:
    return r["agree"] or bool(r.get("upstream_grace"))


def _previously_down() -> set[str]:
    """Sources whose site was down on the last recorded run, let through or not
    (so a site that stays down keeps failing instead of alternating)."""
    try:
        lines = HISTORY.read_text(encoding="utf-8").splitlines()
        return set(json.loads(lines[-1]).get("site_down") or []) if lines else set()
    except (OSError, ValueError, AttributeError):
        return set()


def apply_upstream_grace(results: list[dict]) -> None:
    """A site-down source is not compared this run, unless it was down last run too."""
    prev = _previously_down()
    for r in results:
        if r["agree"] or not r.get("upstream_down"):
            continue
        if r["source_id"] in prev:
            r["down_twice"] = True
        else:
            r["upstream_grace"] = True


def _record(results: list[dict]) -> None:
    """Append what this sweep found, so a green week is visible later."""
    from datetime import datetime, timedelta, timezone

    row = {"at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
           "sources": len(results),
           "agree": all(_passes(r) for r in results),
           "disagreed": [r["source_id"] for r in results if not _passes(r)],
           "errors": [r["source_id"] for r in results if r["error"]],
           "upstream": [r["source_id"] for r in results if r.get("upstream_grace")],
           "site_down": [r["source_id"] for r in results if not r["agree"] and r.get("upstream_down")]}
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        # Instrumentation must not fail the gate it measures.
        print(f"WARN: could not record the snapshot: {exc}")


def _load_sources() -> list[dict]:
    return json.loads((BASE_DIR / "config" / "sources.json").read_text())["sources"]


def main() -> int:
    parser = argparse.ArgumentParser(description="A/B a fetcher against its listing_template")
    parser.add_argument("--source", help="compare this source id")
    parser.add_argument("--all", action="store_true", help="compare every source that has a template")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    parser.add_argument("--no-record", action="store_true",
                        help="do not append a snapshot to data/ab-gate.jsonl")
    parser.add_argument("--recheck", action="store_true",
                        help="re-run each disagreeing source once before failing "
                             "(for the weekly cron: the live races are flaky, drift is not)")
    args = parser.parse_args()

    sources = _load_sources()
    if args.source:
        chosen = [s for s in sources if s["id"] == args.source]
        if not chosen:
            print(f"no such source: {args.source}")
            return 2
        if not chosen[0].get("listing_template"):
            print(f"{args.source} has no listing_template to compare against")
            return 2
    elif args.all:
        chosen = [s for s in sources if s.get("listing_template")]
    else:
        parser.error("give --source ID or --all")
    # A template-first source (2026-10-13) never had a hand-written fetcher:
    # there is nothing to A/B it against. Say so instead of a KeyError.
    no_counterpart = [s["id"] for s in chosen if not has_hand_written(s["id"])]
    chosen = [s for s in chosen if has_hand_written(s["id"])]
    for sid in no_counterpart:
        print(f"  · {sid:24s} no hand-written counterpart (template from the start), not compared")
    if not chosen:
        return 0

    results = [compare_source(s) for s in chosen]
    if args.recheck:
        by_id = {s["id"]: s for s in chosen}
        for i, r in enumerate(results):
            if r["agree"]:
                continue
            print(f"  … {r['source_id']}: {'error' if r['error'] else 'disagreed'}, re-running once")
            second = compare_source(by_id[r["source_id"]])
            second["rechecked"] = True
            results[i] = second
    apply_upstream_grace(results)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    else:
        for r in results:
            if r.get("upstream_grace"):
                print(f"  ⏳ {r['source_id']:24s} site temporarily down (HTTP {r['upstream_down']}), "
                      "not compared this run -- fails if still down next run")
                continue
            if r["error"]:
                print(f"  ✗ {r['source_id']:24s} {r['error']}"
                      + ("  | site down on the previous run too" if r.get("down_twice") else ""))
                continue
            mark = "✓" if r["agree"] else "✗"
            print(f"  {mark} {r['source_id']:24s} hand-written {r['bespoke']:2d} / template "
                  f"{r['template']:2d}"
                  + ("" if r["agree"] else
                     f"  | only hand-written {len(r['only_bespoke'])}, only template "
                     f"{len(r['only_template'])}"
                     + (", order differs" if r["order_differs"] else "")))
            if r["agree"] and r["order_differs"]:
                print("      same rows, different order (harmless: not date_sorted)")
            if r["dropped_fields"]:
                print(f"      template drops: {', '.join(r['dropped_fields'])}")
            for k in r["only_bespoke"][:3]:
                print(f"      only hand-written: {k}")
            for k in r["only_template"][:3]:
                print(f"      only template:     {k}")
    if args.all and not args.no_record:
        _record(results)
    return 0 if all(_passes(r) for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
