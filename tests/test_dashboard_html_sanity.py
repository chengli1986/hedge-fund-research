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


def test_check_with_no_expected_is_an_error_unless_dev_mode():
    """No sources.json is a failure now, not a skip.

    This test previously asserted the skip, which is the fail-open the 09-11
    review found: the same missing input makes publish.py render zero fund
    sections under a headline reading "0 funds tracked", so the one input whose
    loss most damages the page was the one that disarmed the gate. The dev
    escape hatch is preserved, but it has to be asked for.
    """
    rendered = [_fund_section("aqr")]
    html = _build_html(rendered)

    strict = cdh.check_dashboard(html, expected_ids=set())
    strict_count = [c for c in strict["checks"] if c["check"] == "fund_section_count"][0]
    assert not strict_count["passed"]
    assert not strict["ok"]

    dev = cdh.check_dashboard(html, expected_ids=set(), allow_missing_sources=True)
    dev_count = [c for c in dev["checks"] if c["check"] == "fund_section_count"][0]
    assert dev_count["passed"]
    assert "skipping" in dev_count["detail"]


class TestTheCheckCannotPassAStalePage:
    """The last gate must verify the page is the one this run wrote.

    Demonstrated 2026-09-11: `touch -d 2020-01-01` on a copy of the live page
    and the checker reported "passes all sanity checks", exit 0. Stage 4 runs
    unconditionally after stages 1-3 fail (`run_pipeline.sh:65`), so a publish
    that silently no-ops leaves yesterday's page on disk and this check blesses
    it. Nothing else looks at the published file: gmia_liveness_audit's CHECKS
    list has no publish check.
    """

    def _page(self, tmp_path, mtime_epoch=None):
        import os
        f = tmp_path / "d.html"
        f.write_text("<html><body><h2><span>x</span></h2></body></html>")
        if mtime_epoch is not None:
            os.utime(f, (mtime_epoch, mtime_epoch))
        return f

    def _run(self, args):
        import subprocess
        import sys as _sys
        return subprocess.run(
            [_sys.executable, str(REPO / "scripts" / "check_dashboard_html.py"), *args],
            capture_output=True, text=True, timeout=60)

    def test_a_page_older_than_this_run_fails(self, tmp_path):
        import time
        old = self._page(tmp_path, mtime_epoch=time.time() - 86400)
        proc = self._run(["--html-path", str(old),
                          "--written-after", str(int(time.time() - 60))])
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "stale" in (proc.stdout + proc.stderr).lower()

    def test_a_page_written_by_this_run_passes_the_freshness_check(self, tmp_path):
        import time
        fresh = self._page(tmp_path)
        proc = self._run(["--html-path", str(fresh),
                          "--written-after", str(int(time.time() - 60)),
                          "--allow-missing-sources"])
        assert "freshness" in proc.stdout, proc.stdout
        assert "✗ freshness" not in proc.stdout, proc.stdout

    def test_without_the_flag_freshness_is_not_claimed(self, tmp_path):
        # Manual runs have no pipeline start time; the check must be absent
        # rather than silently passing, so the report cannot imply it verified
        # something it did not.
        proc = self._run(["--html-path", str(self._page(tmp_path)),
                          "--allow-missing-sources"])
        assert "freshness" not in proc.stdout, proc.stdout


class TestAnEmptySourceListIsNotAPass:
    """Zero expected sources disarmed all six checks and still exited 0.

    Reproduced 2026-09-11 with no config/sources.json and a body of
    `<h1>totally broken page</h1>`: six green ticks, exit 0, including
    "non_empty_sections: all 0 sections have >=1 article". publish.py degrades
    the same input in the opposite direction -- it renders zero fund sections
    and a headline reading "0 funds tracked" -- so the one input whose loss
    most damages the page is the one that turns the gate off.
    """

    def _main(self, tmp_path, monkeypatch, body, extra=()):
        """Drive main() in-process with SOURCES_FILE pointed at nothing.

        Not a subprocess: SOURCES_FILE is absolute (BASE_DIR-derived), so
        changing cwd does not hide it -- an earlier version of this test
        "passed" while the checker was still reading the real 42-source
        config, i.e. it proved nothing about the missing-sources path.
        """
        chk = cdh
        page = tmp_path / "d.html"
        page.write_text(body)
        monkeypatch.setattr(chk, "SOURCES_FILE", tmp_path / "no-such-sources.json")
        monkeypatch.setattr(sys, "argv",
                            ["check_dashboard_html.py", "--html-path", str(page), *extra])
        return chk.main()

    def test_missing_sources_is_an_error_by_default(self, tmp_path, monkeypatch):
        rc = self._main(tmp_path, monkeypatch,
                        "<html><body><h1>totally broken page</h1></body></html>")
        assert rc != 0, "a page with no fund sections passed because the config was gone"

    def test_the_dev_escape_hatch_still_works(self, tmp_path, monkeypatch):
        rc = self._main(tmp_path, monkeypatch,
                        "<html><body><h2><span>x</span></h2></body></html>",
                        extra=("--allow-missing-sources",))
        assert rc == 0

    def test_zero_sections_is_never_reported_as_a_pass(self, tmp_path):
        # "all 0 sections have >=1 article" is a vacuous truth dressed as a
        # green tick; it is what a human would act on.
        chk = cdh
        result = chk.check_dashboard("<html><body></body></html>", {"aqr", "gmo"})
        non_empty = [c for c in result["checks"] if c["check"] == "non_empty_sections"]
        assert non_empty and not non_empty[0]["passed"], result["checks"]


def test_run_pipeline_hands_the_checker_its_start_time():
    """The freshness check is inert unless the pipeline passes a reference.

    Mutation 2026-09-11: dropping `--written-after "$PIPELINE_START_EPOCH"`
    from run_pipeline.sh left all 20 tests green while restoring the whole
    bug — the checker would go back to blessing whatever file is on disk.
    Every other test here drives the checker directly, so none of them can
    see the call site. Third instance this week of a guard that was installed
    and not wired; pinned structurally, like the stage 1/3 entry points.
    """
    script = (REPO / "run_pipeline.sh").read_text()
    assert "PIPELINE_START_EPOCH=$(date +%s)" in script, (
        "run_pipeline.sh no longer captures a start time")
    call = [ln for ln in script.splitlines() if "check_dashboard_html.py" in ln]
    assert call, "run_pipeline.sh no longer runs the dashboard checker"
    assert all("--written-after" in ln for ln in call), (
        "the checker is invoked without --written-after, so it will pass any "
        f"file that happens to be on disk: {call}")
