"""A source that silently fetched nothing must reach a human.

The pipeline already detects this: fetch_articles.record_quality_metrics
stores `last_article_count` and `consecutive_zero_count` per source, and
check_anomalies flags two consecutive zeros. But the alert's only destination
is `log.warning("ANOMALY ...")` in logs/fetch.log, which nothing reads -- so a
detected problem was whispered rather than reported.

Worse, the one channel that does email suppresses itself in exactly this case.
gmia-fetcher-health.py sends only when `alerts["failing"] or warning or
recovered`, and it probes at 04:30 BJT, 45 minutes after the 03:45 pipeline.
On 2026-09-06 acadian-asset fetched 0 articles; by 04:30 the site answered and
every probe was OK, so the email was suppressed and the missed night went
unrecorded. Five months of fetch.log hold exactly one acadian zero -- nobody
saw it.

A zero-article fetch is therefore its own alert condition, reported separately
from the live probe results because the two are different measurements taken
at different times.
"""
import importlib.util
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "gmia-fetcher-health.py"

spec = importlib.util.spec_from_file_location("gfh_zero", SCRIPT)
gfh = importlib.util.module_from_spec(spec)
sys.modules["gfh_zero"] = gfh
spec.loader.exec_module(gfh)

NO_ALERTS = {"failing": [], "warning": [], "recovered": [], "healthy": []}


def _state(tmp_path, payload) -> Path:
    f = tmp_path / "inspection_state.json"
    f.write_text(json.dumps(payload, ensure_ascii=False))
    return f


class TestDetection:
    def test_reports_a_source_that_fetched_nothing(self, tmp_path):
        f = _state(tmp_path, {
            "acadian-asset": {"last_article_count": 0, "consecutive_zero_count": 1,
                              "last_inspected_at": "2026-09-06T19:48:32+00:00"},
            "aqr": {"last_article_count": 10, "consecutive_zero_count": 0},
        })
        got = gfh.pipeline_zero_fetches(f)
        assert [sid for sid, _ in got] == ["acadian-asset"]

    def test_a_source_that_fetched_articles_is_not_reported(self, tmp_path):
        f = _state(tmp_path, {"aqr": {"last_article_count": 10, "consecutive_zero_count": 0}})
        assert gfh.pipeline_zero_fetches(f) == []

    def test_missing_state_file_is_not_an_error(self, tmp_path):
        # The health check must never fail because of its own extra reporting.
        assert gfh.pipeline_zero_fetches(tmp_path / "nope.json") == []

    def test_corrupt_state_file_is_not_an_error(self, tmp_path):
        f = tmp_path / "inspection_state.json"
        f.write_text("{not json")
        assert gfh.pipeline_zero_fetches(f) == []

    def test_sources_are_ordered_for_a_stable_report(self, tmp_path):
        f = _state(tmp_path, {
            "zeta": {"last_article_count": 0}, "alpha": {"last_article_count": 0}})
        assert [sid for sid, _ in gfh.pipeline_zero_fetches(f)] == ["alpha", "zeta"]


class TestItActuallyGetsSent:
    """The load-bearing assertion: this is the line that suppressed the email."""

    def test_a_zero_fetch_alone_triggers_the_email(self):
        assert gfh.should_email(NO_ALERTS, [("acadian-asset", {"last_article_count": 0})]) is True

    def test_all_clear_still_sends_nothing(self):
        assert gfh.should_email(NO_ALERTS, []) is False

    def test_existing_alert_conditions_are_unchanged(self):
        for key in ("failing", "warning", "recovered"):
            alerts = dict(NO_ALERTS, **{key: [("x", {}, None)]})
            assert gfh.should_email(alerts, []) is True, key


class TestItAppearsInBothReports:
    ROWS = [("acadian-asset", {"last_article_count": 0, "consecutive_zero_count": 1,
                               "last_inspected_at": "2026-09-06T19:48:32+00:00"})]

    def test_console_report_names_the_source(self, capsys):
        gfh.print_console_report({}, 1.0, zero_fetches=self.ROWS)
        out = capsys.readouterr().out
        assert "acadian-asset" in out
        assert "0 articles" in out or "抓取 0" in out or "fetched 0" in out

    def test_html_email_names_the_source(self):
        html = gfh.render_html_email({}, NO_ALERTS, {}, 1.0, zero_fetches=self.ROWS)
        assert "acadian-asset" in html

    def test_html_email_omits_the_section_when_there_is_nothing_to_say(self):
        html = gfh.render_html_email({}, NO_ALERTS, {}, 1.0, zero_fetches=[])
        assert "fetched 0 articles" not in html
