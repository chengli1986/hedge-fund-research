"""The stage-1 health page: what it says must be true of the data it read.

Every number on the page already exists somewhere -- inspection_state.json,
gmia-fetcher-health.json, articles.jsonl, template-census.jsonl -- and until
now could only be seen by reading an email at the moment it arrived. The page
is one place to look; these tests are what stop it from showing a number that
is no longer true.

The dangerous failure for a dashboard is not a wrong pixel, it is yesterday's
data presented as today's, so staleness is a first-class output: every input
carries its own timestamp and its own max age, and a stale input both marks
the page and fails the run.
"""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("publish_health", REPO / "scripts" / "publish_health.py")
ph = importlib.util.module_from_spec(spec)
sys.modules["publish_health"] = ph
spec.loader.exec_module(ph)

BJT = timezone(timedelta(hours=8))
NOW = datetime(2026, 9, 26, 4, 50, tzinfo=BJT)


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _sources(*ids, template=None):
    out = []
    for i in ids:
        s = {"id": i, "short_name": i.title(), "method": "ssr",
             "url": f"https://{i}.test/insights"}
        if template:
            s["listing_template"] = {"type": template}
            s["listing_template_active"] = True
        else:
            s["fetch_shape"] = "card_list"
        out.append(s)
    return out


def _inspection(*ids, found=10, zero=0, refusal=None, age_h=0.6):
    return {i: {"last_inspected_at": _iso(age_h), "last_article_count": found,
                "consecutive_zero_count": zero, "last_gated_ratio": 0.0,
                "last_mismatch_count": 0,
                **({"last_refusal": refusal} if refusal else {})} for i in ids}


def _probe(*ids, status="OK", reason="", age_h=0.3):
    return {"last_run": _iso(age_h),
            "sources": {i: {"status": status, "last_articles_count": 10,
                            "last_content_chars": 5000, "last_most_recent_date": "2026-09-25",
                            "last_elapsed_ms": 900, "consecutive_fails": 0,
                            "consecutive_warns": 0, "last_ok_at": _iso(age_h),
                            "last_fail_at": None, "last_failure_reason": reason}
                        for i in ids}}


def _build(**kw):
    args = {"sources": _sources("alpha", "beta"),
            "inspection": _inspection("alpha", "beta"),
            "probe": _probe("alpha", "beta"),
            "rows": [{"source_id": "alpha", "fetched_at": _iso(1)}],
            "census": [], "ab": [], "corrupt_backups": [], "now": NOW}
    args.update(kw)
    return ph.build_health(**args)


class TestShape:
    def test_all_five_stages_are_present_and_only_the_first_is_filled(self):
        """The structure is fixed now so stage 2 is additive, not a rewrite."""
        out = _build()
        assert list(out["stages"]) == ["1_fetch_articles", "2_fetch_content",
                                       "3_analyze", "4_publish", "5_verify"]
        assert out["stages"]["1_fetch_articles"] is not None
        assert all(out["stages"][k] is None for k in list(out["stages"])[1:])

    def test_every_configured_source_gets_a_row(self):
        out = _build(sources=_sources("alpha", "beta", "gamma"),
                     inspection=_inspection("alpha", "beta", "gamma"),
                     probe=_probe("alpha", "beta", "gamma"))
        assert [r["id"] for r in out["stages"]["1_fetch_articles"]["sources"]] == \
            ["alpha", "beta", "gamma"]

    def test_a_source_missing_from_one_input_still_gets_a_row(self):
        """A new source appears in sources.json before the probe has seen it."""
        out = _build(sources=_sources("alpha", "beta", "newbie"),
                     inspection=_inspection("alpha", "beta"),
                     probe=_probe("alpha", "beta"))
        row = [r for r in out["stages"]["1_fetch_articles"]["sources"] if r["id"] == "newbie"][0]
        assert row["found"] is None and row["probe_status"] is None


class TestTotals:
    def test_totals_come_from_the_rows_not_from_a_separate_count(self):
        out = _build(sources=_sources("alpha", "beta", "gamma"),
                     inspection=_inspection("alpha", "beta", "gamma", found=7),
                     probe=_probe("alpha", "beta", "gamma"))
        t = out["stages"]["1_fetch_articles"]["totals"]
        assert t["sources"] == 3 and t["found"] == 21

    def test_new_last_night_counts_rows_fetched_since_the_last_run(self):
        rows = [{"source_id": "alpha", "fetched_at": _iso(1)},
                {"source_id": "alpha", "fetched_at": _iso(2)},
                {"source_id": "beta", "fetched_at": _iso(30)}]   # last night, not this one
        out = _build(rows=rows)
        by_id = {r["id"]: r for r in out["stages"]["1_fetch_articles"]["sources"]}
        assert by_id["alpha"]["new_last_night"] == 2
        assert by_id["beta"]["new_last_night"] == 0
        assert out["stages"]["1_fetch_articles"]["totals"]["new"] == 2

    def test_template_coverage_is_counted(self):
        out = _build(sources=_sources("alpha", template="card_list") + _sources("beta"),
                     inspection=_inspection("alpha", "beta"), probe=_probe("alpha", "beta"))
        t = out["stages"]["1_fetch_articles"]["totals"]
        assert t["template"] == 1 and t["bespoke"] == 1


class TestStaleness:
    def test_fresh_inputs_are_not_stale(self):
        assert _build()["stale"] == []

    def test_pipeline_data_older_than_a_day_is_stale(self):
        out = _build(inspection=_inspection("alpha", "beta", age_h=25))
        assert [s["input"] for s in out["stale"]] == ["pipeline"]
        assert out["stale"][0]["age_hours"] >= 24

    def test_probe_data_older_than_a_day_is_stale(self):
        out = _build(probe=_probe("alpha", "beta", age_h=26))
        assert [s["input"] for s in out["stale"]] == ["probe"]

    def test_a_missing_input_is_stale_not_a_crash(self):
        out = _build(probe={})
        assert [s["input"] for s in out["stale"]] == ["probe"]

    def test_every_input_carries_its_own_timestamp(self):
        """One "last updated" for the whole page would hide the stale half."""
        out = _build()
        assert set(out["inputs"]) >= {"pipeline", "probe"}
        assert all(v.get("at") for v in out["inputs"].values() if v.get("present"))


class TestAlerts:
    def _alerts(self, **kw):
        return _build(**kw)["stages"]["1_fetch_articles"]["alerts"]

    def test_a_quiet_night_raises_nothing(self):
        assert self._alerts() == []

    def test_two_consecutive_zero_fetches_alert(self):
        kinds = [a["kind"] for a in self._alerts(inspection=_inspection("alpha", "beta", zero=2))]
        assert kinds.count("consecutive_zero") == 2

    def test_one_zero_night_does_not_alert(self):
        assert self._alerts(inspection=_inspection("alpha", "beta", zero=1)) == []

    def test_a_refusal_alerts_and_names_itself(self):
        alerts = self._alerts(inspection=_inspection("alpha", "beta",
                                                     refusal="listing_head_moved_back"))
        assert alerts and all(a["kind"] == "refusal" for a in alerts)
        assert "listing_head_moved_back" in alerts[0]["detail"]

    def test_a_failing_probe_alerts_with_its_reason(self):
        alerts = self._alerts(probe=_probe("alpha", "beta", status="FAIL", reason="0 articles"))
        assert alerts[0]["kind"] == "probe_fail" and "0 articles" in alerts[0]["detail"]

    def test_a_warning_probe_alerts_too(self):
        assert self._alerts(probe=_probe("alpha", "beta", status="WARN"))[0]["kind"] == "probe_warn"

    def test_a_disagreeing_ab_gate_alerts(self):
        ab = [{"at": _iso(20), "agree": False, "disagreed": ["alpha"]}]
        alerts = self._alerts(ab=ab)
        assert alerts[0]["kind"] == "ab_gate" and "alpha" in alerts[0]["detail"]

    def test_an_agreeing_ab_gate_is_silent(self):
        assert self._alerts(ab=[{"at": _iso(20), "agree": True, "disagreed": []}]) == []

    def test_a_corrupt_state_backup_alerts(self):
        alerts = self._alerts(corrupt_backups=["inspection_state.json.corrupt-20260926"])
        assert alerts[0]["kind"] == "corrupt_state"

    def test_a_stale_input_is_itself_an_alert(self):
        kinds = [a["kind"] for a in self._alerts(probe={})]
        assert "stale_input" in kinds


class TestTrend:
    def test_the_trend_has_one_entry_per_day_including_empty_days(self):
        rows = [{"source_id": "alpha", "fetched_at": _iso(24 * d + 1)} for d in (0, 2)]
        trend = _build(rows=rows)["stages"]["1_fetch_articles"]["trend"]
        assert len(trend) == ph.TREND_DAYS
        counts = {t["date"]: t["count"] for t in trend}
        assert counts[NOW.date().isoformat()] == 1
        assert counts[(NOW - timedelta(days=1)).date().isoformat()] == 0


class TestExitCode:
    def test_a_stale_input_fails_the_run(self):
        """A dashboard that goes quietly stale is worse than none."""
        assert ph.exit_code(_build(probe={})) == 1

    def test_alerts_alone_do_not_fail_the_run(self):
        """The page's job is to show a failing source, not to re-alert it."""
        out = _build(probe=_probe("alpha", "beta", status="FAIL", reason="x"))
        assert out["stages"]["1_fetch_articles"]["alerts"] and ph.exit_code(out) == 0


class TestRender:
    def test_the_page_states_the_numbers_the_model_holds(self):
        """Testing the model alone is testing the parts, not the assembly."""
        out = _build(sources=_sources("alpha", "beta", "gamma"),
                     inspection=_inspection("alpha", "beta", "gamma", found=7),
                     probe=_probe("alpha", "beta", "gamma"))
        html = ph.render_html(out)
        assert "21" in html and "alpha" in html and "gamma" in html

    def test_a_stale_input_is_visible_on_the_page(self):
        html = ph.render_html(_build(probe={}))
        assert "过期" in html

    def test_source_text_is_escaped(self):
        sources = _sources("alpha")
        sources[0]["short_name"] = "<script>x</script>"
        html = ph.render_html(_build(sources=sources, inspection=_inspection("alpha"),
                                     probe=_probe("alpha")))
        assert "<script>x</script>" not in html and "&lt;script&gt;" in html

    def test_a_future_dated_article_does_not_read_as_negative_days(self):
        """parse_date resolves a month-granularity label to the month's LAST
        day, so several sources legitimately sit days ahead of today. The
        first render said "(-4 天前)"."""
        probe = _probe("alpha")
        probe["sources"]["alpha"]["last_most_recent_date"] = "2026-09-30"
        html = ph.render_html(_build(sources=_sources("alpha"),
                                     inspection=_inspection("alpha"), probe=probe))
        assert "-4" not in html and "4 天后" in html

    def test_an_article_dated_today_says_so(self):
        probe = _probe("alpha")
        probe["sources"]["alpha"]["last_most_recent_date"] = NOW.date().isoformat()
        html = ph.render_html(_build(sources=_sources("alpha"),
                                     inspection=_inspection("alpha"), probe=probe))
        assert "今天" in html

    def test_a_utc_timestamp_is_shown_in_bjt(self):
        """inspection_state.json stamps UTC ("...+00:00") and the page says
        BJT everywhere else. The first render printed 19:49 for an event that
        happened at 03:49 BJT -- this machine's oldest trap, on a page whose
        whole point is that a timestamp can be trusted."""
        inspection = {"alpha": {"last_inspected_at": "2026-09-25T19:45:06+00:00",
                                "last_article_count": 10, "consecutive_zero_count": 0}}
        html = ph.render_html(_build(sources=_sources("alpha"), inspection=inspection,
                                     probe=_probe("alpha")))
        assert "2026-09-26 03:45" in html and "2026-09-25 19:45" not in html

    def test_the_page_is_self_contained(self):
        """docs-site's verify-pages.sh exempts generated pages from its shared
        CSS precisely so the generator does not depend on that repo."""
        html = ph.render_html(_build())
        assert "components.css" not in html and "<script src=" not in html


class TestSelfAudit:
    """Four defects found by auditing the page after it shipped.

    None of them showed in today's numbers -- all 42 rows recompute exactly
    from the raw files. They are about what the page says once something
    changes, which is the only thing a dashboard is for.
    """

    def test_the_no_alert_banner_does_not_hardcode_the_source_count(self):
        """It read "42 源全部正常". A number copied into prose is the defect
        this repo spends most of its guards on."""
        out = _build(sources=_sources("alpha", "beta", "gamma"),
                     inspection=_inspection("alpha", "beta", "gamma"),
                     probe=_probe("alpha", "beta", "gamma"))
        html = ph.render_html(out)
        assert "42" not in html.split("逐源状态")[0].split("告警")[-1]
        assert "3 源" in html

    def test_the_newest_stamp_is_found_across_mixed_offsets(self):
        """A string max over ISO stamps compares the text, not the instant.
        With inspection_state mid-migration from UTC to BJT both appear, and
        "2026-09-27T03:40+08:00" sorts above "2026-09-26T19:45+00:00" while
        being five minutes older."""
        inspection = {"a": {"last_inspected_at": "2026-09-26T19:45:00+00:00"},
                      "b": {"last_inspected_at": "2026-09-27T03:40:00+08:00"}}
        out = _build(inspection=inspection,
                     now=datetime(2026, 9, 27, 4, 50, tzinfo=BJT))
        assert out["inputs"]["pipeline"]["at"] == "2026-09-26T19:45:00+00:00"

    def test_no_sources_at_all_is_a_failure_not_an_empty_page(self):
        """An unreadable sources.json yields []; the page rendered a blank
        table, said nothing was wrong and exited 0."""
        out = _build(sources=[])
        assert [s["input"] for s in out["stale"]] == ["config"]
        assert ph.exit_code(out) == 1
        assert any(a["kind"] == "stale_input" for a
                   in out["stages"]["1_fetch_articles"]["alerts"])

    def test_a_source_that_stopped_being_fetched_is_reported(self):
        """Its stored row keeps yesterday's count, so the table showed a
        healthy-looking number for a source nothing had touched in days."""
        inspection = {"alpha": _inspection("alpha")["alpha"],
                      "beta": _inspection("beta", age_h=6 * 24)["beta"]}
        out = _build(inspection=inspection)
        alerts = [a for a in out["stages"]["1_fetch_articles"]["alerts"]
                  if a["kind"] == "not_inspected"]
        assert [a["source"] for a in alerts] == ["beta"]
        assert "未抓取" in ph.render_html(out)

    def test_a_source_fetched_last_night_is_not_reported(self):
        assert [a for a in _build()["stages"]["1_fetch_articles"]["alerts"]
                if a["kind"] == "not_inspected"] == []
