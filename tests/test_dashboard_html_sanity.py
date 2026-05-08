"""Tests for post-publish dashboard HTML sanity check.

Without these, publish.py's HTML output could degrade silently — wrong fund
count, duplicate sections, empty headers — and the dashboard would only get
caught next time someone happened to look.
"""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_dashboard_html.py"

spec = importlib.util.spec_from_file_location("cdh", SCRIPT)
cdh = importlib.util.module_from_spec(spec)
sys.modules["cdh"] = cdh
spec.loader.exec_module(cdh)


def _fund_section(sid: str, count: int = 5, accent: str = "#abcdef") -> str:
    """Build a minimal valid fund-section like publish.py's output."""
    return (f'<section class="cluster fund-section" data-source-id="{sid}" '
            f'style="--fund-accent:{accent}">'
            f'<div class="cluster-head">'
            f'<h2><span class="badge" style="background:{accent}">Fund</span> '
            f'<span class="cluster-count">{count}</span></h2>'
            f'</div></section>')


def _build_html(sections: list[str]) -> str:
    return f"""<!DOCTYPE html><html><body>
{"".join(sections)}
</body></html>"""


def test_extract_fund_section_ids_basic():
    html = _build_html([_fund_section("aqr"), _fund_section("man-group")])
    assert cdh._extract_fund_section_ids(html) == ["aqr", "man-group"]


def test_extract_cluster_counts():
    html = _build_html([_fund_section("aqr", 12), _fund_section("man-group", 0)])
    counts = cdh._extract_cluster_counts(html)
    assert ("aqr", 12) in counts
    assert ("man-group", 0) in counts


def test_find_duplicate_style_tags():
    html = '<div style="color:red" class="x" style="color:blue">hi</div>'
    found = cdh._find_duplicate_style_tags(html)
    assert len(found) == 1


def test_find_duplicate_style_tags_no_false_positives():
    """Single style attr OR style on different tags must not trip."""
    html = ('<div style="color:red">hi</div>'
            '<span style="color:blue">there</span>')
    assert cdh._find_duplicate_style_tags(html) == []


def test_find_empty_h2():
    html = "<h2></h2><h2>title</h2><h2>   </h2><h2 class='x'></h2>"
    assert cdh._find_empty_h2(html) == 3


def test_check_passes_for_clean_dashboard():
    expected = {"aqr", "man-group", "bridgewater"}
    sections = [_fund_section(s) for s in expected]
    result = cdh.check_dashboard(_build_html(sections), expected)
    assert result["ok"] is True


def test_check_flags_missing_sections():
    """If we expect 19 funds and only see 5, that's a render bug."""
    expected = {f"fund-{i}" for i in range(19)}
    rendered = [_fund_section(f"fund-{i}") for i in range(5)]
    result = cdh.check_dashboard(_build_html(rendered), expected)
    assert result["ok"] is False
    failed = {c["check"] for c in result["checks"] if not c["passed"]}
    assert "fund_section_count" in failed


def test_check_tolerates_off_by_one():
    """publish.py skips funds with 0 articles, so off-by-one is normal."""
    expected = {f"fund-{i}" for i in range(10)}
    rendered = [_fund_section(f"fund-{i}") for i in range(9)]  # one fund had 0 articles
    result = cdh.check_dashboard(_build_html(rendered), expected)
    fund_count_check = [c for c in result["checks"] if c["check"] == "fund_section_count"][0]
    assert fund_count_check["passed"] is True


def test_check_flags_unknown_source_ids():
    """If HTML renders 'fund-x' but it's not in sources.json, that's wrong."""
    expected = {"aqr", "man-group"}
    rendered = [_fund_section("aqr"), _fund_section("hallucinated-fund")]
    result = cdh.check_dashboard(_build_html(rendered), expected)
    assert result["ok"] is False
    failed = {c["check"] for c in result["checks"] if not c["passed"]}
    assert "fund_id_membership" in failed


def test_check_flags_duplicate_section():
    """Same fund rendered twice (e.g., bug in dedup) must be caught."""
    expected = {"aqr", "man-group"}
    rendered = [_fund_section("aqr"), _fund_section("aqr"), _fund_section("man-group")]
    result = cdh.check_dashboard(_build_html(rendered), expected)
    assert result["ok"] is False
    failed = {c["check"] for c in result["checks"] if not c["passed"]}
    assert "no_duplicate_sections" in failed


def test_check_flags_zero_article_section():
    expected = {"aqr"}
    rendered = [_fund_section("aqr", count=0)]
    result = cdh.check_dashboard(_build_html(rendered), expected)
    assert result["ok"] is False
    failed = {c["check"] for c in result["checks"] if not c["passed"]}
    assert "non_empty_sections" in failed


def test_check_flags_duplicate_style_attrs():
    expected = {"aqr"}
    section = (f'<section class="cluster fund-section" data-source-id="aqr" '
               f'style="--fund-accent:#abc" style="color:red">'
               f'<h2><span class="cluster-count">5</span></h2></section>')
    result = cdh.check_dashboard(_build_html([section]), expected)
    assert result["ok"] is False
    failed = {c["check"] for c in result["checks"] if not c["passed"]}
    assert "no_duplicate_style_attrs" in failed


def test_check_flags_empty_h2():
    expected = {"aqr"}
    section = (f'<section class="cluster fund-section" data-source-id="aqr">'
               f'<h2></h2><h2><span class="cluster-count">5</span></h2></section>')
    result = cdh.check_dashboard(_build_html([section]), expected)
    assert result["ok"] is False
    failed = {c["check"] for c in result["checks"] if not c["passed"]}
    assert "no_empty_h2" in failed


def test_check_with_no_expected_skips_count_compare():
    """Dev-mode run with no sources.json — just don't crash."""
    rendered = [_fund_section("aqr")]
    result = cdh.check_dashboard(_build_html(rendered), expected_ids=set())
    # Other checks still run
    fund_count_check = [c for c in result["checks"] if c["check"] == "fund_section_count"][0]
    assert "skipping" in fund_count_check["detail"]
