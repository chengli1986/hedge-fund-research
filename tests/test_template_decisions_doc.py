"""docs/listing-template-decisions.md must match config/sources.json.

A decision record that drifts is worse than none: the next person reads
"hand-written" for a source that moved months ago. Every source id appears in
the doc exactly once, and the TEMPLATE sections list exactly the sources whose
config has listing_template_active, split by template type.
"""
import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "listing-template-decisions.md"


def _sources():
    return json.loads((REPO / "config" / "sources.json").read_text())["sources"]


def _sections() -> dict[str, set[str]]:
    """Heading -> the source ids in that section's table."""
    out: dict[str, set[str]] = {}
    heading = None
    for line in DOC.read_text().splitlines():
        if line.startswith("## "):
            heading = line[3:].strip()
            out[heading] = set()
        elif heading and line.startswith("| ") and not line.startswith("| ---"):
            cell = line.split("|")[1].strip()
            if cell and cell != "source":
                out[heading].add(cell)
    return out


def _ids_in_doc() -> list[str]:
    ids = []
    for names in _sections().values():
        ids.extend(names)
    return ids


class TestDecisionDoc:
    def test_every_source_has_exactly_one_decision(self):
        listed = _ids_in_doc()
        assert len(listed) == len(set(listed)), "a source is listed twice"
        assert set(listed) == {s["id"] for s in _sources()}

    def test_the_template_sections_match_the_active_flags(self):
        sections = _sections()
        in_doc = set()
        for heading, names in sections.items():
            if heading.startswith("TEMPLATE"):
                in_doc |= names
        active = {s["id"] for s in _sources() if s.get("listing_template_active")}
        assert in_doc == active

    def test_each_template_section_matches_that_type(self):
        by_type: dict[str, set[str]] = {}
        for s in _sources():
            if s.get("listing_template_active"):
                by_type.setdefault(s["listing_template"]["type"], set()).add(s["id"])
        for heading, names in _sections().items():
            if not heading.startswith("TEMPLATE"):
                continue
            kind = heading.split("—")[1].strip().split()[0]
            assert names == by_type.get(kind, set()), heading

    def test_the_counts_in_the_headings_are_right(self):
        for heading, names in _sections().items():
            found = re.search(r"\((\d+)\)$", heading)
            if found:
                assert int(found.group(1)) == len(names), heading
