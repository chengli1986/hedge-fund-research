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
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import fetch_articles as fa          # noqa: E402
import listing_templates as lt       # noqa: E402

FIELDS = ("title", "url", "date")


def _key(row: dict) -> tuple:
    return tuple(row.get(f) for f in FIELDS)


def compare_source(source: dict) -> dict:
    """{agree, bespoke, template, only_bespoke, only_template, order_differs, error}"""
    out = {"source_id": source["id"], "agree": False, "error": None,
           "bespoke": 0, "template": 0, "only_bespoke": [], "only_template": [],
           "order_differs": False, "dropped_fields": []}
    try:
        url = fa.get_source_url(dict(source), fa.load_entrypoints())
        bespoke = fa.FETCHERS[source["id"]](dict(source, url=url))
        template = lt.fetch(dict(source, url=url))
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
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
    out["agree"] = b == t and not (set(out["dropped_fields"]) - declared)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="A/B a fetcher against its listing_template")
    parser.add_argument("--source", help="compare this source id")
    parser.add_argument("--all", action="store_true", help="compare every source that has a template")
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args()

    sources = json.loads((BASE_DIR / "config" / "sources.json").read_text())["sources"]
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

    results = [compare_source(s) for s in chosen]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    else:
        for r in results:
            if r["error"]:
                print(f"  ✗ {r['source_id']:24s} {r['error']}")
                continue
            mark = "✓" if r["agree"] else "✗"
            print(f"  {mark} {r['source_id']:24s} hand-written {r['bespoke']:2d} / template "
                  f"{r['template']:2d}"
                  + ("" if r["agree"] else
                     f"  | only hand-written {len(r['only_bespoke'])}, only template "
                     f"{len(r['only_template'])}"
                     + (", order differs" if r["order_differs"] else "")))
            if r["dropped_fields"]:
                print(f"      template drops: {', '.join(r['dropped_fields'])}")
            for k in r["only_bespoke"][:3]:
                print(f"      only hand-written: {k}")
            for k in r["only_template"][:3]:
                print(f"      only template:     {k}")
    return 0 if all(r["agree"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
