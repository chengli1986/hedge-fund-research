#!/usr/bin/env python3
"""Count what the listing templates cover, and what is worth revisiting.

Two rules came out of the migration and were written into
docs/listing-template-decisions.md:

  - One source is a hack, two with the same shape is a knob.
  - A shape with no template is worth one once three sources share it.

Both then depended on someone remembering them while looking at a config
file, which is not a mechanism. This counts instead, monthly, and says
nothing unless there is something to act on:

  * a shape with no template type reaches three hand-written sources
  * a source has no declared fetch_shape -- auto-promote adds sources on its
    own, and a new one needs a decision (and a row in the decisions doc)
  * a SHAPE knob has exactly one user, the early shape of a template rotting
    back into forty-two if-statements (a request parameter is not one)
  * a knob appears that is in neither list, so the rule cannot be applied

Exit code 1 when any of those fire, so the cron mails only then. A snapshot
is appended to data/template-census.jsonl every run, so the coverage trend
is visible without reading git history: sources arrive hand-written (the
synthesis agent writes fetchers), so coverage falls unless someone acts.

    python3 scripts/template_census.py            # report; exit 1 if actionable
    python3 scripts/template_census.py --json     # machine-readable
    python3 scripts/template_census.py --no-record  # do not append a snapshot
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
HISTORY = BASE_DIR / "data" / "template-census.jsonl"

# Shapes a template type already exists for; counting them says nothing.
TEMPLATED_SHAPES = ("card_list", "rss_feed", "api_json")
# Shapes seen in production that have no template type.
OTHER_SHAPES = ("card_list_paginated", "card_list_plus_page", "sitemap_plus_page",
                "attestation")
KNOWN_SHAPES = TEMPLATED_SHAPES + OTHER_SHAPES
REVISIT_AT = 3

# Keys every spec of that type must carry: they are the type, not a knob.
REQUIRED_KEYS = {"type", "fetch", "card", "link", "title", "url", "feed", "endpoint",
                 "items", "date", "wait_selector"}
# Knobs that describe the shape of a page. The two-user rule is about these:
# one user means the template grew a branch for one site.
SHAPE_KNOBS = {"path_prefix", "date_attr", "date_separator", "wait_until", "sort"}
# Counted, never flagged: date_part is a mode of date_separator, params is
# what to ask the endpoint for, categories is which items of a feed we want.
# None of them is a branch for one site's layout.
OTHER_KNOBS = {"date_part", "params", "categories"}


def _load_sources() -> list[dict]:
    return json.loads((BASE_DIR / "config" / "sources.json").read_text())["sources"]


def census(sources: list[dict]) -> dict:
    """Coverage, hand-written shapes, knob usage, and what to act on."""
    coverage: collections.Counter = collections.Counter()
    knobs: collections.Counter = collections.Counter()
    shapes: dict[str, list[str]] = collections.defaultdict(list)
    unclassified: list[str] = []
    unknown: list[tuple[str, str]] = []

    for s in sources:
        if s.get("listing_template_active"):
            spec = s["listing_template"]
            coverage[spec["type"]] += 1
            for key in spec:
                if key not in REQUIRED_KEYS:
                    knobs[key] += 1
            continue
        shape = s.get("fetch_shape")
        if not shape:
            unclassified.append(s["id"])
            continue
        if shape not in KNOWN_SHAPES:
            unknown.append((s["id"], shape))
            continue
        shapes[shape].append(s["id"])

    triggers: list[dict] = []
    for shape, ids in sorted(shapes.items()):
        if shape not in TEMPLATED_SHAPES and len(ids) >= REVISIT_AT:
            triggers.append({"kind": "revisit_at_three", "shape": shape,
                             "count": len(ids), "sources": sorted(ids)})
    if unclassified:
        triggers.append({"kind": "unclassified", "sources": sorted(unclassified)})
    for sid, shape in unknown:
        triggers.append({"kind": "unknown_shape", "source": sid, "shape": shape})
    for knob, n in sorted(knobs.items()):
        if knob not in SHAPE_KNOBS and knob not in OTHER_KNOBS:
            triggers.append({"kind": "unclassified_knob", "knob": knob})
        elif knob in SHAPE_KNOBS and n == 1:
            triggers.append({"kind": "single_user_knob", "knob": knob})

    return {
        "totals": {"sources": len(sources),
                   "template": sum(coverage.values()),
                   "bespoke": len(sources) - sum(coverage.values())},
        "coverage": dict(coverage),
        "shapes": {k: sorted(v) for k, v in sorted(shapes.items())},
        "knobs": dict(knobs),
        "unclassified": sorted(unclassified),
        "triggers": triggers,
    }


def _bjt_now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _record(out: dict) -> None:
    """Append a snapshot so the coverage trend needs no git archaeology."""
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": _bjt_now(), **out["totals"], "coverage": out["coverage"],
           "triggers": [t["kind"] for t in out["triggers"]]}
    with HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _previous() -> dict | None:
    if not HISTORY.exists():
        return None
    rows = [json.loads(l) for l in HISTORY.read_text().splitlines() if l.strip()]
    return rows[-1] if rows else None


def render(out: dict, previous: dict | None = None) -> str:
    t = out["totals"]
    lines = [f"=== GMIA listing-template census — {_bjt_now()[:16].replace('T', ' ')} BJT ===",
             f"{t['template']} of {t['sources']} sources on a template "
             f"({', '.join(f'{k} {v}' for k, v in sorted(out['coverage'].items()))}), "
             f"{t['bespoke']} hand-written"]
    if previous and previous.get("template") is not None:
        delta = t["template"] - previous["template"]
        moved = f"{delta:+d} since {previous['at'][:10]}"
        lines.append(f"  coverage {moved}"
                     + ("  ← sources arrive hand-written; coverage falls on its own"
                        if delta < 0 else ""))
    lines.append("")
    lines.append("Hand-written, by shape:")
    for shape, ids in out["shapes"].items():
        mark = "" if shape in TEMPLATED_SHAPES else f"   (no template type; revisit at {REVISIT_AT})"
        lines.append(f"  {shape:22} {len(ids):2}  {', '.join(ids)}{mark}")
    lines.append("")
    lines.append("Knob usage: " + (", ".join(f"{k} {v}" for k, v in sorted(out["knobs"].items()))
                                   or "none"))
    if out["triggers"]:
        lines.append("")
        lines.append(f"⚠ ACTION ({len(out['triggers'])}):")
        for tr in out["triggers"]:
            if tr["kind"] == "revisit_at_three":
                lines.append(f"  {tr['count']} sources share shape {tr['shape']!r} and it has no "
                             f"template type: {', '.join(tr['sources'])}")
            elif tr["kind"] == "unclassified":
                lines.append(f"  no fetch_shape declared: {', '.join(tr['sources'])} "
                             "-- classify it and add a row to "
                             "docs/listing-template-decisions.md")
            elif tr["kind"] == "unknown_shape":
                lines.append(f"  {tr['source']}: fetch_shape {tr['shape']!r} is not a known shape")
            elif tr["kind"] == "single_user_knob":
                lines.append(f"  shape knob {tr['knob']!r} has one user -- a knob with one user "
                             "is a hack with a config file")
            elif tr["kind"] == "unclassified_knob":
                lines.append(f"  knob {tr['knob']!r} is in no list: add it to SHAPE_KNOBS (the "
                             "two-user rule applies) or OTHER_KNOBS (it does not)")
    else:
        lines.append("")
        lines.append("Nothing to act on.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Listing-template coverage census")
    parser.add_argument("--json", action="store_true", help="print the census as JSON")
    parser.add_argument("--no-record", action="store_true",
                        help="do not append a snapshot to data/template-census.jsonl")
    args = parser.parse_args(argv)

    out = census(_load_sources())
    previous = _previous()
    print(json.dumps(out, ensure_ascii=False, indent=1) if args.json
          else render(out, previous))
    if not args.no_record:
        _record(out)
    return 1 if out["triggers"] else 0


if __name__ == "__main__":
    sys.exit(main())
